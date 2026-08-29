from __future__ import annotations
WHITE, GRAY, BLACK = 0, 1, 2
def find_cycles(adjacency: dict[str, list[str]]) -> list[list[str]]:
    color: dict[str, int] = {n: WHITE for n in adjacency}
    stack: list[str] = []
    stack_pos: dict[str, int] = {}
    found: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()
    def record(cycle: list[str]) -> None:
        sig = frozenset(cycle)
        if sig in seen_signatures:
            return
        seen_signatures.add(sig)
        pivot = cycle.index(min(cycle))
        found.append(cycle[pivot:] + cycle[:pivot])
    for start in adjacency:
        if color[start] != WHITE:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        stack_pos[start] = len(stack)
        stack.append(start)
        while work:
            node, i = work[-1]
            neighbours = adjacency.get(node, [])
            if i < len(neighbours):
                work[-1] = (node, i + 1)
                nxt = neighbours[i]
                if nxt not in color:
                    continue
                if color[nxt] == GRAY:
                    record(stack[stack_pos[nxt]:])
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack_pos[nxt] = len(stack)
                    stack.append(nxt)
                    work.append((nxt, 0))
            else:
                work.pop()
                color[node] = BLACK
                del stack_pos[node]
                stack.pop()
    return sorted(found, key=lambda c: (len(c), c))
def format_cycle(cycle: list[str]) -> str:
    return " -> ".join(cycle + [cycle[0]])
def files_in_cycles(cycles: list[list[str]]) -> set[str]:
    return {node for cycle in cycles for node in cycle}