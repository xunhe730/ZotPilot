"""Vision table extraction utilities: response parsing, rendering, and context building."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .captions import DetectedCaption

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AgentResponse:
    """Parsed output from a vision transcription agent."""

    headers: list[str]
    rows: list[list[str]]
    footnotes: str
    table_label: str | None        # "Table 1", "Table A.1", etc.
    caption: str                   # Full caption text from image
    is_incomplete: bool            # Table extends beyond crop
    incomplete_reason: str         # Which edge(s) are cut off
    raw_shape: tuple[int, int]     # (num_data_rows, num_cols)
    parse_success: bool            # Whether JSON parsing succeeded
    raw_response: str              # Original response text (for debug DB)
    recrop_needed: bool            # Model requests a tighter crop
    recrop_bbox_pct: list[float] | None  # [x0, y0, x1, y1] 0-100 pct


# ---------------------------------------------------------------------------
# Worked examples (shared across all prompt templates)
# ---------------------------------------------------------------------------

EXTRACTION_EXAMPLES = """\
## Worked Examples

### Example A — Simple numeric table

Image shows:

  | Treatment | N   | Mean  | SD   | p      |
  |-----------|-----|-------|------|--------|
  | Drug A    | 124 | 45.2  | 12.3 | —      |
  | Drug B    | 131 | 41.8  | 11.9 | 0.042  |
  | Placebo   | 128 | 50.1  | 13.7 | <0.001 |

  * p-values from two-sided t-test vs. Drug A

TRANSCRIBER output:
{
  "table_label": "Table 2",
  "caption": "",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["Treatment", "N", "Mean", "SD", "p"],
  "rows": [
    ["Drug A", "124", "45.2", "12.3", "—"],
    ["Drug B", "131", "41.8", "11.9", "0.042"],
    ["Placebo", "128", "50.1", "13.7", "<0.001"]
  ],
  "footnotes": "* p-values from two-sided t-test vs. Drug A"
}

### Example B — Table with inline section headers and significance markers

Image shows:

  Table 3. Results by subgroup

  | Variable   | Coeff.     | SE    | p         |
  |------------|------------|-------|-----------|
  | Males      |            |       |           |
  |   Age      | −0.12*     | 0.05  | 0.018     |
  |   BMI      | 0.34**     | 0.11  | <0.001    |
  | Females    |            |       |           |
  |   Age      | −0.08      | 0.06  | 0.182     |
  |   BMI      | 0.29*      | 0.12  | 0.015     |

  * p < 0.05; ** p < 0.01

TRANSCRIBER output:
{
  "table_label": "Table 3",
  "caption": "Table 3. Results by subgroup",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["Variable", "Coeff.", "SE", "p"],
  "rows": [
    ["Males", "", "", ""],
    ["Age", "−0.12^{*}", "0.05", "0.018"],
    ["BMI", "0.34^{**}", "0.11", "<0.001"],
    ["Females", "", "", ""],
    ["Age", "−0.08", "0.06", "0.182"],
    ["BMI", "0.29^{*}", "0.12", "0.015"]
  ],
  "footnotes": "* p < 0.05; ** p < 0.01"
}

Key points:
• "Males" and "Females" are inline section headers → own rows with "" in data cols.
• Significance stars rendered as LaTeX superscripts: "−0.12^{*}", "0.34^{**}".
• Minus sign is Unicode − (U+2212), not ASCII hyphen.

### Example C — Multi-level headers with subscripts

Image shows a table whose headers span two levels:

  |          | Baseline       | Follow-up      |
  | Group    | Mean   | SD    | Mean   | SD    |
  |----------|--------|-------|--------|-------|
  | Control  | 72.4   | 8.1   | 71.9   | 8.3   |
  | Treated  | 73.1   | 7.9   | 68.2   | 7.4   |

Flattened headers: ["Group", "Baseline / Mean", "Baseline / SD", \
"Follow-up / Mean", "Follow-up / SD"].

