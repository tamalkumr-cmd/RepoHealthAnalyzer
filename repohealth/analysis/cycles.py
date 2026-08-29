"""Circular dependency detection (SRS 4.3.2.2).

Iterative DFS with three-colour marking:
    WHITE = unvisited, GRAY = on the current recursion stack, BLACK = finished.
An edge into a GRAY node is a back edge, and the cycle is the slice of the
stack from that node onward. Iterative rather than recursive so a deep import
chain cannot blow the Python recursion limit.
"""

from __future__ import annotations

WHITE, GRAY, BLACK = 0, 1, 2


def find_cycles(adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Returns each cycle as a list of paths, e.g. ['a.py', 'b.py', 'c.py']
    meaning a -> b -> c -> a. Duplicates (same cycle, different entry point)
    are collapsed."""
    color: dict[str, int] = {n: WHITE for n in adjacency}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()

    def record(cycle: list[str]) -> None:
        sig = frozenset(cycle)
        if sig in seen_signatures:
            return
        seen_signatures.add(sig)
        # rotate so the alphabetically first node leads -- stable output
        pivot = cycle.index(min(cycle))
        found.append(cycle[pivot:] + cycle[:pivot])

    for start in adjacency:
        if color[start] != WHITE:
            continue

        # each frame: (node, iterator index into its neighbour list)
        work: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        stack.append(start)
        on_stack.add(start)

        while work:
            node, i = work[-1]
            neighbours = adjacency.get(node, [])

            if i < len(neighbours):
                work[-1] = (node, i + 1)
                nxt = neighbours[i]

                if nxt not in color:
                    continue  # edge to a node outside the graph
                if color[nxt] == GRAY:
                    idx = stack.index(nxt)
                    record(stack[idx:])
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, 0))
            else:
                work.pop()
                color[node] = BLACK
                stack.pop()
                on_stack.discard(node)

    return sorted(found, key=lambda c: (len(c), c))


def format_cycle(cycle: list[str]) -> str:
    """['a.py','b.py'] -> 'a.py -> b.py -> a.py'"""
    return " -> ".join(cycle + [cycle[0]])


def files_in_cycles(cycles: list[list[str]]) -> set[str]:
    return {node for cycle in cycles for node in cycle}
