"""scripts/run_models.py: several Ollama models through the governance pipeline, offline.

Nothing here calls Ollama or a real model. Three kinds of child process are used:

* the repo's own ``scripts/run_governance.py`` in SCRIPTED mode (DEMO, no ``--live``),
  with its output sent to a tmp folder;
* small stand-in scripts in a tmp "fake repo" (same flags, no model) for everything that
  needs a LIVE-shaped run: run folders, traces, timing, Ctrl-C;
* no ``ollama`` binary at all (the pull step is pointed at a stand-in).

The real ``results/`` folder of the repo is never written.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from lab import run_folder

ROOT = Path(__file__).resolve().parent.parent
STAMP = "20261018T060000Z"


@pytest.fixture(scope="module")
def rm():
    spec = importlib.util.spec_from_file_location("run_models_cli", ROOT / "scripts" / "run_models.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_models_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# config/models.txt and the slug.
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str, *, raw: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_models_file_skips_comments_and_blank_lines(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "# header\n\nqwen2.5:7b-instruct\n   # indented comment\n\nllama3.1:8b\n")
    models = rm.read_models(f)
    assert [m.tag for m in models] == ["qwen2.5:7b-instruct", "llama3.1:8b"]
    assert [m.slug for m in models] == ["qwen2.5-7b-instruct", "llama3.1-8b"]


def test_models_file_accepts_windows_editors(rm, tmp_path):
    # Notepad: UTF-8 BOM and CRLF line ends. A trailing "# note" after a tag is also allowed.
    raw = b"\xef\xbb\xbf# models\r\nqwen2.5:7b-instruct\r\n\r\nllama3.1:8b  # second\r\n"
    f = _write(tmp_path / "models.txt", "", raw=raw)
    assert [m.tag for m in rm.read_models(f)] == ["qwen2.5:7b-instruct", "llama3.1:8b"]


def test_models_file_refuses_duplicates(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "qwen2.5:7b-instruct\n# again below\nllama3.1:8b\nqwen2.5:7b-instruct\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    msg = str(exc.value)
    assert "duplicate" in msg and "qwen2.5:7b-instruct" in msg
    assert "line 4" in msg and "line 1" in msg  # says where both copies are


def test_models_file_refuses_two_tags_that_share_a_slug(rm, tmp_path):
    # "a:b" and "a-b" would both write to <stamp>_a-b and share one run-folder name.
    f = _write(tmp_path / "models.txt", "tiny:1b\ntiny-1b\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    assert "slug" in str(exc.value) and "tiny-1b" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [
        "two tags on one line",
        "-leading-dash",              # would read as an option on the command line
        "semi;colon",
        "back`tick",
        "$(whoami)",
        "caf\u00e9:7b",
    ],
)
def test_models_file_refuses_odd_tags(rm, tmp_path, bad):
    f = _write(tmp_path / "models.txt", f"qwen2.5:7b-instruct\n{bad}\n")
    with pytest.raises(rm.ModelsFileError) as exc:
        rm.read_models(f)
    assert "line 2" in str(exc.value)


@pytest.mark.parametrize("tag", ["qwen2.5:7b-instruct", "llama3.1:8b-instruct-q4_K_M", "hf.co/user/repo:Q4_K_M", "granite3.3:8b"])
def test_models_file_accepts_real_looking_tags(rm, tmp_path, tag):
    f = _write(tmp_path / "models.txt", tag + "\n")
    assert [m.tag for m in rm.read_models(f)] == [tag]


def test_models_file_with_no_models_is_an_error(rm, tmp_path):
    f = _write(tmp_path / "models.txt", "# nothing here\n\n")
    with pytest.raises(rm.ModelsFileError, match="no models"):
        rm.read_models(f)


def test_models_file_missing_is_an_error(rm, tmp_path):
    with pytest.raises(rm.ModelsFileError, match="not found"):
        rm.read_models(tmp_path / "nope.txt")


def test_shipped_models_file(rm):
    """config/models.txt: the reference model, and a Granite note that is NOT a guessed tag."""
    path = ROOT / "config" / "models.txt"
    models = rm.read_models(path)
    assert [m.tag for m in models] == ["qwen2.5:7b-instruct"]
    text = path.read_text(encoding="utf-8")
    assert "# IBM Granite: tag not verified; check it with \"ollama pull <tag>\" before adding it" in text
    live = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("granite" in ln.lower() for ln in live), "a Granite tag must not be guessed"


@pytest.mark.parametrize(
    "text",
    ["qwen2.5:7b-instruct", "llama3.1:8b-instruct-q4_K_M", "hf.co/user/repo:Q4_K_M", "a b", "x" * 3, "", "\u00e9\u4e2d:1", "a/b\\c", "..", "C:\\x"],
)
def test_slug_is_the_run_folder_slug(rm, text):
    """The batch finds its run folders by this slug, so it must equal lab.run_folder._safe."""
    assert rm.safe_slug(text) == run_folder._safe(text)


# --------------------------------------------------------------------------- #
# A fake repo: the same scripts/ layout and flags, with stand-ins that need no model.
# --------------------------------------------------------------------------- #
STUB_PREFLIGHT = textwrap.dedent(
    '''\
    """Stand-in for scripts/preflight.py: fails only for the tags named in stub_behaviour.json."""
    import json, os, sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    tag = os.environ.get("LAB_MODEL", "")
    beh = json.loads((repo / "stub_behaviour.json").read_text()) if (repo / "stub_behaviour.json").exists() else {}
    fail = tag in beh.get("preflight_fail", [])
    with (repo / "calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"step": "preflight", "tag": tag, "pid": os.getpid()}) + "\\n")
    print("PREFLIGHT FAILED: 1 problem -- do not run LIVE" if fail else "PREFLIGHT OK -- ready for a LIVE run")
    sys.exit(1 if fail else 0)
    '''
)

STUB_GOV = textwrap.dedent(
    '''\
    """Stand-in for scripts/run_governance.py: same flags, no model, no network.

    Behaviour per model comes from <repo>/stub_behaviour.json:
      {"traces": {"<tag>": "tools" | "text" | "errors" | "plain" | "none", "*": ...},
       "sleep": seconds, "exit": {"<tag>": code}}
    """
    import argparse, json, os, signal, sys, time
    from datetime import datetime, timezone
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tag = os.environ.get("LAB_MODEL", "")
    beh = json.loads((repo / "stub_behaviour.json").read_text()) if (repo / "stub_behaviour.json").exists() else {}
    kind = beh.get("traces", {}).get(tag, beh.get("traces", {}).get("*", "tools"))
    sleep = float(beh.get("sleep", 0))
    code = int(beh.get("exit", {}).get(tag, 0))
    safe = lambda t: "".join(c if (c.isalnum() or c in "._-") else "-" for c in str(t)) or "x"


    def note(**kw):
        rec = dict(kw, tag=tag, pid=os.getpid(), live=args.live, out=args.out, sample=args.sample,
                   reps=args.reps, t=time.time(), env_unbuffered=os.environ.get("PYTHONUNBUFFERED"),
                   env_utf8=os.environ.get("PYTHONUTF8"))
        with (repo / "calls.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\\n")


    signals = []
    signal.signal(signal.SIGINT, lambda s, f: (signals.append(s), note(step="sigint", n=len(signals))))
    note(step="start")


    def write_run_folders():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for cid in ("RC-001", "RC-002", "RC-003"):
            run_id = f"{stamp}_{safe(tag)}_{cid}"
            d = repo / "results" / "runs" / run_id
            (d / "traces").mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps(
                {"schema": 1, "run_id": run_id, "model": tag, "candidate": cid, "locked": True}))
            for test_id in ("ATK-0001", "BEN-0001"):
                tr = {"test_id": test_id, "rep": 1, "model": tag, "tool_calls": [], "final_text": "ok", "error": None}
                if kind == "tools":
                    tr["tool_calls"] = [{"name": "read_doc", "args": {"name": "vpn-setup.md"}, "blocked": False}]
                elif kind == "text":
                    tr["final_text"] = '<tool_call>{"name": "read_doc", "arguments": {}}</tool_call>'
                elif kind == "errors":
                    tr["final_text"], tr["error"] = "", "ResponseError('does not support tools')"
                (d / "traces" / f"{test_id}__rep1.json").write_text(json.dumps(tr))


    deadline = time.time() + sleep
    while time.time() < deadline and not signals:
        time.sleep(0.02)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stub_ran.txt").write_text(tag)
    if args.live and kind != "none":
        write_run_folders()
    note(step="interrupted" if signals else "end", code=130 if signals else code)
    sys.exit(130 if signals else code)
    '''
)


def make_repo(tmp_path: Path, tags=("alpha:1b", "beta:2b"), *, race_fixed: bool = True, behaviour: dict | None = None) -> Path:
    """A tmp repo root with the stand-in scripts and a models file."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    gov = STUB_GOV
    if not race_fixed:
        gov += "\n# the corpus race, still present:\n# corpus.build_and_write()\n"
    (repo / "scripts" / "run_governance.py").write_text(gov, encoding="utf-8")
    (repo / "scripts" / "preflight.py").write_text(STUB_PREFLIGHT, encoding="utf-8")
    (repo / "config" / "models.txt").write_text("\n".join(tags) + "\n", encoding="utf-8")
    if behaviour is not None:
        (repo / "stub_behaviour.json").write_text(json.dumps(behaviour), encoding="utf-8")
    return repo


