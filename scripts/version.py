#!/usr/bin/env python3
"""Print the canonical OctoSlave version.

Single source of truth: the ``[project] version`` field in pyproject.toml.
An ``OTS_VERSION`` environment variable (e.g. a manual CI override) takes
precedence when set. Used by the CI installer jobs so the macOS .app bundle,
the Windows installer, and every artifact filename all carry the same number.

Usage:
    python scripts/version.py
"""
import os
import sys
from pathlib import Path


def resolve_version() -> str:
    env = os.environ.get("OTS_VERSION", "").strip()
    if env:
        return env
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib  # type: ignore
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        return tomllib.load(f)["project"]["version"]


if __name__ == "__main__":
    try:
        sys.stdout.write(resolve_version())
    except Exception as exc:  # pragma: no cover - surfaced in CI logs
        sys.stderr.write(f"version.py: failed to resolve version: {exc}\n")
        sys.exit(1)
