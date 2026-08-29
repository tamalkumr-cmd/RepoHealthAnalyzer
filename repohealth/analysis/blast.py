"""Blast radius (SRS 4.3.2.3).

Blast radius of a file = how many other files transitively depend on it. This
is reachability on the reversed graph: BFS out from the file along
"who-imports-me" edges. Direct dependents are tracked separately because
"3 files import this directly, 47 depend on it eventually" is the more useful
sentence in a terminal report.
"""

from __future__ import annotations

from collections import deque


def blast_radius(reverse_adjacency: dict[str, list[str]]) -> dict[str, int]:
    """Transitive dependent count per node. Excludes the node itself."""
    result: dict[str, int] = {}
    for node in reverse_adjacency:
        seen = {node}
        queue = deque(reverse_adjacency.get(node, []))
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            queue.extend(reverse_adjacency.get(cur, []))
        result[node] = len(seen) - 1
    return result


def direct_dependents(reverse_adjacency: dict[str, list[str]]) -> dict[str, int]:
    return {node: len(deps) for node, deps in reverse_adjacency.items()}


def fan_out(adjacency: dict[str, list[str]]) -> dict[str, int]:
    """How many files this one imports -- high fan-out signals a god module."""
    return {node: len(deps) for node, deps in adjacency.items()}


def rank_fragile(
    blast: dict[str, int],
    churn: dict[str, int],
    cc: dict[str, int],
    top: int = 15,
) -> list[dict[str, object]]:
    """Fragile = heavily depended on AND unstable AND complex.

    Multiplicative so a file must score on more than one axis to rank; a
    high-blast-radius file that never changes is not a risk.
    """
    rows = []
    for path, radius in blast.items():
        c = churn.get(path, 0)
        x = cc.get(path, 0)
        score = (radius + 1) * (c + 1) * (x + 1)
        rows.append(
            {"path": path, "blast_radius": radius, "churn": c, "cc": x, "risk_score": score}
        )
    rows.sort(key=lambda r: r["risk_score"], reverse=True)
    return rows[:top]


def risk_level(score: int, scores: list[int]) -> str:
    """Percentile banding against the repo's own distribution."""
    if not scores:
        return "low"
    ordered = sorted(scores)
    rank = sum(1 for s in ordered if s <= score) / len(ordered)
    if rank >= 0.9:
        return "high"
    if rank >= 0.7:
        return "medium"
    return "low"
