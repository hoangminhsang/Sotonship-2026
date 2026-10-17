#!/usr/bin/env python3
"""Preflight before a LIVE run: is the machine ready, and does nothing leave it?

One line per check, ``OK`` or ``FAIL``; exit 1 if any check fails.

    ollama   GET {OLLAMA_HOST}/api/tags answers within 5 s
    model    the model is present; its digest is printed (and compared to --digest)
    files    corpus, data, policy and control map are present
    audit    the audit log's hash chain verifies
    host     OLLAMA_HOST is localhost / 127.0.0.1 (prompts stay on this machine)
    tracing  LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY / LANGSMITH_* are unset

Standard library only (plus the repo's own ``lab.governance.audit``).

    python scripts/preflight.py [--digest sha256:...] [--model NAME] [--host URL] [--log PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_MODEL = "qwen2.5:7b-instruct"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_LOG = ROOT / "governance_out" / "audit_log.jsonl"
TIMEOUT_S = 5.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUIRED_FILES = (
    "corpus/dev.jsonl",
    "corpus/test.jsonl",
    "data/assets.json",
    "data/rules.yaml",
    "lab/governance/policy.yaml",
    "lab/assurance/control_map.yaml",
)
TRACING_VARS = ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_ENDPOINT")


def fetch_tags(host: str, timeout: float = TIMEOUT_S) -> dict:
    """GET ``/api/tags`` from Ollama. Raises on any network or decode error."""
    with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout) as resp:
        return json.load(resp)


def find_model(tags: dict, model: str) -> dict | None:
    """Exact name match, then base-name match (``qwen2.5:7b-instruct`` ~ ``:latest``)."""
    models = tags.get("models", []) if isinstance(tags, dict) else []
    for rec in models:
        if rec.get("name") == model:
            return rec
    want = model.split(":")[0]
    for rec in models:
        if str(rec.get("name", "")).split(":")[0] == want:
            return rec
    return None


def _norm_digest(d: str) -> str:
    d = (d or "").strip()
    return d if (d.startswith("sha256:") or not d) else f"sha256:{d}"


def run_checks(*, host: str, model: str, want_digest: str | None, log_path: Path,
               env: dict, fetch=fetch_tags) -> tuple[list[str], list[str]]:
    """Run every check. Returns ``(ok_lines, fail_lines)``; never raises."""
    ok: list[str] = []
    fail: list[str] = []

    # 1. Ollama answers, 2. model present + digest.
    try:
        tags = fetch(host, TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - any failure means "not ready"
        fail.append(f"FAIL ollama: {host}/api/tags did not answer within {TIMEOUT_S:.0f} s "
                    f"({type(e).__name__}: {e})")
        fail.append(f"FAIL model: cannot check {model!r} (Ollama unreachable)")
    else:
        ok.append(f"OK   ollama: {host}/api/tags answered")
        rec = find_model(tags, model)
        if rec is None:
            names = ", ".join(str(m.get("name")) for m in tags.get("models", [])) or "none"
            fail.append(f"FAIL model: {model!r} not present (installed: {names})")
        else:
            digest = _norm_digest(str(rec.get("digest", "")))
            line = f"model: {rec.get('name')} digest {digest or '(none reported)'}"
            if want_digest:
                if digest == _norm_digest(want_digest):
                    ok.append(f"OK   {line} (matches --digest)")
                else:
                    fail.append(f"FAIL {line} does not match --digest {_norm_digest(want_digest)}")
            else:
                ok.append(f"OK   {line}")

    # 3. Files.
    missing = [f for f in REQUIRED_FILES if not (ROOT / f).exists()]
    if missing:
        fail.append(f"FAIL files: missing {', '.join(missing)}")
    else:
        ok.append(f"OK   files: {len(REQUIRED_FILES)} required files present")

    # 4. Audit log.
    try:
        from lab.governance import audit
        if not Path(log_path).exists():
            ok.append(f"OK   audit: no log yet at {log_path} (nothing to verify)")
        else:
            check = audit.verify_log(log_path)
            if check.ok:
                ok.append(f"OK   audit: {check.detail}")
            else:
                fail.append(f"FAIL audit: TAMPERING at entry #{check.bad_seq} -- {check.detail}")
    except Exception as e:  # noqa: BLE001 - a malformed log is a failure, not a crash
        fail.append(f"FAIL audit: could not verify {log_path} ({type(e).__name__}: {e})")

    # 5. Host stays local.
    hostname = urlparse(host).hostname or ""
    if hostname in LOCAL_HOSTS:
        ok.append(f"OK   host: {host} is local")
    else:
        fail.append(f"FAIL host: {host} is not localhost/127.0.0.1 -- prompts would leave this machine")

    # 6. No tracing upload. Names only; values are never printed.
    leaking = [k for k in TRACING_VARS if env.get(k)]
    leaking += sorted(k for k in env if k.startswith("LANGSMITH_") and env.get(k))
    if leaking:
        fail.append(f"FAIL tracing: unset {', '.join(leaking)} (run traces could be uploaded)")
    else:
        ok.append("OK   tracing: no LangChain/LangSmith tracing variables set")

    return ok, fail


def main(argv: list[str] | None = None, *, fetch=fetch_tags, env: dict | None = None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Preflight checks before a LIVE run.")
    ap.add_argument("--model", default=env.get("LAB_MODEL", DEFAULT_MODEL))
    ap.add_argument("--host", default=env.get("OLLAMA_HOST", DEFAULT_HOST))
    ap.add_argument("--digest", default=None, help="expected model digest (e.g. from the README)")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help="audit log to verify")
    args = ap.parse_args(argv)

    ok, fail = run_checks(host=args.host, model=args.model, want_digest=args.digest,
                          log_path=Path(args.log), env=env, fetch=fetch)
    for line in ok + fail:
        print(line)
    if fail:
        print(f"PREFLIGHT FAILED: {len(fail)} problem{'s' if len(fail) != 1 else ''} -- do not run LIVE")
        return 1
    print("PREFLIGHT OK -- ready for a LIVE run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
