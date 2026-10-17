"""Insecure Agent Attack Lab — the *before* half of a before/after study.

This package builds a deliberately insecure, tool-using LLM agent, attacks it
with a corpus of indirect prompt injections, and measures how often the
attacks succeed. Everything is local and synthetic; the only "secret" is a
clearly fake string so a leak is unmistakable and harmless.

Module map:
    world    -- generate/load the synthetic Northwind Retail world
    mcp_server -- MCP server exposing three unguarded tools (stdio)
    agent    -- insecure LangGraph agent + Ollama model wiring
    attacks  -- goal x technique x place injection corpus generator
    benign   -- benign request generator
    runner   -- executes tests, writes one trace per run
    oracle   -- deterministic rule-based violation checker
    metrics  -- ASR, per-technique ASR, benign success, Wilson CI
    fixes    -- violation class -> suggested control
    defences -- D1-D3 enforcement for the *after* study (+ deterministic replay)

Sub-packages (the Agent Release Gate):
    governance -- short loop: fingerprint, gate, decision, hash-chained audit log
    assurance  -- long loop: risk register, control mapping, evidence, traceability
"""

__all__ = [
    "world",
    "mcp_server",
    "agent",
    "attacks",
    "benign",
    "runner",
    "oracle",
    "metrics",
    "fixes",
    "defences",
]