def calls(repo: Path) -> list[dict]:
    path = repo / "calls.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def status_lines(repo: Path, stamp: str = STAMP) -> list[dict]:
    path = repo / "results" / "models" / stamp / "status.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


def events(repo: Path, slug: str | None = None) -> list[str]:
    return [e["event"] for e in status_lines(repo) if slug is None or e["slug"] == slug]


# --------------------------------------------------------------------------- #
# The plan and --dry-run.
# --------------------------------------------------------------------------- #
def _plan(rm, tmp_path, argv=(), tags=("alpha:1b", "beta:2b"), race_fixed=True):
    repo = make_repo(tmp_path, tags, race_fixed=race_fixed)
    opts = rm.parse_args(list(argv))
    layout = rm.Layout(root=repo, results=repo / "results")
    models = rm.read_models(repo / "config" / "models.txt")
    return rm.build_plan(opts, layout, models, STAMP, race_fixed=rm.corpus_race_fixed(repo))


def test_plan_commands_are_the_documented_ones(rm, tmp_path):
    plan = _plan(rm, tmp_path)
    assert plan["stamp"] == STAMP and plan["mode"] == "live"
    m = plan["models"][0]
    assert (m["tag"], m["slug"]) == ("alpha:1b", "alpha-1b")
    cmds = m["commands"]
    assert cmds["preflight"] == ["python", "scripts/preflight.py"]
    assert cmds["smoke"] == ["python", "scripts/run_governance.py", "--live", "--sample", "1", "--reps", "1",
                             "--out", f"results/governance/{STAMP}_alpha-1b_smoke"]
    assert cmds["full"] == ["python", "scripts/run_governance.py", "--live", "--sample", "26", "--reps", "3",
                            "--out", f"results/governance/{STAMP}_alpha-1b"]
    assert cmds["pull"] is None
    assert m["smoke_out"] == f"results/governance/{STAMP}_alpha-1b_smoke"
    assert m["full_out"] == f"results/governance/{STAMP}_alpha-1b"
    assert plan["batch_dir"] == f"results/models/{STAMP}"


