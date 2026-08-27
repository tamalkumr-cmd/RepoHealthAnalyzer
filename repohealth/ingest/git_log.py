"""Git repository ingestion & parsing (SRS 4.1).

Walks the commit history and emits per-commit file-change records. Code churn
itself is derived in SQL (Store.churn) rather than counted here, so re-running
ingestion on new commits stays incremental.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from git import InvalidGitRepositoryError, Repo

from ..config import is_excluded


class NotAGitRepo(Exception):
    pass


def open_repo(path: str | Path) -> Repo:
    try:
        return Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError as exc:
        raise NotAGitRepo(f"{path} is not inside a Git repository") from exc


def iter_commits(
    repo: Repo,
    limit: int | None = None,
    exclude: list[str] | None = None,
    python_only: bool = True,
) -> Iterator[dict[str, Any]]:
    exclude = exclude or []
    try:
        commits = repo.iter_commits(max_count=limit)
    except ValueError:
        return  # empty repository, no HEAD yet

    for commit in commits:
        files: list[dict[str, Any]] = []
        # commit.stats runs a diff against the first parent; on merge commits
        # this is intentionally the diff to parent[0] only.
        for path, stat in commit.stats.files.items():
            path = str(path).replace("\\", "/")
            if python_only and not path.endswith(".py"):
                continue
            if is_excluded(path, exclude):
                continue
            files.append(
                {
                    "path": path,
                    "lines_added": stat.get("insertions", 0),
                    "lines_deleted": stat.get("deletions", 0),
                }
            )
        if not files:
            continue
        yield {
            "sha": commit.hexsha,
            "author_name": commit.author.name,
            "author_email": commit.author.email,
            "authored_at": commit.authored_datetime.isoformat(),
            "message": commit.message.strip().splitlines()[0][:200] if commit.message else "",
            "files": files,
        }


def python_files(repo: Repo, exclude: list[str] | None = None) -> list[str]:
    """Tracked .py files in the working tree, repo-relative, excludes applied."""
    exclude = exclude or []
    root = Path(repo.working_tree_dir)
    out = []
    for entry in repo.git.ls_files("*.py").splitlines():
        entry = entry.strip().replace("\\", "/")
        if not entry or is_excluded(entry, exclude):
            continue
        if (root / entry).is_file():
            out.append(entry)
    return sorted(out)


def staged_python_files(repo: Repo, exclude: list[str] | None = None) -> list[str]:
    """Files staged for the pending commit — the gatekeeper's input (SRS 4.4)."""
    exclude = exclude or []
    try:
        diff = repo.index.diff("HEAD")
    except Exception:
        diff = repo.index.diff(None)
    paths = set()
    for d in diff:
        for p in (d.a_path, d.b_path):
            if p and p.endswith(".py") and not is_excluded(p, exclude):
                paths.add(p.replace("\\", "/"))
    root = Path(repo.working_tree_dir)
    return sorted(p for p in paths if (root / p).is_file())
