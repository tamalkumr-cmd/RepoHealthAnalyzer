"""Dependency graph construction (SRS 4.3.2.1)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def module_name(rel_path: str, source_root: str = "") -> str:
    """pkg/sub/mod.py -> pkg.sub.mod ; pkg/__init__.py -> pkg

    `source_root` is the directory imports are resolved relative to. Under a
    `src/` layout the file src/myapp/app.py is imported as `myapp.app`, not
    `src.myapp.app`, so the root prefix is stripped first.
    """
    p = rel_path.replace("\\", "/")
    if source_root and p.startswith(source_root + "/"):
        p = p[len(source_root) + 1:]
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p == "__init__.py":
        return ""
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def source_root_for(rel_path: str, pathset: set[str]) -> str:
    """The directory this file's imports are resolved against.

    Walks up from the file while each ancestor directory is a package (has an
    __init__.py). The first ancestor that is NOT a package is the source root:
    that is the directory Python itself would have on sys.path.

        src/myapp/core/config.py  with __init__.py in myapp/ and core/  -> "src"
        pkg/core/settings.py      with __init__.py in pkg/ and core/    -> ""
        scripts/build.py          with no __init__.py anywhere          -> "scripts"
    """
    parts = rel_path.replace("\\", "/").split("/")
    d = len(parts) - 1
    while d > 0:
        if "/".join(parts[:d]) + "/__init__.py" in pathset:
            d -= 1
        else:
            break
    return "/".join(parts[:d])


def build_module_index(paths: list[str]) -> dict[str, str]:
    """module name -> repo-relative file path.

    Each file is registered under its source-root-relative name. The
    repo-root-relative name is also registered when it does not collide, so
    monorepos that import across roots both ways still resolve.
    """
    pathset = set(p.replace("\\", "/") for p in paths)
    index: dict[str, str] = {}
    fallback: dict[str, str] = {}

    for p in paths:
        root = source_root_for(p, pathset)
        primary = module_name(p, root)
        if primary:
            index.setdefault(primary, p)
        if root:
            alt = module_name(p, "")
            if alt and alt != primary:
                fallback.setdefault(alt, p)

    for name, p in fallback.items():
        index.setdefault(name, p)
    return index


def _package_of(rel_path: str, source_root: str = "") -> str:
    """Dotted package containing this file, for relative-import resolution."""
    p = rel_path.replace("\\", "/")
    if p.endswith("/__init__.py") or p == "__init__.py":
        return module_name(p, source_root)
    if source_root and p.startswith(source_root + "/"):
        p = p[len(source_root) + 1:]
    parent = "/".join(p.split("/")[:-1])
    return parent.replace("/", ".") if parent else ""


def _resolve(target: str, index: dict[str, str]) -> str | None:
    """Longest-prefix match: `pkg.mod.Class` resolves to `pkg.mod` if that's a file."""
    if not target:
        return None
    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in index:
            return index[candidate]
    return None


@dataclass
class Edge:
    from_path: str
    to_path: str
    kind: str


@dataclass
class DependencyGraph:
    nodes: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    unresolved: dict[str, int] = field(default_factory=dict)

    def adjacency(self) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.to_path not in adj.setdefault(e.from_path, []):
                adj[e.from_path].append(e.to_path)
        return adj

    def reverse_adjacency(self) -> dict[str, list[str]]:
        rev: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.from_path not in rev.setdefault(e.to_path, []):
                rev[e.to_path].append(e.from_path)
        return rev

    def to_json(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": [{"from": e.from_path, "to": e.to_path, "kind": e.kind} for e in self.edges],
        }


def extract_imports(
    rel_path: str, source: str, index: dict[str, str], source_root: str = ""
) -> tuple[list[Edge], list[str]]:
    """Returns (resolved edges, unresolved module names). Never raises."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, ValueError, RecursionError):
        return [], []

    edges: list[Edge] = []
    unresolved: list[str] = []
    pkg = _package_of(rel_path, source_root)
    seen: set[tuple[str, str]] = set()

    def add(target: str, kind: str) -> None:
        hit = _resolve(target, index)
        if hit is None:
            unresolved.append(target)
            return
        if hit == rel_path:
            return
        key = (rel_path, hit)
        if key in seen:
            return
        seen.add(key)
        edges.append(Edge(rel_path, hit, kind))

    def add_hit(hit: str | None, kind: str) -> bool:
        if hit is None or hit == rel_path:
            return False
        key = (rel_path, hit)
        if key not in seen:
            seen.add(key)
            edges.append(Edge(rel_path, hit, kind))
        return True

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, "import")

        elif isinstance(node, ast.ImportFrom):
            # Resolve the dotted package the names are being imported FROM.
            # For a relative import that means walking up from this file's own
            # package by one level per leading dot.
            if node.level and node.level > 0:
                parts = pkg.split(".") if pkg else []
                up = node.level - 1
                if up:
                    parts = parts[: len(parts) - up]
                base = ".".join(p for p in parts if p)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
                kind = "relative"
            else:
                base = node.module or ""
                kind = "from"

            if not base and not node.names:
                continue

            # `from X import a` is ambiguous: `a` may be a SUBMODULE of package
            # X, or an attribute defined inside module X. The submodule is a
            # real file-level dependency, so it wins when one exists -- without
            # this, `from . import b` resolves to the package __init__ and the
            # edge to b.py is lost entirely.
            resolved_any = False
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in index and add_hit(index[candidate], kind):
                    resolved_any = True

            if not resolved_any:
                hit = _resolve(base, index) if base else None
                if hit is not None:
                    add_hit(hit, kind)
                elif base:
                    unresolved.append(base)

    return edges, unresolved


def build_graph(root: str | Path, paths: list[str]) -> DependencyGraph:
    index = build_module_index(paths)
    pathset = set(p.replace("\\", "/") for p in paths)
    graph = DependencyGraph(nodes=sorted(paths))
    root = Path(root)

    for rel in paths:
        try:
            source = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        edges, unresolved = extract_imports(
            rel, source, index, source_root_for(rel, pathset)
        )
        graph.edges.extend(edges)
        for name in unresolved:
            top = name.split(".")[0]
            graph.unresolved[top] = graph.unresolved.get(top, 0) + 1

    return graph


def build_graph_with_overrides(
    root: str | Path, paths: list[str], overrides: dict[str, str]
) -> DependencyGraph:
    """Same as build_graph, but `overrides` supplies source text for selected
    paths instead of reading from disk.

    The gatekeeper needs this: it must build the graph from the STAGED content
    of modified files, not from the working tree, which may have moved on.
    """
    index = build_module_index(paths)
    pathset = set(p.replace("\\", "/") for p in paths)
    graph = DependencyGraph(nodes=sorted(paths))
    root = Path(root)

    for rel in paths:
        if rel in overrides:
            source = overrides[rel]
        else:
            try:
                source = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        edges, _ = extract_imports(
            rel, source, index, source_root_for(rel, pathset)
        )
        graph.edges.extend(edges)

    return graph
