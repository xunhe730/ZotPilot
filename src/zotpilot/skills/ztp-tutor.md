---
name: ztp-tutor
description: >
  Deep reading guide for a single paper already in the Zotero library, tailored
  to the reader's persona (purpose, background, language). Writes 5-dimension
  color highlights with per-sentence understanding comments
  in the reader's language (Chinese by default), figure/table/equation
  annotations, and a page-1 argument-structure
  overview directly into the Zotero-stored PDF. Original PDF is always
  backed up to a .ztpbak sidecar before any write.
  Trigger on: "论文导读", "/ztp-tutor", "帮我导读", "五维导读",
  "deep reading guide", "tutoring this paper", "给这篇论文做导读",
  "annotate this paper for reading", "帮我精读", "论文精读",
  "reading guide for", "导读一下", "帮我读这篇", "批注这篇论文",
  "阅读引导", "reading assistant", "paper walkthrough", "guided reading".
  For finding and ingesting new papers, use ztp-research instead.
  For synthesizing multiple papers already in the library, use ztp-review instead.
---
# Deep Reading Guide (五维导读)

## Language Policy

Two independent languages:

- **Interface language** — detect from the triggering message; use it for every
  user-facing chat message.
- **Annotation language** — the language of the in-PDF `comment` fields AND the
  page-1 overview. It follows the reader's persona (Step 2a): use the reader's
  stated native / preferred annotation language. When the persona indicates no
  language, default to Chinese.

The annotation language is independent of the paper's language — the goal is to
explain a (often foreign-language) paper in the reader's own language. Carry the
chosen language on the overview via its `lang` field (`"zh"`, `"en"`, …) so the
page-1 overview labels render in the same language (Step 5).

---

## Step 1 — Resolve the paper

Call `get_paper_for_tutor(title_or_doc_id)` with the title or item key the
user provided.

**If the response contains `needs_disambiguation: true`**, the tool found
multiple candidates. Present them as a numbered list showing `doc_id`,
`title`, `authors`, and `year`. Then ask exactly one question and stop:

> 找到多篇匹配的论文，请告诉我你想导读哪一篇（回复编号）：
>
> 1. [doc_id] 《title》— authors (year)
> 2. …

Wait for the user to pick a single item, then call `get_paper_for_tutor`
again with the selected `doc_id`. Do not proceed until exactly one paper is
confirmed.

**If the tool raises a ToolError** mentioning "no text layer" or "scanned",
tell the user that this PDF has no embedded text layer and OCR is needed
before the reading guide can be written. Stop here.

**If the tool raises any other ToolError**, surface the message verbatim and
stop.

---

## Step 2 — Read the persona and existing annotations

The response from Step 1 carries two personalization inputs:

### 2a. Persona (`persona: string | null`)

The raw text of the `## 阅读画像 (Reading Persona)` section from
`~/.config/zotpilot/ZOTPILOT.md`, or `null` if absent.

**Parse by intent-matching, not literal enum.** The persona is stored as
labeled lines (`英文水平：…`, `领域熟悉度：…`, …), so match each axis by its
LABEL first: a bare token like `入门` means English-beginner under `英文水平`
but field-novice under `领域熟悉度` — never let one token set an axis it was not
labeled for. Match leniently within each labeled value:

**English proficiency — gates the term + long-sentence layer:**
Match any of: `英文弱`, `英文不好`, `英文一般`, `入门`, `中等`, `poor`,
`intermediate`, `beginner`, `basic`, `不太好`, `一般般`.
If any such pattern appears, treat English proficiency as "weak" and ENABLE
the term and long-sentence annotation layers.
If the raw text says `高级`, `expert`, `advanced`, `proficient`, or similar
strength signals, treat English proficiency as "strong" and SUPPRESS those
layers entirely.
When no match is found, default to "moderate" and SUPPRESS those layers
(conservative default: do not add extra layers unless clearly warranted).

