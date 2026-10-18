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

    Behaviour comes from <repo>/stub_behaviour.json:
      {"traces": {"<tag>": "tools" | "text" | "errors" | "plain" | "none", "*": ...},
       "sleep": seconds, "stubborn": true (ignore Ctrl-C), "unlocked_on_interrupt": true,
       "exit_smoke": {"<tag>": code}, "exit_full": {"<tag>": code}}
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


    class _Corpus:
        """Stands in for lab.corpus. The old build_and_write() rewrote corpus/*.jsonl on every run."""

        def check_matches_generator(self):
            return {}

        def build_and_write(self):
            return None


    corpus = _Corpus()
    # @CORPUS_CALL@

    tag = os.environ.get("LAB_MODEL", "")
    beh = json.loads((repo / "stub_behaviour.json").read_text()) if (repo / "stub_behaviour.json").exists() else {}
    kind = beh.get("traces", {}).get(tag, beh.get("traces", {}).get("*", "tools"))
    sleep = float(beh.get("sleep", 0))
    stubborn = bool(beh.get("stubborn"))
    code = int(beh.get("exit_smoke" if args.out.endswith("_smoke") else "exit_full", {}).get(tag, 0))
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


    def write_run_folders(locked=True):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for cid in ("RC-001", "RC-002", "RC-003"):
            run_id = f"{stamp}_{safe(tag)}_{cid}"
            d = repo / "results" / "runs" / run_id
            (d / "traces").mkdir(parents=True)
            (d / "manifest.json").write_text(json.dumps(
                {"schema": 1, "run_id": run_id, "model": tag, "candidate": cid, "locked": locked}))
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
    while time.time() < deadline and (stubborn or not signals):
        time.sleep(0.02)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stub_ran.txt").write_text(tag)
    if args.live and kind != "none":
        write_run_folders(locked=not (signals and beh.get("unlocked_on_interrupt")))
    note(step="interrupted" if signals else "end", code=130 if signals else code)
    sys.exit(130 if signals else code)
    '''
)


def make_repo(tmp_path: Path, tags=("alpha:1b", "beta:2b"), *, race_fixed: bool = True, behaviour: dict | None = None) -> Path:
    """A tmp repo root with the stand-in scripts and a models file."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    # The race fix is read from the code: a call to check_matches_generator and none to build_and_write.
    gov = STUB_GOV.replace("# @CORPUS_CALL@", f"corpus.{'check_matches_generator' if race_fixed else 'build_and_write'}()")
    (repo / "scripts" / "run_governance.py").write_text(gov, encoding="utf-8")
    (repo / "scripts" / "preflight.py").write_text(STUB_PREFLIGHT, encoding="utf-8")
    (repo / "config" / "models.txt").write_text("\n".join(tags) + "\n", encoding="utf-8")
    if behaviour is not None:
        (repo / "stub_behaviour.json").write_text(json.dumps(behaviour), encoding="utf-8")
    return repo


def calls(repo: Path) -> list[dict]:
    path = repo / "calls.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def status_lines(repo: Path) -> list[dict]:
    """The lines of the one batch's status.jsonl (a CLI process picks its own stamp)."""
    (path,) = (repo / "results" / "models").glob("*/status.jsonl")
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
    race = rm.race_fix_check(repo)
    return rm.build_plan(opts, layout, models, STAMP, race_fixed=race.fixed, race_why=race.why)


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
# --parallel and the corpus race. The fix is read from the CODE of run_governance.py: a call to
# check_matches_generator and no call to build_and_write. A comment, a docstring or a string
# that mentions build_and_write( must not count (lane A's file has such a comment).
# --------------------------------------------------------------------------- #
FIXED_BUT_MENTIONS_THE_OLD_CALL = '''\
"""Run the pipeline.

The old build_and_write() rewrote corpus/*.jsonl on every run.
"""
import sys


def run(corpus):
    # Read-only check. The old build_and_write() rewrote corpus/*.jsonl on every
    # run, so two runs started together raced each other on the same files.
    was = "corpus.build_and_write( )"
    try:
        corpus.check_matches_generator()
    except corpus.CorpusMismatch as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0
'''

STILL_REWRITES_THE_CORPUS = '''\
from lab import corpus


def run():
    corpus.build_and_write()
    return 0
