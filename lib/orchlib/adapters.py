"""Agent adapters — everything orch knows about a specific agent CLI.

The harness was Devin-only: launch argv, the TUI-ready regex, the
input-box marker, the permission menu, max-tier gating and the session
DB were all hard-coded. They are DATA now (`adapters/<id>.json`) for the
same reason `orch batch` reads a manifest instead of a `declare -A` map:
a mapping that lives in data can be audited, and a wrong one fails
loudly at load time instead of silently mid-run.

This module is pure (no orca/subprocess calls) so it is unit-testable;
bin/orch does the I/O.

Model specifier: `[<agent>:]<tier|model-id>`
  tier  = medium | high | max (shared vocabulary; each adapter maps a tier
          to a concrete model + effort)
  model = any id the adapter knows; a known id also picks its agent, so the
          legacy `--model swe-2-high` keeps meaning devin
"""
import json
import os
import re
import shlex

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
BUILTIN_DIR = os.path.join(HARNESS_DIR, "adapters")
TIERS = ("medium", "high", "max")
DEFAULT_AGENT = "devin"
# legacy shorthands that predate adapters (`--allow swe-2-max`)
LEGACY_ALLOW = {"swe-2-max": "devin:max"}


class AdapterError(Exception):
    pass


def search_dirs():
    """User adapters win over builtins: $ORCH_ADAPTERS_DIR, then
    ./.orch/adapters (per-repo), then the harness's adapters/."""
    dirs = []
    env = os.environ.get("ORCH_ADAPTERS_DIR")
    if env:
        dirs += [d for d in env.split(os.pathsep) if d]
    dirs.append(os.path.join(".orch", "adapters"))
    dirs.append(BUILTIN_DIR)
    return dirs


_CACHE = {}


