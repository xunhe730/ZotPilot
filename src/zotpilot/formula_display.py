"""Display helpers for recognized LaTeX formulas."""
from __future__ import annotations


def latex_to_display_math(latex: str) -> str:
    """Wrap recognized LaTeX as Markdown display math for UI/chat rendering."""
    body = (latex or "").strip()
    if not body:
        return ""
    body = body.strip("$")
    if body.startswith(r"\[") and body.endswith(r"\]"):
        body = body[2:-2].strip()
    return f"$$\n{body}\n$$"
