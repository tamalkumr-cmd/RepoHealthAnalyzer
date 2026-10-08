# RepoHealth Analyzer

Catches code-quality degradation by comparing each file against **its own history**, and blocks it at `git commit` before it enters the repository.

Runs entirely on your machine. No server, no account, no source code leaving local storage.

---

## Why

Code rots one reasonable decision at a time. Someone adds an `if` for an edge case, then someone else adds another, and six months later a function is 200 lines that nobody understands — even though every individual commit was reviewed and sensible on the day.

Existing tools miss this because they examine the *state*, not the *change*:

- **A reviewer** sees eight changed lines, not that the file's complexity has tripled since March.
- **A linter** flags the same inherited problems on every run, until people stop reading its output.
- **CI** reports only after the commit is already in history and the developer has moved on.

And some problems don't live in a single file at all. A circular import between three modules is invisible in any one of them, because none of them is wrong by itself.

RepoHealth compares each file to its own rolling baseline — so it stays silent about code that has always been complex, and speaks only when something is actively getting worse.

---

## Install

Requires **Python 3.10+** and **Git** on `PATH`.

```bash
git clone https://github.com/littlestuart07/repohealth-analyzer.git
cd repohealth-analyzer

python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

pip install -e ".[dashboard]"
```

That installs a `repohealth` command available from any directory.

Drop `[dashboard]` if you only want the CLI and the commit hook — FastAPI and Uvicorn are optional, and nothing else depends on them.

<details>
<summary><b>Windows: if activation is blocked</b></summary>

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Run once per user. No administrator rights needed.
</details>

<details>
<summary><b>Without installing (running from source)</b></summary>

```bash
pip install GitPython radon
python -m repohealth.cli scan .
```

Every `repohealth X` below becomes `python -m repohealth.cli X`, and must be run from the project root.
</details>

---

## Use

```bash
repohealth scan .            # analyse and record a baseline
repohealth score . --effort  # health score + refactoring estimates
repohealth report .          # files ranked by risk
repohealth cycles .          # circular imports, as chains
repohealth serve .           # dashboard at http://127.0.0.1:8000
```

`scan` is the only command that writes. Everything else reads what it recorded.

Analyse any repository by passing its path:

```bash
repohealth scan ~/projects/some-other-repo
repohealth serve ~/projects/some-other-repo
```

### Turning on the commit gate

```bash
repohealth scan .     # establish baselines FIRST
repohealth init .     # then install the hook
```

Order matters — with no baselines, the gate has nothing to compare against and passes everything through.

From then on, a commit that spikes a file's complexity past the threshold is refused:

```
  COMMIT BLOCKED — RepoHealth Gatekeeper
  ────────────────────────────────────────────────────────

  ✖ pkg/billing.py
    complexity rose 140.0% above its baseline
    · complexity 24 vs baseline 10.0  (+140.0%, limit +20%)
    · line 4: compute_invoice() complexity 19 (limit 10)
    · line 4: compute_invoice() nested 6 deep (limit 4)

  ✖ new circular import
    · pkg/billing.py -> pkg/utils.py -> pkg/billing.py

  ────────────────────────────────────────────────────────
  Intentional? Commit with: git commit --no-verify
  Checked in 0.03s
```

**The override is deliberate.** Sometimes complexity rises because a file legitimately does more now. A gate that can't be overridden is one developers disable entirely, so `--no-verify` stays available — and after intentional work, re-run `repohealth scan .` to make the new code the baseline.

The hook lives in `.git/hooks/`, which Git doesn't version, so **each developer runs `repohealth init` on their own machine**. That's the standard trade-off for local hooks, and it's why CI remains the unskippable backstop.

```bash
repohealth init . --uninstall    # remove it
```

---

## Configuration

Create `config.json` in the repository being analysed (not in this project). Only the keys you override need to be present; everything else keeps its default.

```json
{
  "gatekeeper": {
    "complexity_spike_pct": 20.0,
    "baseline_window": 5,
    "min_baseline_cc": 5,
    "block_new_cycles": true
  },
  "thresholds": {
    "max_cc_per_function": 10,
    "max_nesting_depth": 4,
    "max_class_loc": 200
  },
  "exclude_paths": [".venv/", "node_modules/", "tests/fixtures/"]
}
```

| Setting | Default | What it does |
|---|---|---|
| `complexity_spike_pct` | 20.0 | Percentage rise above baseline that blocks a commit |
| `baseline_window` | 5 | Completed runs averaged into the baseline |
| `min_baseline_cc` | 5 | Below this, percentages are noise — 2→3 is a 50% "spike" |
| `block_new_cycles` | true | Block commits that introduce a circular import |
| `allow_unbaselined` | true | Let a brand-new file through; it has no history yet |

