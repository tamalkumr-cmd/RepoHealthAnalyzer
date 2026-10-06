"""Git repository ingestion & parsing (SRS 4.1)."""

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
    """Python files staged for the pending commit -- the gatekeeper's input.

    Uses `git diff --cached --name-only --diff-filter=ACMR`, which reports
    exactly what the pending commit will contain. Deleted files are excluded
    by the filter since there is nothing left to analyse.
    """
    exclude = exclude or []
    try:
        out = repo.git.diff("--cached", "--name-only", "--diff-filter=ACMR")
    except Exception:
        return []

    root = Path(repo.working_tree_dir)
    paths = []
    for entry in out.splitlines():
        entry = entry.strip().replace("\\", "/")
        if not entry or not entry.endswith(".py"):
            continue
        if is_excluded(entry, exclude):
            continue
        if (root / entry).is_file():
            paths.append(entry)
    return sorted(set(paths))


def staged_blob(repo: Repo, rel_path: str) -> str | None:
    """Content of a file AS STAGED, not as it sits on disk.

    These differ whenever the developer has edited a file further after
    `git add`. The gate must judge what is actually being committed.
    """
    try:
        return repo.git.show(f":{rel_path}")
    except Exception:
        return None


def head_sha(repo: Repo) -> str | None:
    try:
        return repo.head.commit.hexsha
    except Exception:
        return None
