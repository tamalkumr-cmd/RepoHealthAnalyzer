"""End-to-end: ingestion, persistence, the gatekeeper, and scoring.

These build real Git repositories rather than mocking, because the behaviour
under test is precisely how the system reads Git.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repohealth.analysis.blast import blast_radius
from repohealth.analysis.cycles import find_cycles
from repohealth.analysis.depgraph import build_graph
from repohealth.analysis.scoring import compute_health, refactor_estimate
from repohealth.config import DEFAULTS, is_excluded, load_config
from repohealth.db.store import Store
from repohealth.hooks.gate import run_gate
from repohealth.hooks.installer import install, status, uninstall
from repohealth.ingest.git_log import (
    iter_commits,
    open_repo,
    python_files,
    staged_blob,
    staged_python_files,
)


# ---------------- config ----------------

def test_config_deep_merge_preserves_siblings(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"gatekeeper": {"complexity_spike_pct": 50}}), encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg["gatekeeper"]["complexity_spike_pct"] == 50
    assert cfg["gatekeeper"]["baseline_window"] == DEFAULTS["gatekeeper"]["baseline_window"]


def test_missing_config_falls_back_to_defaults(tmp_path):
    assert load_config(tmp_path)["gatekeeper"]["complexity_spike_pct"] == 20.0


def test_exclusion_matching():
    ex = [".venv/", "node_modules/", "build/"]
    assert is_excluded(".venv/lib/x.py", ex)
    assert is_excluded("project/node_modules/y.py", ex)
    assert not is_excluded("src/app.py", ex)


# ---------------- ingestion ----------------

def test_commits_and_churn_are_ingested(cycle_repo):
    repo = open_repo(cycle_repo)
    for i in range(3):
        cycle_repo.write("pkg/utils.py", f"# rev {i}\ndef add(a,b):\n    return a+b\n")
        cycle_repo.commit(f"edit {i}")

    commits = list(iter_commits(repo, limit=100, exclude=DEFAULTS["exclude_paths"]))
    assert len(commits) == 4

    with Store(cycle_repo / ".repohealth" / "t.db") as store:
        rid = store.upsert_repo(str(cycle_repo), "t")
        store.insert_commits(rid, commits)
        churn = {r["path"]: r["churn"] for r in store.churn(rid)}
    assert churn["pkg/utils.py"] == 4           # initial + 3 edits
    assert churn["pkg/auth_controller.py"] == 1


def test_rescanning_is_incremental(cycle_repo):
    repo = open_repo(cycle_repo)
    commits = list(iter_commits(repo, limit=100, exclude=DEFAULTS["exclude_paths"]))
    with Store(cycle_repo / ".repohealth" / "t.db") as store:
        rid = store.upsert_repo(str(cycle_repo), "t")
        assert store.insert_commits(rid, commits) == 1
        assert store.insert_commits(rid, commits) == 0   # nothing new the second time


def test_non_python_files_are_not_ingested(git_repo):
    git_repo.write("README.md", "# hello\n")
    git_repo.write("app.py", "x = 1\n")
    git_repo.commit("mixed")
    commits = list(iter_commits(open_repo(git_repo), exclude=DEFAULTS["exclude_paths"]))
    paths = {f["path"] for c in commits for f in c["files"]}
    assert paths == {"app.py"}


def test_python_files_lists_only_tracked_files(cycle_repo):
    (cycle_repo / "scratch.py").write_text("x = 1\n", encoding="utf-8")  # untracked
    files = python_files(open_repo(cycle_repo), DEFAULTS["exclude_paths"])
    assert "scratch.py" not in files
    assert "pkg/utils.py" in files


# ---------------- graph on a real repo ----------------

def test_cycle_is_detected_end_to_end(cycle_repo):
    repo = open_repo(cycle_repo)
    files = python_files(repo, DEFAULTS["exclude_paths"])
    graph = build_graph(cycle_repo, files)
    cycles = find_cycles(graph.adjacency())
    assert len(cycles) == 1
    assert set(cycles[0]) == {
        "pkg/auth_controller.py", "pkg/user_service.py", "pkg/db_connector.py",
    }


def test_shared_module_has_the_highest_blast_radius(cycle_repo):
    repo = open_repo(cycle_repo)
    graph = build_graph(cycle_repo, python_files(repo, DEFAULTS["exclude_paths"]))
    blast = blast_radius(graph.reverse_adjacency())
    assert blast["pkg/core/settings.py"] == max(blast.values())


# ---------------- persistence ----------------

def test_baseline_is_a_rolling_average_not_the_last_value(tmp_path):
    """One anomalous commit must not poison the gatekeeper threshold."""
    with Store(tmp_path / "t.db") as store:
        rid = store.upsert_repo(str(tmp_path), "t")
        for cc in (10, 10, 10, 10, 50):          # last run is the anomaly
            run = store.start_run(rid, None)
            store.insert_file_metrics(run, [{"path": "a.py", "cc_total": cc}])
            store.finish_run(run)
        baseline = store.baseline_cc("a.py", window=5)
    assert baseline == 18.0                       # mean, not 50


def test_interrupted_run_is_ignored(tmp_path):
    with Store(tmp_path / "t.db") as store:
        rid = store.upsert_repo(str(tmp_path), "t")
        good = store.start_run(rid, None)
        store.insert_file_metrics(good, [{"path": "a.py", "cc_total": 10}])
        store.finish_run(good)

        crashed = store.start_run(rid, None)      # never finished
        store.insert_file_metrics(crashed, [{"path": "a.py", "cc_total": 999}])

        assert store.baseline_cc("a.py", window=5) == 10.0
        assert store.latest_run_id(rid) == good


def test_unparsed_files_are_excluded_from_the_baseline(tmp_path):
    with Store(tmp_path / "t.db") as store:
        rid = store.upsert_repo(str(tmp_path), "t")
        for parsed, cc in ((True, 20), (False, 0), (True, 20)):
            run = store.start_run(rid, None)
            store.insert_file_metrics(run, [{"path": "a.py", "cc_total": cc, "parsed": parsed}])
            store.finish_run(run)
        assert store.baseline_cc("a.py", window=5) == 20.0


def test_no_history_returns_no_baseline(tmp_path):
    with Store(tmp_path / "t.db") as store:
        store.upsert_repo(str(tmp_path), "t")
        assert store.baseline_cc("never_seen.py") is None


# ---------------- gatekeeper (SRS 4.4) ----------------

def _scan(repo_path: Path):
    """Minimal scan: enough to establish baselines for the gate."""
    from repohealth.analysis.complexity import analyze_paths

    repo = open_repo(repo_path)
    cfg = load_config(repo_path)
    with Store(repo_path / cfg["db_path"]) as store:
        rid = store.upsert_repo(str(repo_path), repo_path.name)
        store.insert_commits(rid, list(iter_commits(repo, exclude=cfg["exclude_paths"])))
        files = python_files(repo, cfg["exclude_paths"])
        run = store.start_run(rid, None)
        store.insert_file_metrics(
            run, [m.as_row() for m in analyze_paths(repo_path, files, cfg["thresholds"])]
        )
        graph = build_graph(repo_path, files)
        store.insert_cycles(run, find_cycles(graph.adjacency()))
        store.finish_run(run)


SIMPLE = "def f(x):\n    if x: return 1\n    return 0\n"
COMPLEX = "def f(x):\n" + "".join(f"    if x == {i}: return {i}\n" for i in range(25))


def test_gate_passes_when_nothing_is_staged(cycle_repo):
    _scan(cycle_repo)
    result = run_gate(cycle_repo)
    assert result.passed and "no staged" in result.skipped_reason


def test_gate_passes_a_harmless_change(cycle_repo):
    cycle_repo.write("pkg/calc.py", SIMPLE * 8)
    cycle_repo.commit("add calc")
    _scan(cycle_repo)

    cycle_repo.write("pkg/calc.py", SIMPLE * 8 + "\ndef extra():\n    return 1\n")
    cycle_repo.git("add", "pkg/calc.py")
    assert run_gate(cycle_repo).passed


def test_gate_blocks_a_complexity_spike(cycle_repo):
    cycle_repo.write("pkg/calc.py", SIMPLE * 8)
    cycle_repo.commit("add calc")
    _scan(cycle_repo)

    cycle_repo.write("pkg/calc.py", SIMPLE * 8 + COMPLEX)
    cycle_repo.git("add", "pkg/calc.py")

    result = run_gate(cycle_repo)
    assert not result.passed
    v = [x for x in result.violations if x.kind == "complexity_spike"]
    assert v and v[0].path == "pkg/calc.py"
    assert v[0].detail                      # names the offending function


def test_gate_allows_a_brand_new_file(cycle_repo):
    """A file with no history has nothing to compare against. Blocking a
    developer's first commit to a new file would be hostile."""
    _scan(cycle_repo)
    cycle_repo.write("pkg/brand_new.py", COMPLEX)
    cycle_repo.git("add", "pkg/brand_new.py")
    assert run_gate(cycle_repo).passed


