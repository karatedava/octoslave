import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# e-INFRA CZ defaults
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".octoslave"
CONFIG_FILE = CONFIG_DIR / "config.json"

BASE_URL = "https://llm.ai.e-infra.cz/v1"
DEFAULT_MODEL = "kimi-k2.7"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NIM_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"

# Built-in backends are first-class — they have dedicated config keys and
# helpers (e.g. Ollama auto-discovery). Anything else is a user-defined
# OpenAI-compatible provider stored in `custom_providers`.
BUILTIN_BACKENDS = ("einfra", "ollama", "nim")

# Reserved provider IDs that user cannot pick when adding a custom provider.
RESERVED_PROVIDER_IDS = set(BUILTIN_BACKENDS)

KNOWN_MODELS = [
    "deepseek-v3.2",
    "deepseek-v3.2-thinking",
    "qwen3.5",
    "qwen3.5-122b",
    "qwen3-coder",
    "qwen3-coder-30b",
    "qwen3-coder-next",
    "gpt-oss-120b",
    "kimi-k2.7",
    "kimi-k2.6",
    "mistral-medium-3.5",
    "llama-4-scout-17b-16e-instruct",
    "gemma4",
    "glm-4.7",
    "glm-5",
    "glm-5.1",
    "thinker",
    "coder",
    "agentic",
    "mini",
    "redhatai-scout",
]

# Static fallback list — used when the API key is absent or the /models call fails.
# Only includes models confirmed accessible on a standard (free) NIM account.
# NOTE: The live /v1/models catalog contains 100+ models; many require paid tiers
#       or specific account permissions. This list is curated for general access.
NIM_KNOWN_MODELS = [
    # Llama 4
    "meta/llama-4-maverick-17b-128e-instruct",
    # NVIDIA Nemotron (free tier — confirmed working)
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/llama-3.1-nemotron-nano-8b-v1",
    # Llama 3.x
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-405b-instruct",
    "meta/llama-3.1-70b-instruct",
    # Qwen 3
    "qwen/qwen3.5-122b-a10b",
    "qwen/qwen3-coder-480b-a35b-instruct",
    # DeepSeek (correct NIM model IDs)
    "deepseek-ai/deepseek-v3.2",
    # Gemma / Phi
    "google/gemma-3-27b-it",
    "google/gemma-4-31b-it",
    "microsoft/phi-4-mini-instruct",
    # Mistral (may require paid tier on some accounts)
    "mistralai/mistral-large-2-instruct",
    "mistralai/mistral-small-4-119b-2603",
]

# ---------------------------------------------------------------------------
# Per-backend default role models for the research pipeline
# ---------------------------------------------------------------------------

PIPELINE_ROLES = (
    "researcher", "hypothesis", "skeptic", "coder", "debugger",
    "evaluator", "orchestrator", "reporter", "merger",
)

EINFRA_ROLE_MODELS: dict[str, str] = {
    "researcher":   "deepseek-v3.2-thinking",
    "hypothesis":   "deepseek-v3.2-thinking",
    "skeptic":      "deepseek-v3.2-thinking",
    "coder":        "kimi-k2.6",
    "debugger":     "qwen3-coder-30b",
    "evaluator":    "kimi-k2.6",
    "orchestrator": "kimi-k2.6",
    "reporter":     "kimi-k2.6",
    "merger":       "deepseek-v3.2",
}

NIM_ROLE_MODELS: dict[str, str] = {
    # Heavy reasoning roles — largest confirmed-stable NVIDIA model
    "researcher":   "nvidia/nemotron-3-super-120b-a12b",
    "hypothesis":   "nvidia/nemotron-3-super-120b-a12b",
    "skeptic":      "nvidia/nemotron-3-super-120b-a12b",
    "coder":        "nvidia/nemotron-3-super-120b-a12b",
    "debugger":     "nvidia/nemotron-3-super-120b-a12b",
    "evaluator":    "nvidia/nemotron-3-super-120b-a12b",
    # Synthesis / long-form writing — Llama-3.3-70b-instruct hit NIM 504 gateway
    # timeouts (~14 min wait) on every Orchestrator + Reporter call across two
    # consecutive runs. Switched to 120b nemotron — slower per call, but
    # completes reliably; same family as the heavy roles.
    "orchestrator": "nvidia/nemotron-3-super-120b-a12b",
    "reporter":     "nvidia/nemotron-3-super-120b-a12b",
    "merger":       "nvidia/nemotron-3-super-120b-a12b",
}


