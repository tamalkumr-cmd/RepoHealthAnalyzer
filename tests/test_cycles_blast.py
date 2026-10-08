"""Cycle detection (SRS 4.3.2.2) and blast radius (SRS 4.3.2.3)."""

from __future__ import annotations

import sys

import pytest

from repohealth.analysis.blast import (
    blast_radius,
    direct_dependents,
    fan_out,
    rank_fragile,
    risk_level,
)
from repohealth.analysis.cycles import files_in_cycles, find_cycles, format_cycle


# ---------------- cycles ----------------

def test_acyclic_graph_has_no_cycles():
    assert find_cycles({"a": ["b"], "b": ["c"], "c": []}) == []


def test_two_file_cycle_is_found():
    cycles = find_cycles({"a": ["b"], "b": ["a"]})
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b"}


def test_three_file_cycle_reports_the_concrete_chain():
    """A boolean 'a cycle exists' is not actionable. The developer needs the path."""
    cycles = find_cycles({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b", "c"}
    assert format_cycle(cycles[0]).endswith(cycles[0][0])


def test_self_loop_is_a_cycle():
    assert find_cycles({"a": ["a"]}) == [["a"]]


def test_two_independent_cycles_both_reported():
    cycles = find_cycles({"a": ["b"], "b": ["a"], "c": ["d"], "d": ["c"]})
    assert len(cycles) == 2


def test_same_cycle_reached_from_different_entry_points_is_reported_once():
    graph = {"entry1": ["a"], "entry2": ["b"], "a": ["b"], "b": ["a"]}
    assert len(find_cycles(graph)) == 1


def test_cycle_output_is_deterministic():
    graph = {"a": ["b"], "b": ["c"], "c": ["a"]}
    assert find_cycles(graph) == find_cycles(graph)


def test_edge_to_a_node_outside_the_graph_is_ignored():
    assert find_cycles({"a": ["missing"]}) == []


def test_deep_chain_does_not_exhaust_the_recursion_limit():
    """The DFS is iterative precisely so a long import chain cannot crash it.
    This chain is several times Python's default recursion limit."""
    n = sys.getrecursionlimit() * 3
    graph = {f"m{i}": [f"m{i+1}"] for i in range(n)}
    graph[f"m{n}"] = ["m0"]          # close the loop
    cycles = find_cycles(graph)
    assert len(cycles) == 1
    assert len(cycles[0]) == n + 1


def test_files_in_cycles_collects_every_participant():
    assert files_in_cycles([["a", "b"], ["c", "d", "e"]]) == {"a", "b", "c", "d", "e"}


# ---------------- blast radius ----------------

def test_blast_radius_counts_transitive_dependents():
    # c <- b <- a   (reverse adjacency: who imports me)
    rev = {"c": ["b"], "b": ["a"], "a": []}
    assert blast_radius(rev)["c"] == 2
    assert blast_radius(rev)["b"] == 1
    assert blast_radius(rev)["a"] == 0


def test_blast_radius_is_zero_for_an_isolated_file():
    assert blast_radius({"a": []})["a"] == 0


def test_files_inside_a_cycle_report_their_real_dependent_count():
    """Regression: the SCC optimisation indexed components, not files, so every
    file in a cycle reported blast radius 0 -- silently hiding exactly the files
    the tool exists to flag."""
    rev = {
        "a": ["c"], "b": ["a"], "c": ["b"],   # a <- c <- b <- a, a 3-file cycle
        "shared": ["a", "b", "c"],
    }
    out = blast_radius(rev)
    assert out["a"] == 2 and out["b"] == 2 and out["c"] == 2
    assert out["shared"] == 3


def test_blast_radius_matches_a_naive_bfs_reference():
    """The optimised implementation must agree with the obvious one."""
    from collections import deque

    rev = {
        "core": ["svc_a", "svc_b"],
        "svc_a": ["api"], "svc_b": ["api", "worker"],
        "api": [], "worker": ["api"],
    }

    def naive(radj):
        out = {}
        for node in radj:
            seen, q = {node}, deque(radj.get(node, []))
            while q:
                cur = q.popleft()
                if cur in seen:
                    continue
                seen.add(cur)
                q.extend(radj.get(cur, []))
            out[node] = len(seen) - 1
        return out

    assert blast_radius(rev) == naive(rev)


def test_empty_graph_returns_empty():
    assert blast_radius({}) == {}


def test_direct_dependents_and_fan_out():
    assert direct_dependents({"a": ["x", "y"], "x": [], "y": []})["a"] == 2
    assert fan_out({"a": ["x", "y"], "x": [], "y": []})["a"] == 2


# ---------------- risk ranking ----------------

def test_risk_is_multiplicative_so_one_axis_alone_does_not_rank():
    """A heavily depended-on file that never changes is not a risk."""
    blast = {"stable": 50, "hot": 2}
    churn = {"stable": 0, "hot": 30}
    cc = {"stable": 1, "hot": 40}
    ranked = rank_fragile(blast, churn, cc, top=2)
    assert ranked[0]["path"] == "hot"


def test_rank_fragile_respects_the_top_limit():
    blast = {f"f{i}": i for i in range(20)}
    assert len(rank_fragile(blast, {}, {}, top=5)) == 5


def test_uniform_scores_are_not_all_high_risk():
    """Regression: percentile banding alone put every tie in the top decile, so a
    perfectly healthy repository reported every file as high risk."""
    scores = [100] * 20
    assert {risk_level(s, scores) for s in scores} == {"low"}


def test_a_clear_outlier_is_high_risk():
    scores = [10] * 19 + [500]
    assert risk_level(500, scores) == "high"
    assert risk_level(10, scores) == "low"


def test_risk_level_on_empty_distribution():
    assert risk_level(5, []) == "low"