**Reading depth — gates annotation density:**
- `速览` / `quick read` / `overview` / `skim` → sparse: thesis claim + key
  figures + 1–2 annotations per present dimension.
- `技术细节` / `technical` / `detailed` / `in-depth` / `深度` → standard:
  fuller coverage of method, evidence, and proof steps.
- `全面综述` / `comprehensive` / `thorough` / `全面` / `详尽` → maximal:
  annotate every independent understanding point within the hard caps.

Density is set by reading depth ALONE — domain familiarity changes how much
background each comment carries, not how many there are. If `速览` collides with
the weak-English term / long-sentence layers, stay within the sparse budget and
keep only the most essential helps.

When depth cannot be recognized, default to `速览`.

**Annotation language — sets the language of every in-PDF comment + overview:**
Match a stated native / preferred language, e.g. `母语：英文`, `批注语言：中文`,
`用英文批注`, `native language: English`, `annotate in English`. Use that
language for all `comment` fields and the page-1 overview, and set the overview
`lang` accordingly (Step 5). When no language is stated, default to Chinese
(`zh`). This is independent of English proficiency — a reader may want English
annotations yet still need the term / long-sentence layers, or vice versa.

**Reading purpose — reallocates emphasis across the five dimensions (it does NOT
raise the annotation count):** match the reader's intent and steer the limited
budget toward the points that serve it:
- 入门 / 背景 / `background` / 了解 → emphasize `thesis`, `concept`, big-picture
  significance and `conclusion`; go lighter on method internals.
- 复现 / 实现 / `implement` / `reproduce` / 跑代码 → emphasize `method`,
  equations, experimental setup and any hyperparameters; the method section is
  the priority surface.
- 评审 / 审稿 / 批判 / `review` / `critique` → emphasize claim↔evidence links,
  `rebuttal` / limitations, methodology soundness and unstated assumptions;
  call out weak or unsupported claims explicitly.
- 找结果 / 特定问题 / `specific finding` → emphasize the `evidence` (tables /
  figures) bearing on the reader's question; lighter elsewhere.
- 综述 / 定位 / `survey` / `positioning` → emphasize `thesis`, the contribution,
  relation to prior work and `conclusion`.
If the triggering message states a purpose for THIS paper (e.g. 「我想复现它的
方法」), use it for this run, overriding any persona default. When no purpose is
given anywhere, default to a balanced five-dimension reading.

*Example:* with purpose `复现` on a method paper, most of the budget goes to
`method` + equations + setup while `rebuttal` keeps only its one required note;
with purpose `评审` on the same paper, the budget shifts instead to
claim↔evidence gaps and limitations.

**Domain familiarity — sets how much background each comment carries:**
- 新手 / 入门 / `novice` / 不熟 → comments add orienting context: what a term
  builds on, why a result matters, how it fits the field; define field jargon
  even when English proficiency is strong.
- 熟悉 / 专家 / `expert` / `familiar` → skip basics; focus comments on what is
  novel, the specific contribution, and where the work is weak or surprising.
- Default (中等 / unstated): explain non-obvious constructs but not textbook basics.

**Comment style — shapes how each `comment` reads (not what is covered):**
- 结构化 / 要点 / `bullet` / `structured` → terse, label-led notes
  (`论点：…` / `证据：…`), one idea per comment.
- 叙述 / `narrative` / `prose` → short flowing prose, 1–2 sentences.
- 提问 / `socratic` / 启发 → end with a brief check question that nudges the
  reader to connect the point to the argument.
- Default: clear explanatory prose.

**If `persona` is `null`:**

First **infer, don't interrogate**. Take whatever the reader already signaled in
the triggering message as given and do NOT re-ask it — e.g. 「我想复现这篇的
方法」→ purpose=复现; 「用英文批注」→ language=en; 「我是这领域新手」→
familiarity=novice.

Then offer a low-friction quick-start (one short message, then wait) — a stated
default plan plus an open invitation, NOT a six-field form:

