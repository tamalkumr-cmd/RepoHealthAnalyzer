from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_NAME = "repohealth.db"
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
class Store:
    def __init__(self, db_path: str | os.PathLike):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
    def __enter__(self) -> "Store":
        return self
    def __exit__(self, *exc) -> None:
        self.close()
    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()
    def upsert_repo(self, path: str, name: str) -> int:
        cur = self.conn.execute("SELECT repo_id FROM repos WHERE path = ?", (path,))
        row = cur.fetchone()
        if row:
            return row["repo_id"]
        cur = self.conn.execute(
            "INSERT INTO repos (path, name, created_at) VALUES (?, ?, ?)",
            (path, name, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)
    def insert_commits(self, repo_id: int, commits: Iterable[dict[str, Any]]) -> int:
        """commits: dicts with sha, author_name, author_email, authored_at,
        message, files -> [{path, lines_added, lines_deleted}]."""
        inserted = 0
        for c in commits:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO commits
                   (repo_id, sha, author_name, author_email, authored_at, message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    repo_id,
                    c["sha"],
                    c.get("author_name"),
                    c.get("author_email"),
                    c.get("authored_at"),
                    c.get("message"),
                ),
            )
            if cur.rowcount == 0:
                continue
            commit_id = int(cur.lastrowid)
            inserted += 1
            self.conn.executemany(
                """INSERT INTO file_changes (commit_id, path, lines_added, lines_deleted)
                   VALUES (?, ?, ?, ?)""",
                [
                    (commit_id, f["path"], f.get("lines_added", 0), f.get("lines_deleted", 0))
                    for f in c.get("files", [])
                ],
            )
        self.conn.commit()
        return inserted

    def churn(self, repo_id: int) -> list[sqlite3.Row]:
        """Code churn = number of commits that touched each file, plus line volume."""
        return self.conn.execute(
            """SELECT fc.path                       AS path,
                      COUNT(*)                      AS churn,
                      SUM(fc.lines_added)           AS lines_added,
                      SUM(fc.lines_deleted)         AS lines_deleted,
                      COUNT(DISTINCT c.author_email) AS authors,
                      MAX(c.authored_at)            AS last_touched
               FROM file_changes fc
               JOIN commits c ON c.commit_id = fc.commit_id
               WHERE c.repo_id = ?
               GROUP BY fc.path
               ORDER BY churn DESC""",
            (repo_id,),
        ).fetchall()
    def latest_commit_sha(self, repo_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT sha FROM commits WHERE repo_id = ? ORDER BY authored_at DESC LIMIT 1",
            (repo_id,),
        ).fetchone()
        return row["sha"] if row else None
    def start_run(self, repo_id: int, commit_sha: str | None, kind: str = "full") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (repo_id, commit_sha, started_at, status, kind) VALUES (?,?,?,?,?)",
            (repo_id, commit_sha, _now(), "running", kind),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE runs SET completed_at = ?, status = ? WHERE run_id = ?",
            (_now(), status, run_id),
        )
        self.conn.commit()
    def insert_file_metrics(self, run_id: int, metrics: Sequence[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT INTO file_metrics
               (run_id, path, cc_total, cc_max, cc_avg, loc, num_functions,
                num_classes, max_class_loc, max_nesting, parsed, parse_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id,
                    m["path"],
                    m.get("cc_total", 0),
                    m.get("cc_max", 0),
                    m.get("cc_avg", 0.0),
                    m.get("loc", 0),
                    m.get("num_functions", 0),
                    m.get("num_classes", 0),
                    m.get("max_class_loc", 0),
                    m.get("max_nesting", 0),
                    1 if m.get("parsed", True) else 0,
                    m.get("parse_error"),
                )
                for m in metrics
            ],
        )
        self.conn.commit()

    def baseline_cc(self, path: str, window: int = 5) -> float | None:
        """Rolling average of cc_total over the last `window` completed runs.

        A rolling window is used instead of the single previous value so one
        noisy commit cannot poison the gatekeeper threshold (SRS 4.4.1.2).
        """
        rows = self.conn.execute(
            """SELECT fm.cc_total
               FROM file_metrics fm
               JOIN runs r ON r.run_id = fm.run_id
               WHERE fm.path = ? AND fm.parsed = 1 AND r.status = 'completed'
               ORDER BY fm.run_id DESC
               LIMIT ?""",
            (path, window),
        ).fetchall()
        if not rows:
            return None
        return sum(r["cc_total"] for r in rows) / len(rows)

    def metrics_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM file_metrics WHERE run_id = ? ORDER BY cc_total DESC", (run_id,)
        ).fetchall()


    def insert_dependencies(self, run_id: int, edges) -> None:
        self.conn.executemany(
            "INSERT INTO dependencies (run_id, from_path, to_path, import_kind) VALUES (?,?,?,?)",
            [(run_id, e.from_path, e.to_path, e.kind) for e in edges],
        )
        self.conn.commit()

    def insert_cycles(self, run_id: int, cycles: list[list[str]]) -> None:
        import json as _json
        self.conn.executemany(
            "INSERT INTO cycles (run_id, path_json, length) VALUES (?,?,?)",
            [(run_id, _json.dumps(c), len(c)) for c in cycles],
        )
        self.conn.commit()

    def insert_risk(self, run_id: int, rows: Sequence[dict[str, Any]]) -> None:
        self.conn.executemany
        self.conn.executemany(
            """INSERT INTO risk_analysis (run_id, path, churn, blast_radius, risk_score, risk_level)
               VALUES (?,?,?,?,?,?)""",
            [(run_id, r["path"], r.get("churn", 0), r.get("blast_radius", 0),
              r.get("risk_score", 0), r.get("risk_level")) for r in rows],
        )
        self.conn.commit()

    def latest_run_id(self, repo_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT run_id FROM runs WHERE repo_id=? AND status='completed' "
            "ORDER BY run_id DESC LIMIT 1", (repo_id,)).fetchone()
        return row["run_id"] if row else None

    def cycles_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cycles WHERE run_id = ? ORDER BY length", (run_id,)).fetchall()

    def dependencies_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM dependencies WHERE run_id = ?", (run_id,)).fetchall()

    def risk_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM risk_analysis WHERE run_id = ? ORDER BY risk_score DESC",
            (run_id,)).fetchall()