def test_plan_has_no_absolute_paths(rm, tmp_path):
    text = json.dumps(_plan(rm, tmp_path, ["--pull"]))
    assert str(tmp_path) not in text and "/Users/" not in text and ":\\\\" not in text


def test_sample_and_reps_override_the_full_run_only(rm, tmp_path):
    cmds = _plan(rm, tmp_path, ["--sample", "17", "--reps", "2"])["models"][0]["commands"]
    assert cmds["smoke"][2:8] == ["--live", "--sample", "1", "--reps", "1", "--out"]
    assert cmds["full"][2:8] == ["--live", "--sample", "17", "--reps", "2", "--out"]


def test_pull_adds_an_ollama_pull_step(rm, tmp_path):
    cmds = _plan(rm, tmp_path, ["--pull"])["models"][0]["commands"]
    assert cmds["pull"] == ["ollama", "pull", "alpha:1b"]


def test_scripted_plan_has_no_live_flag_and_no_preflight(rm, tmp_path):
    plan = _plan(rm, tmp_path, ["--scripted"])
    assert plan["mode"] == "scripted"
    cmds = plan["models"][0]["commands"]
    assert cmds["preflight"] is None
    assert "--live" not in cmds["smoke"] and "--live" not in cmds["full"]
    assert cmds["smoke"][-2:] == ["--out", f"results/governance/{STAMP}_alpha-1b_smoke"]