TRANSCRIBER output:
{
  "table_label": null,
  "caption": "",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["Group", "Baseline / Mean", "Baseline / SD", \
"Follow-up / Mean", "Follow-up / SD"],
  "rows": [
    ["Control", "72.4", "8.1", "71.9", "8.3"],
    ["Treated", "73.1", "7.9", "68.2", "7.4"]
  ],
  "footnotes": ""
}

### Example D — Parenthetical standard errors and confidence intervals

Image shows a regression table (very common in economics/medical papers):

  Table 5. Regression results
  | Variable       | Model 1           | Model 2           |
  |----------------|-------------------|-------------------|
  | Age            | 0.034**           | 0.029*            |
  |                | (0.012)           | (0.013)           |
  | Income (log)   | 1.24***           | 1.18***           |
  |                | (0.31)            | (0.33)            |
  | Education      |                   | 0.087             |
  |                |                   | (0.054)           |
  | Observations   | 1,245             | 1,245             |
  | R²             | 0.34              | 0.37              |

TRANSCRIBER output:
{
  "table_label": "Table 5",
  "caption": "Table 5. Regression results",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["Variable", "Model 1", "Model 2"],
  "rows": [
    ["Age", "0.034^{**} \\n (0.012)", "0.029^{*} \\n (0.013)"],
    ["Income (log)", "1.24^{***} \\n (0.31)", "1.18^{***} \\n (0.33)"],
    ["Education", "", "0.087 \\n (0.054)"],
    ["Observations", "1,245", "1,245"],
    ["R^{2}", "0.34", "0.37"]
  ],
  "footnotes": ""
}

Key points:
• Standard errors in parentheses are MERGED into the coefficient row with \
" \\n " as separator: "0.034^{**} \\n (0.012)".  They are NOT separate rows.
• Significance stars are superscripts: "0.034^{**}".
• "R²" uses LaTeX notation: "R^{2}".
• Comma-separated thousands: preserve as shown ("1,245" not "1245").
• Empty cells for missing regressors: Education × Model 1 → "".

### Example E — Hierarchical row stubs with indentation

Image shows:

  Table 4. Health outcomes
  | Outcome               | OR    | 95% CI          | p     |
  |-----------------------|-------|-----------------|-------|
  | Cardiovascular        |       |                 |       |
  |   Heart failure       | 1.42  | [1.12, 1.80]   | 0.004 |
  |   Stroke              | 1.18  | [0.91, 1.53]   | 0.21  |
  |   MI                  | 1.35  | [1.08, 1.69]   | 0.009 |
  | Metabolic             |       |                 |       |
  |   T2DM                | 2.14  | [1.67, 2.74]   | <0.001|
  |   Dyslipidemia        | 1.56  | [1.29, 1.89]   | <0.001|

TRANSCRIBER output:
{
  "table_label": "Table 4",
  "caption": "Table 4. Health outcomes",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["Outcome", "OR", "95% CI", "p"],
  "rows": [
    ["Cardiovascular", "", "", ""],
    ["Heart failure", "1.42", "[1.12, 1.80]", "0.004"],
    ["Stroke", "1.18", "[0.91, 1.53]", "0.21"],
    ["MI", "1.35", "[1.08, 1.69]", "0.009"],
    ["Metabolic", "", "", ""],
    ["T2DM", "2.14", "[1.67, 2.74]", "<0.001"],
    ["Dyslipidemia", "1.56", "[1.29, 1.89]", "<0.001"]
  ],
  "footnotes": ""
}

Key points:
• "Cardiovascular" and "Metabolic" are inline section headers → own rows, \
"" in all data columns.
• Indentation in the PDF image does NOT appear in JSON — row stubs are flat \
strings without leading spaces.
• Confidence intervals use brackets exactly as shown: "[1.12, 1.80]".  Do \
NOT convert to parentheses or strip brackets.
• "T2DM" and "MI" are abbreviations — preserve as shown, do not expand.

