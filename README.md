<div align="center">
  <h1>ZotPilot</h1>
  <h3>让 AI 接管你的 Zotero</h3>
  <p>
    按语义搜索、探索引用、用自然语言整理文献。<br>
    <b>一个 AI Agent Skill，完整 Zotero 访问，无需插件。</b>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-24_Tools-00B265?style=flat-square" alt="MCP">
    <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-supported-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-supported-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-supported-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>

  <p>
    <a href="#-快速开始">快速开始</a> &bull;
    <a href="#-真实使用案例">案例</a> &bull;
    <a href="#-工作原理">架构</a> &bull;
    <a href="#-常用命令">命令</a> &bull;
    <a href="README_EN.md">English</a>
  </p>
</div>

---

## 问题

你的 Zotero 里有几百篇论文。写 Related Work 时，你*记得*读过一篇关于"睡眠纺锤波与记忆巩固"的研究——但在 Zotero 里怎么也搜不到。因为你记住的是*概念*，Zotero 只能匹配*原文词汇*。

这是所有文献管理工具的通病：
- **Zotero 搜索是关键词匹配**——"memory consolidation during sleep"找不到写的是"sleep spindle-dependent replay"的论文，虽然说的是同一件事
- **没法跨论文提问**——"哪些论文的 Results 里报告了 N400 效应？"这种问题，只能逐篇打开 PDF 翻
- **表格数据被锁在 PDF 里**——你知道某篇论文有个准确率对比表，但搜不到表格内容
- **引用关系是盲区**——"谁引用了这篇？他们怎么评价？"要手动去 Google Scholar 查
- **整理文献全靠手动**——按主题给 200 篇论文打标签、分类到集合，是纯体力活

## 方案

ZotPilot 在你的 Zotero 文献库上建了一套**本地 RAG 系统**（检索增强生成），通过 MCP 协议暴露给 AI agent，让 AI 能直接按语义搜索、读取、整理你的论文。

**技术原理：**

```
Zotero SQLite ──→ PDF 提取（PyMuPDF）──→ 分块 + 章节分类 ──→ 向量嵌入（Gemini/本地）──→ ChromaDB
     │                                        │
     │            ┌─────────────────────────────┘
     ▼            ▼
  元数据      语义检索 + 重排序
  (标题、作者、     (相似度^0.7 × 章节权重 × 期刊质量)
   DOI、标签)           │
     │                  ▼
     └──────→ 24 个 MCP 工具 ←── AI Agent（Claude Code / OpenCode / OpenClaw）
                   │
            Zotero Web API（写操作：标签、集合）
```

- **索引阶段**：从 Zotero SQLite（只读）读取元数据，用 PyMuPDF 提取 PDF 全文、表格和图表，按学术章节（Abstract/Methods/Results/…）分块，生成向量嵌入存入 ChromaDB
- **检索阶段**：用户查询经向量化后在 ChromaDB 中做余弦相似度搜索，结果经过**章节感知重排序**（Results 章节比 References 章节权重高）和**期刊质量加权**（SCImago Q1 期刊排名更高）
- **写操作**：标签和集合管理通过 Zotero 官方 Web API（Pyzotero），变更自动同步回 Zotero
- **引用图谱**：通过 OpenAlex API 查询被引和参考文献关系

**关键设计决策：**
- 完全本地——论文不离开你的电脑（除 Gemini/DashScope 嵌入 API 调用外）
- Zotero SQLite 只读——Zotero 运行时也安全
- 非对称嵌入——文档用 `RETRIEVAL_DOCUMENT` 编码，查询用 `RETRIEVAL_QUERY` 编码，提升检索质量
- 内置 Skill——不只给 AI 工具，还教会 AI *选哪个工具、怎么串联*

---

## 为什么选 ZotPilot？

| 方案 | 语义搜索 | 了解文献结构 | 帮你整理 | 引用图谱 | 安装 |
|------|:-:|:-:|:-:|:-:|--------|
| **Zotero 自带搜索** | 否 | 否 | 否 | 否 | 无 |
| **把 PDF 喂给 AI** | 是 | 否（丢失章节信息） | 否 | 否 | 手动，受 token 限制 |
| **自建 RAG 管线** | 是 | 看实现 | 否 | 否 | 数小时搭建 |
| **ZotPilot** | **是** | **是（章节+期刊+表格）** | **是** | **是（OpenAlex）** | **5 分钟** |