def load(agent_id):
    if agent_id in _CACHE:
        return _CACHE[agent_id]
    for d in search_dirs():
        p = os.path.join(d, f"{agent_id}.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    a = json.load(f)
            except (OSError, ValueError) as e:
                raise AdapterError(f"bad adapter {p}: {e}")
            a.setdefault("id", agent_id)
            a["_path"] = p
            _validate(a)
            _CACHE[agent_id] = a
            return a
    raise AdapterError(
        f"unknown agent '{agent_id}' (known: {', '.join(list_ids())})")


def list_ids():
    ids = set()
    for d in search_dirs():
        if os.path.isdir(d):
            ids.update(f[:-5] for f in os.listdir(d)
                       if f.endswith(".json") and not f.startswith("_"))
    return sorted(ids)


def _validate(a):
    for k in ("provider", "binary", "launch", "models", "screen"):
        if k not in a:
            raise AdapterError(f"adapter {a['id']}: missing '{k}'")
    tiers = a["models"].get("tiers") or {}
    missing = [t for t in TIERS if t not in tiers]
    if missing:
        raise AdapterError(f"adapter {a['id']}: tiers missing {missing}")
    for k, rx in (a.get("screen") or {}).items():
        if isinstance(rx, str) and rx and k not in ("approval_answer",):
            try:
                re.compile(rx.replace("{first}", "x"))
            except re.error as e:
                raise AdapterError(f"adapter {a['id']}: screen.{k}: {e}")


def binary(a):
    """Env override first (ORCH_DEVIN_CMD etc.), then the adapter's binary."""
    env = a.get("binary_env")
    return (os.environ.get(env) if env else None) or a["binary"]


# ---------- model resolution ----------

def default_agent(flag=None, run=None):
    """--agent flag > ORCH_AGENT env > run['agent'] (persisted by
    `orch boot --agent`) > devin."""
    return (flag or os.environ.get("ORCH_AGENT")
            or (run or {}).get("agent") or DEFAULT_AGENT)


def _agent_for_model(model):
    """Reverse lookup: which adapter lists this exact model id?"""
    m = model.lower()
    for aid in list_ids():
        try:
            a = load(aid)
        except AdapterError:
            continue
        if m in (k.lower() for k in a["models"].get("known") or []):
            return aid
    return None


def resolve(spec, agent=None):
    """Parse a model specifier -> dict(agent, tier, model, effort, spec).

    `tier` is None when an explicit model id was given; `model` is None
    when the adapter can't select models from argv (opencode TUI)."""
    spec = (spec or "").strip()
    aid = None
    if ":" in spec:
        aid, spec = spec.split(":", 1)
        aid = aid.strip().lower()
    body = spec.strip()
    low = body.lower()
    if not aid:
        aid = (_agent_for_model(low) if low and low not in TIERS else None) \
            or agent or DEFAULT_AGENT
    a = load(aid)
    models = a["models"]
    if not low:
        low = models.get("default_tier") or "medium"
    if low in TIERS:
        t = models["tiers"][low] or {}
        return {"agent": aid, "tier": low, "model": t.get("model"),
                "effort": t.get("effort"), "spec": f"{aid}:{low}"}
    return {"agent": aid, "tier": _tier_of_model(a, body), "model": body,
            "effort": None, "spec": f"{aid}:{body}"}


def _tier_of_model(a, model):
    m = model.lower()
    for t in TIERS:
        if ((a["models"]["tiers"].get(t) or {}).get("model") or "").lower() == m:
            # a model shared by several tiers (codex high/max) is the lowest
            return t
    return None


def available_models(a, timeout=10):
    """The CLI's own current model list per adapter.models.list_source, or
    None when there is no source / it can't be read. Model ids rotate
    faster than adapters get edited (PR review: the codex tiers shipped
    already stale), so tier mappings are checked against the live list
    instead of trusting what was true when the JSON was written.
      {"json": [paths], "key": "models", "field": "slug", "skip_if": {...}}
      {"cmd": [argv]}   one model id per output line (first token)"""
    src = (a.get("models") or {}).get("list_source") or {}
    if src.get("json"):
        for p in src["json"]:
            p = os.path.expanduser(os.path.expandvars(p))
            if "$" in p or not os.path.isfile(p):
                continue
            try:
                with open(p) as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            items = doc.get(src.get("key") or "models") if isinstance(
                doc, dict) else doc
            skip = src.get("skip_if") or {}
            out = []
            for m in items or []:
                if not isinstance(m, dict):
                    continue
                if any(m.get(k) == v for k, v in skip.items()):
                    continue
                v = m.get(src.get("field") or "slug")
                if v:
                    out.append(str(v))
            return out
        return None
    if src.get("cmd"):
        import subprocess
        try:
            p = subprocess.run(src["cmd"], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except Exception:
            return None
        if p.returncode != 0:
            return None
        out = [ln.split()[0] for ln in p.stdout.splitlines()
               if ln.strip() and not ln.lstrip().startswith(("#", "-"))]
        return out or None
    return None


def stale_models(a, avail):
    """Tier models the CLI no longer lists (fuzzy CLIs match by prefix)."""
    if not avail:
        return []
    low = [m.lower() for m in avail]
    fuzzy = (a.get("models") or {}).get("fuzzy")
    out = []
    for t in TIERS:
        m = ((a["models"]["tiers"].get(t) or {}).get("model") or "").lower()
        if not m:
            continue
        if m in low or (fuzzy and any(x.startswith(m) or m in x
                                      for x in low)):
            continue
        out.append(f"{t}={m}")
    return out


def is_gated(r):
    """Max-tier gate. The tier itself, or a model on the adapter's
    gated list. For fuzzy-matching CLIs (devin resolves partial names)
    any input containing 'max' or being a prefix of a gated id is gated —
    'swe-2-ma' or a bare 'swe-2' would otherwise slip through and
    resolve to a max variant."""
    if r["tier"] == "max":
        return True
    a = load(r["agent"])
    m = (r["model"] or "").lower()
    if not m:
        return False
    gated = [g.lower() for g in a["models"].get("gated_models") or []]
    if m in gated:
        return True
    if a["models"].get("fuzzy"):
        return "max" in m or any(g.startswith(m) for g in gated)
    return False


def normalize_allow(allow):
    """--allow value(s) -> set of 'agent:max' / '*:max' tokens.
    Accepts 'max', 'claude:max', 'devin,claude:max', legacy 'swe-2-max'."""
    if not allow:
        return set()
    items = allow if isinstance(allow, (list, tuple, set)) else \
        str(allow).split(",")
    out = set()
    for it in items:
        it = str(it).strip().lower()
        if not it:
            continue
        it = LEGACY_ALLOW.get(it, it)
        if it == "max":
            it = "*:max"
        elif ":" not in it:
            it = f"{it}:max"   # bare agent name = that agent's max tier
        out.add(it)
    return out


def allowed(r, allow_tokens):
    if not is_gated(r):
        return True
    return bool({"*:max", f"{r['agent']}:max"} & set(allow_tokens))


# ---------- launch ----------

def _expand(tokens, mapping):
    out = []
    for t in tokens:
        if t in mapping and isinstance(mapping[t], list):
            out.extend(mapping[t])
            continue
        if re.search(r"\{(model|effort)\}", t) and not all(
                mapping.get(k) for k in re.findall(r"\{(\w+)\}", t)):
            return []   # a template whose value is unset drops entirely
        out.append(re.sub(r"\{(\w+)\}",
                          lambda m: str(mapping.get(m.group(1), m.group(0))),
                          t))
    return out


def launch_argv(a, r, hook_args=None, extra=None):
    """Adapter argv template -> concrete argv list."""
    L = a["launch"]
    mapping = {"model": r.get("model"), "effort": r.get("effort")}
    mapping.update({
        "{binary}": None,
        "{bypass}": list(L.get("bypass") or []),
        "{model_args}": _expand(L.get("model_args") or [], mapping)
        if r.get("model") else [],
        "{effort_args}": _expand(L.get("effort_args") or [], mapping)
        if r.get("effort") else [],
        "{hook_args}": list(hook_args or []),
    })
    out = []
    for t in L.get("argv") or ["{binary}"]:
        if t == "{binary}":
            out.extend(shlex.split(binary(a)))
        elif t in mapping and isinstance(mapping[t], list):
            out.extend(mapping[t])
        else:
            out.append(t)
    return out + list(extra or [])


def launch_command(a, r, hook_args=None, env=None):
    """Shell command string for `orca terminal create --command`."""
    envs = dict(a["launch"].get("env") or {})
    envs.update(env or {})
    pre = ["env"] + [f"{k}={v}" for k, v in envs.items()] if envs else []
    return " ".join(shlex.quote(x) for x in
                    pre + launch_argv(a, r, hook_args))


# ---------- screen ----------

def screen_rx(a, key, first=None):
    rx = (a.get("screen") or {}).get(key) or ""
    if not rx:
        return None
    if "{first}" in rx:
        if not first:
            return None
        rx = rx.replace("{first}", re.escape(first))
    return re.compile(rx, re.M | re.I if key != "input_echo" else re.M)


def screen_match(a, key, text, first=None):
    rx = screen_rx(a, key, first)
    return bool(rx and rx.search(text or ""))


def startup_prompt(a, text):
    """First startup gate (workspace trust, bypass confirmation, upgrade
    notice) visible on the screen -> keys to send, or None. Measured:
    trust gates stalled a claude worker before its first turn; Orca's
    own blocked-prompt classifier lists the same family."""
    for sp in (a.get("screen") or {}).get("startup_prompts") or []:
        if re.search(sp["match"], text or "", re.I):
            return sp.get("keys") or ["enter"]
    return None


def approval_key(a, text):
    """Menu number to press on an approval menu: the option whose label
    matches approval_answer (e.g. 'bypass', "don't ask again")."""
    if not screen_match(a, "approval", text):
        return None
    ans = (a.get("screen") or {}).get("approval_answer") or "always"
    for m in re.finditer(r"(?m)^\s*[\[>❯›]?\s*(\d)[).:\]]?\s+(.*)$", text):
        if re.search(ans, m.group(2), re.I):
            return m.group(1)
    return None


def spec_notes(a):
    return (a.get("spec_notes") or "").strip()
