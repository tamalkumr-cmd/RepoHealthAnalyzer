"""Blast radius (SRS 4.3.2.3).

Blast radius of a file = how many other files transitively depend on it,
i.e. reachability on the reversed graph. Computed via Tarjan SCC condensation
plus a single reverse-topological sweep with bitmask reachability, which
replaces a per-node BFS with one near-linear pass.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from functools import lru_cache
from typing import Iterable, Iterator


def _all_nodes(adjacency: dict[str, list[str]]) -> Iterable[str]:
    seen: set[str] = set()
    for node, deps in adjacency.items():
        if node not in seen:
            seen.add(node)
            yield node
        for d in deps:
            if d not in seen:
                seen.add(d)
                yield d


def _scc_ids(adjacency: dict[str, list[str]]) -> tuple[dict[str, int], int]:
    """Iterative Tarjan. Returns (node -> scc id, scc count).

    Tarjan emits SCCs in reverse topological order, so for any cross-component
    edge u -> v, scc_id[v] < scc_id[u]. The sweep below relies on that.
    """
    index_counter = 0
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    scc_id: dict[str, int] = {}
    next_scc = 0

    for root in _all_nodes(adjacency):
        if root in index:
            continue
        call_stack: list[tuple[str, Iterator[str]]] = [(root, iter(adjacency.get(root, ())))]
        index[root] = lowlink[root] = index_counter
        index_counter += 1
        stack.append(root)
        on_stack[root] = True

        while call_stack:
            v, children = call_stack[-1]
            advanced = False
            for w in children:
                if w not in index:
                    index[w] = lowlink[w] = index_counter
                    index_counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    call_stack.append((w, iter(adjacency.get(w, ()))))
                    advanced = True
                    break
                if on_stack.get(w, False) and index[w] < lowlink[v]:
                    lowlink[v] = index[w]
            if advanced:
                continue
            call_stack.pop()
            if call_stack:
                parent = call_stack[-1][0]
                if lowlink[v] < lowlink[parent]:
                    lowlink[parent] = lowlink[v]
            if lowlink[v] == index[v]:
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc_id[w] = next_scc
                    if w == v:
                        break
                next_scc += 1

    return scc_id, next_scc


def blast_radius(reverse_adjacency: dict[str, list[str]]) -> dict[str, int]:
    """Transitive dependent count per node, excluding the node itself.

    Bits index SCCs, but the metric must count FILES -- an SCC holding three
    mutually-importing files contributes 3, not 1. Hence the size table.
    Omitting it silently reports blast radius 0 for every file in a cycle.
    """
    if not reverse_adjacency:
        return {}

    scc_id, scc_count = _scc_ids(reverse_adjacency)

    size = [0] * scc_count
    for node, sid in scc_id.items():
        size[sid] += 1

    scc_adj: list[set[int]] = [set() for _ in range(scc_count)]
    for u, deps in reverse_adjacency.items():
        su = scc_id[u]
        for v in deps:
            sv = scc_id[v]
            if su != sv:
                scc_adj[su].add(sv)

    reach = [0] * scc_count
    for sid in range(scc_count):
        bm = 1 << sid
        for child in scc_adj[sid]:
            bm |= reach[child]
        reach[sid] = bm

    total = [0] * scc_count
    for sid in range(scc_count):
        bm, t = reach[sid], 0
        while bm:
            low = bm & -bm
            t += size[low.bit_length() - 1]
            bm ^= low
        total[sid] = t

    return {node: total[scc_id[node]] - 1 for node in reverse_adjacency}


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
    heap: list[tuple[int, int, str, int, int, int]] = []
    counter = 0
    for path, radius in blast.items():
        c = churn.get(path, 0)
        x = cc.get(path, 0)
        score = (radius + 1) * (c + 1) * (x + 1)
        item = (score, counter, path, radius, c, x)
        counter += 1
        if len(heap) < top:
            heapq.heappush(heap, item)
        elif top and score > heap[0][0]:
            heapq.heapreplace(heap, item)
    heap.sort(key=lambda t: t[0], reverse=True)
    return [
        {"path": path, "blast_radius": radius, "churn": c, "cc": x, "risk_score": score}
        for score, _, path, radius, c, x in heap
    ]


@lru_cache(maxsize=32)
def _sorted_scores(scores_key: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(sorted(scores_key))


def risk_level(score: int, scores: list[int]) -> str:
    """Percentile banding against the repo's own distribution."""
    if not scores:
        return "low"
    ordered = _sorted_scores(tuple(scores))
    rank = bisect_right(ordered, score) / len(ordered)
    if rank >= 0.9:
        return "high"
    if rank >= 0.7:
        return "medium"
    return "low"
