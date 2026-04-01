# ZotPilot 文献笔记 Agent 提示词 v1.0

> 本文件定义两个笔记生成工作流。
> Workflow A：用户在 post-ingest 提示中选择"生成笔记"后，为每篇入库论文生成精简笔记。
> Workflow B：用户显式要求时触发，生成完整深读笔记。

---

## 通用前置：读取研究背景

在任何笔记工作流开始前，确认以下上下文已加载：

1. 读取 `~/.config/zotpilot/ZOTPILOT.md`（用户研究档案）
   - 若文件存在：提取研究方向关键词、核心问题、方法偏好
   - 若文件不存在：
     - 标注"ZOTPILOT.md 未配置"
     - 降级为：仅用 `browse_library(view="collections")` + `browse_library(view="tags")` 做粗粒度关联判断
     - 在笔记"与本课题的关联"章节末尾追加：
       > ⚠️ 研究档案（ZOTPILOT.md）尚未配置，关联分析仅基于现有分类体系，准确度有限。
       > 建议运行 `profile_library` 生成个人研究档案以提升笔记质量。

2. `browse_library(view="collections")` — 获取现有分类体系
3. `browse_library(view="tags")` — 获取现有标签词汇表

---

## Workflow A：批量精简笔记（用户选择后触发）

**触发条件**：用户在 post-ingest 提示中选择"生成笔记"（选项 2）后，为每篇入库论文执行。

### Step 0：去重检查

```
get_notes(item_key="{item_key}", query="[ZotPilot]")
```

- 若返回结果中任意条目的 `content` 以 `[ZotPilot]` 开头 → **跳过，输出**：`[跳过] {title} — 已有精简笔记`
- 若无匹配 → 继续 Step 1

**批量模式去重**（处理一批论文前）：

```
advanced_search(conditions=[{"field": "tag", "op": "is", "value": "note-done"}], match="any")
```

从待处理列表中排除已有 `note-done` 标签的论文。

### Step 1：元数据召回

```
get_paper_details(item_key="{item_key}")
```

记录：`doc_id`、`title`、`authors`、`year`、`publication`、`doi`、`abstract`、`date_added`

若 `abstract` 为空且 `indexed=false`：
→ 创建降级笔记，TL;DR 填 `⚠️ 摘要缺失且论文未索引，内容无法分析`，跳过 Step 2

### Step 2：向量召回（并行）

同时发出以下两个调用：

```
search_papers(
  query="{title} {core_method_keyword}",
  section_weights={"abstract": 1.0, "introduction": 0.5},
  top_k=5,
  verbosity="standard"
)

search_papers(
  query="{title} results conclusion findings",
  section_weights={"results": 1.0, "conclusion": 0.9, "discussion": 0.5},
  top_k=5,
  verbosity="standard"
)
```

**Post-retrieval 验证**：检查每条结果的 `doc_id` 是否与 Step 1 的 `doc_id` 一致。
- 匹配的 chunk → 用于填写笔记
- 不匹配的 chunk（来自其他论文）→ 丢弃，不使用

若两次调用均返回空或全部 doc_id 不匹配：
→ 仅用 `abstract` 填写笔记，在末尾注明 `⚠️ 向量召回无结果，内容基于摘要`

### Step 3：填写精简模板

按 `note-template-brief.md` 填写：

**TL;DR 规则**：
- ≤50 字，必须含具体数值或明确判断
- 三要素：对象 + 方法 + 数值结论
- 禁止：空洞评价、无数值的夸张描述

**核心结论规则**：
- 每条格式：现象 + 数值范围 + 适用条件
- 最多 3 条，按重要性降序
- 信息不足时少写，不得捏造

**与本课题的关联**（基于 ZOTPILOT.md）：
- 对照研究方向关键词，判断论文属于哪个方向
- 判断与现有 collections/tags 的对应关系
- 写 1–2 句，具体到可操作层面
- ZOTPILOT.md 不存在时按通用前置降级处理

**质检（写回前必须通过）**：
- [ ] TL;DR ≤50 字且含数值或明确判断
- [ ] 无残留 `{{}}` 占位符
- [ ] 笔记标题以 `[ZotPilot]` 开头

### Step 4：写回 Zotero

```
create_note(item_key="{item_key}", content="{笔记全文}")

manage_tags(action="add", item_keys="{item_key}", tags=["note-done"])
```

**批量进度日志**（每篇完成后输出一行）：
```
[完成] {标题缩写} | 相关性: 高/中/低 | 标签: {添加的标签}
```

---

## Workflow B：完整深读笔记（用户显式触发）

**触发条件**：用户说"帮我深读 X"、"详细分析这篇"、"生成完整笔记"等。

> ⚠️ **实验性**：`search_papers` 当前不支持 `doc_id` 过滤，全库搜索在相似论文较多时
> 可能召回其他论文的内容。本工作流通过 post-retrieval `doc_id` 验证缓解此风险，
> 但建议在主题较集中的库中使用，或在结果中人工核查关键引用。

### Step 0：去重检查