ZotPilot 相比自建 RAG 的核心优势：**不只是"能搜到"，而是搜到后知道这段话来自 Results 还是 Methods，来自 Q1 期刊还是会议论文，并据此排序。** 加上表格/图表搜索、引用图谱和 Zotero 写操作，形成完整的文献研究工作流。

---

## 快速开始

### 方式一：自动安装（推荐）

把这段话复制给你的 AI agent：

> 帮我安装 ZotPilot skill：clone https://github.com/xunhe730/ZotPilot.git 到我的 skills 目录，然后帮我配置 Zotero 文献库。

Agent 会 clone 仓库、安装 CLI、配置 Zotero、注册 MCP 服务器。重启一次后即可搜索。

### 方式二：手动安装

```bash
# Claude Code
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot

# OpenCode
git clone https://github.com/xunhe730/ZotPilot.git ~/.config/opencode/skills/zotpilot

# OpenClaw
git clone https://github.com/xunhe730/ZotPilot.git ~/.openclaw/skills/zotpilot
```

重启你的 AI agent。

### 首次使用流程

当你第一次说"搜索我的 Zotero……"时，Skill 会引导你完成安装：

1. **自动安装 CLI** — `scripts/run.py` 检测到缺少 `zotpilot` 命令，通过 `uv tool install` 安装
2. **配置 Zotero** — 自动检测 Zotero 数据目录，询问你选择嵌入模型（Gemini 或离线本地模型）
3. **注册 MCP 服务器** — 运行 `claude mcp add`（或 OpenCode/OpenClaw 对应命令）
4. **重启一次** — MCP 工具在重启后生效
5. **索引论文** — 第二次启动时，索引你的文献库（每篇约 2-5 秒）
6. **开始搜索** — 之后直接用自然语言提问即可

> **嵌入模型选择：** Gemini（推荐，免费额度在 https://aistudio.google.com/apikey 获取）、DashScope/百炼（国内推荐，在 https://bailian.console.aliyun.com/ 获取 API key）或 Local（离线，无需 API key）。Skill 会在安装时询问你。

---

## 真实使用案例

### 案例 1：文献综述

**你：** "我有哪些关于 transformer 用于 EEG 分类的论文？"

**AI 的内部流程（由 Skill 引导）：**
```
→ 检查索引就绪状态（get_index_stats）
→ 选择 search_topic（不是 search_papers——这是综述任务）
→ 返回 12 篇论文，按相关性排序
→ 报告：年份范围 2019–2024，主要作者，最佳段落
```

**结果：** 结构化概览，包含论文标题、作者和关键发现——没有打开任何 PDF。

### 案例 2：查找特定证据

**你：** "找到 N400 振幅与预测误差相关的证据"

**AI 的内部流程：**
```
→ 选择 search_papers（具体论断，不是综述）
→ 使用 required_terms=["N400"] 强制精确匹配
→ 设置 section_weights={"results": 1.0, "discussion": 0.8}
→ 返回带页码和引用键的段落
```

**结果：** 来自 3 篇论文的直接引用，带 `[Author2022, p.12]` 标注。

### 案例 3：按主题整理

**你：** "给所有深度学习论文打标签，移到'DL Methods'集合"

**AI 的内部流程：**
```
→ search_topic("deep learning") → 找到 28 篇匹配论文
→ create_collection("DL Methods") → 创建 Zotero 文件夹
→ 对每篇论文：add_to_collection + add_item_tags(["deep-learning"])
→ 修改超过 5 篇时先确认
```

**结果：** 28 篇论文已打标签并整理。变更通过 Web API 同步到 Zotero。

