"""Tests for formula display helpers."""

from zotpilot.formula_display import latex_to_display_math


def test_latex_to_display_math_wraps_formula():
    assert latex_to_display_math(r"\sigma = E\varepsilon") == "$$\n\\sigma = E\\varepsilon\n$$"


def test_latex_to_display_math_unwraps_display_math():
    assert latex_to_display_math(r"\[\eta = \sigma_h / \sigma_{VM}\]") == "$$\n\\eta = \\sigma_h / \\sigma_{VM}\n$$"


def test_latex_to_display_math_handles_empty_input():
    assert latex_to_display_math("") == ""