> 还没有你的阅读画像，我可以直接开始：默认 **中文批注 · 速览 · 均衡覆盖五维**。
> 想更贴合你，任选一项告诉我即可（其余用默认）：
> · 阅读目的：入门 / 复现 / 评审 / 找结果 / 综述
> · 批注语言 · 英文水平 · 领域熟悉度 · 导读深度 · 风格偏好（要点 / 叙述 / 提问）
> 直接回「开始」就用默认。

Accept partial answers — a one-word reply like 「复现」 or 「开始」 is enough;
fill every unstated axis with the default and never block on a full profile.

**Persist the stable traits** the reader states (批注语言 / 英文水平 /
领域熟悉度 / 导读深度 / 风格偏好) so future runs don't re-ask: call
`save_reading_persona(persona_text=...)` with one labeled markdown line per
stated axis, e.g.:

```
- 批注语言：中文
- 英文水平：入门
- 领域熟悉度：中等
- 导读深度：速览
- 风格偏好：结构化要点
```

It writes the `## 阅读画像 (Reading Persona)` section to
`~/.config/zotpilot/ZOTPILOT.md`; confirm the save (the tool returns
`{saved, path, action}`). Reading **purpose** is per-paper — use it for this run
from the trigger or the quick-start reply, and persist it as a default only when
the reader frames it as a standing preference. If the reader replies 「开始」 or
「跳过」, proceed on defaults (Chinese annotations · sparse · balanced five
dimensions · English-proficiency moderate · moderate familiarity · plain prose)
and do not ask again this run.

### 2b. Existing annotations (`existing_annotations: list`)

The list of foreign (non-ZotPilot) annotations already in the PDF. Read it
as a personalization signal:

- Treat pages with more than 2 foreign annotation spans as already heavily
  covered by the user. Reduce proposed annotations on those pages — skip
  claims or spans the user has already highlighted.
- Do NOT propose any annotation whose quote clearly overlaps a foreign
  highlight on the same page. The code enforces an IoU > 0.5 rejection gate;
  be courteous and pre-skip obvious overlaps at this planning stage.
- Foreign annotations on a page do NOT block the five-dimension coverage
  entirely — aim to cover remaining independent understanding points around
  the user's existing work.

---

## Step 3 — Produce the annotation list from `page_texts`

Read `page_texts` (the list of `{page_num, text}` objects, one per page).
This is the primary reading surface for annotation planning — use it rather
than `sectioned_text` alone, because `page_texts` supplies the `page_num`
values that become `page_hint` in every annotation spec.

Produce a compact Prompt-B annotation list. Hard rules:

**Five dimensions — cover every dimension the paper actually has:**
- `thesis` — 核心论点 (the central claim the paper defends)
- `concept` — 关键概念 (a key term, definition, or theoretical construct)
- `evidence` — 实证证据 (empirical result, experiment, or supporting data)
- `rebuttal` — 让步反驳 (limitation, counter-argument, or scope boundary)
- `method` — 方法论 (a methodological choice, algorithm step, or design decision)

For each dimension that the paper genuinely contains, produce at least one
annotation. Skip a dimension only when the paper truly lacks it (e.g., a
purely theoretical paper with no empirical section has no `evidence`).
Never duplicate-color the same text span across two dimensions.

**Personalize within the budget (Step 2a) — reallocate, don't inflate:**
- **Reading purpose** reallocates emphasis: keep each present dimension's one
  required annotation, then spend the remaining budget on the dimensions the
  purpose emphasizes and trim de-emphasized ones to their minimum.
- **Domain familiarity** sets how much background each `comment` carries
  (novice → orienting context; expert → novelty + critique).
- **Comment style** sets how each `comment` is phrased (structured / narrative
  / Socratic).
These shape the SAME annotations and respect the Step 6 density ceiling.

