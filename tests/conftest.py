"""Shared fixtures.

The analysis engines are pure functions over source text and graph structures,
so most tests need no repository at all. The few that exercise ingestion or the
gatekeeper build a real throwaway Git repository in a temp directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


class Repo(type(Path())):  # type: ignore[misc]
    """A Path to a Git repository, with write/commit/git helpers.

    Subclassing Path rather than wrapping it means the fixture can be passed
    straight to any function expecting a path, which is most of the codebase.
    """

    def write(self, rel: str, content: str) -> Path:
        p = self / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def commit(self, message: str = "change") -> None:
        _git(self, "add", "-A")
        _git(self, "commit", "-q", "-m", message)

    def git(self, *args: str) -> str:
        return _git(self, *args)


@pytest.fixture
def git_repo(tmp_path: Path) -> Repo:
    """An empty initialised Git repository."""
    repo = Repo(tmp_path / "repo")
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


@pytest.fixture
def cycle_repo(git_repo):
    """Repository containing the three-file import cycle from SRS Appendix B,
    plus a shared settings module several files depend on."""
    git_repo.write("pkg/__init__.py", "")
    git_repo.write("pkg/core/__init__.py", "")
    git_repo.write("pkg/core/settings.py", "TIMEOUT = 30\n")
    git_repo.write(
        "pkg/auth_controller.py",
        "from pkg.user_service import get_user\n"
        "from pkg.core.settings import TIMEOUT\n"
        "def login(u):\n    return get_user(u)\n",
    )
    git_repo.write(
        "pkg/user_service.py",
        "from pkg.db_connector import query\n"
        "def get_user(u):\n    return query(u)\n",
    )
    git_repo.write(
        "pkg/db_connector.py",
        "from pkg.auth_controller import login\n"
        "from pkg.core.settings import TIMEOUT\n"
        "def query(s):\n    return login(s)\n",
    )
    git_repo.write(
        "pkg/utils.py",
        "from pkg.core.settings import TIMEOUT\n"
        "def add(a, b):\n    return a + b\n",
    )
    git_repo.commit("initial")
    return git_repo