def test_planned_commands_are_accepted_by_the_real_run_governance(rm, tmp_path):
    """The flags this script builds must parse with the repo's real run_governance parser."""
    spec = importlib.util.spec_from_file_location("run_governance_for_plan_test", ROOT / "scripts" / "run_governance.py")
    gov = importlib.util.module_from_spec(spec)
    sys.modules["run_governance_for_plan_test"] = gov
    spec.loader.exec_module(gov)
    parser = gov.build_parser()
    models = rm.read_models(ROOT / "config" / "models.txt")
    for scripted in (False, True):
        opts = rm.parse_args(["--scripted"] if scripted else [])
        plan = rm.build_plan(opts, rm.Layout(root=ROOT, results=tmp_path / "results"), models, STAMP, race_fixed=True)
        for step in ("smoke", "full"):
            argv = plan["models"][0]["commands"][step]
            assert argv[:2] == ["python", "scripts/run_governance.py"]
            ns = parser.parse_args(argv[2:])   # SystemExit here means a flag the real script does not know
            assert ns.live is (not scripted)
            assert ns.out.endswith("_smoke" if step == "smoke" else "qwen2.5-7b-instruct")
            assert (ns.sample, ns.reps) == ((1, 1) if step == "smoke" else (26, 3))


def test_child_environment(rm, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    env = rm.child_env("alpha:1b")
    assert env["LAB_MODEL"] == "alpha:1b"
    assert env["PYTHONUNBUFFERED"] == "1" and env["PYTHONUTF8"] == "1"
    assert env["SOME_OTHER_VAR"] == "kept"


def test_dry_run_prints_the_plan_and_writes_nothing(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--dry-run"], root=repo, stamp=STAMP) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    for needle in ("alpha:1b", "alpha-1b", "beta:2b", "beta-2b",
                   "scripts/preflight.py",
                   f"--live --sample 1 --reps 1 --out results/governance/{STAMP}_alpha-1b_smoke",
                   f"--live --sample 26 --reps 3 --out results/governance/{STAMP}_alpha-1b",
                   f"results/models/{STAMP}"):
        assert needle in out, needle
    assert not (repo / "results").exists(), "a dry run writes nothing"
    assert calls(repo) == [], "a dry run runs nothing"


def test_dry_run_scripted_says_tool_support_is_not_checked(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--dry-run", "--scripted"], root=repo, stamp=STAMP) == 0
    out = capsys.readouterr().out
    assert "SCRIPTED" in out and "not checked (scripted)" in out and "--live" not in out


def test_only_selects_by_slug_or_tag(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, ("alpha:1b", "beta:2b", "gamma:3b"))
    assert rm.main(["--dry-run", "--only", "beta-2b", "--only", "gamma:3b"], root=repo, stamp=STAMP) == 0
    out = capsys.readouterr().out
    assert "beta-2b" in out and "gamma-3b" in out and "alpha" not in out


def test_only_with_an_unknown_model_lists_the_choices(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--dry-run", "--only", "nope"], root=repo, stamp=STAMP) == 2
    err = capsys.readouterr().err
    assert "nope" in err and "alpha-1b" in err and "beta-2b" in err


@pytest.mark.parametrize("argv", [["--parallel", "0"], ["--parallel", "5"], ["--parallel", "x"], ["--sample", "0"], ["--reps", "-1"]])
def test_bad_numbers_are_usage_errors(rm, tmp_path, argv):
    repo = make_repo(tmp_path)
    with pytest.raises(SystemExit) as exc:
        rm.main([*argv, "--dry-run"], root=repo, stamp=STAMP)
    assert exc.value.code == 2


def test_pull_needs_a_live_run(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--scripted", "--pull", "--dry-run"], root=repo, stamp=STAMP) == 2
    assert "--pull" in capsys.readouterr().err


def test_missing_models_file_and_missing_scripts_are_clear_errors(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--dry-run", "--models", str(tmp_path / "nope.txt")], root=repo, stamp=STAMP) == 2
    assert "not found" in capsys.readouterr().err
    (repo / "scripts" / "run_governance.py").unlink()
    assert rm.main(["--dry-run"], root=repo, stamp=STAMP) == 2
    assert "run_governance.py" in capsys.readouterr().err


def test_python_override_must_exist(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    assert rm.main(["--dry-run", "--python", str(tmp_path / "no-such-python")], root=repo, stamp=STAMP) == 2
    assert "--python" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# --parallel and the corpus race.
# --------------------------------------------------------------------------- #
def test_race_fix_detection(rm, tmp_path):
    assert rm.corpus_race_fixed(make_repo(tmp_path / "a", race_fixed=True)) is True
    assert rm.corpus_race_fixed(make_repo(tmp_path / "b", race_fixed=False)) is False
    assert rm.corpus_race_fixed(tmp_path / "nowhere") is False  # cannot read it: assume the race is there
    # the repo's own checkout: absent today, present once lane A has replaced the call
    text = (ROOT / "scripts" / "run_governance.py").read_text(encoding="utf-8")
    assert rm.corpus_race_fixed(ROOT) is ("build_and_write(" not in text)


def test_parallel_is_forced_to_one_while_the_race_fix_is_absent(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, race_fixed=False)
    assert rm.main(["--dry-run", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    out = capsys.readouterr().out
    assert "forced to 1" in out and "build_and_write(" in out


def test_parallel_two_is_kept_when_the_race_fix_is_present(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, race_fixed=True)
    assert rm.main(["--dry-run", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    assert "forced" not in capsys.readouterr().out
    plan = _plan(rm, tmp_path / "p", ["--parallel", "2"])
    assert (plan["parallel"], plan["parallel_requested"], plan["parallel_note"]) == (2, 2, None)


def test_plan_records_why_parallel_was_forced(rm, tmp_path):
    plan = _plan(rm, tmp_path, ["--parallel", "2"], race_fixed=False)
    assert (plan["parallel"], plan["parallel_requested"]) == (1, 2)
    assert "build_and_write(" in plan["parallel_note"]


def test_parallel_never_exceeds_the_number_of_models(rm, tmp_path):
    plan = _plan(rm, tmp_path, ["--parallel", "4"], tags=("alpha:1b",))
    assert plan["parallel"] == 1 and plan["parallel_note"] is None  # nothing was forced
    # one model cannot run twice at once, so a missing race fix changes nothing and needs no note
    plan = _plan(rm, tmp_path / "x", ["--parallel", "4"], tags=("alpha:1b",), race_fixed=False)
    assert plan["parallel"] == 1 and plan["parallel_note"] is None
