"""Dependency graph construction and import resolution (SRS 4.3.2.1)."""

from __future__ import annotations

from repohealth.analysis.depgraph import (
    build_module_index,
    extract_imports,
    module_name,
    source_root_for,
)


def test_module_name_from_path():
    assert module_name("pkg/sub/mod.py") == "pkg.sub.mod"
    assert module_name("pkg/__init__.py") == "pkg"
    assert module_name("mod.py") == "mod"


def test_plain_import_resolves():
    idx = build_module_index(["a.py", "b.py"])
    edges, _ = extract_imports("a.py", "import b\n", idx)
    assert [(e.from_path, e.to_path) for e in edges] == [("a.py", "b.py")]


def test_from_import_resolves_to_the_module_not_the_attribute():
    """`from pkg.utils import add` is ambiguous: `add` could be a submodule or a
    function. Longest-prefix matching tries the longer name first and falls back."""
    idx = build_module_index(["pkg/__init__.py", "pkg/utils.py", "app.py"])
    edges, _ = extract_imports("app.py", "from pkg.utils import add\n", idx)
    assert [e.to_path for e in edges] == ["pkg/utils.py"]


def test_longest_prefix_prefers_the_submodule_when_one_exists():
    idx = build_module_index(["pkg/__init__.py", "pkg/utils/__init__.py",
                              "pkg/utils/add.py", "app.py"])
    edges, _ = extract_imports("app.py", "from pkg.utils import add\n", idx)
    assert [e.to_path for e in edges] == ["pkg/utils/add.py"]


def test_stdlib_and_third_party_are_excluded():
    """Only first-party files become nodes -- the graph must stay confined to
    code the developer can actually refactor."""
    idx = build_module_index(["app.py"])
    edges, unresolved = extract_imports(
        "app.py", "import json\nimport os\nimport pandas\n", idx
    )
    assert edges == []
    assert set(unresolved) == {"json", "os", "pandas"}


def test_relative_import_resolves():
    idx = build_module_index(["pkg/__init__.py", "pkg/a.py", "pkg/b.py"])
    edges, _ = extract_imports("pkg/a.py", "from . import b\n", idx)
    assert [e.to_path for e in edges] == ["pkg/b.py"]


def test_parent_relative_import_resolves():
    idx = build_module_index(
        ["pkg/__init__.py", "pkg/core/__init__.py", "pkg/core/cfg.py", "pkg/sub/__init__.py", "pkg/sub/m.py"]
    )
    edges, _ = extract_imports("pkg/sub/m.py", "from ..core import cfg\n", idx)
    assert [e.to_path for e in edges] == ["pkg/core/cfg.py"]


def test_self_import_is_not_an_edge():
    idx = build_module_index(["pkg/__init__.py", "pkg/a.py"])
    edges, _ = extract_imports("pkg/a.py", "import pkg.a\n", idx)
    assert edges == []


def test_duplicate_imports_collapse_to_one_edge():
    idx = build_module_index(["a.py", "b.py"])
    edges, _ = extract_imports("a.py", "import b\nfrom b import x\nimport b\n", idx)
    assert len(edges) == 1


def test_unparseable_file_yields_no_edges_without_raising():
    idx = build_module_index(["a.py", "b.py"])
    edges, unresolved = extract_imports("a.py", "import b\ndef f(\n", idx)
    assert edges == [] and unresolved == []


# ---- source root detection (the src/ layout fix) ----

def test_source_root_flat_layout():
    paths = {"pkg/__init__.py", "pkg/core/__init__.py", "pkg/core/s.py"}
    assert source_root_for("pkg/core/s.py", paths) == ""


def test_source_root_src_layout():
    paths = {"src/myapp/__init__.py", "src/myapp/app.py"}
    assert source_root_for("src/myapp/app.py", paths) == "src"


def test_source_root_script_without_package():
    assert source_root_for("scripts/build.py", {"scripts/build.py"}) == "scripts"


def test_source_root_top_level_file():
    assert source_root_for("setup.py", {"setup.py"}) == ""


def test_src_layout_imports_resolve():
    """src/myapp/app.py is imported as `myapp.app`, never `src.myapp.app`.
    Before the source-root fix this produced a graph with zero edges."""
    paths = ["src/myapp/__init__.py", "src/myapp/app.py", "src/myapp/services.py"]
    idx = build_module_index(paths)
    edges, _ = extract_imports(
        "src/myapp/app.py", "from myapp.services import fetch\n", idx,
        source_root_for("src/myapp/app.py", set(paths)),
    )
    assert [e.to_path for e in edges] == ["src/myapp/services.py"]


def test_monorepo_two_source_roots():
    paths = ["src/api/__init__.py", "src/api/main.py",
             "services/worker/__init__.py", "services/worker/job.py"]
    idx = build_module_index(paths)
    assert idx["api.main"] == "src/api/main.py"
    assert idx["worker.job"] == "services/worker/job.py"
