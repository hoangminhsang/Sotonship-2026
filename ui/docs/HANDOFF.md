# Demo UI — handoff (saved Sun 18 Oct, 05:0x)

## State
- Branch `ui/demo` in the lab repo (`hoangminhsang/Sotonship-2026`), based on `origin/main` `d825341`. Only `ui/` is added; no backend file is changed.
- The build stopped partway: the session usage limit stopped all four build agents. Their files were written, but **integration, screenshots, the README and QA did not run**.
- Checked: `export.py` and `app.js` parse. `python3 ui/export.py --quiet` exits 0: Results REAL · Policy REAL · Gate PENDING · Decisions PENDING · Assurance PENDING · Controls REAL.
- **Not checked:** whether the page renders correctly in a browser, the layout at 1920×1080 and 1366×768, keyboard use, offline use, and the provenance check.

## Files
- `index.html`, `app.css`, `app.js`: the page (screens 1–5).
- `fonts.css`: the five embedded font faces, copied from the pitch carousel (SIL OFL 1.1). Do not open it in an editor; it is about 146 KB of base64.
- `export.py`, `sources.json`: the data adapter. It reads the backend files and writes `data/demo.js`.
- `fixtures/*.sample.json`: SAMPLE gate, audit and assurance data. It is used only with `--allow-sample`.
- `data/demo.js`: exported **without** samples.
- `docs/BUILD_CONTRACT.md`: the full spec. It covers the rules, data facts, the `window.DEMO` schema, the DOM vocabulary and QA. Read it first.
- `docs/PLAN.md`: the approved plan.

## Next steps
1. `python3 ui/export.py --allow-sample`, then open `ui/index.html`, then fix what is broken.
2. Take screenshots at 1920×1080 and 1366×768, and do a keyboard-only run of the demo (see `BUILD_CONTRACT.md` §11).
3. Write `ui/README.md` (`BUILD_CONTRACT.md` §11 and the plan list its sections).
4. Re-export without `--allow-sample`, then commit, push and open the PR.
5. After the review PR is merged: map the real governance outputs (`docs/DATA_CONTRACT.md`) in `export.py` and `sources.json` only.
