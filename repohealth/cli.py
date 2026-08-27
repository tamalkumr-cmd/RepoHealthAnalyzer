"""RepoHealth Analyzer CLI.

    repohealth scan [PATH]     ingest git history + analyze complexity
    repohealth report [PATH]   print churn / complexity / hotspot summary
    repohealth check --staged  gatekeeper (phase 4 — not wired yet)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .analysis.complexity import analyze_paths
from .config import load_config
from .db.store import Store
from .ingest.git_log import NotAGitRepo, iter_commits, open_repo, python_files


class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"

    @classmethod
    def off(cls) -> None:
        for k in list(vars(cls)):
            if k.isupper():
                setattr(cls, k, "")


def _resolve(path: str):
    repo = open_repo(path)
    root = Path(repo.working_tree_dir)
    cfg = load_config(root)
    return repo, root, cfg


def cmd_scan(args) -> int:
    started = time.perf_counter()
    repo, root, cfg = _resolve(args.path)
    db_path = root / cfg["db_path"]

    with Store(db_path) as store:
        repo_id = store.upsert_repo(str(root), root.name)

        commits = list(
            iter_commits(repo, limit=cfg["history_limit"], exclude=cfg["exclude_paths"])
        )
        new = store.insert_commits(repo_id, commits)
        print(f"{C.CYAN}Ingested{C.RESET} {new} new commits ({len(commits)} scanned)")

        files = python_files(repo, cfg["exclude_paths"])
        run_id = store.start_run(repo_id, store.latest_commit_sha(repo_id), kind="full")
        metrics = analyze_paths(root, files, cfg["thresholds"])
        store.insert_file_metrics(run_id, [m.as_row() for m in metrics])
        store.finish_run(run_id)

        failed = [m for m in metrics if not m.parsed]
        print(f"{C.CYAN}Analyzed{C.RESET} {len(metrics)} Python files "
              f"({len(failed)} unparsed) -> run #{run_id}")
        for m in failed[:5]:
            print(f"  {C.YELLOW}skipped{C.RESET} {m.path}: {m.parse_error}")

    print(f"{C.DIM}Completed in {time.perf_counter() - started:.2f}s{C.RESET}")
    return 0


def cmd_report(args) -> int:
    repo, root, cfg = _resolve(args.path)
    db_path = root / cfg["db_path"]
    if not db_path.exists():
        print(f"{C.RED}No database.{C.RESET} Run `repohealth scan` first.")
        return 1

    with Store(db_path) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
        row = store.conn.execute(
            "SELECT run_id FROM runs WHERE repo_id=? AND status='completed' "
            "ORDER BY run_id DESC LIMIT 1", (repo_id,)).fetchone()
        if row is None:
            print("No completed runs.")
            return 1
        metrics = store.metrics_for_run(row["run_id"])

    print(f"\n{C.BOLD}Churn x complexity (top {args.top}){C.RESET}")
    print(f"{'file':<48} {'churn':>6} {'cc':>6} {'nest':>5} {'loc':>6}")
    print("-" * 74)
    ranked = sorted(
        metrics,
        key=lambda m: churn.get(m["path"], 0) * max(m["cc_total"], 1),
        reverse=True,
    )
    for m in ranked[: args.top]:
        ch = churn.get(m["path"], 0)
        flag = C.RED if ch >= 5 and m["cc_total"] >= 20 else ""
        print(f"{flag}{m['path'][:48]:<48} {ch:>6} {m['cc_total']:>6} "
              f"{m['max_nesting']:>5} {m['loc']:>6}{C.RESET}")
    print()
    return 0


def cmd_check(args) -> int:
    print(f"{C.YELLOW}Gatekeeper not implemented yet (phase 4).{C.RESET}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repohealth", description=__doc__)
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="ingest history and analyze complexity")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("report", help="print a summary of the latest run")
    r.add_argument("path", nargs="?", default=".")
    r.add_argument("--top", type=int, default=15)
    r.set_defaults(func=cmd_report)

    c = sub.add_parser("check", help="pre-commit gatekeeper")
    c.add_argument("path", nargs="?", default=".")
    c.add_argument("--staged", action="store_true")
    c.set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_color or not sys.stdout.isatty():
        C.off()
    try:
        return args.func(args)
    except NotAGitRepo as exc:
        print(f"{C.RED}error:{C.RESET} {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
