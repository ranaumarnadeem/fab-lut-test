"""Graph helpers."""
from __future__ import annotations


def tarjan(nodes: list[str], succ) -> list[list[str]]:
    """Iterative Tarjan; returns SCCs."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succ(root)))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(succ(w))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


class LoopGraph:
    """Pure-Python twin of native LoopGraph (native/loops.cpp): node i depends on succ[i];
    set_succ changes one node, first_cycle returns the first loop SCC of tarjan (roots in
    index order), or []."""

    def __init__(self, succ: list[list[int]]):
        self.succ = [list(s) for s in succ]

    def set_succ(self, v: int, s: list[int]) -> None:
        self.succ[v] = list(s)

    def __len__(self) -> int:
        return len(self.succ)

    def first_cycle(self) -> list[int]:
        succ = self.succ
        for comp in tarjan(range(len(succ)), lambda n: succ[n]):
            if len(comp) > 1 or comp[0] in succ[comp[0]]:
                return comp
        return []


def loop_graph(succ: list[list[int]]):
    """The native LoopGraph when it builds, else the Python one."""
    from fabtest import native
    mod = native.load()
    return mod.LoopGraph(succ) if mod is not None else LoopGraph(succ)