### Example F — Blank index column with merged header and frequency ratios

Image shows a table where column 0 has NO header text (blank), inline category \
headers group the rows, one column holds slash-separated frequency ratios, and \
a header ("95 % Confidence Limits") has space-separated values that might appear \
as two columns:

  Table 4. Odds ratios for association of age with polyp classification
  |                    | Histology Frequency | Odds  | 95 % Confidence |         |
  |                    | Multiple/Single     | Ratio | Limits          | p-value |
  |--------------------|---------------------|-------|-----------------|---------|
  | Females: Age       |                     |       |                 |         |
  |  50-<60 referent   | 755/1754            | 1.00  |                 |         |
  |  60-<70            | 659/1389            | 1.10  |    0.97 1.25    | 0.130   |
  |  70-<80            | 441/810             | 1.27  |    1.10 1.46    | 0.001   |
  | Males: Age         |                     |       |                 |         |
  |  50-<60 referent   | 1127/2192           | 1.00  |                 |         |
  |  60-<70            | 931/1657            | 1.09  |    0.98 1.22    | 0.106   |

TRANSCRIBER output:
{
  "table_label": "Table 4",
  "caption": "Table 4. Odds ratios for association of age with polyp classification",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["", "Histology Frequency Multiple/Single", "Odds Ratio", \
"95 % Confidence Limits", "p-value"],
  "rows": [
    ["Females: Age", "", "", "", ""],
    ["50- < 60 referent", "755/1754", "1.00", "", ""],
    ["60- < 70", "659/1389", "1.10", "0.97 1.25", "0.130"],
    ["70- < 80", "441/810", "1.27", "1.10 1.46", "0.001"],
    ["Males: Age", "", "", "", ""],
    ["50- < 60 referent", "1127/2192", "1.00", "", ""],
    ["60- < 70", "931/1657", "1.09", "0.98 1.22", "0.106"]
  ],
  "footnotes": ""
}

Key points:
• Column 0 header is "" (blank) — this is CORRECT.  The column holds row \
labels and inline category headers.  Do NOT omit it.
• "Histology Frequency Multiple/Single" is ONE column with slash-separated \
frequency ratios like "755/1754".  Do NOT split this into two columns.
• "95 % Confidence Limits" is a single column with space-separated values.  \
Do NOT split this into multiple columns just because there is a space in the
data - the column header should make sense as a unit "95% Confidence Limits is
more sensible than "95% Limits" and "Confidence" as separate columns with similar
numbers.
• "Females: Age" and "Males: Age" are inline section headers — own rows \
with "" in all data columns.
• Referent rows ("50- < 60 referent") have no CI or p-value — those cells \
are "" (empty), NOT omitted.
• ALL 5 columns must appear in every row.  A common error is to drop the \
"Histology Frequency Multiple/Single" column entirely because the blank \
column-0 header confuses the column count."""


# ---------------------------------------------------------------------------
# Single-agent system prompt
# ---------------------------------------------------------------------------

_PROMPT_BODY = """\
## Role

You are a table transcription agent. Given PNG image(s) of a table region \
from an academic paper, plus raw text extracted from the PDF text layer, \
extract the table into structured JSON.

Your single output is a JSON object matching the schema at the end of this \
prompt. Do not add commentary, prose, or extra keys.

---

## Input Format

You receive:

1. **One or more PNG images** of the table region. When there are multiple \
images, they are overlapping vertical strips of the same table, ordered \
top-to-bottom, with approximately 15 % overlap between adjacent strips. \
Deduplicate rows that appear near strip boundaries (a row visible at the \
bottom of strip N and the top of strip N+1 is the SAME row — include it \
once).

2. **Raw extracted text** from the PDF text layer. This is provided as a \
cross-check only: use it to verify numbers and Latin words, but trust the \
image for structure, layout, column boundaries, and special characters \
(Greek letters, mathematical symbols, Unicode minus signs, etc.).