**Per-annotation fields:**
```
{
  quote:      the verbatim excerpt from the paper text (≤ 1000 bytes UTF-8),
  dimension:  one of the five keys above,
  comment:    per-sentence understanding note in the annotation language (≤ 500 bytes UTF-8),
  page_hint:  the page_num of the page where this quote appears (1-based),
  kind:       "highlight" for all prose/term/equation/caption annotations,
              "region"    for figure and materialized-table region notes,
  subtype:    one of: dim | term | long_sentence | figure | figure_caption |
                      table | equation   (informational; drives coverage report),
  page:       required when kind="region" — copy verbatim from figures[]/tables[],
  bbox:       required when kind="region" — copy verbatim from figures[]/tables[]
}
```

**Hard byte caps enforced at the tool boundary:**
- `comment` ≤ 500 bytes (UTF-8)
- `quote` ≤ 1000 bytes (UTF-8)
- Total annotations ≤ 200
- `overview` total ≤ 2000 bytes

Never reproduce large blocks of text. Compact list only.

---

## Step 4 — Emit MIXED-kind annotation specs

Cover all element types. For each element type below, follow the exact
mechanism — the `kind`, `subtype`, and source of coordinates are not
judgment calls.

### 4a. Five-dimension prose claims
- `kind="highlight"`, `subtype="dim"`, `dimension` = the matching key.
- `quote` is the verbatim sentence or clause that carries the claim.
- `page_hint` = the `page_num` from `page_texts` where the quote appears.
- Do not invent a `page` or `bbox` field on highlight annotations.

### 4b. 关键术语 (English-proficiency weak only)
- Only emit when Step 2a determined English proficiency is "weak".
- `kind="highlight"`, `subtype="term"`, `dimension="concept"`.
- Short quote of the term itself (≤ 40 characters preferred).
- `comment` = brief gloss in the annotation language: what the term means in this paper's context.
- `page_hint` from the page where the term first appears.

### 4c. 长难句 (English-proficiency weak only)
- Only emit when Step 2a determined English proficiency is "weak".
- `kind="highlight"`, `subtype="long_sentence"`, `dimension="method"` or
  `"concept"` as appropriate.
- Quote the full difficult sentence (≤ 200 characters preferred).
- `comment` = grammar skeleton + a translation into the annotation language, in natural prose.
- `page_hint` from the page where the sentence appears.

### 4d. 图 Figure
For EVERY entry in `figures[]` (all are guaranteed to have `bbox` and
`caption` from the extractor):

**Region note** (the primary anchor at the figure):
- `kind="region"`, `subtype="figure"`.
- `page` = `figure.page_num` — copy verbatim, do not edit.
- `bbox` = `figure.bbox` — copy the four-element list verbatim, do not edit
  or synthesize coordinates. Never compute, estimate, or modify a bbox.
- `dimension` = `"evidence"` (figures are usually evidence or method; use
  your judgment but do not leave blank).
- `comment` = figure 导读: one or two sentences (in the annotation language)
  describing what this figure shows and why it matters to the argument.
- Leave `quote` as an empty string `""` for region notes.

**Caption highlight** (secondary anchor on the caption text):
- `kind="highlight"`, `subtype="figure_caption"`.
- `quote` = the caption text from `figure.caption` (the full caption string,
  truncated to 1000 bytes if needed).
- `page_hint` = `figure.page_num`.
- `comment` = same brief 导读 as the region note, or a complementary note.
- If the caption text is very short (< 12 chars), omit the caption highlight
  and keep only the region note.

### 4e. 表 Table

**If the table is in `tables[]`** (materialized, bbox present):
- Emit a region note exactly as in §4d, using `table.bbox`, `table.page_num`,
  `subtype="table"`.
- Also emit a caption highlight for `table.caption` with `subtype="table"`,
  `page_hint=table.page_num`. If caption is null or very short, omit the
  caption highlight.

**If a page has `tables_on_page[page_num] > 0` but no entry in `tables[]`**
(detected but not materialized — no bbox available):
- Emit a text-anchored highlight on the caption text or the nearest "Table N"
  label you can locate in `page_texts`, `subtype="table"`, `kind="highlight"`.
