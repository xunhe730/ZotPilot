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

你的 Zotero 里有几百篇论文。你*记得*读过一篇关于"睡眠纺锤波与记忆巩固"的研究——但 Zotero 只能匹配精确关键词。

当你让 AI 帮忙时，会遇到这些问题：
- **无法访问**：AI 根本读不了你的 Zotero 文献库
- **手动猜关键词**：Zotero 搜索需要精确词——"sleep spindles"找不到"spindle oscillations"
- **逐个打开 PDF**：找一个表格或结论意味着点开一篇篇论文
- **无引用感知**：不能问"谁引用了这篇？"或"影响力如何？"
- **标签很痛苦**：按主题整理几百篇论文需要数小时拖拽

## 方案

ZotPilot 是一个 **AI Agent Skill**，赋予 AI 助手对 Zotero 文献库的完整读写权限——语义搜索、引用图谱、表格提取、标签管理……全部通过自然语言完成。

```
你："找关于睡眠纺锤波与记忆巩固的论文"
 → Skill 触发 → MCP 服务器按语义搜索你的文献库
 → 返回排序结果，包含段落、页码和引用键
```

**不用复制粘贴。不用猜关键词。不用打开 PDF。** AI 直接读取你的文献库，而且知道*如何做研究*——用哪个工具、什么参数、怎么串联多步工作流。

---

## 为什么选 ZotPilot？

| 方案 | 按语义搜索？ | 了解你的文献库？ | 帮你整理？ | 安装时间 |
|------|:-:|:-:|:-:|--------|
| **Zotero 自带搜索** | 否 | 是 | 否 | 无 |
| **把 PDF 喂给 AI** | 是 | 部分（token 限制） | 否 | 手动 |
| **通用 MCP 搜索工具** | 部分 | 无结构感知 | 否 | 中等 |
| **本地 RAG 管线** | 是 | 是 | 否 | 数小时 |
| **ZotPilot** | **是** | **是——完整 Zotero 访问** | **是——标签和集合** | **5 分钟** |

### ZotPilot 有什么不同？

1. **语义搜索**——"记忆巩固在睡眠中的作用"能找到"睡眠纺锤波依赖的记忆重放"，即使这些词没有一起出现
2. **章节感知排序**——知道段落来自方法、结果还是摘要，相应加权
3. **期刊质量加权**——Q1 期刊论文排名更高（SCImago 分区数据）
4. **完整读写访问**——不仅搜索：浏览集合、添加标签、移动论文、创建文件夹
5. **引用图谱**——"谁引用了这篇？"和"这篇引用了什么？"（OpenAlex）
6. **表格和图表搜索**——在整个文献库中查找特定数据表和图表
7. **内置 Skill**——不仅给 AI 工具，还教会 AI *如何做研究*

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

> **嵌入模型选择：** Gemini（推荐，免费额度在 https://aistudio.google.com/apikey 获取）或 Local（离线，无需 API key）。Skill 会在安装时询问你。

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

| 模型 | API Key | 质量 | 离线 |
|------|---------|------|------|
| **Gemini** `gemini-embedding-001` | 需要（有免费额度） | MTEB 第一 | 否 |
| **本地** `all-MiniLM-L6-v2` | 不需要 | 良好 | 是 |

### 数据存储

```
~/.config/zotpilot/config.json      # 配置（Zotero 路径、嵌入模型）
~/.local/share/zotpilot/chroma/     # ChromaDB 向量索引
```

你的 Zotero 数据直接从 SQLite 数据库只读。索引存在本地。没有数据离开你的电脑（除非使用 Gemini 的嵌入 API 调用）。

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

**会修改我的 Zotero 数据库吗？**
不会。ZotPilot 以只读模式读取 SQLite 数据库。写操作（标签、集合）通过 Zotero 的官方 Web API，正常同步。

**新增论文到 Zotero 后怎么办？**
再运行 `zotpilot index`——增量索引，只处理新增/变更的论文。

**可以不用 API key 吗？**
可以。安装时选择 `--provider local` 使用离线嵌入模型（all-MiniLM-L6-v2），不需要任何 API key，一切在本地运行。

**索引需要多久？**
每篇论文约 2-5 秒。300 篇论文约 10-15 分钟。用 `--limit 10` 先测试。

**支持哪些 AI agent？**
Claude Code、OpenCode 和 OpenClaw。任何支持 Skill + MCP 协议的 agent。

**Zotero 开着时可以用吗？**
可以。ZotPilot 以只读模式打开 SQLite 数据库，永远不会写入。

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