3. **Caption text** that triggered this crop. The caption was extracted from \
the PDF text layer and may be garbled or incomplete. Read the actual caption \
from the image and return the corrected version.

---

## Caption Verification

Read the actual caption from the image.

- Return the full corrected caption text in `caption` (e.g. \
"Table 3. Results by subgroup and sex").
- Return just the label portion in `table_label` (e.g. "Table 3", \
"Table A.1"). If the label uses a letter suffix such as "Table A", \
include it.
- If no caption is visible in the image, return `table_label: null` \
and `caption: ""`.

The provided caption is a hint, not ground truth. Always prefer what is \
actually visible in the image.

---

## Formatting Standards

Apply these formatting rules to all extracted cell values:

**Significance markers** — render as LaTeX superscripts: `^{*}`, `^{**}`, \
`^{***}`. Examples: `"0.034^{**}"`, `"1.24^{***}"`.

**Negative numbers** — use the Unicode minus sign U+2212 (−), not the ASCII \
hyphen-minus (-). The image always shows a longer dash for negatives.

**Multi-level headers** — flatten with " / " as separator. Example: a \
two-level header where the parent is "Baseline" and the child is "Mean" \
becomes `"Baseline / Mean"`.

**Inline section headers** — rows that span the full table width (e.g. \
"Males", "Panel A: Females") get their own row with `""` in every data \
column. Do not merge them into adjacent data rows.

**Standard errors in parentheses** — merge the SE row into the coefficient \
row using `" \\n "` as separator. Example: coefficient `0.034^{**}` with \
SE `(0.012)` on the next line → `"0.034^{**} \\n (0.012)"`. The SE is NOT \
a separate row.

**Confidence intervals** — preserve brackets exactly as shown. `[1.12, 1.80]` \
stays `"[1.12, 1.80]"`. Do not convert brackets to parentheses.

**Comma-separated thousands** — preserve as shown. `"1,245"` not `"1245"`.

**Empty cells** — use `""` (empty string). Never use `null`, `"-"`, or \
`"N/A"` unless the image explicitly shows those characters.

**Special symbols** — R² → `"R^{2}"`, β → `"\\beta"`, α → `"\\alpha"`. \
Preserve all other LaTeX-style notation visible in the image.

---

## Pitfall Warnings

These are common extraction errors. Avoid them:

- **Do NOT split columns on spaces in data.** A column whose header is \
"95 % Confidence Limits" is ONE column even when its cells contain \
space-separated values like `"0.97 1.25"`. Split decisions must be based \
on the column header structure, not on spaces within cell data.

- **Do NOT split slash-separated ratios.** `"755/1754"` is a single cell \
value, not two columns.

- **Do NOT drop columns with blank headers.** A column whose header cell is \
empty (`""`) is still a real column and must appear in `headers` and in \
every row.

- **Do NOT expand abbreviations.** `"T2DM"`, `"MI"`, `"BMI"` — transcribe \
exactly as shown. Never spell out.

- **Row count** — `rows` contains only data rows. Exclude header rows from \
the row count.

- **Cell count** — every row must have exactly N cells where \
N == len(headers). If a row appears to have fewer cells (e.g. an inline \
section header), pad with `""`.

- **Multi-strip deduplication** — when multiple strip images are provided, \
scan the boundary between strip N and strip N+1. Any row that appears \
in the bottom portion of strip N AND the top portion of strip N+1 is the \
same physical row. Include it exactly once.

---

## Re-Crop Instructions

After transcribing the table, assess whether the crop is adequate:

- **Table extends below the crop boundary**: the table has more rows than \
visible in the image (you can tell because the bottom border of the table \
is cut off, or the last row is clearly a mid-table row with no closing \
horizontal rule).
- **Crop includes too much non-table content**: substantial amounts of \
body text, other tables, or figures are visible above or below the table.

