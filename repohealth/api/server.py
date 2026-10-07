"""Local REST API (SRS 3.4).

Binds to 127.0.0.1 only. There is no authentication because there is no
network surface: the server is unreachable from outside the machine, which is
the same guarantee the rest of the system makes about source code never
leaving local storage.

Every endpoint reads the last completed run from SQLite. Nothing here
re-analyses; `repohealth scan` is the only writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analysis.blast import blast_radius, direct_dependents
from ..analysis.depgraph import build_graph
from ..analysis.scoring import compute_health, refactor_estimate
from ..config import load_config
from ..db.store import Store
from ..ingest.git_log import open_repo, python_files

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def create_app(repo_path: str = ".") -> FastAPI:
    repo = open_repo(repo_path)
    root = Path(repo.working_tree_dir)
    cfg = load_config(root)
    db_path = root / cfg["db_path"]

    app = FastAPI(title="RepoHealth Analyzer", version="0.5.0", docs_url="/api/docs")

    def _store() -> Store:
        if not db_path.exists():
            raise HTTPException(
                status_code=409,
                detail="No analysis database. Run `repohealth scan` first.",
            )
        return Store(db_path)

    def _latest(store: Store) -> tuple[int, int]:
        repo_id = store.upsert_repo(str(root), root.name)
        run_id = store.latest_run_id(repo_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="No completed analysis runs.")
        return repo_id, run_id

    # ---------------- meta ----------------

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        with _store() as store:
            repo_id, run_id = _latest(store)
            row = store.conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            commits = store.conn.execute(
                "SELECT COUNT(*) AS n FROM commits WHERE repo_id = ?", (repo_id,)
            ).fetchone()["n"]
            runs = store.conn.execute(
                "SELECT run_id, started_at, completed_at FROM runs "
                "WHERE repo_id = ? AND status='completed' ORDER BY run_id DESC LIMIT 30",
                (repo_id,),
            ).fetchall()
        return {
            "repo": {"name": root.name, "path": str(root)},
            "run": {
                "id": run_id,
                "commit_sha": row["commit_sha"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            },
            "commits_ingested": commits,
            "run_history": [dict(r) for r in runs],
        }

    # ---------------- health score ----------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with _store() as store:
            repo_id, run_id = _latest(store)
            metrics = [dict(m) for m in store.metrics_for_run(run_id)]
            churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in store.cycles_for_run(run_id)]
            blast = {
                r["path"]: r["blast_radius"] for r in store.risk_for_run(run_id)
            }
        return compute_health(metrics, churn, blast, cycles, cfg).as_dict()

    @app.get("/api/health/trend")
    def health_trend() -> list[dict[str, Any]]:
        """Score recomputed for each stored run -- the data for a trend chart.

        Timestamped runs have been accumulating since phase 1; this is what
        makes 'is it getting better or worse' answerable rather than just
        'how bad is it now'.
        """
        out = []
        with _store() as store:
            repo_id, _ = _latest(store)
            churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
            runs = store.conn.execute(
                "SELECT run_id, completed_at FROM runs WHERE repo_id = ? "
                "AND status='completed' ORDER BY run_id",
                (repo_id,),
            ).fetchall()
            for r in runs:
                rid = r["run_id"]
                metrics = [dict(m) for m in store.metrics_for_run(rid)]
                if not metrics:
                    continue
                cycles = [json.loads(c["path_json"]) for c in store.cycles_for_run(rid)]
                blast = {x["path"]: x["blast_radius"] for x in store.risk_for_run(rid)}
                bd = compute_health(metrics, churn, blast, cycles, cfg)
                out.append(
                    {
                        "run_id": rid,
                        "at": r["completed_at"],
                        "score": round(bd.score, 1),
                        "cycles": len(cycles),
                        "files": bd.facts.get("files", 0),
                    }
                )
        return out

    # ---------------- files ----------------

    @app.get("/api/files")
    def files(limit: int = 100) -> list[dict[str, Any]]:
        with _store() as store:
            repo_id, run_id = _latest(store)
            metrics = {m["path"]: dict(m) for m in store.metrics_for_run(run_id)}
            risk = [dict(r) for r in store.risk_for_run(run_id)]
            churn_rows = {r["path"]: dict(r) for r in store.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in store.cycles_for_run(run_id)]

        in_cycle = {p for c in cycles for p in c}
        out = []
        for r in risk[:limit]:
            m = metrics.get(r["path"], {})
            ch = churn_rows.get(r["path"], {})
            est = refactor_estimate(
                {**m, "path": r["path"]}, cfg["thresholds"], r["path"] in in_cycle
            )
            out.append(
                {
                    "path": r["path"],
                    "churn": r["churn"],
                    "authors": ch.get("authors", 0),
                    "last_touched": ch.get("last_touched"),
                    "cc_total": m.get("cc_total", 0),
                    "cc_max": m.get("cc_max", 0),
                    "max_nesting": m.get("max_nesting", 0),
                    "loc": m.get("loc", 0),
                    "blast_radius": r["blast_radius"],
                    "risk_score": r["risk_score"],
                    "risk_level": r["risk_level"],
                    "in_cycle": r["path"] in in_cycle,
                    "refactor": est,
                }
            )
        return out

    # ---------------- graph ----------------

    @app.get("/api/graph")
    def graph() -> dict[str, Any]:
        with _store() as store:
            repo_id, run_id = _latest(store)
            edges = [
                {"from": d["from_path"], "to": d["to_path"], "kind": d["import_kind"]}
                for d in store.dependencies_for_run(run_id)
            ]
            metrics = {m["path"]: dict(m) for m in store.metrics_for_run(run_id)}
            risk = {r["path"]: dict(r) for r in store.risk_for_run(run_id)}
            churn = {r["path"]: r["churn"] for r in store.churn(repo_id)}
            cycles = [json.loads(r["path_json"]) for r in store.cycles_for_run(run_id)]

        in_cycle = {p for c in cycles for p in c}
        nodes = [
            {
                "id": p,
                "label": p.split("/")[-1],
                "cc": m.get("cc_total", 0),
                "loc": m.get("loc", 0),
                "churn": churn.get(p, 0),
                "blast_radius": risk.get(p, {}).get("blast_radius", 0),
                "risk_level": risk.get(p, {}).get("risk_level", "low"),
                "in_cycle": p in in_cycle,
            }
            for p, m in metrics.items()
        ]
        return {"nodes": nodes, "edges": edges, "cycles": cycles}

    @app.get("/api/graph/live")
    def graph_live() -> dict[str, Any]:
        """Graph rebuilt from the working tree rather than the last run.

        Useful while editing; `/api/graph` is the one the dashboard uses.
        """
        paths = python_files(repo, cfg["exclude_paths"])
        g = build_graph(root, paths)
        rev = g.reverse_adjacency()
        payload = g.to_json()
        payload["blast_radius"] = blast_radius(rev)
        payload["direct_dependents"] = direct_dependents(rev)
        return payload

    # ---------------- cycles ----------------

    @app.get("/api/cycles")
    def cycles_endpoint() -> list[dict[str, Any]]:
        with _store() as store:
            _, run_id = _latest(store)
            rows = store.cycles_for_run(run_id)
        return [
            {"length": r["length"], "chain": json.loads(r["path_json"])} for r in rows
        ]

    # ---------------- config ----------------

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return cfg

    @app.put("/api/config")
    def put_config(payload: dict[str, Any]) -> dict[str, Any]:
        """Writes config.json in the scanned repository (UC-4.5).

        Only keys already present in the loaded config are accepted, so a
        malformed request cannot introduce silently-ignored settings.
        """
        allowed = {"gatekeeper", "thresholds", "scoring", "exclude_paths", "history_limit"}
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(400, f"unknown config keys: {sorted(unknown)}")

        target = root / "config.json"
        current = {}
        if target.exists():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        for k, v in payload.items():
            if isinstance(v, dict) and isinstance(current.get(k), dict):
                current[k].update(v)
            else:
                current[k] = v
        target.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        cfg.update(load_config(root))
        return {"written": str(target), "config": cfg}

    # ---------------- dashboard ----------------

    if WEB_DIR.is_dir() and (WEB_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(WEB_DIR / "index.html"))
    else:
        @app.get("/")
        def index_placeholder():
            return JSONResponse(
                {
                    "message": "API is running. The dashboard arrives in phase 6.",
                    "endpoints": [
                        "/api/meta", "/api/health", "/api/health/trend",
                        "/api/files", "/api/graph", "/api/cycles",
                        "/api/config", "/api/docs",
                    ],
                }
            )

    return app


def serve(repo_path: str = ".", host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(repo_path), host=host, port=port, log_level="warning")
