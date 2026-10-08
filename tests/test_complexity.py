"""Static analysis & complexity engine (SRS 4.2)."""

from __future__ import annotations

import pytest

from repohealth.analysis.complexity import analyze_source

TH = {"max_cc_per_function": 10, "max_nesting_depth": 4, "max_class_loc": 200}


def test_trivial_function_has_complexity_one():
    m = analyze_source("a.py", "def f():\n    return 1\n", TH)
    assert m.parsed
    assert m.cc_total == 1
    assert m.num_functions == 1


def test_each_branch_adds_complexity():
    src = (
        "def f(x):\n"
        "    if x > 1: return 1\n"
        "    if x > 2: return 2\n"
        "    if x > 3: return 3\n"
        "    return 0\n"
    )
    assert analyze_source("a.py", src, TH).cc_total == 4


def test_nesting_depth_counts_stacked_blocks():
    src = (
        "def f(items, flag):\n"
        "    for a in items:\n"
        "        if flag:\n"
        "            while a:\n"
        "                try:\n"
        "                    a -= 1\n"
        "                except ValueError:\n"
        "                    pass\n"
    )
    assert analyze_source("a.py", src, TH).max_nesting == 4


def test_nesting_resets_at_each_function_boundary():
    """A method's depth must reflect its own structure, not its position.

    Without the reset, an ordinary method inside a class inside a `with` would
    report depth 3 before writing a single branch, and every file in a package
    would look deeply nested.
    """
    flat = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    nested_position = (
        "class A:\n"
        "    def m(self, x):\n"
        "        if x:\n"
        "            return 1\n"
        "        return 0\n"
    )
    assert analyze_source("a.py", flat, TH).max_nesting == 1
    assert analyze_source("b.py", nested_position, TH).max_nesting == 1


def test_same_complexity_different_nesting_is_distinguished():
    """Radon alone cannot separate these; the AST visitor is why both are measured."""
    flat = (
        "def f(x):\n"
        "    if x is None: return False\n"
        "    if x < 0: return False\n"
        "    if x > 100: return False\n"
        "    return True\n"
    )
    deep = (
        "def f(x):\n"
        "    if x is not None:\n"
        "        if x >= 0:\n"
        "            if x <= 100:\n"
        "                return True\n"
        "    return False\n"
    )
    a, b = analyze_source("a.py", flat, TH), analyze_source("b.py", deep, TH)
    assert a.cc_total == b.cc_total       # same decision count
    assert a.max_nesting < b.max_nesting  # different readability


def test_class_span_is_measured():
    src = "class Big:\n" + "".join(f"    def m{i}(self): pass\n" for i in range(50))
    m = analyze_source("a.py", src, TH)
    assert m.num_classes == 1
    assert m.max_class_loc == 51


def test_syntax_error_is_recorded_not_raised():
    """SRS 5.4.2 -- half-typed code is routine when a pre-commit hook fires."""
    m = analyze_source("broken.py", "def f(\n", TH)
    assert m.parsed is False
    assert "SyntaxError" in m.parse_error
    assert m.cc_total == 0


def test_empty_file_is_valid():
    m = analyze_source("a.py", "", TH)
    assert m.parsed
    assert m.cc_total == 0 and m.loc == 0


def test_hotspots_name_the_offending_construct():
    src = "def f(x):\n" + "".join(f"    if x == {i}: return {i}\n" for i in range(15))
    hotspots = analyze_source("a.py", src, TH).hotspots
    assert any(h["kind"] == "complexity" and h["name"] == "f" for h in hotspots)
    assert all(h["line"] > 0 for h in hotspots if h["kind"] == "complexity")


def test_async_functions_are_counted():
    src = "async def f(x):\n    if x:\n        return 1\n    return 0\n"
    m = analyze_source("a.py", src, TH)
    assert m.num_functions == 1 and m.max_nesting == 1


def test_unicode_source_does_not_break_parsing():
    m = analyze_source("a.py", "def f():\n    return 'café ☕'\n", TH)
    assert m.parsed
