# Scoring Guide — Step 2 of Agent Research Discovery

For each candidate paper from `search_academic_databases`, compute a weighted score (0–10) across 4 dimensions:

| Dimension | Default weight | Description |
|-----------|---------------|-------------|
| Query relevance | 40% | How directly the paper answers the user's query (based on abstract) |
| User context fit | 30% | Alignment with ZOTPILOT.md research focus and discipline (0% when no ZOTPILOT.md) |
| Quality signal | 20% | Citation count (normalized by year), source type (journal > conference > preprint) |
| Recency | 10% | Publication year proximity to current year |

**When no ZOTPILOT.md**: redistribute context fit weight → relevance becomes 70%, context 0%.

## Intent-driven weight adjustments

Agent judges from natural language — not keyword matching:

| Intent signal | Relevance | Context | Quality | Recency |
|---------------|-----------|---------|---------|---------|
| "最新" / "recent advances" | 35% | 25% | 10% | 30% |
| "经典" / "foundational work" | 35% | 25% | 30% | 10% |
| "探索新方向" / "exploring new area" | 60% | 0% | 25% | 15% |
| "高引" / "high impact" | 35% | 25% | 35% | 5% |
| "综述" / "survey" | 50% | 30% | 15% | 5% + add `type:review` filter |

## Display format (show to user before ingesting)

```
1. [9.2] Attention Is All You Need (Vaswani et al., 2017) · 8420引用 · OA
   "直接奠定你研究的 transformer 基础，与你的 VLM 方向高度契合"

2. [7.8] CLIP (Radford et al., 2021) · 3200引用 · OA
   "视觉语言对齐的代表工作，补充你库中 contrastive learning 的空白"
```