The dashboard's threshold panel writes to this same file.

---

## How it works

```
CLI ·  pre-commit hook  ·  dashboard        three entry points, one code path
                  ↓
         orchestration layer
                  ↓
  churn · complexity · dep graph · scoring   pure functions, no I/O
                  ↓
              SQLite                         baselines, metrics, runs
                  ↓
         local Git repository                read-only
```

**Two execution paths.** `scan` is the full path: ingest history, analyse every tracked file, build the graph. The hook is the fast path — staged files only, one indexed baseline lookup each, no graph rebuild above an 800-file guard. That's what keeps commits under the 1.5-second budget.

Some details worth knowing:

- **Churn is derived in SQL**, not stored as a counter, so re-scanning is incremental and the metric can never drift out of sync with the commits actually ingested.
- **Baselines are a rolling average** over the last *N* completed runs, so one unusual commit can't poison the threshold.
- **The gate judges staged content** via `git show :path`, not the working tree — if you keep editing after `git add`, the commit contains what was staged.
- **Only new cycles block.** Pre-existing ones are recorded and ignored, otherwise the first commit after install would be unblockable in any repo that already has one.
- **Parse failures don't abort a run.** Half-typed code is routine when a hook fires mid-edit; the file is recorded as unparsed and excluded from baselines.
- **Imports resolve by longest prefix**, so `from pkg.utils import add` correctly distinguishes a submodule from a function. Anything unresolved is stdlib or third-party and is dropped — the graph stays confined to code you can actually refactor.
- **`src/` layouts are handled.** The source root is found by walking up while each ancestor directory is a package, which is the directory Python itself would put on `sys.path`.

### Why `web/d3.min.js` is committed

The dashboard makes **zero external requests** — no CDN, no web fonts. Vendoring D3 is what makes "nothing leaves this machine" true of the dashboard as well as the analysis. It is deliberate, not an accidentally committed dependency.

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

78 tests covering the four analysis engines, persistence, the gatekeeper, hook installation, and scoring. They build real throwaway Git repositories rather than mocking, because the behaviour under test is precisely how the system reads Git.

Several encode bugs that were found and fixed, so they stay fixed:

- Files inside an import cycle reported blast radius 0, because the SCC optimisation counted components instead of files — silently hiding exactly the files the tool exists to flag.
- `from . import b` resolved to the package's `__init__.py`, losing the edge to `b.py` entirely.
- A healthy repository reported every file as high risk, because percentile banding alone puts ties in the top decile.

---

## Performance

Measured on Flask (83 files, 50 commits):

| Operation | Time |
|---|---|
| Full scan | 1.30s |
| Gate, 1 staged file in a 600-file repo | 0.18–0.27s |
| Gate, 900-file repo (cycle check skipped) | 0.03s |

Ingestion uses a single `git log --numstat` call. The obvious implementation — iterating commits and reading `commit.stats` — spawns one `git diff` subprocess per commit and took **25.5 seconds** on the same repository.

---

## Limitations

Stated plainly, because they're real:

- **Python only.** The AST work is language-specific.
- **A local hook is bypassable** (`--no-verify`) and must be installed per machine. CI is the unskippable layer; this is the fast first one.
- **Renames reset history.** A renamed file loses its baseline until it's scanned again, so the gate stops protecting it in the meantime.
- **Dynamic imports are invisible.** `importlib.import_module(name)` and conditional imports inside `if TYPE_CHECKING:` blocks never appear in the graph.
- **It can't tell "worse" from "does more now."** A file that gained real functionality will trip the gate. That's what the override is for.
- **`__init__.py` cycles are often intentional** in Python packages — a package re-exporting from submodules that import back. Flask reports 28 cycles for largely this reason.

---

## Project layout

```
repohealth/
├── cli.py              entry point: scan, report, cycles, graph, init, score, serve, check
├── config.py           defaults + deep-merge loader
├── ingest/git_log.py   commit history, churn, staged files
├── analysis/
│   ├── complexity.py   radon CC + AST visitor for nesting and class span
│   ├── depgraph.py     module index, longest-prefix import resolution
│   ├── cycles.py       iterative DFS, three-colour marking
│   ├── blast.py        Tarjan SCC condensation, risk ranking
│   └── scoring.py      0–100 health score, refactoring estimates
├── hooks/
│   ├── gate.py         staged-file comparison against baseline
│   └── installer.py    writes .git/hooks/pre-commit
├── db/                 schema.sql + data access
├── api/server.py       FastAPI, localhost only
└── web/index.html      dashboard
```

Built for Software Engineering Lab (BCSE301P), VIT Chennai.