> **注意：** 写操作（标签、集合）需要 Zotero Web API 凭据。见下方[启用写操作](#启用写操作)。

### 案例 4：引用探索

**你：** "谁引用了 Wang 2022？他们怎么评价局限性？"

**AI 的内部流程：**
```
→ search_boolean("Wang 2022") → 找到论文，获取 doc_id
→ find_citing_papers(doc_id) → 通过 OpenAlex 找到 15 篇引用论文
→ search_papers("limitations of Wang 2022 approach") 在这些论文中搜索
→ 返回具体的批评段落
```

---

## 常用命令

| 你说什么 | 发生什么 |
|---------|---------|
| *"搜索我的论文，关于 X"* | 语义搜索所有已索引论文 |
| *"我有哪些关于 X 的文献？"* | 主题综述——按相关性分组返回论文 |
| *"找某作者关于 Y 的论文"* | 布尔搜索 + 论文详情 |
| *"展示比较 X 的表格"* | 搜索提取的表格内容 |
| *"谁引用了这篇论文？"* | 通过 OpenAlex 查找引用 |
| *"给这些论文打上 X 标签"* | 通过 Zotero Web API 添加标签 |
| *"创建一个叫 X 的集合"* | 创建 Zotero 文件夹 |
| *"索引了多少论文？"* | 索引健康检查 |

---

## 24 个 MCP 工具

### 搜索与发现

| 工具 | 功能 |
|------|------|
| `search_papers` | 语义搜索，支持章节/期刊加权和多维过滤 |
| `search_topic` | 主题级论文发现，按文档去重 |
| `search_boolean` | 精确词匹配（AND/OR），使用 Zotero 全文索引 |
| `search_tables` | 搜索表头、单元格、标题 |
| `search_figures` | 搜索图表标题和描述 |
| `get_passage_context` | 展开任意结果的上下文段落 |

### 浏览与理解

| 工具 | 功能 |
|------|------|
| `get_library_overview` | 分页展示全部论文及索引状态 |
| `get_paper_details` | 完整元数据：标题、作者、摘要、DOI、标签 |
| `list_collections` | 所有 Zotero 文件夹及层级 |
| `get_collection_papers` | 特定集合中的论文 |
| `list_tags` | 所有标签按频率排序 |
| `get_index_stats` | 索引健康：文档数、chunk 数、未索引论文 |

### 整理与写入

| 工具 | 功能 |
|------|------|
| `add_item_tags` / `remove_item_tags` | 添加或删除标签（非破坏性） |
| `set_item_tags` | 替换论文的全部标签 |
| `add_to_collection` / `remove_from_collection` | 在文件夹间移动论文 |
| `create_collection` | 创建新文件夹（支持嵌套） |

### 引用与影响力

| 工具 | 功能 |
|------|------|
| `find_citing_papers` | 谁引用了这篇？（OpenAlex） |
| `find_references` | 这篇引用了什么？ |
| `get_citation_count` | 被引次数和参考文献数 |

### 管理

| 工具 | 功能 |
|------|------|
| `index_library` | 索引新增/变更论文（增量） |
| `get_reranking_config` | 查看排序权重 |
| `get_vision_costs` | 监控视觉 API 用量 |

---

## 工作原理

ZotPilot 是一个 **AI Agent Skill**——一个包含指令（[SKILL.md](SKILL.md)）和引导脚本（[scripts/run.py](scripts/run.py)）的仓库，AI agent 自动加载。Skill 触发一个拥有 24 个工具的 MCP 服务器，提供完整的 Zotero 访问。

### 架构

```
~/.claude/skills/zotpilot/          （或 OpenCode/OpenClaw 对应路径）
├── SKILL.md                        # 决策树：安装 → 索引 → 研究
├── scripts/run.py                  # 引导脚本：自动安装 CLI + 委托执行
├── references/                     # 深入参考文档
│   ├── tool-guide.md               # 详细参数指南
│   ├── troubleshooting.md          # 常见问题 + 修复方案
│   └── install-steps.md            # 手动安装参考
└── src/zotpilot/                   # MCP 服务器源码（24 个工具）
```

当你提到 Zotero 或论文时，AI 会：
1. 加载 `SKILL.md` → 运行 `scripts/run.py status --json`
2. 如果未安装 → 自动安装 CLI，配置 Zotero，注册 MCP
3. 如果未索引 → 索引你的论文（Gemini 或本地嵌入）
4. 如果就绪 → 选择正确的工具，设置最优参数，格式化结果

### 关键设计决策

- **本地优先** — 论文永远不离开你的电脑。Zotero SQLite 只读
- **Web API 写入** — 标签/集合变更通过 Zotero 官方 API 同步
- **章节感知排序** — 组合分 = 相似度^0.7 x 章节权重 x 期刊质量
- **非对称嵌入** — 文档和查询使用不同编码（Gemini）
- **Skill 而非工具** — SKILL.md 教会 AI *选哪个*工具，*怎么*串联

### 嵌入模型

| 模型 | API Key | 质量 | 离线 | 备注 |
|------|---------|------|------|------|
| **Gemini** `gemini-embedding-001` | 需要（有免费额度） | MTEB 第一 | 否 | 推荐，768 维 |
| **DashScope** `text-embedding-v3` | 需要（阿里云百炼） | 优秀 | 否 | 国内推荐，1024 维，¥0.0005/千 token |
| **本地** `all-MiniLM-L6-v2` | 不需要 | 良好 | 是 | 384 维，完全离线 |

> **注意：** 嵌入模型（provider）的选择在首次索引时就会确定。不同 provider 的向量维度不兼容（Gemini 768 维、DashScope 1024 维、Local 384 维），切换 provider 后必须用 `zotpilot index --force` 重新索引全部论文。请在索引前慎重选择。

### 数据存储

```
~/.config/zotpilot/config.json      # 配置（Zotero 路径、嵌入模型）
~/.local/share/zotpilot/chroma/     # ChromaDB 向量索引
```

你的 Zotero 数据直接从 SQLite 数据库只读。索引存在本地。没有数据离开你的电脑（除非使用 Gemini 或 DashScope 的嵌入 API 调用）。

---

## 启用写操作

搜索和引用工具开箱即用。要**整理文献库**（添加标签、移动论文、创建集合），需要 Zotero Web API 密钥。

### 获取凭据

1. 前往 [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. 点击 **"Create new private key"**
3. 勾选 **"Allow library access"** 和 **"Allow write access"**
4. 保存——复制密钥
5. 记下你的 **User ID**（同页面显示的数字——不是用户名）

### 配置方式一：让 Agent 帮你配（推荐）

拿到密钥和 User ID 后，直接告诉你的 AI agent：

> 帮我启用 ZotPilot 写操作，我的 Zotero API Key 是 `xxxxx`，User ID 是 `12345`。

Agent 会自动执行 `claude mcp remove` + `claude mcp add` 并提示你重启。

### 配置方式二：手动配置

```bash
claude mcp remove zotpilot
claude mcp add -s user \
  -e GEMINI_API_KEY=<你的gemini密钥> \
  -e ZOTERO_API_KEY=<你的zotero密钥> \
  -e ZOTERO_USER_ID=<你的用户ID> \
  zotpilot -- zotpilot
```

重启 AI agent。

> 没有这些凭据，所有读取/搜索操作仍然正常。只有标签和集合管理需要。

---

## 常见问题

### 基本问题

**会修改我的 Zotero 数据库吗？**
不会。ZotPilot 以 `mode=ro&immutable=1` 打开 SQLite 数据库，绝对只读。写操作（标签、集合）通过 Zotero 官方 Web API v3，变更正常同步回 Zotero 客户端。

**Zotero 开着时可以用吗？**
可以。只读模式打开数据库，和 Zotero 并行运行完全安全。

**支持哪些 AI agent？**
Claude Code、OpenCode 和 OpenClaw。任何支持 Skill + MCP 协议的 agent 都可以。

### 成本与资源

**Gemini 嵌入要花钱吗？**
Gemini Embedding API 有**免费额度**（约 1,000 请求/天，2025.12 缩减后的限额）。ZotPilot 每 32 个文本块发一次请求，一篇 10 页的论文大约产生 15-25 个块，即 1 次请求。免费额度足够一次性索引数百篇论文。**搜索时每次查询也需要 1 个嵌入请求**（查询文本需要向量化后才能做余弦相似度搜索）。付费价格 $0.15/百万 token，日常搜索成本几乎可以忽略。本地模型（`--provider local`）不消耗任何 API 请求。

**DashScope/百炼嵌入怎么样？**
阿里云百炼（DashScope）提供 `text-embedding-v3` 模型，1024 维向量，适合国内用户（无需翻墙）。价格 ¥0.0005/千 token，极其便宜。安装时选择 `--provider dashscope`，设置 `DASHSCOPE_API_KEY` 环境变量即可。API key 在 https://bailian.console.aliyun.com/ 获取。

**本地嵌入模型有什么代价？**
`all-MiniLM-L6-v2` 模型约 80MB，首次使用时自动下载。之后完全离线运行，零 API 成本。质量低于 Gemini（384 维 vs 768 维），但对于中小型文献库完全够用。

**ChromaDB 索引占多少空间？**
大约每 100 篇论文 1MB。300 篇论文的索引约 3MB，可以忽略不计。

**Vision 表格提取要花钱吗？**
这是可选功能，默认启用但需要 `ANTHROPIC_API_KEY`。使用 Claude Haiku 通过 Batch API 重新提取 PDF 表格（修复 PyMuPDF 可能搞乱的合并单元格、多级表头）。成本记录在 `vision_costs.json` 中。不设置 Anthropic API key 则自动跳过，不影响文本搜索。

### 索引与内容

**索引需要多久？**
每篇论文约 2-5 秒（PDF 提取 + 嵌入）。300 篇约 10-15 分钟。用 `--limit 10` 先测试。新增论文运行 `zotpilot index` 会增量索引，只处理未索引的。

**扫描版/纯图片 PDF 能搜索吗？**
能。PyMuPDF 内置 OCR 自动识别纯图片页面并提取文字。提取出的文字和正常 PDF 文字一样进入向量索引。但 OCR 质量取决于扫描质量——模糊扫描可能提取不完整。

**图片/图表会变成向量吗？**
图表图片本身**不会**嵌入为向量。ZotPilot 提取的是图表的**标题文字（caption）和引用该图表的上下文段落**，这些文字会被嵌入。图片 PNG 文件保存在本地磁盘，搜索 `search_figures` 时返回图片路径。所以你可以按"Figure 3 的标题说了什么"搜索，但不能按图片内容搜索。

**特别长的专著（几百页）怎么处理？**
默认跳过超过 40 页的文献（可通过 `--max-pages` 调整，`--max-pages 0` 表示不限制）。索引完成后会列出被跳过的长文献，由用户决定是否索引。也可以用 `--item-key KEY` 单独索引特定长文献。

其他过滤选项：
- 用 `--title "pattern"` 按标题正则过滤，只索引匹配的论文
- 用 `--limit N` 限制一次索引的数量
- 已索引的不会重复索引（按 PDF hash 跟踪）

**可以不用 API key 吗？**
可以。安装时选择 `--provider local`，使用 all-MiniLM-L6-v2 离线模型。不需要任何 API key，一切在本地运行。如果你在国内、不方便用 Gemini，也可以选择 `--provider dashscope` 使用阿里云百炼。

### 引用图谱

**引用查询的数据来源是什么？**
[OpenAlex](https://openalex.org/)——一个免费开放的学术元数据库，覆盖约 2.5 亿篇学术文献。通过 DOI 查找论文的被引和参考文献。匿名访问 1 请求/秒，设置 `OPENALEX_EMAIL` 后提升到 10 请求/秒。

**知网（CNKI）论文支持引用查询吗？**
取决于论文是否有 DOI。OpenAlex 的覆盖范围以英文学术文献为主，部分中文期刊有收录（尤其是有英文 DOI 的）。如果你的知网论文在 Zotero 里有 DOI 字段且 OpenAlex 有收录，引用查询可以工作。没有 DOI 的论文（如部分中文硕博论文）无法使用引用功能，但语义搜索和标签管理不受影响。

**没有 DOI 的论文怎么办？**
语义搜索、表格搜索、布尔搜索、标签管理——这些功能全部正常，不需要 DOI。只有 `find_citing_papers`、`find_references`、`get_citation_count` 这三个引用工具需要 DOI。

---

## 故障排查

详见 [references/troubleshooting.md](references/troubleshooting.md)。快速修复：

| 问题 | 解决 |
|------|------|
| 安装后找不到 Skill | 检查路径：`ls ~/.claude/skills/zotpilot/SKILL.md` |
| `zotpilot: command not found` | 运行 `python3 scripts/run.py status`（自动安装） |
| MCP 工具不可用 | `claude mcp add -s user zotpilot -- zotpilot` 然后重启 |
| 搜索结果为空 | 先运行 `zotpilot index`，或尝试更宽泛的查询 |
| `GEMINI_API_KEY not set` | 设置环境变量，或切换到本地模型：`zotpilot setup --non-interactive --provider local` |
| 不确定哪里出了问题 | 运行 `zotpilot doctor` 查看详细诊断 |

---

## 参与贡献

<details>
<summary><b>开发环境搭建</b></summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev

# 运行测试
uv run pytest              # 106 个测试

# 代码检查
uv run ruff check src/
```

</details>

欢迎贡献！详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 一句话总结

**没有 ZotPilot：** 在 Zotero 猜关键词 → 逐个打开 PDF → 复制粘贴给 AI → 循环

**有了 ZotPilot：** 告诉 AI 你需要什么 → 语义搜索、查找证据、探索引用、整理论文——一个对话搞定。

```bash
# 30 秒开始
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot
# 重启 Claude Code，然后："搜索我的 Zotero……"
```

---

<div align="center">
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">报告 Bug</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/issues">功能建议</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/discussions">讨论区</a>
  </p>
  <sub>MIT License &copy; 2026 Xiaodong Zhuang</sub>
</div>