# ---------------------------------------------------------------------------
# Council ("improved") role models
# ---------------------------------------------------------------------------
# The improved single-agent mode unifies a small pool of role-specialized
# e-INFRA models behind one agent surface (see council.py):
#   worker   — drives the tool loop (best agentic / tool-calling / coding)
#   thinker  — plans & course-corrects (strongest reasoning)
#   verifier — independent critic that reviews risky actions & completion
# Distinct model *families* are deliberate — they catch different mistakes.

COUNCIL_ROLES = ("worker", "thinker", "verifier")

# Default assignment when the preferred ids resolve live.
COUNCIL_ROLE_MODELS: dict[str, str] = {
    "worker":   "kimi-k2.7",
    "thinker":  "kimi-k2.7",
    "verifier": "glm-5.2",
}

# Per-role preference chains: the first id that exists in the live /v1/models
# catalog wins; otherwise we fall back down the chain. This is how the user's
# requested "deepseek-v4" is honored if/when the endpoint exposes it, without
# hard-failing when it does not.
COUNCIL_ROLE_PREFERENCES: dict[str, list[str]] = {
    "worker":   ["kimi-k2.7", "kimi-k2.6"],
    "thinker":  ["kimi-k2.7", "glm-5.2", "kimi-k2.6"],
    "verifier": ["glm-5.2", "kimi-k2.7"],
}

# Escalation pool for the *alternate* Worker — a strong model from a DIFFERENT
# family than the primary Worker, used only when the primary gets stuck (repeated
# errors, a verifier-deadlocked action, or rejected completion). Switching family
# at the point of failure harvests ensemble diversity where it matters: a model
# from another family often clears a block the first one cannot. First live id
# whose family differs from the resolved Worker wins; if none differ, escalation
# is simply disabled (graceful).
COUNCIL_WORKER_ESCALATION: list[str] = [
    "kimi-k2.6","glm-5.2","deepseek-v3.2-thinking"
]

# ---------------------------------------------------------------------------
# Ultra tier — multi-model best-of-N debate (deeper orchestration)
# ---------------------------------------------------------------------------
# A diverse, isolated panel that independently drafts a candidate (e.g. a plan or
# a critical solution), which an aggregator then synthesizes into one stronger
# answer. Isolation between panelists preserves diversity (prevents "orchestration
# collapse"); the synthesis harvests the union of their strengths — the mechanism
# that lets a non-frontier pool exceed any single model. kimi/glm prioritized,
# with one different-family reasoner for genuine diversity. Resolved against the
# live catalog; deduped by family so the panel is actually diverse.
COUNCIL_DEBATE_MODELS: list[str] = [
    "kimi-k2.7", "glm-5.2", "deepseek-v3.2-thinking",
]
# How many panelists to actually use (kept small for latency/cost).
COUNCIL_DEBATE_PANEL = 3


def resolve_debate_panel(available: list[str] | None, worker: str = "") -> list[str]:
    """Pick up to COUNCIL_DEBATE_PANEL debate models, one per family, from the
    live catalog (prioritizing the configured order). Falls back to the configured
    ids when the catalog is unknown. The primary worker is included first so the
    panel always contains the main model plus diverse second opinions."""
    avail = {m for m in (available or [])}
    picked: list[str] = []
    seen_fam: set[str] = set()
    for cand in ([worker] if worker else []) + COUNCIL_DEBATE_MODELS:
        if not cand:
            continue
        if avail and cand not in avail:
            continue
        fam = _model_family(cand)
        if fam in seen_fam or cand in picked:
            continue
        picked.append(cand)
        seen_fam.add(fam)
        if len(picked) >= COUNCIL_DEBATE_PANEL:
            break
    return picked


def _model_family(model: str) -> str:
    """Coarse model-family key (vendor/series) for diversity checks."""
    m = (model or "").lower()
    for fam in ("kimi", "glm", "deepseek", "qwen", "llama", "mistral", "gemma", "gpt"):
        if fam in m:
            return fam
    return m.split("-")[0] if m else ""


