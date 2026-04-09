<div align="center">
  <h2>🧭 Your AI Pilot for Zotero</h2>
  <img src="assets/banner.jpg" alt="ZotPilot" width="100%">

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-Tool%20Suite-00B265?style=flat-square" alt="MCP">
    <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-supported-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-supported-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-supported-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>

  <p>
    <a href="#快速开始">快速开始</a> &bull;
    <a href="#和其他方案的区别">对比</a> &bull;
    <a href="#工作原理">架构</a> &bull;
    <a href="#如何更新">更新</a> &bull;
    <a href="#常见问题">FAQ</a> &bull;
    <a href="README_EN.md">English</a>
  </p>
</div>

---

## 这是什么

ZotPilot 是一个 MCP server，给你的 Zotero 文献库加上语义搜索、引用图谱查询和 AI 辅助整理功能。附带 Agent Skill 提供安装引导和使用指南。

具体来说，它在你本地的 Zotero 数据上建了一套向量索引，然后通过 MCP 协议暴露一组搜索、引用、整理、摄取和浏览器保存工具给 AI agent。AI 可以按意思搜论文（不是关键词匹配）、定位到具体章节段落、查谁引了谁、帮你打标签分类、读写笔记和批注。论文数据不离开你的电脑。支持 No-RAG 模式——不配置 embedding 也能使用元数据搜索、笔记、标签等基础功能。

---

## 为什么要做这个

写 Related Work 的时候，你记得读过一篇关于"睡眠纺锤波与记忆巩固"的研究，但在 Zotero 里搜不到。因为你记的是概念，Zotero 只匹配原文词汇。搜 "memory consolidation during sleep" 找不到写 "sleep spindle-dependent replay" 的论文，虽然说的是一回事。

除了搜索，还有几个问题 Zotero 解决不了：

- "哪些论文的 Results 里报告了 N400 效应？"——只能逐篇打开 PDF 翻
- 你知道某篇论文有个准确率对比表，但搜不到表格内容
- "谁引用了这篇？他们怎么评价？"——要手动去 Google Scholar 查
- 按主题给 200 篇论文打标签、分类到集合——纯体力活

---

## 快速开始

### 方式一：让 agent 帮你装

把这段话复制给你的 AI agent：

> 帮我安装 ZotPilot skill：clone https://github.com/xunhe730/ZotPilot.git 到我的 skills 目录，然后帮我配置 Zotero 文献库。

Agent 会 clone 仓库、装 CLI、配好 Zotero、注册 MCP 服务器。重启一次就能用。

**Skills 目录（clone 目标）：**

| 平台 | 目标路径 |
|------|----------|
| Claude Code | `~/.claude/skills/zotpilot` |
| Codex CLI | `~/.agents/skills/zotpilot` |
| OpenCode | `~/.config/opencode/skills/zotpilot` |
| Gemini CLI | `~/.gemini/skills/zotpilot` |
| Cursor | `~/.cursor/skills/zotpilot` |
| Windsurf | `~/.codeium/windsurf/skills/zotpilot` |

### 方式二：手动装

**1. Clone 到 skills 目录（Tier 1 平台，有 Skill 支持）：**

```bash
# Claude Code
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot

# Codex CLI
git clone https://github.com/xunhe730/ZotPilot.git ~/.agents/skills/zotpilot

# OpenCode
git clone https://github.com/xunhe730/ZotPilot.git ~/.config/opencode/skills/zotpilot

# Gemini CLI
git clone https://github.com/xunhe730/ZotPilot.git ~/.gemini/skills/zotpilot

# Cursor
git clone https://github.com/xunhe730/ZotPilot.git ~/.cursor/skills/zotpilot

# Windsurf
git clone https://github.com/xunhe730/ZotPilot.git ~/.codeium/windsurf/skills/zotpilot
```

**2. 注册 MCP 服务器：**

> **Windows 用户**：将下方命令中的 `python3` 替换为 `python`

有两种方式传 API key 给 MCP 服务器：

**方式 A（推荐）：设环境变量。** 在 shell profile 里 `export GEMINI_API_KEY=<key>`，服务器启动时自动读取。key 不进 shell history，不写入配置文件。适合 Claude Code / Codex / Gemini CLI 等从终端启动的客户端。