def test_gate_judges_staged_content_not_the_working_tree(cycle_repo):
    """If the developer keeps editing after `git add`, the commit contains what
    was staged -- so that is what must be judged."""
    cycle_repo.write("pkg/calc.py", SIMPLE * 8)
    cycle_repo.commit("add calc")
    _scan(cycle_repo)

    cycle_repo.write("pkg/calc.py", SIMPLE * 8)          # harmless, staged
    cycle_repo.git("add", "pkg/calc.py")
    cycle_repo.write("pkg/calc.py", SIMPLE * 8 + COMPLEX)  # spike, NOT staged

    assert run_gate(cycle_repo).passed
    assert "def f(x):\n    if x == 0" not in (staged_blob(open_repo(cycle_repo), "pkg/calc.py") or "")


def test_gate_does_not_blame_a_pre_existing_cycle(cycle_repo):
    """The fixture already contains a cycle. Only a NEW one may block."""
    _scan(cycle_repo)
    cycle_repo.write("pkg/utils.py",
                     "from pkg.core.settings import TIMEOUT\ndef add(a,b):\n    return a+b\n# touch\n")
    cycle_repo.git("add", "pkg/utils.py")
    result = run_gate(cycle_repo)
    assert not [v for v in result.violations if v.kind == "new_cycle"]


