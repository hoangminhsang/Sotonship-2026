"""Fix suggester: map each violation class to a concrete control.

A lookup table, not code analysis and not an LLM. It reads the violation
classes the oracle produced and emits suggestions, mirroring how a pentest
report recommends fixes from the outside. Each entry names the control, a short
rationale, and the matching *after*-study defence (D1-D3).
"""
from __future__ import annotations

FIXES: dict[str, dict] = {
    "V_read_restricted": {
        "control": "Tool allow-list + resource-level permissions",
        "detail": (
            "query_assets must refuse restricted ids/classifications; the agent "
            "should not have this tool for doc-summary tasks."
        ),
        "after_defence": "D2 (check tool inputs in code)",
    },
    "V_exfiltration": {
        "control": "Argument validation on create_ticket",
        "detail": (
            "Reject assignees outside allowed domains; strip/deny bodies "
            "containing secret markers. Apply least privilege so the agent "
            "can't read the secret in the first place."
        ),
        "after_defence": "D2 (check tool inputs in code)",
    },
    "V_tool_misuse": {
        "control": "Human-in-the-loop for high-impact actions",
        "detail": (
            "Route P1 tickets (or any unrequested high-priority action) to human "
            "approval instead of auto-executing."
        ),
        "after_defence": "D3 (ask a human)",
    },
}

# Cross-cutting control that applies whenever any injection landed via content
# the agent read (place in P1/P2/P3): input trust labels (D1).
TRUST_LABEL_FIX: dict = {
    "control": "Input trust labels on retrieved content",
    "detail": "Wrap tool/document output as untrusted data, not instructions.",
    "after_defence": "D1 (mark outside content)",
}


def suggest(verdict_classes: list[str], injected: bool) -> list[dict]:
    """Return the suggested controls for a verdict.

    ``injected`` is true for any attack test (payload present) and false for
    benign tests; when true, the cross-cutting trust-label fix is appended.
    """
    out = [FIXES[c] for c in verdict_classes if c in FIXES]
    if injected:
        out.append(TRUST_LABEL_FIX)
    return out