**方式 B（兼容性备选）：注册时通过 CLI 参数传入。** `register` 会把 key 写进 MCP 客户端配置文件，客户端启动时注入给服务器。所有 MCP 客户端都支持（包括 Cursor / Windsurf 等不继承 shell 环境变量的 IDE）。注意：key 会留在 shell history 和配置文件明文中。

```bash
# 推荐：先设环境变量，再注册
export GEMINI_API_KEY=<key>
python3 scripts/run.py register          # Tier 1（源码安装）
zotpilot register                        # Tier 2（pip/uv 安装）

# 兼容性备选：通过 CLI 参数传 key（IDE 客户端可能需要）
python3 scripts/run.py register --gemini-key <key>    # Tier 1
zotpilot register --gemini-key <key>                  # Tier 2

# 指定平台：
python3 scripts/run.py register --platform claude-code  # 或: zotpilot register --platform claude-code
```

v0.5.0 MVP 官方支持：Claude Code、Codex CLI。

**3. 重启你的 AI agent。**

**4.（可选）启用写操作** — 搜索和引用装好就能用，打标签、建集合需要 Zotero Web API 密钥：

1. 打开 [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. 记下页面顶部的 **数字 User ID**（例如 `12345678`，不是用户名）
3. 点 **"Create new private key"**，勾上 "Allow library access" 和 "Allow write access"，复制 key

<img src="assets/zotero-api-key.png" alt="Zotero API Key 页面" width="100%">

4. 保存凭证（**推荐：写入 config 文件，对所有 MCP 客户端生效**）：

```bash
zotpilot config set zotero_user_id 12345678   # 数字 ID，不是用户名
zotpilot config set zotero_api_key YOUR_KEY
```

> ⚠️ Key 以明文存储在 `~/.config/zotpilot/config.json`（Windows: `%APPDATA%\zotpilot\config.json`）。
> ZotPilot 会在 Unix 上把这个文件写成 `0600` 权限，但你仍然不应该把它同步到公开仓库或共享备份里。
> 确保该目录不被 git 追踪。

验证配置：

```bash
zotpilot doctor   # 应显示 [source: config file] ✓
```

<details>
<summary>其他配置方式</summary>

**环境变量（仅对当前 shell session 有效）：**

```bash
export ZOTERO_USER_ID=12345678
export ZOTERO_API_KEY=YOUR_KEY
```

环境变量优先级高于 config 文件。在 `.zshrc` / `.bashrc` 里 export 可持久化，但 IDE 客户端（Cursor/Windsurf）可能读不到 shell 环境变量。

**通过 `register` 注册时写入 MCP 配置（旧方式）：**

```bash
# Tier 1（源码安装）— 重新注册时带上所有已有的 key，否则会丢失：
python3 scripts/run.py register --gemini-key <gemini密钥> --zotero-api-key <zotero密钥> --zotero-user-id <用户ID>
# Tier 2（pip/uv 安装）：
zotpilot register --gemini-key <gemini密钥> --zotero-api-key <zotero密钥> --zotero-user-id <用户ID>
```

> **注意**：`register` 会整体替换 MCP 配置中的 ZotPilot 条目。如果之前注册时带了 `--gemini-key`，重新注册时也要带上，否则会丢失嵌入 API 密钥。推荐改用 `config set` 避免此问题。

</details>

不配也行，搜索和引用照常用，只有标签和集合管理需要。

### 第一次用会发生什么

你说"搜我的 Zotero"时，Skill 会走一遍安装流程：

1. 检测到缺少 `zotpilot` 命令，自动安装（优先 `uv tool install`，失败则 fallback 到 `pip install`）
2. 检测 Zotero 数据目录，问你选哪个嵌入模型
3. 注册 MCP 服务器（如果还没注册的话）
4. 你重启一次，MCP 工具生效
5. 索引论文，每篇 2-5 秒
6. 之后直接问就行

**嵌入模型有三个选项：**

| 模型 | API Key | 质量 | 离线 | 默认维度 |
|------|:---:|------|:---:|------|
| Gemini [`gemini-embedding-001`](https://ai.google.dev/gemini-api/docs/embeddings) | 是（[免费额度](https://aistudio.google.com/apikey)） | [MTEB 68.32](https://huggingface.co/spaces/mteb/leaderboard) | 否 | 768 |
| DashScope [`text-embedding-v4`](https://help.aliyun.com/zh/model-studio/embedding) | 是（[免费额度](https://bailian.console.aliyun.com/)） | [MTEB 68.36 / C-MTEB 70.14](https://huggingface.co/spaces/mteb/leaderboard) | 否 | 1024 |
| Local [`all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | 本地部署（免费） | [MTEB ~56](https://huggingface.co/spaces/mteb/leaderboard) | 是 | 384 |

注意：选了之后不好换。三个模型的向量维度不一样，换模型要 `zotpilot index --force` 全部重新索引。先想好再选。

---

## 实际用起来是什么样

**语义搜索：**

> "sleep spindle 与记忆巩固的关系"

返回 3 篇论文，虽然原文用的是 "spindle-dependent replay"。Zotero 搜不到这个。

**按章节定位：**

> "哪些论文的 Results 里报告了 N400 效应？"

只返回 Results 章节的段落，带 `[Author2022, p.12]` 引用。Introduction 和 References 里随口提到的不会出现在结果里。Q1 期刊的结果排前面。

**批量整理：**

> "给所有深度学习论文打标签，移到 DL Methods 集合"

语义搜索匹配到 28 篇，自动打标签、建集合、同步回 Zotero。超过 5 篇会先问你确认。

**引用探索：**

> "谁引用了 Wang 2022？他们怎么评价局限性？"

通过 OpenAlex 找到 15 篇引用论文，在里面搜批评段落。

**搜表格：**

> "找比较不同模型准确率的表格"

搜 PDF 里提取出来的表头、单元格数据、表标题。

---

## 和其他方案的区别

| 方案 | 语义搜索 | 知道章节结构 | 能帮你整理 | 引用图谱 | 安装耗时 |
|------|:-:|:-:|:-:|:-:|--------|
| Zotero 自带搜索 | 否 | 否 | 否 | 否 | 无 |
| 把 PDF 喂给 AI | 是 | 否（章节信息丢了） | 否 | 否 | 手动，受 token 限制 |
| 自己搭 RAG | 是 | 看你怎么搭 | 否 | 否 | 几小时起步 |
| ZotPilot | 是 | 是 | 是 | 是（OpenAlex） | 约 5 分钟 |

和自建 RAG 比，ZotPilot 的区别在于搜到之后它知道这段话在 Results 还是 Methods 里，发在 Q1 期刊还是 workshop，据此调整排序。排序公式是 `相似度^0.7 × 章节权重 × 期刊质量`。加上表格搜索、引用图谱和 Zotero 写操作，基本覆盖了文献研究的主要流程。

---

## 常用命令

| 你说什么 | 发生什么 |
|---------|---------|
| "搜索我的论文，关于 X" | 语义搜索所有已索引论文 |
| "我有哪些关于 X 的文献？" | 按主题返回论文列表 |
| "找某作者关于 Y 的论文" | 精确词匹配 + 论文详情 |
| "展示比较 X 的表格" | 搜索 PDF 里提取的表格内容 |
| "谁引用了这篇论文？" | 通过 OpenAlex 查引用 |
| "给这些论文打上 X 标签" | 通过 Zotero Web API 加标签 |
| "创建一个叫 X 的集合" | 创建 Zotero 文件夹 |
| "索引了多少论文？" | 索引状态检查 |

---

## 如何更新

**v0.3.0+ 用户**（推荐）：
```bash
zotpilot update
```
自动探测你的安装方式（uv / pip / editable），同时更新 CLI 和所有平台的 skill 目录。
更新后建议立即运行：

```bash
zotpilot status
```

确认三件事：
- `Version` 是你期望的版本
- `Registered` 包含你正在使用的客户端
- `Skill dirs` 指向实际部署的客户端 skill 目录

如果客户端仍然表现得像旧版本，先看 `zotpilot status` 的
`Registered`、`Skill dirs`、`Drift` 三项，再完全重启 AI 客户端。

| 标志 | 用途 |
|------|------|
| `--check` | 只查是否有新版本，不安装 |
| `--dry-run` | 预览所有操作，不执行任何更改 |
| `--cli-only` | 只更新 CLI，跳过 skill 目录 |
| `--skill-only` | 只更新 skill 目录，跳过 CLI |

Skill 目录升级前会自动检查：符号链接、脏工作树、非 ZotPilot 仓库会被跳过并打印警告，不会误操作。

**v0.2 及更早版本用户**（手动升级到最新版）：
```bash
# pip / uv 安装
uv tool upgrade zotpilot
# 或
pip install --upgrade zotpilot

# git clone 安装（skill 目录）
# 进入你的 skill 目录（各平台路径见"快速开始"章节）
cd <your-skill-dir>/zotpilot
git pull
```

源码用户在本地修改了 skill 或脚本后，需要重新部署运行态：

```bash
zotpilot sync
```

然后重启客户端。

---

## 主要 MCP 工具

默认启动是 `core` profile，只暴露 9 个工作流主工具，给 Skill 驱动客户端用来避免工具列表被截断：
`search_topic`、`search_papers`、`get_passage_context`、`advanced_search`、`get_paper_details`、`search_academic_databases`、`ingest_papers`、`get_ingest_status`、`get_index_stats`。

需要更多浏览、写操作或完整工具面时，设置环境变量：

```bash
export ZOTPILOT_TOOL_PROFILE=extended   # 聚合浏览/写操作/admin 工具
# 或
export ZOTPILOT_TOOL_PROFILE=all        # 完整工具面
```

<details>
<summary>默认 Core Profile（9 个）</summary>

| 工具 | 说明 |
|------|------|
| `search_papers` | 语义搜索，可以按章节、期刊加权 |
| `search_topic` | 按主题找论文，结果按文档去重 |
| `advanced_search` | 多条件元数据搜索（年份/作者/标签/集合等），无需索引 |
| `get_passage_context` | 展开某个结果的上下文 |
| `get_paper_details` | 看一篇论文的完整元数据 |
| `search_academic_databases` | 搜外部学术数据库，不直接入库 |
| `ingest_papers` | 将候选论文批量加入 Zotero |
| `get_ingest_status` | 轮询异步入库进度 |
| `get_index_stats` | 看索引就绪状态，也可带出未索引论文分页 |

</details>

<details>
<summary>Extended Profile（聚合 + Admin）</summary>

| 工具 | 说明 |
|------|------|
| `browse_library` | 统一浏览 overview / tags / collections / papers / feeds |
| `search_boolean` | 精确词匹配（AND/OR） |
| `search_tables` | 搜表格内容 |
| `search_figures` | 搜图表标题 |
| `get_notes` | 读取和搜索笔记 |
| `get_annotations` | 读取高亮和评论（需要 ZOTERO_API_KEY） |
| `profile_library` | 对当前文献库做概览分析 |
| `get_citations` | 统一返回引用/被引/计数 |
| `manage_tags` | 统一标签增删改（单篇或批量） |
| `create_collection` | 建文件夹 |
| `manage_collections` | 统一文件夹增删（单篇或批量） |
| `create_note` | 给论文添加笔记（需要 ZOTERO_API_KEY） |
| `index_library` | 索引新论文（增量，支持分批：`batch_size=20`，循环调用直到 `has_more=false`） |
| `save_urls` | 通过 Connector 从真实浏览器页面保存论文 |
| `switch_library` | 切换 metadata/write 工具的目标 library |

</details>

`get_index_stats` 现在合并了旧的未索引论文、重排配置和视觉费用查询能力；`browse_library(view="feeds")` 合并了原来的 feed 工具。

完整工具面和参数说明以 [docs/tools-reference.md](docs/tools-reference.md) 为准；若文档与默认工具数量不一致，以 profile 配置为准。

---

## 工作原理

ZotPilot 的核心是一个 MCP server，通过 [SKILL.md](SKILL.md) 提供安装和使用指导，[scripts/run.py](scripts/run.py) 负责自动安装和跨平台注册。AI agent 加载后会启动 MCP server，并按 MCP 暴露搜索、引用、整理、摄取和 Connector 保存能力。

```
索引（跑一次）
Zotero SQLite ──→ PDF 提取 ──→ 分块 + 章节分类 ──→ 向量嵌入 ──→ ChromaDB

使用（每次查询）
AI Agent ──→ MCP 工具 ──────────┬── 语义搜索 ──→ ChromaDB ──→ 重排序 ──→ 结果
                               ├── 引用图谱 ──→ OpenAlex
                               ├── 文献浏览 ──→ Zotero SQLite
                               ├── 写操作   ──→ Zotero Web API ──→ 同步回 Zotero
                               └── 浏览器保存 ──→ Bridge + Connector ──→ Zotero Desktop
```

**索引阶段：** 从 Zotero SQLite（只读）读元数据，用 PyMuPDF 提取 PDF 全文、表格和图表，按学术章节（Abstract / Methods / Results / …）分块，生成向量嵌入存入 ChromaDB。

**检索阶段：** 查询文本向量化后在 ChromaDB 做余弦相似度搜索，结果经过章节感知重排序（Results 比 References 权重高）和期刊质量加权（SCImago Q1 期刊排前面）。

**写操作：** 标签和集合管理通过 Zotero 官方 Web API（Pyzotero），变更自动同步回 Zotero 客户端。

**引用图谱：** 通过 OpenAlex API 查被引和参考文献关系。

几个设计上的选择：

- Zotero SQLite 用 `mode=ro&immutable=1` 打开，只读。Zotero 开着也没事。
- 论文数据不外传，唯一的网络请求是嵌入 API（选 Local 模型连这个都没有）。
- 文档和查询用不同编码（Gemini 的 `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`），检索质量比用同一种编码好。
- SKILL.md 不只暴露工具接口，还告诉 AI 什么场景用哪个工具、怎么组合。

### 文件结构

```
~/.claude/skills/zotpilot/          # 或 ~/.agents/skills/zotpilot/（Codex）
├── SKILL.md                        # 决策树：安装 → 索引 → 研究
├── scripts/run.py                  # 引导脚本：自动安装 CLI + 委托执行
├── references/                     # 参考文档
│   ├── tool-guide.md               # 工具参数详解
│   ├── troubleshooting.md          # 常见问题
│   └── setup-guide.md             # 安装配置指南
├── src/zotpilot/                   # MCP 服务器源码
└── connector/                      # 浏览器扩展（Chrome MV3）
    ├── src/browserExt/             # 扩展核心代码
    └── build.sh                    # 构建脚本
```

### Connector（浏览器扩展）

`connector/` 是 ZotPilot 的浏览器侧组件，基于官方 Zotero Connector fork，加了 AI agent 调用路径。

```text
Agent → ZotPilot MCP tool → 本地 bridge (127.0.0.1:2619) → Chrome 扩展 → Zotero Desktop
```

使用 `save_urls` 工具时需要安装此扩展。安装方式见 [connector/README_zh-CN.md](connector/README_zh-CN.md)。

### 数据存储

```
# Linux
~/.config/zotpilot/config.json
~/.local/share/zotpilot/chroma/

# macOS
~/.config/zotpilot/config.json
~/.local/share/zotpilot/chroma/

# Windows
%APPDATA%\zotpilot\config.json
%APPDATA%\zotpilot\chroma\
```

---

## 常见问题

<details>
<summary>会不会改我的 Zotero 数据库？</summary>

不会。SQLite 用 `mode=ro&immutable=1` 打开，物理上写不进去。标签和集合的修改走 Zotero 官方 Web API v3，变更正常同步回 Zotero 客户端。

</details>

<details>
<summary>Zotero 开着能用吗？</summary>

能，只读模式不冲突。

</details>

<details>
<summary>支持哪些 agent？</summary>

**Tier 1（Skill + MCP）：** Claude Code、Codex CLI、OpenCode、Gemini CLI、Cursor、Windsurf — 完整支持，Skill 提供使用指导 + MCP 提供工具。

只要支持 MCP 协议的 AI agent 都可以接入 ZotPilot 的搜索和管理工具。

</details>

<details>
<summary>Gemini 嵌入花多少钱？</summary>

免费额度大概 1,000 请求/天。一篇 10 页的论文大约用 1 次请求（每 32 个文本块算 1 次），搜索每次也是 1 次。免费额度够索引几百篇。超出后 $0.15/百万 token，基本可以忽略。Local 模型不花钱。

</details>

<details>
<summary>DashScope/百炼怎么样？</summary>

阿里云百炼的 `text-embedding-v4`，1024 维，MTEB 68.36 / C-MTEB 70.14。国内不用翻墙，¥0.0005/千 token，新用户 100 万 token 免费额度。装的时候选 `--provider dashscope`，key 在 https://bailian.console.aliyun.com/ 拿。

</details>

<details>
<summary>本地模型怎么样？</summary>

`all-MiniLM-L6-v2`，80MB 左右，第一次用自动下载，之后不联网。MTEB 约 56 分（Gemini 68、DashScope 68），几百篇以内的库够用。

</details>

<details>
<summary>索引多久？占多大空间？</summary>

每篇 2-5 秒，300 篇大概 15 分钟。索引大小约 1MB / 100 篇。`--limit 10` 可以先试试。跑过的不会重复跑。

</details>

<details>
<summary>扫描版 PDF / 图表 / 特别长的书怎么办？</summary>

- 扫描版：自动 OCR 回退——当 PyMuPDF 提取文本过少时，自动用 Tesseract 全页 OCR 重新提取。需要安装 Tesseract：macOS `brew install tesseract tesseract-lang`，Ubuntu/Debian `sudo apt install tesseract-ocr tesseract-ocr-chi-sim`，Windows 从 [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) 下载安装
- 图表：提取的是标题文字和上下文段落，不是图片本身。图片 PNG 存在本地
- 超长文献：默认跳过 40 页以上的（`--max-pages` 可以调），也可以用 `--item-key` 单独索引某一篇
- 分批索引：MCP 默认每次处理 20 篇（`batch_size=20`），Agent 循环调用直到 `has_more=false`。CLI 默认一次全跑
- 表格修复：可选功能，用 Claude Haiku 重新提取复杂表格，需要 `ANTHROPIC_API_KEY`

</details>

<details>
<summary>能不用任何 API key 吗？</summary>

能。选 `--provider local` 就行，全部本地跑。

</details>

<details>
<summary>Vision 表格提取是什么？</summary>

可选功能。用 Claude Haiku（通过 Batch API）重新提取 PDF 表格，修复 PyMuPDF 可能搞乱的合并单元格和多级表头。需要 `ANTHROPIC_API_KEY`。不设就自动跳过，不影响文本搜索。成本记录在 `vision_costs.json` 里。

</details>

<details>
<summary>引用数据从哪来？知网论文行吗？</summary>

用的是 [OpenAlex](https://openalex.org/)，覆盖大约 2.5 亿篇文献，通过 DOI 查。有 DOI 的知网论文可以查，没 DOI 的查不了引用，但语义搜索和标签管理不需要 DOI，照常用。

</details>

---

## 出了问题

| 症状 | 怎么办 |
|------|------|
| 找不到 Skill | 确认 clone 到了正确的 skills 目录：Claude Code `~/.claude/skills/`、Codex `~/.agents/skills/`、OpenCode `~/.config/opencode/skills/`、Gemini `~/.gemini/skills/`、Cursor `~/.cursor/skills/`、Windsurf `~/.codeium/windsurf/skills/` |
| `zotpilot: command not found` | `python3 scripts/run.py status`（会自动装）；Windows 用 `python`。Windows 用户还需将 `%APPDATA%\Python\PythonXYY\Scripts` 加入 PATH |
| MCP 工具没出来 | 重新注册 MCP 服务器然后重启 |
| 搜出来是空的 | 先跑 `zotpilot index`，或者换个更宽泛的搜索词 |
| `GEMINI_API_KEY not set` | 设环境变量，或 `zotpilot setup --non-interactive --provider local` 换本地模型 |
| OpenCode MCP 超时 (`-32001`) | 见 [troubleshooting](references/troubleshooting.md#opencode)——需在 `opencode.json` 设 `experimental.mcp_timeout` |
| 不知道哪出了问题 | 跑 `zotpilot doctor` |

更多见 [references/troubleshooting.md](references/troubleshooting.md)。

---

<details>
<summary>开发 / 贡献</summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot

# MCP server（Python）
uv sync --extra dev
uv run pytest
uv run ruff check src/

# Connector（浏览器扩展）
cd connector
npm install
./build.sh -d              # 开发构建
```

欢迎贡献，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

</details>

---

<div align="center">
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">报告问题</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/issues">功能建议</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/discussions">讨论</a>
  </p>
  <sub>MIT License &copy; 2026 xunhe</sub>
</div>
