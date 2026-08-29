"""Dependency graph construction (SRS 4.3.2.1).

Builds a module index from the repo's Python files, then resolves every import
statement against it. Imports that do not resolve to a file inside the repo are
third-party or stdlib and are dropped -- they are not nodes in the graph.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def module_name(rel_path: str) -> str:
    """pkg/sub/mod.py -> pkg.sub.mod ; pkg/__init__.py -> pkg"""
    p = rel_path.replace("\\", "/")
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p == "__init__.py":
        return ""
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def build_module_index(paths: list[str]) -> dict[str, str]:
    """module name -> repo-relative file path."""
    index: dict[str, str] = {}
    for p in paths:
        name = module_name(p)
        if name:
            index[name] = p
    return index


def _package_of(rel_path: str) -> str:
    """Dotted package containing this file, for relative-import resolution."""
    p = rel_path.replace("\\", "/")
    if p.endswith("/__init__.py") or p == "__init__.py":
        return module_name(p)
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
    kind: str  # "import" | "from" | "relative"


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


def extract_imports(rel_path: str, source: str, index: dict[str, str]) -> tuple[list[Edge], list[str]]:
    """Returns (resolved edges, unresolved module names). Never raises."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, ValueError, RecursionError):
        return [], []

    edges: list[Edge] = []
    unresolved: list[str] = []
    pkg = _package_of(rel_path)
    seen: set[tuple[str, str]] = set()

    def add(target: str, kind: str) -> None:
        hit = _resolve(target, index)
        if hit is None:
            unresolved.append(target)
            return
        if hit == rel_path:
            return  # self-import, not a real edge
        key = (rel_path, hit)
        if key in seen:
            return
        seen.add(key)
        edges.append(Edge(rel_path, hit, kind))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, "import")

        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # relative: from . import x  /  from ..pkg import y
                base_parts = pkg.split(".") if pkg else []
                up = node.level - 1
                base_parts = base_parts[: len(base_parts) - up] if up else base_parts
                base = ".".join(p for p in base_parts if p)
                target = f"{base}.{node.module}" if node.module else base
                if not target:
                    continue
                hit = _resolve(target, index)
                if hit is None:
                    # `from . import sibling` -- each alias is its own module
                    for alias in node.names:
                        add(f"{target}.{alias.name}" if target else alias.name, "relative")
                elif hit != rel_path:
                    key = (rel_path, hit)
                    if key not in seen:
                        seen.add(key)
                        edges.append(Edge(rel_path, hit, "relative"))
            else:
                base = node.module or ""
                hit = _resolve(base, index)
                if hit is not None:
                    if hit != rel_path:
                        key = (rel_path, hit)
                        if key not in seen:
                            seen.add(key)
                            edges.append(Edge(rel_path, hit, "from"))
                else:
                    # `from pkg import mod` where pkg is a package directory
                    for alias in node.names:
                        add(f"{base}.{alias.name}" if base else alias.name, "from")

    return edges, unresolved


def build_graph(root: str | Path, paths: list[str]) -> DependencyGraph:
    index = build_module_index(paths)
    graph = DependencyGraph(nodes=sorted(paths))
    root = Path(root)

    for rel in paths:
        try:
            source = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        edges, unresolved = extract_imports(rel, source, index)
        graph.edges.extend(edges)
        for name in unresolved:
            top = name.split(".")[0]
            graph.unresolved[top] = graph.unresolved.get(top, 0) + 1

    return graph