- If no caption or "Table N" text can be found in the page text, emit
  nothing for this table. It will appear in `unplaced` as
  `unanchorable_table`.
- NEVER synthesize a `bbox` for a table that is not in `tables[]`.

### 4f. 公式 Equation
- Use `kind="highlight"`, `subtype="equation"`.
- Quote the SPECIFIC explanatory sentence that describes or derives the
  equation (the prose adjacent to the equation, not the equation glyphs
  themselves). Choose a sentence ≥ 12 characters.
- `comment` = an explanation, in the annotation language, of what the equation
  means and how it connects to the argument.
- Do NOT use `kind="region"` for equations — there is no extractor bbox.
- If the explanatory sentence is ambiguous (appears more than once on the
  page), the code will report `ambiguous_multi_match`. In that case, try a
  longer surrounding sentence that is unique.

---

## Step 5 — Build the `overview` dict

Construct a compact argument-structure map for the page-1 sticky-note:

```json
{
  "lang":      "zh|en|… — the annotation language from Step 2a (drives overview labels)",
  "thesis":    "核心论点，一句话",
  "skeleton": {
    "question":   "研究问题",
    "claim":      "主要论点",
    "evidence":   "关键证据",
    "rebuttal":   "让步/局限",
    "conclusion": "结论"
  },
  "strongest": "最有力的论据",
  "weakest":   "最薄弱的环节"
}
```

All text fields in the annotation language (set `lang` to match), short phrases. Total JSON serialized to ≤ 2000 bytes.

---

## Step 6 — Apply density rules ("满秩 but just-right")

Before calling `annotate_pdf`, review the full annotation list against the
density rules:

- **Span every independent understanding point** — each annotation should
  add a piece of understanding that the others do not already cover.
- **No redundancy** — if two annotations say the same thing about the same
  span, remove one.
- **Scale to persona depth:**
  - `速览`: thesis + key figures + 1–2 per present dimension + terms/sentences
    if English-weak. Aim for 8–20 total annotations for a typical paper.
  - `技术细节`: fuller method/evidence coverage. Aim for 20–50.
  - `全面综述`: every independent point. Up to the 200-annotation cap.
- **Density comes from reading depth only** — domain familiarity changes
  per-comment background, not the annotation count. If `速览` collides with the
  weak-English term/long-sentence layers, stay within the sparse budget and keep
  only the most essential helps.
- **Heavily annotated pages** (user has > 2 foreign annotations on the page):
  drop redundant annotations on that page, but do not skip all annotations.

The code enforces M3 caps (`comment` ≤ 500 B, `quote` ≤ 1000 B, ≤ 200
annotations, `overview` ≤ 2000 B) at the tool boundary. Stay well within
them — these are hard rejection limits, not soft suggestions.

---

## Step 7 — Pre-skip obvious overlaps with user annotations

Review `existing_annotations` one more time before the final list:

- If a planned quote clearly covers the same span as a user's existing
  highlight on the same page (obvious overlap), drop the planned annotation.
  The code enforces an IoU > 0.5 rejection gate and will record it as
  `user_already_annotated`; pre-skipping avoids a cluttered unplaced report.
- Partial overlaps (e.g., user highlighted a short term; you are highlighting
  the full sentence containing it) are fine — keep the annotation.
- The page-1 overview sticky-note is exempt from this check; it goes in
  regardless of existing annotations on page 1.

---

## Step 8 — Call `annotate_pdf`

**Pass the annotation payload via a file, not inline.** The annotation list is
large; passing it inline as a tool argument produces a cluttered, hard-to-read
approval prompt. Instead:

1. Write the payload to a temp JSON file (use the Write tool), shaped as:
   ```json
   { "annotations": [ ...the final list from Steps 3–7... ],
     "overview":    { ...the dict from Step 5... } }
   ```
   Put it in the OS temp directory so it works on every platform — e.g.
   `$TMPDIR/ztp-tutor-<doc_id>.json` (macOS/Linux) or
   `%TEMP%\ztp-tutor-<doc_id>.json` (Windows). Do NOT hardcode `/tmp`.
