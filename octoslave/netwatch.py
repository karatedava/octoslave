"""Keeping runs alive across a closed lid or a dropped network.

When the laptop sleeps or loses its network in the middle of a run, three
things used to go wrong:

* **A model call in flight never ended.** Its TCP connection died silently
  while the machine slept, so the read just waited for data that would never
  come — up to the five-minute silence limit — and a Stop pressed meanwhile
  was only noticed when that wait ran out.
* **An outage ended the run.** Retries are budgeted in seconds (5, 10, 20, 30),
  so a network that needed longer than about a minute to come back used them
  all up. The run then "failed over" to another model on the *same*
  unreachable server, failed again, and stopped.
* **The compute node's SSH connection hung** (see ``remote.py``, which uses
  :func:`slept_since` from here).

This module gives the HTTP layer and the retry loops what they need:

* :func:`slept_seconds` / :func:`slept_since` — whether this machine has slept
  since some earlier moment (a monotonic clock that stops during sleep, read
  against one that does not).
* :func:`install` — wraps the network streams of an OpenAI client's connection
  pool. Blocking reads are sliced, so a Stop lands within a second; a
  connection that lived through a sleep is checked by TCP keepalive (kept if
  the server still answers, failed within seconds if not) instead of being
  waited on; and an idle pooled connection that slept is never reused.
* :func:`wait_out_outage` — when a call fails and the server cannot even be
  reached, wait until it can (saying so, Stop-aware) rather than spending the
  retry budget and switching models, which cannot help.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import urllib.request
from urllib.parse import urlsplit

import httpcore

from . import interrupt

# A sleep at least this long makes a connection suspect. Shorter gaps are
# scheduling noise; and a connection usually survives a brief doze anyway.
SLEEP_GAP = 20.0

# Longest single blocking read. Bounds how long a Stop or a wake-up can go
# unnoticed inside a read; the read's own timeout is enforced across slices.
_READ_SLICE = 1.0

# TCP keepalive (idle, interval, count). Normal: a connection whose path died
# while the model was silent (a long prefill) fails after ~2 min instead of the
# 5-min read timeout. After a sleep: a dead connection fails in ~11 s, while
# one that survived keeps streaming — the server's TCP stack still answers.
_KEEPALIVE = (60, 15, 4)
_KEEPALIVE_AFTER_SLEEP = (2, 3, 3)


# ---------------------------------------------------------------------------
# Sleep detection
# ---------------------------------------------------------------------------

def slept_seconds() -> float | None:
    """Total time this machine has spent asleep, in seconds — or None where
    that cannot be told. Only differences between two readings mean anything.

    macOS: CLOCK_MONOTONIC keeps counting during sleep, CLOCK_UPTIME_RAW does
    not. Linux: CLOCK_BOOTTIME counts suspend, CLOCK_MONOTONIC does not. Both
    are immune to wall-clock changes (NTP, time zones), unlike time.time()."""
    try:
        if sys.platform == "darwin":
            return (time.clock_gettime(time.CLOCK_MONOTONIC)
                    - time.clock_gettime(time.CLOCK_UPTIME_RAW))
        if sys.platform.startswith("linux"):
            return (time.clock_gettime(time.CLOCK_BOOTTIME)
                    - time.clock_gettime(time.CLOCK_MONOTONIC))
    except (AttributeError, OSError, ValueError):
        pass
    return None


def slept_since(mark: float | None, gap: float = SLEEP_GAP) -> bool:
    """True if the machine slept for at least ``gap`` seconds since ``mark``
    (an earlier :func:`slept_seconds` reading)."""
    if mark is None:
        return False
    now = slept_seconds()
    return now is not None and now - mark >= gap


# ---------------------------------------------------------------------------
# Watched network streams (installed into an OpenAI client's httpcore pool)
# ---------------------------------------------------------------------------

def _set_keepalive(sock, idle: int, interval: int, count: int) -> bool:
    """Arm TCP keepalive on ``sock``. False when the platform refuses."""
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Linux calls the idle time TCP_KEEPIDLE, macOS TCP_KEEPALIVE.
        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_opt is None:
            return False
        sock.setsockopt(socket.IPPROTO_TCP, idle_opt, idle)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
        return True
    except (OSError, AttributeError, ValueError, TypeError):
        return False


_SLEPT = "The connection was lost while the computer was asleep."


def is_sleep_error(exc: BaseException | None) -> bool:
    """True if ``exc`` (or what caused it) is a connection given up because the
    machine slept — not a fault of the model or the server."""
    seen = 0
    while exc is not None and seen < 10:
        if _SLEPT in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


class _WatchedStream(httpcore.NetworkStream):
    """An httpcore network stream that notices Stop and sleep.

    Reads are issued in short slices: a slice that times out has consumed
    nothing (TLS keeps any partial record buffered), so the next slice simply
    carries on, and between slices the stream can check for a Stop or a
    wake-up. The caller's own read timeout still applies, across slices.
    """

    def __init__(self, inner: httpcore.NetworkStream, mark: float | None = None):
        self._inner = inner
        self._mark = slept_seconds() if mark is None else mark
        self._suspect = False      # lived through a sleep; keepalive is checking it
        _set_keepalive(self._socket(), *_KEEPALIVE)

    def _socket(self):
        try:
            return self._inner.get_extra_info("socket")
        except Exception:  # noqa: BLE001
            return None

    def _note_sleep(self) -> None:
        """The machine slept since this stream last moved data. Keep it only
        if TCP keepalive can prove it alive quickly; otherwise give it up."""
        self._mark = slept_seconds()
        if _set_keepalive(self._socket(), *_KEEPALIVE_AFTER_SLEEP):
            self._suspect = True
            return
        raise httpcore.ReadError(_SLEPT)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if interrupt.should_stop():
                raise interrupt.StopRequested()
            if slept_since(self._mark):
                self._note_sleep()
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise httpcore.ReadTimeout("The read operation timed out")
            step = _READ_SLICE if left is None else min(_READ_SLICE, left)
            try:
                data = self._inner.read(max_bytes, step)
            except httpcore.ReadTimeout:
                continue                      # nothing consumed — read on
            except httpcore.ReadError as exc:
                if self._suspect:
                    # Keepalive found it dead after the sleep: report it as the
                    # sleep's doing, so it is retried at once and not counted.
                    raise httpcore.ReadError(_SLEPT) from exc
                raise
            if self._suspect:
                if not data:
                    # The server hung up while we slept (its FIN arrives now).
                    raise httpcore.ReadError(_SLEPT)
                # It survived the sleep: back to the normal keepalive pace.
                self._suspect = False
                _set_keepalive(self._socket(), *_KEEPALIVE)
            self._mark = slept_seconds()
            return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if interrupt.should_stop():
            raise interrupt.StopRequested()
        if slept_since(self._mark):
            # A request must not be sent into a connection that sat through a
            # sleep: it is very likely dead, and the answer would never come.
            # Failing here lets the client retry on a fresh connection.
            raise httpcore.WriteError(_SLEPT)
        self._inner.write(buffer, timeout)
        self._mark = slept_seconds()

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        inner = self._inner.start_tls(ssl_context, server_hostname=server_hostname,
                                      timeout=timeout)
        return _WatchedStream(inner, mark=self._mark)

    def get_extra_info(self, info: str):
        # The pool asks this of an IDLE connection before reusing it ("has the
        # server hung up?"). One that slept is answered "yes", so it is closed
        # and a fresh connection is opened instead of a dead one being reused.
        if info == "is_readable" and slept_since(self._mark):
            return True
        return self._inner.get_extra_info(info)


class _WatchedBackend(httpcore.NetworkBackend):
    """Wraps every stream the pool opens in a :class:`_WatchedStream`."""

    def __init__(self, inner: httpcore.NetworkBackend):
        self._inner = inner

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _WatchedStream(self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options))

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _WatchedStream(self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options))

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def install(client) -> bool:
    """Watch the network streams of an OpenAI ``client`` (see module doc).

    Patches the client's own connection pools in place — including any proxy
    pools — so proxy settings, limits and timeouts are exactly what the SDK
    chose. Returns False (and changes nothing) when the client is not built the
    way this expects; the run then simply works as before."""
    http = getattr(client, "_client", None)
    transports = [getattr(http, "_transport", None)]
    transports += list((getattr(http, "_mounts", None) or {}).values())
    done = False
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            continue
        if not isinstance(backend, _WatchedBackend):
            try:
                pool._network_backend = _WatchedBackend(backend)
            except Exception:  # noqa: BLE001
                continue
        done = True
    return done


# ---------------------------------------------------------------------------
# Waiting out an outage
# ---------------------------------------------------------------------------

# Seconds between reachability checks while the server is unreachable: often
# at first (after a wake the network is usually back within seconds), then
# backing off for a long outage. A check is one TCP connect — cheap.
_OUTAGE_POLL = (2, 3, 5, 5, 5, 10, 10, 15, 15, 20, 30)
# How long to wait before giving up. A day: a run left going overnight should
# carry on in the morning, and Stop ends the wait at any time.
_OUTAGE_MAX = float(os.environ.get("OCTOSLAVE_OFFLINE_WAIT_MAX") or 24 * 3600)


def _probe_target(base_url: str) -> tuple[str, int] | None:
    """Where a request to ``base_url`` actually connects: the configured proxy
    for it, if any (the same settings httpx reads), else the server itself."""
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
        if not host:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    try:
        proxies = urllib.request.getproxies()
        proxy = proxies.get(parts.scheme) or proxies.get("all")
        if proxy and not urllib.request.proxy_bypass(host):
            p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
            if p.hostname:
                return p.hostname, p.port or (443 if p.scheme == "https" else 80)
    except Exception:  # noqa: BLE001 — unusual proxy settings: probe the server
        pass
    return host, port


# A server on this machine (local Ollama, a tunnel's local end) that cannot be
# reached is not running — no network is involved, and waiting for one to come
# back would only hide that. Such failures keep the normal retry path. (Tests
# that serve a model on 127.0.0.1 turn this on.)
WAIT_ON_LOOPBACK = False


def _is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def reachable(target: tuple[str, int], timeout: float = 5.0) -> bool:
    """True if a TCP connection to ``target`` (host, port) can be opened."""
    try:
        with socket.create_connection(target, timeout=timeout):
            return True
    except OSError:
        return False


def _stopped() -> bool:
    """Stop requested for this thread: the user's Stop (``interrupt``), or the
    Lab's own Stop — Lab threads are not interrupt-registered; their session's
    status says it (exposed on the cluster-job context)."""
    if interrupt.should_stop():
        return True
    try:
        from .science.tools import _compute_ctx
        return bool(getattr(getattr(_compute_ctx(), "session", None), "stopped", False))
    except Exception:  # noqa: BLE001
        return False


def _pause(seconds: float) -> bool:
    """Sleep ``seconds`` in short slices; True as soon as a stop is requested."""
    end = time.monotonic() + seconds
    while True:
        if _stopped():
            return True
        rest = end - time.monotonic()
        if rest <= 0:
            return False
        if interrupt.wait(min(1.0, rest)):
            return True


def _took(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


def wait_out_outage(client, who: str = "") -> bool:
    """Called after a model call failed on the connection. If the model server
    cannot be reached at all, wait until it can.

    Returns True once an outage has been waited out — the caller should retry
    straight away and not count the failure. Returns False when the server is
    reachable (the failure is the model's, so normal retries and failover
    apply) or the wait gave up after ``_OUTAGE_MAX``. Raises
    ``interrupt.StopRequested`` on a Stop."""
    target = _probe_target(str(getattr(client, "base_url", "") or ""))
    if target is None or (_is_loopback(target[0]) and not WAIT_ON_LOOPBACK):
        return False
    if reachable(target):
        return False
    from . import display
    host = target[0]
    tag = f"[{who}] " if who else ""
    display.print_info(
        f"{tag}No connection to the model server ({host}) — the network is down "
        f"or the computer just woke up. Waiting for it to come back; the work "
        f"carries on by itself (Stop ends it).")
    display.net_status("offline", host)
    started = time.time()          # wall clock: the wait may include a sleep
    i = 0
    while True:
        step = _OUTAGE_POLL[min(i, len(_OUTAGE_POLL) - 1)]
        i += 1
        if _pause(step):
            display.net_status("ok", host)
            raise interrupt.StopRequested()
        if reachable(target):
            display.print_info(f"{tag}Connection to {host} is back after "
                               f"{_took(time.time() - started)} — carrying on.")
            display.net_status("ok", host)
            return True
        if time.time() - started > _OUTAGE_MAX:
            display.print_error(f"{tag}{host} has been unreachable for "
                                f"{_took(time.time() - started)} — giving up.")
            display.net_status("ok", host)
            return False