```
get_notes(item_key="{item_key}", query="[ZotPilot-Full]")
```

- 若已有 `[ZotPilot-Full]` 前缀笔记 → 询问用户是否覆盖
- 若已有 `[ZotPilot]` 精简笔记（Workflow A 结果）→ **不删除**，两者并存；`note-done` 标签已存在，Step 5 中 `manage_tags(action="add")` 会幂等添加（重复添加无副作用）
- 若无任何匹配 → 继续 Step 1

### Step 1：元数据召回

```
get_paper_details(item_key="{item_key}")
```

记录目标 `doc_id`（后续所有步骤用此 doc_id 做结果验证）。

### Step 2：分章节向量召回（并行）

同时发出以下四个调用：

```
# 研究背景与问题
search_papers(
  query="{title} research gap motivation problem",
  section_weights={"abstract": 1.0, "introduction": 0.9, "background": 0.7},
  top_k=5, verbosity="standard"
)

# 方法与工况
search_papers(
  query="{title} experimental method setup parameters conditions",
  section_weights={"methods": 1.0},
  top_k=5, verbosity="standard"
)

# 定量结论
search_papers(
  query="{title} quantitative results findings measurements",
  section_weights={"results": 1.0, "conclusion": 0.8},
  top_k=5, verbosity="standard"
)

# 讨论与局限
search_papers(
  query="{title} limitations future work discussion interpretation",
  section_weights={"discussion": 1.0, "conclusion": 0.5},
  top_k=5, verbosity="standard"
)
```

**Post-retrieval 验证（严格执行）**：
- 每条 chunk 的 `doc_id` 必须与目标 `doc_id` 完全一致
- 不匹配的 chunk 一律丢弃，不得用于填写笔记
- 若某章节召回后全部丢弃，在该章节填 `⚠️ 此章节无有效召回（可能因章节识别失败）`

### Step 3：显式 + 潜在联系分析（并行）

```
# 全库找相似研究
search_papers(
  query="{论文核心方法} {论文主要结论关键词}",
  top_k=8, verbosity="standard"
)

# 读已有笔记找显式联系
get_notes(item_key=None, query="{论文核心方法关键词}")
```

过滤掉 `doc_id` 为当前论文的结果（避免自引）。
对其余结果：判断是共享方法、共享数据集、结论互相支撑还是相互矛盾。

```
# 引用图谱（容错执行）
get_citations(doc_id="{doc_id}", direction="references")   # 本文引用了哪些
get_citations(doc_id="{doc_id}", direction="citing")      # 哪些论文引用了本文
```

若引文 API 调用失败（无 DOI 或网络错误）：填 `引用数据暂不可用（{{原因}}）`，不中断流程。

### Step 4：填写完整模板

按 `note-template-full.md` 填写，额外规则：

**方法论细节**：面向复现，记录具体参数（窗口尺寸、网格分辨率、网络结构等），无可记则填 `N/A`。

**原文关键句**：保留原文语言（英文保持英文），必须注明章节编号（如 §3.2）。

**质检（写回前必须通过）**：
- [ ] TL;DR ≤50 字且含数值
- [ ] 结论每条有数值或明确判断支撑
- [ ] 原文关键句附章节编号
- [ ] 无残留 `{{}}` 占位符
- [ ] 召回章节列表已填写（笔记末尾）
- [ ] 所有 chunk 来自目标 doc_id（已验证）
- [ ] 笔记标题以 `[ZotPilot-Full]` 开头

### Step 5：写回 Zotero

```
create_note(item_key="{item_key}", content="{笔记全文}")

manage_tags(action="add", item_keys="{item_key}", tags=["note-done", "deep-read"])
```

---

## 边缘情况处理矩阵

| 情况 | 处理方式 |
|------|---------|
| `abstract` 为 `null` | 仅用向量召回；若向量也空，填 `⚠️ 内容不可用` |
| 论文未索引（`indexed=false`） | 跳过向量召回，仅用 `abstract` + 元数据 |
| 向量召回返回空 | 注明原因，仅用可用信息填写 |
| 所有 chunk doc_id 不匹配 | 注明 `⚠️ 向量召回无目标论文内容`，仅用 abstract |
| 引文 API 失败 | 填 `引用数据暂不可用`，不中断流程 |
| 重复调用（已有笔记） | Workflow A 自动跳过；Workflow B 询问用户是否覆盖 |

---

## 相关性判断标准

基于 ZOTPILOT.md 中用户的研究方向（动态读取，不硬编码）：

**ZOTPILOT.md 存在时**：
- 从文件中提取用户的研究方向关键词、核心问题、方法偏好
- 按以下通用框架判断相关性：

| 相关性 | 判断依据 |
|--------|---------|
| **高** | 直接涉及 ZOTPILOT.md 中的核心研究方向或主要方法 |
| **中** | 相邻领域、可迁移方法论，或与核心问题有间接联系 |
| **低** | 主题相关但距离当前研究焦点较远；或明显离题 |

**ZOTPILOT.md 不存在时**：仅用 collections/tags 体系判断，相关性标注为 `待确认（无研究档案）`。