2. Call `annotate_pdf` with just:
   ```
   doc_id:     the doc_id from Step 1
   specs_path: the temp JSON file path
   ```
   The approval prompt then shows only the doc_id and the path — the bulky
   content lives in the file (reviewable as a normal, collapsible file diff).

Only fall back to passing `annotations` + `overview` inline if writing a temp
file is not possible in the environment.

Read the returned dict carefully. It contains:

- `placed`: list of successfully placed annotations
- `unplaced`: list of `{label, reason}` for each annotation that could not
  be placed
- `overview_placed`: bool
- `backup_path`: the `.ztpbak` sidecar path
- `coverage`: breakdown by subtype (figures, tables_region, tables_caption,
  tables_unanchorable, terms, long_sentences, equations)
- `verified`: bool — the post-write verification result
- `summary`: pre-formatted one-line summary (interface language)

---

## Step 9 — Error handling

**`ScannedPdfError` / "no text layer":** Tell the user the PDF lacks an
embedded text layer and OCR is needed. Do not retry.

**`annotate_pdf` raises ToolError:** Surface the message verbatim. If the
error is "backup failed" or "preflight failed", tell the user the PDF was
NOT modified.

**`unplaced` entries:**

| reason | action |
|--------|--------|
| `ambiguous_multi_match` | Try a longer, more unique span from the same sentence; retry the single annotation if possible |
| `too_short` | Note to user — quote was below the 12-char minimum; the code rejected it |
| `no_match` | Note to user — the exact text could not be found in the PDF |
| `user_already_annotated` | Note to user — you already covered this point; skipped to avoid duplication |
| `region_clustered` | The figure/table sticky-note could not find a clear spot near existing annotations; a caption highlight was used instead |
| `unanchorable_table` | No bbox available and no caption text found; the table could not be anchored |
| `page_density_exceeded` | Page already has many annotations; this item was deferred |

For `ambiguous_multi_match`, attempt one retry with a longer surrounding
sentence before reporting it as unplaced.

**`verified: false`:** Tell the user the write verification failed and the
original PDF was restored from `.ztpbak`. No data was lost.

---

## Step 10 — Coverage summary

Relay a concise one-line coverage summary to the user, in the interface language. Build it from
the `coverage` dict and `unplaced` list. Example format:

> 五维齐全 · 5图已标 · 2表（1表仅按标题锚定）· 3术语 · 2长难句 · 备份 foo.pdf.ztpbak · 4处未定位（其中2处用户已批注）

Populate the fields from the actual result:

- **五维** state: list which dimensions are present; if any are missing say
  e.g. `缺 rebuttal`.
- **N图**: number of figures with a placed region note.
- **M表**: breakdown of `tables_region` (region placed) vs `tables_caption`
  (caption-anchor only) vs `tables_unanchorable` (failed).
- **K术语 / J长难句**: from `coverage.terms` and `coverage.long_sentences`.
- **备份 path**: the `backup_path` from the result.
- **X处未定位**: total `len(unplaced)`; break out `user_already_annotated`
  count separately so the user understands their existing annotations were
  respected.

If `overview_placed` is false, note that the page-1 overview note could not
be placed and suggest the user check page 1.

---

## Safety Note

- The original PDF is always backed up to a `.ztpbak` sidecar file **before
  any byte is written**. If anything fails, the original is restored from
  that backup — it is never consumed or deleted by the rollback process.
- Re-running `/ztp-tutor` on the same paper replaces only the ZotPilot
  annotations from the prior run. Foreign annotations (yours or from other
  tools) are never touched, cleared, or counted.
- The `.ztpbak` file persists after a successful run for manual recovery.
  It is safe to delete once you are satisfied with the reading guide.
