"""Fingerprint: an exact ID card of what was tested.

A "yes" only counts for the precise thing that was tested. If one letter of the
system prompt changes, or the model, or the tools, or the defences, the
fingerprint changes -- and the old approval no longer applies (the re-test rule
in :mod:`lab.governance.decision`).

Everything here is plain hashing of inputs, so a fingerprint is reproducible
and carries no secrets (only digests of content).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def sha256_text(text: str) -> str:
    """Short, stable sha256 of a string (hex, ``sha256:`` prefixed)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# The fields that define *what was tested*. Two candidates with the same
# identity fields are the same experiment; a change in any of them invalidates
# an old approval. ``candidate_id``, ``submitted_by`` and ``created_at`` are
# bookkeeping and are deliberately excluded.
IDENTITY_FIELDS = (
    "model",
    "model_digest",
    "system_prompt_sha256",
    "tools",
    "defences",
    "rules_sha256",
    "test_set_sha256",
)


@dataclass
class Fingerprint:
    """The ID card written before a candidate is tested."""

    candidate_id: str
    submitted_by: str
    model: str
    model_digest: str
    system_prompt_sha256: str
    tools: list[str]
    defences: list[str]
    rules_sha256: str
    test_set_sha256: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def identity_hash(self) -> str:
        """Hash of the identity fields only -- the key the re-test rule compares."""
        ident = {k: getattr(self, k) for k in IDENTITY_FIELDS}
        canonical = json.dumps(ident, sort_keys=True, separators=(",", ":"))
        return sha256_text(canonical)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identity_hash"] = self.identity_hash()
        return d


def fingerprint(
    candidate_id: str,
    submitted_by: str,
    *,
    model: str,
    model_digest: str,
    system_prompt: str,
    tools: list[str],
    defences: list[str],
    rules_text: str,
    test_set_text: str,
) -> Fingerprint:
    """Build a :class:`Fingerprint` from the raw candidate inputs.

    The prompt, rules and test set are hashed (not stored), so the ID card is
    compact and reveals nothing sensitive.
    """
    return Fingerprint(
        candidate_id=candidate_id,
        submitted_by=submitted_by,
        model=model,
        model_digest=model_digest,
        system_prompt_sha256=sha256_text(system_prompt),
        tools=sorted(tools),
        defences=sorted(defences),
        rules_sha256=sha256_text(rules_text),
        test_set_sha256=sha256_text(test_set_text),
    )


def save(fp: Fingerprint, out_dir: Path | str) -> Path:
    """Write the fingerprint to ``<out_dir>/<candidate_id>.json`` and return it."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{fp.candidate_id}.json"
    path.write_text(json.dumps(fp.to_dict(), indent=2), encoding="utf-8")
    return path


def load(path: Path | str) -> Fingerprint:
    """Load a fingerprint written by :func:`save` (ignores derived fields)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.pop("identity_hash", None)
    return Fingerprint(**data)
