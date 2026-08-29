"""RepoHealth Analyzer CLI.

    repohealth scan [PATH]     ingest git history, analyze complexity + dependencies
    repohealth report [PATH]   churn / complexity / risk summary
    repohealth cycles [PATH]   list circular import chains
    repohealth graph [PATH]    export the dependency graph as JSON
    repohealth check --staged  gatekeeper (phase 4 -- not wired yet)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .analysis.blast import blast_radius, direct_dependents, rank_fragile, risk_level
from .analysis.complexity import analyze_paths
from .analysis.cycles import find_cycles, format_cycle
from .analysis.depgraph import build_graph
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


def _require_db(root: Path, cfg) -> Path:
    db_path = root / cfg["db_path"]
    if not db_path.exists():
        print(f"{C.RED}No database.{C.RESET} Run `repohealth scan` first.")
        raise SystemExit(1)
    return db_path


def cmd_scan(args) -> int:
    started = time.perf_counter()
    repo, root, cfg = _resolve(args.path)

    with Store(root / cfg["db_path"]) as store:
        repo_id = store.upsert_repo(str(root), root.name)

        commits = list(iter_commits(repo, limit=cfg["history_limit"], exclude=cfg["exclude_paths"]))
        new = store.insert_commits(repo_id, commits)
        print(f"{C.CYAN}Ingested{C.RESET} {new} new commits ({len(commits)} scanned)")

        files = python_files(repo, cfg["exclude_paths"])
        run_id = store.start_run(repo_id, store.latest_commit_sha(repo_id), kind="full")

        # --- complexity ---
        metrics = analyze_paths(root, files, cfg["thresholds"])
        store.insert_file_metrics(run_id, [m.as_row() for m in metrics])
        failed = [m for m in metrics if not m.parsed]
        print(f"{C.CYAN}Analyzed{C.RESET} {len(metrics)} Python files ({len(failed)} unparsed)")
        for m in failed[:5]:
            print(f"  {C.YELLOW}skipped{C.RESET} {m.path}: {m.parse_error}")

        # --- dependency graph ---
        graph = build_graph(root, files)
        store.insert_dependencies(run_id, graph.edges)
        adj, rev = graph.adjacency(), graph.reverse_adjacency()
        print(f"{C.CYAN}Graph{C.RESET} {len(graph.nodes)} nodes, {len(graph.edges)} internal edges")

        # --- cycles ---
        found = find_cycles(adj)
        store.insert_cycles(run_id, found)
        if found:
            print(f"{C.RED}Cycles{C.RESET} {len(found)} circular import chain(s) detected")
            for c in found[:3]:
                print(f"  {C.RED}!{C.RESET} {format_cycle(c)}")
            if len(found) > 3:
                print(f"  {C.DIM}... {len(found) - 3} more (run `repohealth cycles`){C.RESET}")
        else:
            print(f"{C.GREEN}Cycles{C.RESET} none detected")

        # --- risk ---
        blast = blast_radius(rev)
        churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
        cc = {m.path: m.cc_total for m in metrics}
        ranked = rank_fragile(blast, churn, cc, top=len(files))
        all_scores = [int(r["risk_score"]) for r in ranked]
        for r in ranked:
            r["risk_level"] = risk_level(int(r["risk_score"]), all_scores)
        store.insert_risk(run_id, ranked)

        store.finish_run(run_id)
        print(f"{C.DIM}Run #{run_id} completed in {time.perf_counter() - started:.2f}s{C.RESET}")
    return 0


def cmd_report(args) -> int:
    repo, root, cfg = _resolve(args.path)
    with Store(_require_db(root, cfg)) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        if run_id is None:
            print("No completed runs.")
            return 1
        risk = store.risk_for_run(run_id)
        metrics = {m["path"]: m["cc_total"] for m in store.metrics_for_run(run_id)}
        cycle_rows = store.cycles_for_run(run_id)

    print(f"\n{C.BOLD}Risk ranking (run #{run_id}, top {args.top}){C.RESET}")
    print(f"{'file':<44} {'churn':>6} {'cc':>5} {'blast':>6} {'score':>7} {'level':>7}")
    print("-" * 80)
    for r in risk[: args.top]:
        colour = {"high": C.RED, "medium": C.YELLOW}.get(r["risk_level"], "")
        cc = metrics.get(r["path"], 0)
        print(f"{colour}{r['path'][:44]:<44} {r['churn']:>6} {cc:>5} "
              f"{r['blast_radius']:>6} {int(r['risk_score']):>7} {r['risk_level']:>7}{C.RESET}")

    if cycle_rows:
        print(f"\n{C.RED}{len(cycle_rows)} circular dependency chain(s){C.RESET}")
        for row in cycle_rows[: args.top]:
            print(f"  {format_cycle(json.loads(row['path_json']))}")
    print()
    return 0


def cmd_cycles(args) -> int:
    repo, root, cfg = _resolve(args.path)
    with Store(_require_db(root, cfg)) as store:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        rows = store.cycles_for_run(run_id) if run_id else []

    if not rows:
        print(f"{C.GREEN}No circular imports detected.{C.RESET}")
        return 0
    print(f"\n{C.RED}{C.BOLD}{len(rows)} circular import chain(s){C.RESET}\n")
    for i, row in enumerate(rows, 1):
        chain = json.loads(row["path_json"])
        print(f"{C.RED}[{i}]{C.RESET} length {row['length']}")
        for step in chain:
            print(f"      {step}")
        print(f"      {C.DIM}-> back to {chain[0]}{C.RESET}\n")
    return 1 if args.strict else 0


def cmd_graph(args) -> int:
    repo, root, cfg = _resolve(args.path)
    files = python_files(repo, cfg["exclude_paths"])
    graph = build_graph(root, files)
    rev = graph.reverse_adjacency()
    payload = graph.to_json()
    payload["blast_radius"] = blast_radius(rev)
    payload["direct_dependents"] = direct_dependents(rev)
    payload["cycles"] = find_cycles(graph.adjacency())

    text = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out} ({len(graph.nodes)} nodes, {len(graph.edges)} edges)")
    else:
        print(text)
    return 0


def cmd_check(args) -> int:
    print(f"{C.YELLOW}Gatekeeper not implemented yet (phase 4).{C.RESET}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repohealth", description=__doc__)
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="ingest history, analyze complexity and dependencies")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("report", help="risk summary of the latest run")
    r.add_argument("path", nargs="?", default=".")
    r.add_argument("--top", type=int, default=15)
    r.set_defaults(func=cmd_report)

    cy = sub.add_parser("cycles", help="list circular import chains")
    cy.add_argument("path", nargs="?", default=".")
    cy.add_argument("--strict", action="store_true", help="exit 1 if any cycle exists")
    cy.set_defaults(func=cmd_cycles)

    g = sub.add_parser("graph", help="export dependency graph as JSON")
    g.add_argument("path", nargs="?", default=".")
    g.add_argument("--out", help="write to file instead of stdout")
    g.set_defaults(func=cmd_graph)

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
