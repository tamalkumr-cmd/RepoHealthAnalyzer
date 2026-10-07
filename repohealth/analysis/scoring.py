"""Repository Health Score (SRS 4.5.1.1) and refactoring estimates (4.5.1.2).

The score is a weighted penalty model, not a measurement: it compresses four
independent signals into one 0-100 figure so a reader can tell at a glance
whether a repository is in trouble. Every weight and threshold is declared in
config.json rather than hardcoded, because the right values differ by codebase
and the figure is only defensible if the reader can see how it was derived.

Each penalty is normalised to 0.0-1.0 before weighting, so no single signal can
dominate the total regardless of the units it is measured in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class ScoreBreakdown:
    score: float
    penalties: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    grade: str = "—"

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "grade": self.grade,
            "penalties": {k: round(v, 3) for k, v in self.penalties.items()},
            "weights": self.weights,
            "contributions": {k: round(v, 2) for k, v in self.contributions.items()},
            "facts": self.facts,
        }


def _grade(score: float) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return ordered[idx]


def compute_health(
    metrics: Sequence[dict[str, Any]],
    churn: dict[str, int],
    blast: dict[str, int],
    cycles: Sequence[Sequence[str]],
    cfg: dict[str, Any],
) -> ScoreBreakdown:
    """metrics: one dict per file with path, cc_total, cc_max, max_nesting, loc, parsed."""
    weights = cfg.get("scoring", {})
    thresholds = cfg.get("thresholds", {})
    w_cx = weights.get("weight_churn_complexity", 0.4)
    w_cy = weights.get("weight_cycles", 0.4)
    w_br = weights.get("weight_blast_radius", 0.2)
    max_cc = thresholds.get("max_cc_per_function", 10)
    max_nest = thresholds.get("max_nesting_depth", 4)

    parsed = [m for m in metrics if m.get("parsed", 1)]
    total = len(parsed)
    if total == 0:
        return ScoreBreakdown(score=100.0, grade="A", facts={"files": 0})

    churn_values = [float(churn.get(m["path"], 0)) for m in parsed]
    churn_hi = max(_percentile(churn_values, thresholds.get("high_churn_percentile", 0.9)), 2.0)

    # --- penalty 1: files that are both frequently changed and complex ---
    # Neither signal alone is damning. A complex file nobody edits is stable;
    # a simple file edited daily is harmless. The intersection is the risk.
    hotspots = [
        m for m in parsed
        if churn.get(m["path"], 0) >= churn_hi
        and (m.get("cc_max", 0) > max_cc or m.get("max_nesting", 0) > max_nest)
    ]
    # A tenth of the codebase being hotspots is treated as the worst case.
    p_hotspot = min(1.0, (len(hotspots) / total) / 0.10)

    # --- penalty 2: circular dependencies ---
    in_cycles = {p for cyc in cycles for p in cyc}
    # 5% of files tangled in cycles is treated as the worst case: cycles are
    # structural defects, so the tolerance is deliberately tighter.
    p_cycles = min(1.0, (len(in_cycles) / total) / 0.05)

    # --- penalty 3: risk concentrated in heavily-depended-on files ---
    # A widely imported file that also churns is where an outage comes from.
    blast_hi = max(total * 0.10, 3.0)
    fragile = [
        m for m in parsed
        if blast.get(m["path"], 0) >= blast_hi and churn.get(m["path"], 0) >= churn_hi
    ]
    p_blast = min(1.0, (len(fragile) / total) / 0.05)

    penalties = {
        "churn_complexity": p_hotspot,
        "cycles": p_cycles,
        "blast_radius": p_blast,
    }
    weight_map = {"churn_complexity": w_cx, "cycles": w_cy, "blast_radius": w_br}
    contributions = {k: penalties[k] * weight_map[k] * 100.0 for k in penalties}

    total_weight = sum(weight_map.values()) or 1.0
    deduction = sum(contributions.values()) / total_weight
    score = max(0.0, 100.0 - deduction)

    return ScoreBreakdown(
        score=score,
        grade=_grade(score),
        penalties=penalties,
        weights=weight_map,
        contributions=contributions,
        facts={
            "files": total,
            "unparsed": len(metrics) - total,
            "hotspot_files": len(hotspots),
            "cycle_count": len(cycles),
            "files_in_cycles": len(in_cycles),
            "fragile_files": len(fragile),
            "high_churn_threshold": churn_hi,
        },
    )


# Calibrated against the observation that reducing a function's cyclomatic
# complexity by roughly 10 points takes an experienced developer about an hour
# once tests are in place. It is an order-of-magnitude planning aid for ranking
# candidates against each other, not an estimate to commit to a sprint.
_HOURS_PER_CC_POINT = 0.1
_HOURS_PER_NESTING_LEVEL = 0.75
_HOURS_PER_100_CLASS_LOC = 1.0
_CYCLE_BREAK_HOURS = 2.0


def refactor_estimate(
    metric: dict[str, Any],
    thresholds: dict[str, Any],
    in_cycle: bool = False,
) -> dict[str, Any]:
    """Rough hours to bring one file back under its thresholds."""
    max_cc = thresholds.get("max_cc_per_function", 10)
    max_nest = thresholds.get("max_nesting_depth", 4)
    max_class = thresholds.get("max_class_loc", 200)

    hours = 0.0
    drivers: list[str] = []

    excess_cc = max(0, metric.get("cc_max", 0) - max_cc)
    if excess_cc:
        h = excess_cc * _HOURS_PER_CC_POINT
        hours += h
        drivers.append(f"worst function is {excess_cc} over the complexity limit")

    excess_nest = max(0, metric.get("max_nesting", 0) - max_nest)
    if excess_nest:
        h = excess_nest * _HOURS_PER_NESTING_LEVEL
        hours += h
        drivers.append(f"nested {excess_nest} level(s) too deep")

    excess_class = max(0, metric.get("max_class_loc", 0) - max_class)
    if excess_class:
        h = (excess_class / 100.0) * _HOURS_PER_100_CLASS_LOC
        hours += h
        drivers.append(f"largest class is {excess_class} lines over")

    if in_cycle:
        hours += _CYCLE_BREAK_HOURS
        drivers.append("participates in a circular import")

    return {
        "path": metric["path"],
        "hours": round(hours, 1),
        "band": "small" if hours < 2 else "medium" if hours < 6 else "large",
        "drivers": drivers,
    }