If either condition is true, set `recrop.needed = true` and provide \
`recrop.bbox_pct` as `[x0_pct, y0_pct, x1_pct, y1_pct]` where each \
value is 0–100, measured relative to the full visible region (all strips \
combined, top-left origin). The re-crop coordinates define the tightest \
bounding box that contains just the table (including its caption and \
footnotes).

If the crop is adequate, set `recrop.needed = false` and omit `bbox_pct` \
(or set it to `[0, 0, 100, 100]`).

---

## Output Schema

Return exactly this JSON structure and no other text:

```json
{
  "table_label": "<'Table N' or null if no label visible>",
  "caption": "<full caption text as read from image, or empty string>",
  "is_incomplete": false,
  "incomplete_reason": "",
  "headers": ["col1", "col2", "..."],
  "rows": [["r1c1", "r1c2", "..."], ["..."]],
  "footnotes": "<footnote text below the table, or empty string>",
  "recrop": {
    "needed": false,
    "bbox_pct": [0, 0, 100, 100]
  }
}
```

Field definitions:

- `table_label`: the label only (e.g. `"Table 3"`), or `null` if not visible.
- `caption`: the full caption sentence(s) as visible in the image, \
including the label. Empty string if not visible.
- `is_incomplete`: `true` if the table is cut off and rows are missing.
- `incomplete_reason`: which edge(s) are cut off, e.g. \
`"bottom edge cut off"`. Empty string when `is_incomplete` is false.
- `headers`: flat list of column header strings (multi-level headers \
flattened with " / ").
- `rows`: list of data rows; each row is a list of cell strings with \
the same length as `headers`.
- `footnotes`: any footnote or note text appearing below the table body. \
Empty string if none.
- `recrop.needed`: whether a tighter crop is needed.
- `recrop.bbox_pct`: re-crop coordinates as `[x0, y0, x1, y1]` in 0–100 \
percentages. Omit or set to `[0, 0, 100, 100]` when `needed` is false.\
"""

VISION_FIRST_SYSTEM = _PROMPT_BODY + "\n\n" + EXTRACTION_EXAMPLES


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------


def _parse_agent_json(raw_text: str) -> dict | None:
    """Parse agent JSON response, stripping code fences and stray text.

    Returns parsed dict or None on failure.
    """
    text = raw_text.strip()

    # Strip ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: find first { ... } block via regex (handles leading/trailing noise)
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


def parse_agent_response(raw_text: str, agent_label: str) -> "AgentResponse":
    """Parse raw API response text into AgentResponse.

    Public wrapper around _parse_agent_json that validates headers/rows are lists
    and builds a fully populated AgentResponse.
    """
    _failure = AgentResponse(
        headers=[], rows=[], footnotes="",
        table_label=None, caption="", is_incomplete=False,
        incomplete_reason="", raw_shape=(0, 0),
        parse_success=False, raw_response=raw_text,
        recrop_needed=False, recrop_bbox_pct=None,
    )

    parsed = _parse_agent_json(raw_text)
    if parsed is None:
        logger.warning("%s failed to parse JSON", agent_label)
        return _failure

    try:
        headers = parsed["headers"]
        rows = parsed["rows"]
        if not isinstance(headers, list):
            raise ValueError("headers must be a list")
        if not isinstance(rows, list):
            raise ValueError("rows must be a list")
        for r in rows:
            if not isinstance(r, list):
                raise ValueError("each row must be a list")

        table_label = parsed.get("table_label")
        caption = str(parsed.get("caption", ""))

        recrop_dict = parsed.get("recrop", {})
        recrop_needed = bool(recrop_dict.get("needed", False))
        raw_bbox_pct = recrop_dict.get("bbox_pct")
        if isinstance(raw_bbox_pct, list) and len(raw_bbox_pct) == 4 and all(
            isinstance(v, (int, float)) for v in raw_bbox_pct
        ):
            recrop_bbox_pct: list[float] | None = [float(v) for v in raw_bbox_pct]
        else:
            recrop_bbox_pct = None

        return AgentResponse(
            headers=[str(h) for h in headers],
            rows=[[str(c) for c in row] for row in rows],
            footnotes=str(parsed.get("footnotes", "")),
            table_label=table_label if isinstance(table_label, str) else None,
            caption=caption,
            is_incomplete=bool(parsed.get("is_incomplete", False)),
            incomplete_reason=str(parsed.get("incomplete_reason", "")),
            raw_shape=(len(rows), len(headers)),
            parse_success=True,
            raw_response=raw_text,
            recrop_needed=recrop_needed,
            recrop_bbox_pct=recrop_bbox_pct,
        )

    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("%s response validation failed: %s", agent_label, exc)
        return _failure


# ---------------------------------------------------------------------------
# Garbled encoding detection
# ---------------------------------------------------------------------------

_GARBLE_WARNING = (
    "⚠ WARNING: The raw text below may have GARBLED SYMBOL ENCODING. "
    "This PDF uses fonts that map display glyphs to wrong Unicode codepoints "
    "(e.g. Greek letters Ω, Ψ, Λ rendered as V, C, L in the text layer; "
    "parentheses as ð/Þ; arrows as !; set membership as [). "
    "For NUMBERS, DIGITS, and LATIN WORDS the raw text is still reliable. "
    "For GREEK LETTERS, MATHEMATICAL SYMBOLS, and SPECIAL CHARACTERS, "
    "TRUST THE IMAGE over the raw text."
)


def build_common_ctx(raw_text: str, caption: str | None, garbled: bool = False) -> str:
    """Build the common context block shared by all agents for a table.

    Prepends a garble warning when *garbled* is True so agents trust the
    image for symbols while still using raw text for numbers/digits.
    """
    parts: list[str] = []
    if garbled:
        parts.append(_GARBLE_WARNING)
    parts.append(f"## Raw extracted text\n\n{raw_text}")
    if caption:
        parts.append(f"## Caption\n\n{caption}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def compute_all_crops(
    page: pymupdf.Page,
    captions: list[DetectedCaption],
    *,
    caption_type: str = "table",
) -> list[tuple[DetectedCaption, tuple[float, float, float, float]]]:
    """Compute crop bboxes for captions of the given type.

    For each caption matching caption_type, the crop region is:
    - Top: caption.bbox[1] (include caption in crop)
    - Bottom: next caption's bbox[1] (any type), or page.rect.y1
    - Left: page.rect.x0
    - Right: page.rect.x1

    Args:
        page: PyMuPDF page object.
        captions: All detected captions on this page (sorted by y_center).
        caption_type: Which caption type to compute crops for ("table" or "figure").

    Returns:
        List of (caption, crop_bbox) tuples for matching captions only.
    """
    results: list[tuple[DetectedCaption, tuple[float, float, float, float]]] = []
    page_x0 = page.rect.x0
    page_x1 = page.rect.x1
    page_y1 = page.rect.y1

    for i, cap in enumerate(captions):
        if cap.caption_type != caption_type:
            continue

        top = cap.bbox[1]

        # Bottom boundary: next caption of any type, or page bottom
        if i + 1 < len(captions):
            bottom = captions[i + 1].bbox[1]
        else:
            bottom = page_y1

        if bottom <= top:
            continue

        results.append((cap, (page_x0, top, page_x1, bottom)))

    return results


def _split_into_strips(
    bbox: tuple[float, float, float, float],
    overlap_frac: float = 0.15,
) -> list[tuple[float, float, float, float]]:
    """Split a tall bbox into overlapping horizontal strips.

    Each strip height equals the crop width (making it square), so
    width becomes the long edge after API resize. Adjacent strips
    overlap by overlap_frac of the strip height.

    Returns list of strip bboxes, ordered top-to-bottom.
    """
    x0, y0, x1, y1 = bbox
    crop_width = x1 - x0
    crop_height = y1 - y0

    strip_height_pt = crop_width
    overlap_pt = strip_height_pt * overlap_frac
    step_pt = strip_height_pt - overlap_pt

    # If the crop is shorter than one strip, return it unchanged
    if crop_height <= strip_height_pt:
        return [(x0, y0, x1, y1)]

    strips: list[tuple[float, float, float, float]] = []
    strip_top = y0
    while strip_top < y1:
        strip_bottom = min(strip_top + strip_height_pt, y1)
        strips.append((x0, strip_top, x1, strip_bottom))
        if strip_bottom >= y1:
            break
        strip_top += step_pt

    return strips


def render_table_region(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
    *,
    dpi_floor: int = 150,
    dpi_cap: int = 300,
    strip_dpi_threshold: int = 200,
) -> list[tuple[bytes, str]]:
    """Render a table region as one or more PNGs.

    Returns list of (png_bytes, media_type). Usually 1 image; multiple
    when crop height > width and effective DPI < strip_dpi_threshold.

    The Anthropic API resizes images so the long edge is 1568px.
    When height is the long edge, effective DPI drops. Strips fix
    this by splitting tall crops so width becomes the long edge.

    Args:
        page: PyMuPDF page object.
        bbox: (x0, y0, x1, y1) crop region in PDF points.
        dpi_floor: Minimum render DPI.
        dpi_cap: Maximum render DPI.
        strip_dpi_threshold: Multi-strip trigger. If height > width
            and effective_dpi < this value, split into strips.
            Default 200 for initial crops. Pass 250 for re-crops.
    """
    x0, y0, x1, y1 = bbox
    width_in = (x1 - x0) / 72
    height_in = (y1 - y0) / 72
    long_edge_in = max(width_in, height_in)
    effective_dpi = 1568 / long_edge_in

    if height_in > width_in and effective_dpi < strip_dpi_threshold:
        sub_bboxes = _split_into_strips(bbox)
        results: list[tuple[bytes, str]] = []
        for sx0, sy0, sx1, sy1 in sub_bboxes:
            strip_width_in = (sx1 - sx0) / 72
            optimal_dpi = max(dpi_floor, min(dpi_cap, int(1568 / strip_width_in)))
            clip = pymupdf.Rect(sx0, sy0, sx1, sy1)
            mat = pymupdf.Matrix(optimal_dpi / 72, optimal_dpi / 72)
            pix = page.get_pixmap(matrix=mat, clip=clip)
            results.append((pix.tobytes("png"), "image/png"))
        return results
    else:
        optimal_dpi = max(dpi_floor, min(dpi_cap, int(1568 / long_edge_in)))
        clip = pymupdf.Rect(x0, y0, x1, y1)
        mat = pymupdf.Matrix(optimal_dpi / 72, optimal_dpi / 72)
        pix = page.get_pixmap(matrix=mat, clip=clip)
        return [(pix.tobytes("png"), "image/png")]


def compute_recrop_bbox(
    original_bbox: tuple[float, float, float, float],
    bbox_pct: list[float],
) -> tuple[float, float, float, float]:
    """Convert re-crop percentages to absolute PDF coordinates.

    Args:
        original_bbox: The original crop region (x0, y0, x1, y1) in PDF points.
        bbox_pct: [x0_pct, y0_pct, x1_pct, y1_pct] where each value is 0-100,
            relative to the original crop dimensions.

    Returns:
        Absolute (x0, y0, x1, y1) in PDF points, clamped to original bbox.
    """
    ox0, oy0, ox1, oy1 = original_bbox
    w = ox1 - ox0
    h = oy1 - oy0

    def clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    x0 = ox0 + clamp(bbox_pct[0], 0.0, 100.0) / 100.0 * w
    y0 = oy0 + clamp(bbox_pct[1], 0.0, 100.0) / 100.0 * h
    x1 = ox0 + clamp(bbox_pct[2], 0.0, 100.0) / 100.0 * w
    y1 = oy0 + clamp(bbox_pct[3], 0.0, 100.0) / 100.0 * h

    return (x0, y0, x1, y1)