'''


def test_the_old_call_named_only_in_a_comment_and_a_docstring_is_not_the_race(rm):
    """The plain text search the first version used would have said "absent" here."""
    assert "build_and_write(" in FIXED_BUT_MENTIONS_THE_OLD_CALL
    check = rm.race_fix_in_source(FIXED_BUT_MENTIONS_THE_OLD_CALL)
    assert check.fixed is True and check.problem == ""


def test_a_real_build_and_write_call_is_the_race(rm):
    check = rm.race_fix_in_source(STILL_REWRITES_THE_CORPUS)
    assert check.fixed is False and "build_and_write(" in check.why and check.problem == ""


@pytest.mark.parametrize("source, fixed", [
    ("corpus.check_matches_generator()\n", True),
    ("check_matches_generator()\n", True),                                      # imported by name
    ("lab.corpus.check_matches_generator(seed=7)\n", True),
    ("def f():\n    corpus.check_matches_generator()\n", True),                  # inside a function
    ("try:\n    corpus.check_matches_generator()\nexcept Exception:\n    pass\n", True),
    ("corpus.check_matches_generator()\ncorpus.build_and_write()\n", False),    # both: the rewrite is still there
    ("corpus.check_matches_generator()\nbuild_and_write()\n", False),           # imported by name
    ("def f():\n    return lab.corpus.build_and_write()\n", False),
    ("x = 1\n", False),                                                          # neither: no read-only check either
    ("# corpus.check_matches_generator()\nx = 1\n", False),                      # a comment is not a call
    ('"""corpus.check_matches_generator()"""\nx = 1\n', False),                  # nor is a docstring
    ("x = 'corpus.check_matches_generator()'\n", False),                         # nor a string
    ("f = corpus.check_matches_generator\n", False),                             # a reference is not a call
    ("def check_matches_generator():\n    pass\n", False),                       # a definition is not a call
])
def test_the_race_fix_is_judged_by_calls_only(rm, source, fixed):
    check = rm.race_fix_in_source(source)
    assert check.fixed is fixed and check.why


def test_the_source_may_be_bytes_with_a_bom_or_a_coding_line(rm):
    assert rm.race_fix_in_source(b"\xef\xbb\xbfcorpus.check_matches_generator()\r\n").fixed is True
    latin = b"# -*- coding: latin-1 -*-\ncorpus.check_matches_generator()  # caf\xe9\n"
    assert rm.race_fix_in_source(latin).fixed is True


@pytest.mark.parametrize("source", [
    "def broken(:\n",
    "x = (\n",
    "corpus.check_matches_generator()\n\x00\n",
    b"x = '\xff'\ncorpus.check_matches_generator()\n",    # not UTF-8 and no coding line
])
def test_a_file_that_does_not_parse_counts_as_no_fix_and_says_why(rm, source):
    check = rm.race_fix_in_source(source)
    assert check.fixed is False
    assert check.problem.startswith("cannot parse scripts/run_governance.py (")
    assert check.problem in check.why


def test_race_fix_check_reads_the_file(rm, tmp_path):
    assert rm.race_fix_check(make_repo(tmp_path / "a", race_fixed=True)).fixed is True
    assert rm.race_fix_check(make_repo(tmp_path / "b", race_fixed=False)).fixed is False
    assert rm.corpus_race_fixed(make_repo(tmp_path / "c", race_fixed=True)) is True      # the plain bool form
    assert rm.corpus_race_fixed(make_repo(tmp_path / "d", race_fixed=False)) is False
    nowhere = rm.race_fix_check(tmp_path / "nowhere")                                     # cannot read: assume the race
    assert nowhere.fixed is False and nowhere.problem.startswith("cannot read scripts/run_governance.py (")
    assert str(tmp_path) not in nowhere.problem, "the plan must not carry absolute paths"


def test_the_stand_in_run_governance_names_the_old_call_without_calling_it(rm, tmp_path):
    """The fake repo is itself a regression case: a def and a docstring name build_and_write, only a call counts."""
    text = (make_repo(tmp_path, race_fixed=True) / "scripts" / "run_governance.py").read_text(encoding="utf-8")
    assert "def build_and_write(" in text and "build_and_write()" in text
    assert rm.race_fix_in_source(text).fixed is True


def test_the_repos_own_run_governance_is_judged_by_its_calls(rm):
    """Absent today, present once lane A's change is in: either way the verdict follows the calls."""
    source = (ROOT / "scripts" / "run_governance.py").read_bytes()
    assert rm.corpus_race_fixed(ROOT) is rm.race_fix_in_source(source).fixed


def test_lane_a_run_governance_has_the_race_fix(rm):
    """Lane A's real file mentions build_and_write() in a comment (line 548) and calls check_matches_generator()."""
    import subprocess
    try:
        shown = subprocess.run(["git", "show", "lane/A:scripts/run_governance.py"], cwd=ROOT,
                               capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available")
    if shown.returncode != 0:
        pytest.skip("the ref lane/A is not in this repository")
    check = rm.race_fix_in_source(shown.stdout)
    assert check.fixed is True, check.why


def test_main_warns_when_run_governance_cannot_be_parsed(rm, tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / "scripts" / "run_governance.py").write_text("def broken(:\n", encoding="utf-8")
    assert rm.main(["--dry-run", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    captured = capsys.readouterr()
    assert "warning: cannot parse scripts/run_governance.py (" in captured.err
    assert "forced to 1" in captured.out and "cannot parse" in captured.out    # the note carries the reason too
    # one model, or --parallel 1: nothing is forced, but the problem is still printed
    assert rm.main(["--dry-run", "--parallel", "1"], root=repo, stamp=STAMP) == 0
    captured = capsys.readouterr()
    assert "cannot parse" in captured.err and "forced" not in captured.out


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


# --------------------------------------------------------------------------- #
# Tool-support check: pure classification, then the run folders it reads.
# --------------------------------------------------------------------------- #
CALL = {"name": "read_doc", "args": {"name": "vpn-setup.md"}}


def _trace(**kw) -> dict:
    base = {"test_id": "ATK-0001", "rep": 1, "tool_calls": [], "final_text": "ok", "error": None}
    base.update(kw)
    return base


def test_structured_tool_call_means_yes(rm):
    r = rm.classify_tool_support([_trace(tool_calls=[CALL]), _trace()])
    assert r.tools == "yes"


def test_blocked_calls_still_count_as_structured_calls(rm):
    blocked = {"name": "create_ticket", "args": {}, "blocked": True}
    assert rm.classify_tool_support([_trace(tool_calls=[blocked])]).tools == "yes"


def test_tool_call_text_means_no_even_if_another_run_made_a_real_call(rm):
    text = _trace(final_text='Sure. <tool_call>{"name": "read_doc", "arguments": {}}</tool_call>')
    r = rm.classify_tool_support([_trace(tool_calls=[CALL]), text])
    assert r.tools == "no" and "<tool_call>" in r.reason


def test_every_run_erroring_means_no(rm):
    r = rm.classify_tool_support([_trace(error="ResponseError('does not support tools')"), _trace(error="boom")])
    assert r.tools == "no" and r.reason == "every run errored"
    assert "does not support tools" in r.detail   # the first error, for whoever reads the status file


def test_some_errors_do_not_disqualify_a_model_that_made_a_call(rm):
    assert rm.classify_tool_support([_trace(error="timeout"), _trace(tool_calls=[CALL])]).tools == "yes"


def test_no_structured_call_means_no(rm):
    r = rm.classify_tool_support([_trace(), _trace(final_text="I can't help with that")])
    assert r.tools == "no" and "no structured tool call" in r.reason


@pytest.mark.parametrize("calls_value", [[{}], ["read_doc"], [None], None, "read_doc", [{"args": {}}]])
def test_malformed_tool_calls_do_not_count(rm, calls_value):
    assert rm.classify_tool_support([_trace(tool_calls=calls_value)]).tools == "no"


def test_no_traces_means_no(rm):
    r = rm.classify_tool_support([])
    assert r.tools == "no" and "no traces" in r.reason


def _mk_run(runs: Path, name: str, model: str, traces: list[dict], *, manifest: dict | None = None) -> Path:
    d = runs / name
    (d / "traces").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(manifest if manifest is not None else {"model": model, "locked": True}))
    for i, tr in enumerate(traces):
        (d / "traces" / f"T{i}__rep1.json").write_text(json.dumps(tr))
    return d


def test_only_new_run_folders_of_the_model_are_read(rm, tmp_path):
    runs = tmp_path / "runs"
    _mk_run(runs, "20261018T050000Z_alpha-1b_RC-001", "alpha:1b", [_trace(tool_calls=[CALL])])   # an earlier run
    before = rm.snapshot_runs(runs)
    mine = _mk_run(runs, "20261018T060000Z_alpha-1b_RC-001", "alpha:1b", [_trace()])
    _mk_run(runs, "20261018T060001Z_beta-2b_RC-001", "beta:2b", [_trace(tool_calls=[CALL])])    # another model, same time
    _mk_run(runs, "20261018T060002Z_alpha-1b-q4_RC-001", "alpha:1b-q4", [_trace(tool_calls=[CALL])])  # similar slug
    (runs / "stray-file.txt").write_text("not a run folder")
    assert [p.name for p in rm.runs_for_model(runs, "alpha:1b", before)] == [mine.name]


def test_run_folder_helpers_survive_missing_and_damaged_files(rm, tmp_path):
    runs = tmp_path / "runs"
    assert rm.snapshot_runs(runs) == set()                 # no results/runs yet
    assert rm.runs_for_model(runs, "alpha:1b", set()) == []
    _mk_run(runs, "a", "alpha:1b", [_trace(tool_calls=[CALL])])
    (runs / "a" / "traces" / "broken__rep1.json").write_text("{not json")
    d = runs / "b"
    d.mkdir()
    (d / "manifest.json").write_text("{not json")
    traces, unreadable = rm.read_traces([runs / "a", d])
    assert len(traces) == 1 and unreadable == 1
    assert [p.name for p in rm.runs_for_model(runs, "alpha:1b", set())] == ["a"]   # "b" has no readable manifest


def test_three_models_in_one_runs_folder_are_classified_each_from_its_own_folders(rm, tmp_path):
    """Fabricated run folders on disk, read the way the batch reads them."""
    runs = tmp_path / "runs"
    before = rm.snapshot_runs(runs)
    for cid in ("RC-001", "RC-002", "RC-003"):
        _mk_run(runs, f"20261018T060000Z_alpha-1b_{cid}", "alpha:1b", [_trace(tool_calls=[CALL]), _trace()])
        _mk_run(runs, f"20261018T060000Z_beta-2b_{cid}", "beta:2b",
                [_trace(tool_calls=[CALL]), _trace(final_text="<tool_call>{}</tool_call>")])
        _mk_run(runs, f"20261018T060000Z_gamma-3b_{cid}", "gamma:3b", [_trace(error="boom"), _trace(error="boom")])
    got = {tag: rm.tool_support_of(rm.runs_for_model(runs, tag, before)) for tag in ("alpha:1b", "beta:2b", "gamma:3b")}
    assert got["alpha:1b"].tools == "yes"
    assert got["beta:2b"].tools == "no" and "<tool_call>" in got["beta:2b"].reason
    assert got["gamma:3b"].tools == "no" and got["gamma:3b"].reason == "every run errored"
    assert rm.tool_support_of([]).tools == "no"     # a smoke run that left no folder is not a pass


def test_unreadable_trace_files_are_mentioned(rm, tmp_path):
    runs = tmp_path / "runs"
    d = _mk_run(runs, "a", "alpha:1b", [_trace(tool_calls=[CALL])])
    (d / "traces" / "broken__rep1.json").write_text("{nope")
    check = rm.tool_support_of([d])
    assert check.tools == "yes" and "1 trace files unreadable" in check.detail


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_python_override_becomes_absolute_without_following_a_venv_symlink(rm, tmp_path, monkeypatch):
    real = tmp_path / "base-python"
    real.write_text("")
    link = tmp_path / "venv-python"
    link.symlink_to(real)
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "r")
    opts = rm.parse_args(["--python", "venv-python", "--scripted"])
    got = rm._check_setup(opts, rm.Layout(root=repo, results=repo / "results"))
    assert got == str(link)   # not str(real): resolving the link would leave the venv


def test_a_missing_ollama_is_reported_as_such(rm, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, ("alpha:1b",))
    monkeypatch.setattr(rm, "OLLAMA_CMD", [str(tmp_path / "no-such-ollama")])
    assert rm.main(["--pull"], root=repo, stamp=STAMP) == 1
    assert events(repo) == ["queued", "pull_start", "pull_done", "skipped"]
    pull_done, skipped = status_lines(repo)[2], status_lines(repo)[3]
    assert pull_done["exit_code"] is None and "could not start" in pull_done["error"]
    assert "could not run" in skipped["reason"]


# --------------------------------------------------------------------------- #
# Running a batch, SCRIPTED. The repo's own run_governance.py, output in a tmp folder.
# --------------------------------------------------------------------------- #
def test_scripted_batch_with_the_real_script_leaves_the_corpus_alone(rm, tmp_path, capsys):
    if sys.platform == "win32" and not rm.corpus_race_fixed(ROOT):
        pytest.skip("v1 run_governance.py rewrites corpus/*.jsonl with Windows line ends (the corpus race itself)")
    corpus_files = [ROOT / "corpus" / "dev.jsonl", ROOT / "corpus" / "test.jsonl"]
    before = [p.read_bytes() for p in corpus_files]
    results = tmp_path / "results"
    assert rm.main(["--scripted", "--smoke-only"], root=ROOT, results_root=results, stamp=STAMP) == 0
    assert [p.read_bytes() for p in corpus_files] == before, "a batch must not change corpus/*.jsonl"
    out = results / "governance" / f"{STAMP}_qwen2.5-7b-instruct_smoke"
    assert (out / "gate_RC-001.txt").is_file() and (out / "audit_log.jsonl").is_file()
    assert not (ROOT / "results" / "models" / STAMP).exists(), "the repo's own results/ folder was written"
    lines = [json.loads(ln) for ln in (results / "models" / STAMP / "status.jsonl").read_text().splitlines()]
    done = next(e for e in lines if e["event"] == "smoke_done")
    assert (done["exit_code"], done["tools"], done["reason"], done["text"]) == (
        0, "not checked", "scripted", "not checked (scripted)")
    assert "qwen2.5-7b-instruct" in capsys.readouterr().out


def test_scripted_status_sequence_and_files(rm, tmp_path):
    repo = make_repo(tmp_path)
    assert rm.main(["--scripted", "--parallel", "1"], root=repo, stamp=STAMP) == 0
    full = ["queued", "preflight_skipped", "smoke_start", "smoke_done", "full_start", "full_done"]
    assert events(repo, "alpha-1b") == full and events(repo, "beta-2b") == full
    lines = status_lines(repo)
    assert [e["event"] for e in lines[:2]] == ["queued", "queued"]          # everything is queued first
    assert [e["position"] for e in lines[:2]] == [1, 2]
    from datetime import datetime
    for e in lines:
        assert {"ts", "event", "slug", "tag"} <= set(e)
        datetime.fromisoformat(e["ts"])
    done = {e["event"]: e for e in lines if e["slug"] == "alpha-1b"}
    assert done["smoke_start"]["out"] == f"results/governance/{STAMP}_alpha-1b_smoke"
    assert done["full_done"]["out"] == f"results/governance/{STAMP}_alpha-1b"
    assert done["full_done"]["exit_code"] == 0
    batch = repo / "results" / "models" / STAMP
    plan = json.loads((batch / "plan.json").read_text(encoding="utf-8"))
    assert plan["stamp"] == STAMP and plan["mode"] == "scripted" and "created_at" in plan
    assert [m["slug"] for m in plan["models"]] == ["alpha-1b", "beta-2b"]
    log = (batch / "alpha-1b.smoke.log").read_text(encoding="utf-8")
    assert log.startswith("$ python scripts/run_governance.py --sample 1 --reps 1 --out ")
    assert (repo / "results" / "governance" / f"{STAMP}_alpha-1b" / "stub_ran.txt").read_text() == "alpha:1b"
    assert not (repo / "results" / "runs").exists(), "scripted runs make no run folders"


def test_children_get_the_model_tag_and_utf8_unbuffered_environment(rm, tmp_path):
    repo = make_repo(tmp_path)
    assert rm.main(["--scripted", "--parallel", "1"], root=repo, stamp=STAMP) == 0
    starts = [c for c in calls(repo) if c["step"] == "start"]
    assert {c["tag"] for c in starts} == {"alpha:1b", "beta:2b"}
    assert all(c["env_unbuffered"] == "1" and c["env_utf8"] == "1" for c in starts)
    full = [c for c in starts if not c["out"].endswith("_smoke")]
    assert all((c["sample"], c["reps"]) == (26, 3) for c in full)
    assert all((c["sample"], c["reps"]) == (1, 1) for c in starts if c["out"].endswith("_smoke"))


def test_a_batch_folder_is_never_reused(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, ("alpha:1b",))
    assert rm.main(["--scripted", "--smoke-only"], root=repo, stamp=STAMP) == 0
    first = (repo / "results" / "models" / STAMP / "status.jsonl").read_bytes()
    assert rm.main(["--scripted", "--smoke-only"], root=repo, stamp=STAMP) == 2
    assert "already exists" in capsys.readouterr().err
    assert (repo / "results" / "models" / STAMP / "status.jsonl").read_bytes() == first


def _intervals(repo: Path) -> dict:
    """{tag: [(start, end), ...]} for the stand-in run_governance, in start order."""
    started, out = {}, {}
    for c in calls(repo):
        if c["step"] == "start" and "out" in c:
            started[c["pid"]] = c["t"]
        elif c["step"] in ("end", "interrupted"):
            out.setdefault(c["tag"], []).append((started[c["pid"]], c["t"]))
    return {tag: sorted(v) for tag, v in out.items()}


def _overlap(a, b) -> bool:
    return max(a[0], b[0]) < min(a[1], b[1])


def _max_concurrency(runs) -> int:
    """The most runs alive at the same moment (sweep over start/end times)."""
    points = sorted([(s, 1) for s, _ in runs] + [(e, -1) for _, e in runs], key=lambda p: (p[0], p[1]))
    alive = best = 0
    for _, step in points:
        alive += step
        best = max(best, alive)
    return best


def test_parallel_two_runs_two_models_at_once_and_both_finish(rm, tmp_path):
    repo = make_repo(tmp_path, behaviour={"sleep": 0.4})
    assert rm.main(["--scripted", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    iv = _intervals(repo)
    assert len(iv["alpha:1b"]) == 2 and len(iv["beta:2b"]) == 2          # smoke and full of both
    assert _overlap(iv["alpha:1b"][0], iv["beta:2b"][0]), "the two smoke runs should overlap"
    assert events(repo, "alpha-1b")[-1] == "full_done" and events(repo, "beta-2b")[-1] == "full_done"
    assert status_lines(repo)[0]["event"] == "queued"


def test_parallel_one_runs_one_model_at_a_time(rm, tmp_path):
    repo = make_repo(tmp_path, behaviour={"sleep": 0.1})
    assert rm.main(["--scripted", "--parallel", "1"], root=repo, stamp=STAMP) == 0
    runs = [r for v in _intervals(repo).values() for r in v]
    assert len(runs) == 4 and _max_concurrency(runs) == 1


def test_the_same_model_never_runs_twice_at_once(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b", "beta:2b", "gamma:3b"), behaviour={"sleep": 0.2})
    assert rm.main(["--scripted", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    iv = _intervals(repo)
    assert set(iv) == {"alpha:1b", "beta:2b", "gamma:3b"}
    for tag, runs in iv.items():
        assert len(runs) == 2, tag
        assert _max_concurrency(runs) == 1, f"{tag}: smoke and full overlapped"
    assert _max_concurrency([r for v in iv.values() for r in v]) == 2     # never more than --parallel


def test_forced_sequential_when_the_race_fix_is_absent(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, race_fixed=False, behaviour={"sleep": 0.1})
    assert rm.main(["--scripted", "--parallel", "2"], root=repo, stamp=STAMP) == 0
    assert "forced to 1" in capsys.readouterr().out
    runs = [r for v in _intervals(repo).values() for r in v]
    assert len(runs) == 4 and _max_concurrency(runs) == 1
    plan = json.loads((repo / "results" / "models" / STAMP / "plan.json").read_text())
    assert (plan["parallel"], plan["parallel_requested"]) == (1, 2) and plan["parallel_note"]


# --------------------------------------------------------------------------- #
# Running a batch, LIVE-shaped (stand-in scripts write run folders; no model anywhere).
# --------------------------------------------------------------------------- #
def _outs(repo: Path) -> list[str]:
    return [c["out"] for c in calls(repo) if c["step"] == "start" and "out" in c]


def test_live_model_with_tool_support_gets_the_full_run(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",))
    assert rm.main([], root=repo, stamp=STAMP) == 0
    assert events(repo) == ["queued", "preflight_start", "preflight_ok", "smoke_start", "smoke_done",
                            "full_start", "full_done"]
    ev = {e["event"]: e for e in status_lines(repo)}
    assert (ev["smoke_done"]["tools"], ev["smoke_done"]["exit_code"]) == ("yes", 0)
    assert len(ev["smoke_done"]["run_dirs"]) == 3 and ev["smoke_done"]["reason"]
    assert ev["preflight_ok"]["exit_code"] == 0
    assert ev["full_done"]["exit_code"] == 0 and len(ev["full_done"]["run_dirs"]) == 3
    assert not set(ev["smoke_done"]["run_dirs"]) & set(ev["full_done"]["run_dirs"])
    assert _outs(repo) == [f"results/governance/{STAMP}_alpha-1b_smoke", f"results/governance/{STAMP}_alpha-1b"]
    starts = [c for c in calls(repo) if c["step"] == "start" and "out" in c]
    assert all(c["live"] for c in starts)


@pytest.mark.parametrize("kind, reason_has", [
    ("text", "<tool_call>"),
    ("errors", "every run errored"),
    ("plain", "no structured tool call"),
    ("none", "no traces"),
])
def test_live_model_without_tool_support_is_skipped_with_the_reason(rm, tmp_path, kind, reason_has):
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"traces": {"*": kind}})
    assert rm.main([], root=repo, stamp=STAMP) == 0           # a skipped model is a result, not a failure
    assert events(repo)[-3:] == ["smoke_start", "smoke_done", "skipped"]
    done = next(e for e in status_lines(repo) if e["event"] == "smoke_done")
    assert done["tools"] == "no" and reason_has in done["reason"]
    assert done["text"].startswith("no (") and reason_has in done["text"]
    skipped = status_lines(repo)[-1]
    assert "tool support" in skipped["reason"] and reason_has in skipped["reason"]
    assert _outs(repo) == [f"results/governance/{STAMP}_alpha-1b_smoke"], "the full run must not start"


def test_a_batch_mixes_models_with_and_without_tool_support(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, ("alpha:1b", "beta:2b"), behaviour={"traces": {"alpha:1b": "tools", "beta:2b": "text"}})
    assert rm.main(["--parallel", "1"], root=repo, stamp=STAMP) == 0
    assert events(repo, "alpha-1b")[-1] == "full_done" and events(repo, "beta-2b")[-1] == "skipped"
    out = capsys.readouterr().out
    assert "alpha-1b" in out and "beta-2b" in out and "no tool support" in out.lower()


def test_preflight_failure_skips_only_that_model(rm, tmp_path):
    repo = make_repo(tmp_path, behaviour={"preflight_fail": ["alpha:1b"]})
    assert rm.main(["--parallel", "1"], root=repo, stamp=STAMP) == 1     # a failed step is a failed batch
    assert events(repo, "alpha-1b") == ["queued", "preflight_start", "preflight_fail", "skipped"]
    assert events(repo, "beta-2b")[-1] == "full_done"
    fail = next(e for e in status_lines(repo) if e["event"] == "preflight_fail")
    assert fail["exit_code"] == 1 and "PREFLIGHT FAILED" in fail["detail"]
    skipped = next(e for e in status_lines(repo) if e["event"] == "skipped" and e["slug"] == "alpha-1b")
    assert "preflight" in skipped["reason"]
    assert all("alpha" not in o for o in _outs(repo)), "no smoke or full run for the model that failed preflight"


def test_a_failing_smoke_run_stops_that_model(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"exit_smoke": {"alpha:1b": 3}})
    assert rm.main([], root=repo, stamp=STAMP) == 1
    assert events(repo)[-3:] == ["smoke_start", "smoke_done", "skipped"]
    ev = {e["event"]: e for e in status_lines(repo)}
    assert ev["smoke_done"]["exit_code"] == 3
    assert "smoke" in ev["skipped"]["reason"] and "3" in ev["skipped"]["reason"]


def test_a_failing_full_run_is_reported(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"exit_full": {"alpha:1b": 4}})
    assert rm.main([], root=repo, stamp=STAMP) == 1
    done = next(e for e in status_lines(repo) if e["event"] == "full_done")
    assert done["exit_code"] == 4


def test_smoke_only_stops_after_the_check(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",))
    assert rm.main(["--smoke-only"], root=repo, stamp=STAMP) == 0
    assert events(repo) == ["queued", "preflight_start", "preflight_ok", "smoke_start", "smoke_done", "skipped"]
    assert "smoke-only" in status_lines(repo)[-1]["reason"]
    assert _outs(repo) == [f"results/governance/{STAMP}_alpha-1b_smoke"]


def test_sample_and_reps_reach_the_full_run(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",))
    assert rm.main(["--sample", "17", "--reps", "2"], root=repo, stamp=STAMP) == 0
    runs = [c for c in calls(repo) if c["step"] == "start" and "out" in c]
    assert [(c["sample"], c["reps"]) for c in runs] == [(1, 1), (17, 2)]


def test_pull_runs_before_preflight(rm, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, ("alpha:1b",))
    fake = tmp_path / "fake_ollama.py"
    fake.write_text("import sys, pathlib\npathlib.Path(sys.argv[0]).with_name('pulled.txt').write_text(' '.join(sys.argv[1:]))\n")
    monkeypatch.setattr(rm, "OLLAMA_CMD", [sys.executable, str(fake)])
    assert rm.main(["--pull", "--smoke-only"], root=repo, stamp=STAMP) == 0
    assert (tmp_path / "pulled.txt").read_text() == "pull alpha:1b"
    assert events(repo)[:5] == ["queued", "pull_start", "pull_done", "preflight_start", "preflight_ok"]


def test_a_failed_pull_skips_the_model_and_says_how_to_check_the_tag(rm, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, ("alpha:1b",))
    fake = tmp_path / "failing_ollama.py"
    fake.write_text("import sys\nprint('pull model manifest: file does not exist')\nsys.exit(1)\n")
    monkeypatch.setattr(rm, "OLLAMA_CMD", [sys.executable, str(fake)])
    assert rm.main(["--pull"], root=repo, stamp=STAMP) == 1
    assert events(repo) == ["queued", "pull_start", "pull_done", "skipped"]
    skipped = status_lines(repo)[-1]
    assert "ollama pull" in skipped["reason"] and "alpha:1b" in skipped["reason"]
    assert calls(repo) == [], "nothing runs after a failed pull"


@pytest.mark.skipif(sys.platform == "win32", reason="relies on POSIX file permissions")
def test_a_python_that_cannot_start_is_reported_not_raised(rm, tmp_path):
    repo = make_repo(tmp_path, ("alpha:1b",))
    not_executable = tmp_path / "python-ish"
    not_executable.write_text("#!/bin/sh\n")
    not_executable.chmod(0o644)
    assert rm.main(["--python", str(not_executable)], root=repo, stamp=STAMP) == 1
    assert events(repo) == ["queued", "preflight_start", "preflight_fail", "skipped"]
    assert "could not start" in status_lines(repo)[2]["detail"]


def test_summary_is_printed_without_numbers_from_nowhere(rm, tmp_path, capsys):
    repo = make_repo(tmp_path, ("alpha:1b", "beta:2b"), behaviour={"traces": {"beta:2b": "errors"}})
    assert rm.main(["--parallel", "1"], root=repo, stamp=STAMP) == 0
    out = capsys.readouterr().out
    assert "Summary" in out
    assert f"results/governance/{STAMP}_alpha-1b" in out
    assert "every run errored" in out


# --------------------------------------------------------------------------- #
# Ctrl-C. A real run_models.py process in the fake repo, real signals (POSIX).
# The Windows path (CTRL_BREAK_EVENT to the child's process group) is not verified.
# --------------------------------------------------------------------------- #
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals; Windows is not verified")


def _start_cli(repo: Path, *argv: str):
    import shutil
    import subprocess
    shutil.copy(ROOT / "scripts" / "run_models.py", repo / "scripts" / "run_models.py")
    log = open(repo / "cli.out", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(repo / "scripts" / "run_models.py"), *argv],
                            cwd=repo, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def _wait_for(predicate, what: str, timeout: float = 20.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@posix_only
@pytest.mark.parametrize("sig_name", ["SIGINT", "SIGTERM"])
def test_stop_signal_reaches_each_child_once_and_skips_queued_models(rm, tmp_path, sig_name):
    import signal
    repo = make_repo(tmp_path, ("alpha:1b", "beta:2b", "gamma:3b"), behaviour={"sleep": 60})
    proc, log = _start_cli(repo, "--scripted", "--parallel", "2")
    try:
        _wait_for(lambda: len([c for c in calls(repo) if c["step"] == "start"]) == 2, "two children to start")
        proc.send_signal(getattr(signal, sig_name))
        assert proc.wait(timeout=30) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
    cs = calls(repo)
    started = {c["tag"] for c in cs if c["step"] == "start"}
    assert started == {"alpha:1b", "beta:2b"}, "the queued model must never start"
    sigints = [c for c in cs if c["step"] == "sigint"]
    assert sorted(c["tag"] for c in sigints) == ["alpha:1b", "beta:2b"] and all(c["n"] == 1 for c in sigints)
    assert {c["tag"] for c in cs if c["step"] == "interrupted"} == {"alpha:1b", "beta:2b"}   # they finished their own cleanup
    for slug in ("alpha-1b", "beta-2b"):
        assert events(repo, slug) == ["queued", "preflight_skipped", "smoke_start", "smoke_done", "skipped"]
    done = next(e for e in status_lines(repo) if e["event"] == "smoke_done")
    assert done["exit_code"] == 130 and done["tools"] == "not checked" and done["reason"] == "interrupted"
    assert events(repo, "gamma-3b") == ["queued", "skipped"]
    assert "interrupted" in status_lines(repo)[-1]["reason"]
    assert "Interrupted" in (repo / "cli.out").read_text()


@posix_only
def test_a_second_ctrl_c_ends_a_child_that_ignores_the_first(rm, tmp_path):
    import signal
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"sleep": 60, "stubborn": True})
    proc, log = _start_cli(repo, "--scripted")
    try:
        _wait_for(lambda: any(c["step"] == "start" for c in calls(repo)), "the child to start")
        proc.send_signal(signal.SIGINT)
        _wait_for(lambda: any(c["step"] == "sigint" for c in calls(repo)), "the child to get the first interrupt")
        assert proc.poll() is None, "the batch waits for the child after the first Ctrl-C"
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=30) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
    assert len([c for c in calls(repo) if c["step"] == "sigint"]) == 1       # the second one ended it, not a second interrupt
    done = next(e for e in status_lines(repo) if e["event"] == "smoke_done")
    assert done["exit_code"] == 143 and done["reason"] == "interrupted"      # 128 + SIGTERM


def _batch(rm, repo: Path, *argv: str):
    """A prepared Batch (no CLI process), to drive the stop logic from a helper thread."""
    opts = rm.parse_args(list(argv))
    layout = rm.Layout(root=repo, results=repo / "results")
    plan = rm.build_plan(opts, layout, rm.read_models(repo / "config" / "models.txt"), STAMP, race_fixed=True)
    batch = rm.Batch(plan, layout, sys.executable, echo=False)
    batch.prepare()
    return batch


def _stop_when_started(batch, repo: Path):
    import threading

    def go():
        _wait_for(lambda: any(c["step"] == "start" for c in calls(repo)), "the child to start")
        batch._stop()

    thread = threading.Thread(target=go)
    thread.start()
    return thread


@posix_only
def test_a_child_that_ignores_the_interrupt_is_ended_after_the_grace_time(rm, tmp_path, monkeypatch):
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"sleep": 60, "stubborn": True})
    monkeypatch.setattr(rm, "GRACE_S", 0.5)
    batch = _batch(rm, repo, "--scripted")
    thread = _stop_when_started(batch, repo)
    assert batch.run() == 130
    thread.join()
    done = next(e for e in status_lines(repo) if e["event"] == "smoke_done")
    assert done["exit_code"] == 143 and done["reason"] == "interrupted"


@posix_only
def test_the_report_lists_run_folders_that_did_not_lock(rm, tmp_path, capsys):
    """If a child ends without locking its folders (the unverified Windows path), say so; delete nothing."""
    repo = make_repo(tmp_path, ("alpha:1b",), behaviour={"sleep": 60, "unlocked_on_interrupt": True})
    batch = _batch(rm, repo)    # LIVE-shaped: the stand-in writes run folders when it is interrupted
    thread = _stop_when_started(batch, repo)
    assert batch.run() == 130
    thread.join()
    out = capsys.readouterr().out
    assert "NOT locked" in out and out.count("results/runs/") == 3
    folders = sorted((repo / "results" / "runs").iterdir())
    assert len(folders) == 3 and all(f.is_dir() for f in folders), "nothing is deleted"


def test_say_survives_a_console_that_cannot_show_the_text(rm, monkeypatch):
    import io
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="ascii", errors="strict")   # like a cp1252 console meeting a dash
    monkeypatch.setattr(sys, "stdout", console)
    rm.say("café — done")
    console.flush()
    assert raw.getvalue() == b"caf? ? done\n"