def resolve_council_models(
    available: list[str] | None = None,
    overrides: dict | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(roles, notes)`` mapping each council role to a concrete model.

    ``available`` is the live model id list (from ``einfra_list_models`` or the
    active provider). ``overrides`` are explicit per-role choices (CLI flags or
    env) that always win. For each role we pick the first preference present in
    ``available``; if none match (or ``available`` is empty/unknown) we fall back
    to ``COUNCIL_ROLE_MODELS`` and record a note so the caller can surface it.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    avail_set = {m for m in (available or [])}
    roles: dict[str, str] = {}
    notes: dict[str, str] = {}
    for role in COUNCIL_ROLES:
        if role in overrides:
            roles[role] = overrides[role]
            if avail_set and overrides[role] not in avail_set:
                notes[role] = f"{overrides[role]} (override; not in live catalog)"
            continue
        if not avail_set:
            # Catalog unknown (no api key / probe failed): use known-safe defaults
            # rather than speculatively picking an unverified preferred id.
            roles[role] = COUNCIL_ROLE_MODELS[role]
            continue
        chosen = None
        for cand in COUNCIL_ROLE_PREFERENCES.get(role, []):
            if cand in avail_set:
                chosen = cand
                break
        if chosen is None:
            chosen = COUNCIL_ROLE_MODELS[role]
            notes[role] = f"{chosen} (default fallback — none of the preferred ids are live)"
        elif chosen != COUNCIL_ROLE_PREFERENCES.get(role, [None])[0]:
            notes[role] = f"{chosen} (first available preference)"
        roles[role] = chosen

    # Resolve the diverse escalation Worker: first available id from a different
    # family than the primary Worker. Honors an explicit override; else best-effort
    # from the live catalog. Left unset when no different-family model is available.
    if overrides.get("worker_alt"):
        roles["worker_alt"] = overrides["worker_alt"]
    elif avail_set:
        # Only from the LIVE catalog — never speculate an id that might 404 when
        # escalation actually fires. No different-family model live → no escalation.
        worker_fam = _model_family(roles.get("worker", ""))
        for cand in COUNCIL_WORKER_ESCALATION:
            if cand in avail_set and _model_family(cand) != worker_fam:
                roles["worker_alt"] = cand
                break

    # Ultra-tier debate panel (diverse, one model per family). Stored as a list
    # under a non-standard key — print_roles / the loop only read the three string
    # roles, so the extra entry is inert unless ultra mode pulls it.
    panel = resolve_debate_panel(list(avail_set) if avail_set else None, roles.get("worker", ""))
    if len(panel) >= 2:
        roles["debate"] = panel  # type: ignore[assignment]
    return roles, notes


# ---------------------------------------------------------------------------
# e-INFRA CZ live model query
# ---------------------------------------------------------------------------

def einfra_list_models(base_url: str = BASE_URL, api_key: str = "") -> list[str]:
    """
    Query e-INFRA CZ's OpenAI-compatible /v1/models endpoint and return model IDs.
    Returns an empty list on any error so callers can fall back to KNOWN_MODELS.
    """
    if not api_key:
        return []
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            return sorted(ids) if ids else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# NVIDIA NIM helpers
# ---------------------------------------------------------------------------

def nim_list_models(nim_url: str = NIM_BASE_URL, nim_api_key: str = "") -> list[str]:
    """
    Query NIM's OpenAI-compatible /v1/models endpoint and return model IDs.
    Returns an empty list on any error so callers can fall back to NIM_KNOWN_MODELS.
    """
    if not nim_api_key:
        return []
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            f"{nim_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {nim_api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            return sorted(ids) if ids else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_is_running(ollama_url: str = OLLAMA_BASE_URL) -> bool:
    """Return True if an Ollama instance is reachable at ollama_url."""
    try:
        import urllib.request
        base = ollama_url.rstrip("/").removesuffix("/v1")
        req = urllib.request.urlopen(f"{base}/api/tags", timeout=3)
        return req.status == 200
    except Exception:
        return False


def ollama_list_models(ollama_url: str = OLLAMA_BASE_URL) -> list[str]:
    """
    Return the list of model names already pulled in Ollama.
    Returns an empty list if Ollama is unreachable.
    """
    try:
        import urllib.request, json as _json
        base = ollama_url.rstrip("/").removesuffix("/v1")
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            data = _json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def ollama_pull_model(model_name: str, ollama_url: str = OLLAMA_BASE_URL) -> bool:
    """
    Pull a model via the Ollama REST API (streaming).
    Prints progress lines to stdout. Returns True on success.
    """
    try:
        import urllib.request, json as _json
        base = ollama_url.rstrip("/").removesuffix("/v1")
        payload = _json.dumps({"name": model_name, "stream": True}).encode()
        req = urllib.request.Request(
            f"{base}/api/pull",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                    status = obj.get("status", "")
                    if "total" in obj and "completed" in obj:
                        pct = int(obj["completed"] / obj["total"] * 100)
                        print(f"\r  {status}: {pct}%", end="", flush=True)
                    else:
                        print(f"\r  {status}        ", end="", flush=True)
                except Exception:
                    pass
        print()  # newline after progress
        return True
    except Exception as e:
        print(f"\n  Pull failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Tool-calling capability ordering for local models
# ---------------------------------------------------------------------------

# Model name prefixes ranked best→worst for OpenAI-format tool calling.
# Models not matching any prefix are treated as lowest priority.
_TOOL_CALL_RANK: list[str] = [
    "qwen2.5",
    "qwen3",
    "llama3.1",
    "llama3.2",
    "llama3.3",
    "mistral-nemo",
    "mistral-small",
    "granite3",
    "phi4",
    "phi3",
    "llama3",
    # Below here: known poor/no tool-calling support
    "gemma",
    "mistral",   # plain mistral:7b doesn't support JSON tool calls reliably
]


def _tool_call_score(model_name: str) -> int:
    """Lower score = better tool calling. Unknown models get worst score."""
    name = model_name.lower().split(":")[0]  # strip tag like :7b
    for i, prefix in enumerate(_TOOL_CALL_RANK):
        if name.startswith(prefix):
            return i
    return len(_TOOL_CALL_RANK)


def sort_by_tool_calling(models: list[str]) -> list[str]:
    """Return models sorted best-first for tool calling support."""
    return sorted(models, key=_tool_call_score)


# ---------------------------------------------------------------------------
# Role → local model assignment for long-research
# ---------------------------------------------------------------------------

# How the 7 pipeline roles are mapped when ≤3 local models are available.
# Priority tiers: if 3 models available, tier-A gets model[0], tier-B gets
# model[1], tier-C gets model[2].  Fewer models collapse tiers.
_ROLE_TIERS: dict[str, int] = {
    # Tier A — primary reasoning (best model)
    "orchestrator": 0,
    "evaluator":    0,
    # Tier B — coding / implementation
    "coder":        1,
    "debugger":     1,
    "reporter":     1,
    # Tier C — reading / writing (lightest model is fine)
    "researcher":   2,
    "hypothesis":   2,
}


def assign_local_models(pulled_models: list[str]) -> dict[str, str]:
    """
    Given a list of pulled Ollama model names (up to 3 are used),
    return a role → model mapping for the research pipeline.
    """
    if not pulled_models:
        raise ValueError("No Ollama models available.")

    # Use at most 3 distinct models
    available = pulled_models[:3]
    n = len(available)

    mapping: dict[str, str] = {}
    for role, tier in _ROLE_TIERS.items():
        # Collapse tiers if fewer than 3 models
        idx = min(tier, n - 1)
        mapping[role] = available[idx]
    return mapping


# ---------------------------------------------------------------------------
# Role model helpers
# ---------------------------------------------------------------------------

def get_role_models(cfg: dict | None = None) -> dict[str, str]:
    """
    Return role → model mapping for the research pipeline.
    Starts from per-backend defaults, then applies any custom overrides
    stored in the config under 'role_models_<backend>'.
    """
    if cfg is None:
        cfg = {}
    backend = cfg.get("backend", "einfra")

    if backend == "nim":
        base = dict(NIM_ROLE_MODELS)
    elif backend == "ollama":
        pulled = ollama_list_models(cfg.get("ollama_url", OLLAMA_BASE_URL))
        base = assign_local_models(pulled) if pulled else dict(EINFRA_ROLE_MODELS)
    else:
        # einfra OR a user-defined custom provider — fall back to a uniform
        # default. The user can override per-role via the committee UI.
        provider = get_custom_provider(cfg, backend)
        default_model = (provider or {}).get("default_model") or cfg.get("default_model") or DEFAULT_MODEL
        if backend in BUILTIN_BACKENDS:
            base = dict(EINFRA_ROLE_MODELS)
        else:
            # For a custom provider we have no per-role recommendation; assign
            # the provider's default model to every role so the pipeline runs.
            base = {role: default_model for role in PIPELINE_ROLES}

    # Apply any per-backend custom overrides saved by the user
    custom = cfg.get(f"role_models_{backend}") or {}
    base.update(custom)
    return base


def save_role_model(role: str, model: str, backend: str) -> None:
    """Persist a single per-role model override for the given backend."""
    cfg = load_config()
    key = f"role_models_{backend}"
    role_models = dict(cfg.get(key) or {})
    role_models[role] = model
    cfg[key] = role_models
    _write_config_file(cfg)


def reset_role_models(backend: str) -> None:
    """Clear all custom per-role overrides for the given backend."""
    cfg = load_config()
    cfg[f"role_models_{backend}"] = {}
    _write_config_file(cfg)


# ---------------------------------------------------------------------------
# Config load / save
# ---------------------------------------------------------------------------

def list_models(cfg: dict | None = None) -> list[str]:
    """
    Return available model names for the active backend.
    - einfra: query /v1/models live; fall back to KNOWN_MODELS on failure
    - ollama: poll the local server; fall back to KNOWN_MODELS
    - nim:    query /v1/models live; fall back to NIM_KNOWN_MODELS on failure
    - custom: query the provider's /v1/models live; fall back to provider.models
    """
    if cfg is None:
        cfg = {}
    backend = cfg.get("backend", "einfra")
    if backend == "ollama":
        pulled = ollama_list_models(cfg.get("ollama_url", OLLAMA_BASE_URL))
        return pulled if pulled else list(KNOWN_MODELS)
    if backend == "nim":
        live = nim_list_models(
            cfg.get("nim_url", NIM_BASE_URL),
            cfg.get("nim_api_key", ""),
        )
        return live if live else list(NIM_KNOWN_MODELS)
    if backend not in BUILTIN_BACKENDS:
        provider = get_custom_provider(cfg, backend)
        if provider:
            live = einfra_list_models(
                provider.get("base_url", ""),
                provider.get("api_key", "") or "x",  # some hosts (vLLM) accept anything
            )
            if live:
                return live
            saved = provider.get("models") or []
            if saved:
                return list(saved)
            default = provider.get("default_model")
            return [default] if default else []
        # Fall through to einfra defaults if the provider was deleted
    # einfra
    live = einfra_list_models(
        cfg.get("base_url", BASE_URL),
        cfg.get("api_key", ""),
    )
    return live if live else list(KNOWN_MODELS)


def load_config() -> dict:
    config = {
        "api_key": "",
        "base_url": BASE_URL,
        "default_model": DEFAULT_MODEL,
        "backend": "einfra",        # "einfra" | "ollama" | "nim" | <custom-id>
        "ollama_url": OLLAMA_BASE_URL,
        "permission_mode": "autonomous",  # "autonomous" | "controlled" | "supervised"
        "nim_api_key": "",
        "nim_url": NIM_BASE_URL,
        "role_models_einfra": {},
        "role_models_nim": {},
        "role_models_ollama": {},
        "custom_providers": [],     # list of {id, name, base_url, api_key, default_model, models?}
        "mcp_servers": [],          # list of {name, enabled, command/args/env | url/headers}
    }
    # Env vars override config file
    if os.environ.get("OCTOSLAVE_API_KEY"):
        config["api_key"] = os.environ["OCTOSLAVE_API_KEY"]
    if os.environ.get("OCTOSLAVE_BASE_URL"):
        config["base_url"] = os.environ["OCTOSLAVE_BASE_URL"]
    if os.environ.get("OCTOSLAVE_MODEL"):
        config["default_model"] = os.environ["OCTOSLAVE_MODEL"]
    if os.environ.get("OCTOSLAVE_BACKEND"):
        config["backend"] = os.environ["OCTOSLAVE_BACKEND"]
    if os.environ.get("OCTOSLAVE_OLLAMA_URL"):
        config["ollama_url"] = os.environ["OCTOSLAVE_OLLAMA_URL"]
    if os.environ.get("OCTOSLAVE_PERMISSION_MODE"):
        config["permission_mode"] = os.environ["OCTOSLAVE_PERMISSION_MODE"]
    if os.environ.get("OCTOSLAVE_NIM_API_KEY"):
        config["nim_api_key"] = os.environ["OCTOSLAVE_NIM_API_KEY"]
    if os.environ.get("OCTOSLAVE_NIM_URL"):
        config["nim_url"] = os.environ["OCTOSLAVE_NIM_URL"]

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            _env_keys = {
                "api_key":          "OCTOSLAVE_API_KEY",
                "base_url":         "OCTOSLAVE_BASE_URL",
                "default_model":    "OCTOSLAVE_MODEL",
                "backend":          "OCTOSLAVE_BACKEND",
                "ollama_url":       "OCTOSLAVE_OLLAMA_URL",
                "permission_mode":  "OCTOSLAVE_PERMISSION_MODE",
                "nim_api_key":      "OCTOSLAVE_NIM_API_KEY",
                "nim_url":          "OCTOSLAVE_NIM_URL",
            }
            for key, env_var in _env_keys.items():
                if not os.environ.get(env_var) and saved.get(key):
                    config[key] = saved[key]
            # Custom providers list — only loaded from file
            providers = saved.get("custom_providers")
            if isinstance(providers, list):
                config["custom_providers"] = [
                    p for p in providers if isinstance(p, dict) and p.get("id")
                ]
            # MCP servers list — only loaded from file
            mcp = saved.get("mcp_servers")
            if isinstance(mcp, list):
                config["mcp_servers"] = [
                    s for s in mcp if isinstance(s, dict) and s.get("name")
                ]
            # role_models_* are dicts — no env-var override, just load from file.
            # We also pick up role_models_<custom-id> dynamically.
            for k, v in saved.items():
                if k.startswith("role_models_") and isinstance(v, dict):
                    config[k] = v
        except (json.JSONDecodeError, OSError):
            pass

    return config


def _write_config_file(cfg: dict) -> None:
    """Persist the full config dict to disk. Caller is responsible for the
    contents — load_config() returns a complete dict ready to be re-saved."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Drop any keys we never want on disk (defensive — none currently)
    data = dict(cfg)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def save_config(
    api_key: str,
    base_url: str = BASE_URL,
    default_model: str = DEFAULT_MODEL,
    backend: str = "einfra",
    ollama_url: str = OLLAMA_BASE_URL,
    permission_mode: str = "autonomous",
    nim_api_key: str | None = None,
    nim_url: str | None = None,
    role_models_einfra: dict | None = None,
    role_models_nim: dict | None = None,
    role_models_ollama: dict | None = None,
    custom_providers: list | None = None,
    **extra_role_models: dict,
):
    """Backwards-compatible save. Preserves any keys not passed explicitly,
    including dynamic `role_models_<custom-id>` entries from disk."""
    # Load existing config to preserve values not explicitly provided
    _existing: dict = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                _existing = json.load(f)
        except Exception:
            pass

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(_existing)  # start from existing so unknown keys survive
    data.update({
        "api_key": api_key,
        "base_url": base_url,
        "default_model": default_model,
        "backend": backend,
        "ollama_url": ollama_url,
        "permission_mode": permission_mode,
        "nim_api_key": nim_api_key if nim_api_key is not None else _existing.get("nim_api_key", ""),
        "nim_url": nim_url if nim_url is not None else _existing.get("nim_url", NIM_BASE_URL),
        "role_models_einfra":  role_models_einfra  if role_models_einfra  is not None else _existing.get("role_models_einfra",  {}),
        "role_models_nim":     role_models_nim      if role_models_nim     is not None else _existing.get("role_models_nim",     {}),
        "role_models_ollama":  role_models_ollama   if role_models_ollama  is not None else _existing.get("role_models_ollama",  {}),
        "custom_providers":    custom_providers     if custom_providers    is not None else _existing.get("custom_providers",    []),
    })
    # Dynamic role_models_<custom-id> overrides — accept kwargs starting with
    # "role_models_" so callers can persist per-custom-backend role mappings.
    for k, v in extra_role_models.items():
        if k.startswith("role_models_") and isinstance(v, dict):
            data[k] = v
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Custom provider CRUD
# ---------------------------------------------------------------------------

# Allowed `id` characters: lowercase letters, digits, dash, underscore.
_PROVIDER_ID_RE = None  # lazily compiled


def _normalize_provider_id(raw: str) -> str:
    """Lowercase, strip, collapse whitespace to '-'."""
    return "-".join((raw or "").strip().lower().split())


def _validate_provider_id(pid: str) -> str | None:
    """Return error message if invalid, else None."""
    import re
    global _PROVIDER_ID_RE
    if _PROVIDER_ID_RE is None:
        _PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
    if not pid:
        return "Provider id is required."
    if pid in RESERVED_PROVIDER_IDS:
        return f"'{pid}' is reserved (built-in backend)."
    if not _PROVIDER_ID_RE.match(pid):
        return ("Provider id must be 1–32 chars: lowercase letters, digits, "
                "dash or underscore; must start with a letter or digit.")
    return None


def _normalize_provider(p: dict) -> dict:
    """Return a clean provider dict from raw user input."""
    pid = _normalize_provider_id(p.get("id") or p.get("name") or "")
    name = (p.get("name") or pid or "").strip() or pid
    base_url = (p.get("base_url") or "").strip().rstrip("/")
    api_key = (p.get("api_key") or "").strip()
    default_model = (p.get("default_model") or "").strip()
    models = p.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    return {
        "id": pid,
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "default_model": default_model,
        "models": list(models),
    }


def get_custom_providers(cfg: dict | None = None) -> list[dict]:
    """Return the list of user-defined custom providers."""
    if cfg is None:
        cfg = load_config()
    providers = cfg.get("custom_providers") or []
    return [p for p in providers if isinstance(p, dict) and p.get("id")]


def get_custom_provider(cfg: dict | None, provider_id: str) -> dict | None:
    """Return the custom provider with this id, or None."""
    if not provider_id:
        return None
    for p in get_custom_providers(cfg):
        if p.get("id") == provider_id:
            return p
    return None


def add_custom_provider(provider: dict) -> dict:
    """Add a new custom provider. Raises ValueError on validation errors."""
    p = _normalize_provider(provider)
    err = _validate_provider_id(p["id"])
    if err:
        raise ValueError(err)
    if not p["base_url"]:
        raise ValueError("base_url is required.")
    cfg = load_config()
    existing = cfg.get("custom_providers") or []
    if any(e.get("id") == p["id"] for e in existing):
        raise ValueError(f"Provider id '{p['id']}' already exists.")
    existing.append(p)
    cfg["custom_providers"] = existing
    _write_config_file(cfg)
    return p


def update_custom_provider(provider_id: str, fields: dict) -> dict:
    """Patch an existing custom provider. id is immutable."""
    cfg = load_config()
    existing = cfg.get("custom_providers") or []
    for i, e in enumerate(existing):
        if e.get("id") == provider_id:
            merged = {**e, **fields, "id": provider_id}
            existing[i] = _normalize_provider(merged)
            cfg["custom_providers"] = existing
            _write_config_file(cfg)
            return existing[i]
    raise ValueError(f"Provider '{provider_id}' not found.")


def remove_custom_provider(provider_id: str) -> bool:
    """Remove a custom provider; if the active backend was this provider,
    fall back to einfra. Returns True if removed."""
    cfg = load_config()
    existing = cfg.get("custom_providers") or []
    new_list = [e for e in existing if e.get("id") != provider_id]
    if len(new_list) == len(existing):
        return False
    cfg["custom_providers"] = new_list
    if cfg.get("backend") == provider_id:
        cfg["backend"] = "einfra"
    # Drop any role_models_<id> entries for the removed provider
    cfg.pop(f"role_models_{provider_id}", None)
    _write_config_file(cfg)
    return True


# ---------------------------------------------------------------------------
# MCP server CRUD
# ---------------------------------------------------------------------------

def get_mcp_servers(cfg: dict | None = None) -> list[dict]:
    """Return the list of configured MCP servers."""
    if cfg is None:
        cfg = load_config()
    servers = cfg.get("mcp_servers") or []
    return [s for s in servers if isinstance(s, dict) and s.get("name")]


def _normalize_mcp_server(s: dict) -> dict:
    """Clean a raw MCP server entry. A `url` means http transport; otherwise
    `command` means stdio transport."""
    name = (s.get("name") or "").strip()
    out: dict = {"name": name, "enabled": bool(s.get("enabled", True))}
    if s.get("url"):
        out["url"] = (s.get("url") or "").strip()
        headers = s.get("headers") or {}
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
    else:
        out["command"] = (s.get("command") or "").strip()
        args = s.get("args") or []
        if isinstance(args, str):
            import shlex
            args = shlex.split(args)
        out["args"] = list(args)
        env = s.get("env") or {}
        if isinstance(env, dict) and env:
            out["env"] = {str(k): str(v) for k, v in env.items()}
        if s.get("cwd"):
            out["cwd"] = str(s["cwd"])
    return out


def add_mcp_server(server: dict) -> dict:
    """Add a new MCP server. Raises ValueError on validation errors."""
    s = _normalize_mcp_server(server)
    if not s["name"]:
        raise ValueError("MCP server name is required.")
    if not s.get("url") and not s.get("command"):
        raise ValueError("MCP server requires either a 'command' (stdio) or a 'url' (http).")
    cfg = load_config()
    existing = cfg.get("mcp_servers") or []
    if any(e.get("name") == s["name"] for e in existing):
        raise ValueError(f"MCP server '{s['name']}' already exists.")
    existing.append(s)
    cfg["mcp_servers"] = existing
    _write_config_file(cfg)
    return s


def ensure_codag_mcp_registered() -> bool:
    """Register the codag MCP server in config.json if codag is installed and
    not already registered. Idempotent and safe to call repeatedly. Returns
    True iff a new registration was written this call.

    Opt out by exporting OCTOSLAVE_NO_CODAG_MCP_REGISTER=1 before octoslave
    starts (or before the lazy auto-install fires).
    """
    if os.environ.get("OCTOSLAVE_NO_CODAG_MCP_REGISTER") == "1":
        return False
    import shutil as _shutil
    if not (_shutil.which("codag") or (Path.home() / ".local" / "bin" / "codag").is_file()):
        return False
    try:
        from .mcp_registry import get_entry, build_config
    except Exception:
        return False
    entry = get_entry("codag")
    if entry is None:
        return False
    for existing in get_mcp_servers():
        if (existing.get("name") or "").lower() == entry["id"]:
            return False
    try:
        server_cfg = build_config(entry, values={})
        add_mcp_server(server_cfg)
        return True
    except Exception:
        return False


def remove_mcp_server(name: str) -> bool:
    """Remove an MCP server by name. Returns True if removed."""
    cfg = load_config()
    existing = cfg.get("mcp_servers") or []
    new_list = [e for e in existing if e.get("name") != name]
    if len(new_list) == len(existing):
        return False
    cfg["mcp_servers"] = new_list
    _write_config_file(cfg)
    return True


def set_mcp_server_enabled(name: str, enabled: bool) -> bool:
    """Toggle an MCP server's enabled flag. Returns True if the server exists."""
    cfg = load_config()
    existing = cfg.get("mcp_servers") or []
    found = False
    for e in existing:
        if e.get("name") == name:
            e["enabled"] = bool(enabled)
            found = True
    if found:
        cfg["mcp_servers"] = existing
        _write_config_file(cfg)
    return found


# ---------------------------------------------------------------------------
# Unified backend → (api_key, base_url, default_model, name) resolver
# ---------------------------------------------------------------------------

def resolve_backend(cfg: dict | None = None) -> dict:
    """
    Single source of truth for converting ``cfg["backend"]`` into the
    arguments needed to construct an OpenAI-compatible client. Returns:
        {
          "backend":  str,            # the active backend id
          "kind":     "builtin"|"custom",
          "name":     str,            # human-readable label
          "api_key":  str,
          "base_url": str,
          "default_model": str,
        }
    Falls back to einfra when the configured backend doesn't resolve
    (e.g. a deleted custom provider).
    """
    if cfg is None:
        cfg = load_config()
    backend = cfg.get("backend", "einfra")

    if backend == "ollama":
        return {
            "backend": "ollama",
            "kind": "builtin",
            "name": "Local (Ollama)",
            "api_key": "ollama",
            "base_url": cfg.get("ollama_url", OLLAMA_BASE_URL),
            "default_model": cfg.get("default_model", DEFAULT_MODEL),
        }
    if backend == "nim":
        return {
            "backend": "nim",
            "kind": "builtin",
            "name": "NVIDIA NIM",
            "api_key": cfg.get("nim_api_key", ""),
            "base_url": cfg.get("nim_url", NIM_BASE_URL),
            "default_model": cfg.get("default_model", NIM_DEFAULT_MODEL),
        }
    if backend not in BUILTIN_BACKENDS:
        provider = get_custom_provider(cfg, backend)
        if provider:
            return {
                "backend": backend,
                "kind": "custom",
                "name": provider.get("name") or backend,
                "api_key": provider.get("api_key", ""),
                "base_url": provider.get("base_url", ""),
                "default_model": provider.get("default_model") or cfg.get("default_model", ""),
            }
        # provider was deleted — fall through to einfra
    return {
        "backend": "einfra",
        "kind": "builtin",
        "name": "e-INFRA CZ",
        "api_key": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", BASE_URL),
        "default_model": cfg.get("default_model", DEFAULT_MODEL),
    }


def list_providers(cfg: dict | None = None) -> list[dict]:
    """Return the full provider catalog for the UI dropdown — built-ins first,
    then user-defined. Each entry: {id, name, kind, configured}."""
    if cfg is None:
        cfg = load_config()
    providers = [
        {
            "id": "einfra",
            "name": "e-INFRA CZ",
            "kind": "builtin",
            "configured": bool(cfg.get("api_key")),
        },
        {
            "id": "ollama",
            "name": "Local (Ollama)",
            "kind": "builtin",
            "configured": True,  # availability checked at switch time
        },
        {
            "id": "nim",
            "name": "NVIDIA NIM",
            "kind": "builtin",
            "configured": bool(cfg.get("nim_api_key")),
        },
    ]
    for p in get_custom_providers(cfg):
        providers.append({
            "id": p["id"],
            "name": p.get("name") or p["id"],
            "kind": "custom",
            "configured": bool(p.get("base_url")),
            "base_url": p.get("base_url", ""),
            "default_model": p.get("default_model", ""),
            "has_api_key": bool(p.get("api_key")),
            "models": p.get("models") or [],
        })
    return providers