def test_gate_blocks_a_newly_introduced_cycle(cycle_repo):
    _scan(cycle_repo)
    # utils already imports settings; make settings import utils back
    cycle_repo.write("pkg/core/settings.py", "from pkg.utils import add\nTIMEOUT = 30\n")
    cycle_repo.git("add", "pkg/core/settings.py")
    result = run_gate(cycle_repo)
    assert [v for v in result.violations if v.kind == "new_cycle"]


def test_gate_meets_the_latency_requirement(cycle_repo):
    """SRS 5.1.1 -- the hook must not make committing feel slow."""
    cycle_repo.write("pkg/calc.py", SIMPLE * 8)
    cycle_repo.commit("add calc")
    _scan(cycle_repo)
    cycle_repo.write("pkg/calc.py", SIMPLE * 9)
    cycle_repo.git("add", "pkg/calc.py")
    assert run_gate(cycle_repo).elapsed < 1.5


def test_gate_without_a_database_passes_with_an_explanation(cycle_repo):
    cycle_repo.write("pkg/x.py", SIMPLE)
    cycle_repo.git("add", "pkg/x.py")
    result = run_gate(cycle_repo)
    assert result.passed and "baseline" in result.skipped_reason


# ---------------- hook installation ----------------

def test_hook_installs_and_uninstalls(cycle_repo):
    repo = open_repo(cycle_repo)
    assert status(repo) == "not installed"

    hook, action = install(repo)
    assert action == "installed" and hook.exists()
    assert status(repo) == "installed"
    assert "repohealth.cli check --staged" in hook.read_text(encoding="utf-8")

    assert uninstall(repo) is True
    assert status(repo) == "not installed"


def test_existing_foreign_hook_is_not_clobbered(cycle_repo):
    repo = open_repo(cycle_repo)
    hooks = Path(repo.git_dir) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho someone elses hook\n", encoding="utf-8")

    _, action = install(repo)
    assert action == "skipped"

    _, action = install(repo, force=True)
    assert action == "replaced"
    assert (hooks / "pre-commit.pre-repohealth").exists()


# ---------------- scoring ----------------

def test_clean_repository_scores_full_marks():
    metrics = [{"path": f"f{i}.py", "cc_total": 3, "cc_max": 2, "max_nesting": 1, "parsed": 1}
               for i in range(10)]
    bd = compute_health(metrics, {}, {}, [], DEFAULTS)
    assert bd.score == 100.0 and bd.grade == "A"


def test_cycles_reduce_the_score():
    metrics = [{"path": f"f{i}.py", "cc_total": 3, "cc_max": 2, "max_nesting": 1, "parsed": 1}
               for i in range(10)]
    clean = compute_health(metrics, {}, {}, [], DEFAULTS).score
    tangled = compute_health(metrics, {}, {}, [["f0.py", "f1.py"]], DEFAULTS).score
    assert tangled < clean


def test_score_never_leaves_the_zero_to_hundred_range():
    metrics = [{"path": f"f{i}.py", "cc_total": 500, "cc_max": 400, "max_nesting": 12, "parsed": 1}
               for i in range(10)]
    churn = {f"f{i}.py": 100 for i in range(10)}
    blast = {f"f{i}.py": 50 for i in range(10)}
    cycles = [[f"f{i}.py" for i in range(10)]]
    score = compute_health(metrics, churn, blast, cycles, DEFAULTS).score
    assert 0.0 <= score <= 100.0


def test_empty_repository_does_not_divide_by_zero():
    assert compute_health([], {}, {}, [], DEFAULTS).score == 100.0


def test_refactor_estimate_scales_with_severity():
    light = refactor_estimate(
        {"path": "a.py", "cc_max": 11, "max_nesting": 2, "max_class_loc": 0},
        DEFAULTS["thresholds"])
    heavy = refactor_estimate(
        {"path": "b.py", "cc_max": 60, "max_nesting": 9, "max_class_loc": 800},
        DEFAULTS["thresholds"], in_cycle=True)
    assert heavy["hours"] > light["hours"]
    assert heavy["drivers"]


def test_compliant_file_needs_no_refactoring():
    est = refactor_estimate(
        {"path": "a.py", "cc_max": 5, "max_nesting": 2, "max_class_loc": 50},
        DEFAULTS["thresholds"])
    assert est["hours"] == 0 and est["drivers"] == []
