<div align="center">
  <h1>ZotPilot</h1>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-26_Tools-00B265?style=flat-square" alt="MCP">
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
    <a href="README_EN.md">English</a>
  </p>
</div>

---

## 这是什么

ZotPilot 是一个 AI Agent Skill，给你的 Zotero 文献库加上语义搜索、引用图谱查询和 AI 辅助整理功能。

具体来说，它在你本地的 Zotero 数据上建了一套向量索引，然后通过 MCP 协议暴露 26 个工具给 AI agent。AI 可以按意思搜论文（不是关键词匹配）、定位到具体章节段落、查谁引了谁、帮你打标签分类。论文数据不离开你的电脑。

---

## 为什么要做这个

写 Related Work 的时候，你记得读过一篇关于"睡眠纺锤波与记忆巩固"的研究，但在 Zotero 里搜不到。因为你记的是概念，Zotero 只匹配原文词汇。搜 "memory consolidation during sleep" 找不到写 "sleep spindle-dependent replay" 的论文，虽然说的是一回事。

除了搜索，还有几个问题 Zotero 解决不了：

- "哪些论文的 Results 里报告了 N400 效应？"——只能逐篇打开 PDF 翻
- 你知道某篇论文有个准确率对比表，但搜不到表格内容
- "谁引用了这篇？他们怎么评价？"——要手动去 Google Scholar 查
- 按主题给 200 篇论文打标签、分类到集合——纯体力活

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

## 快速开始

### 方式一：让 agent 帮你装

把这段话复制给你的 AI agent：

> 帮我安装 ZotPilot skill：clone https://github.com/xunhe730/ZotPilot.git 到我的 skills 目录，然后帮我配置 Zotero 文献库。

Agent 会 clone 仓库、装 CLI、配好 Zotero、注册 MCP 服务器。重启一次就能用。

### 方式二：手动装

**1. Clone 到 skills 目录：**

```bash
# Claude Code
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot

# Codex CLI
git clone https://github.com/xunhe730/ZotPilot.git ~/.agents/skills/zotpilot

# OpenCode
git clone https://github.com/xunhe730/ZotPilot.git ~/.config/opencode/skills/zotpilot

# OpenClaw
git clone https://github.com/xunhe730/ZotPilot.git ~/.openclaw/skills/zotpilot
```

**2. 注册 MCP 服务器：**

```bash
# Claude Code
claude mcp add -s user zotpilot -- zotpilot

# Codex CLI
codex mcp add zotpilot -- zotpilot

# OpenCode / OpenClaw
# 按提示添加 MCP server，command 填 zotpilot，transport 选 stdio
```

**3. 重启你的 AI agent。**

### 第一次用会发生什么

你说"搜我的 Zotero"时，Skill 会走一遍安装流程：

1. 检测到缺少 `zotpilot` 命令，自动通过 `uv tool install` 安装
2. 检测 Zotero 数据目录，问你选哪个嵌入模型
3. 注册 MCP 服务器（如果还没注册的话）
4. 你重启一次，MCP 工具生效
5. 索引论文，每篇 2-5 秒
6. 之后直接问就行

**嵌入模型有三个选项：**

| 模型 | 需要 API Key | 质量 | 离线 | 维度 |
|------|:---:|------|:---:|------|
| Gemini `gemini-embedding-001` | 是（[免费额度](https://aistudio.google.com/apikey)） | MTEB 排名第一 | 否 | 768 |
| DashScope `text-embedding-v3` | 是（[阿里云百炼](https://bailian.console.aliyun.com/)） | 很好 | 否 | 1024 |
| Local `all-MiniLM-L6-v2` | 否 | 够用 | 是 | 384 |

注意：选了之后不好换。三个模型的向量维度不一样，换模型要 `zotpilot index --force` 全部重新索引。先想好再选。

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

## 26 个 MCP 工具

<details>
<summary>搜索（6 个）</summary>

| 工具 | 说明 |
|------|------|
| `search_papers` | 语义搜索，可以按章节、期刊加权 |
| `search_topic` | 按主题找论文，结果按文档去重 |
| `search_boolean` | 精确词匹配（AND/OR） |
| `search_tables` | 搜表格内容 |
| `search_figures` | 搜图表标题 |
| `get_passage_context` | 展开某个结果的上下文 |

</details>

<details>
<summary>浏览（6 个）</summary>

| 工具 | 说明 |
|------|------|
| `get_library_overview` | 列出所有论文和索引状态 |
| `get_paper_details` | 看一篇论文的完整元数据 |
| `list_collections` | 列出所有文件夹 |
| `get_collection_papers` | 看某个文件夹里的论文 |
| `list_tags` | 列出所有标签 |
| `get_index_stats` | 索引状态：多少篇、多少 chunk |

</details>

<details>
<summary>写操作（11 个）</summary>

| 工具 | 说明 |
|------|------|
| `add_item_tags` / `remove_item_tags` | 加/删标签（单篇） |
| `set_item_tags` | 替换全部标签（单篇） |
| `add_to_collection` / `remove_from_collection` | 移进/移出文件夹（单篇） |
| `create_collection` | 建文件夹 |
| `batch_tags(action="add\|set\|remove")` | 批量标签操作（最多 100 篇） |
| `batch_collections(action="add\|remove")` | 批量文件夹操作（最多 100 篇） |

</details>

<details>
<summary>引用（3 个）</summary>

| 工具 | 说明 |
|------|------|
| `find_citing_papers` | 谁引了这篇（OpenAlex） |
| `find_references` | 这篇引了谁 |
| `get_citation_count` | 被引次数 |

</details>

<details>
<summary>管理（4 个）</summary>

| 工具 | 说明 |
|------|------|
| `index_library` | 索引新论文（增量） |
| `get_reranking_config` | 看排序权重 |
| `get_vision_costs` | 看视觉 API 用量 |

</details>

---

## 工作原理

ZotPilot 是一个 AI Agent Skill：一个包含指令文件（[SKILL.md](SKILL.md)）和引导脚本（[scripts/run.py](scripts/run.py)）的仓库，AI agent 加载后会启动一个 MCP 服务器。

```
索引（跑一次）
Zotero SQLite ──→ PDF 提取 ──→ 分块 + 章节分类 ──→ 向量嵌入 ──→ ChromaDB

使用（每次查询）
AI Agent ──→ 26 个 MCP 工具 ──┬── 语义搜索 ──→ ChromaDB ──→ 重排序 ──→ 结果
                               ├── 引用图谱 ──→ OpenAlex
                               ├── 文献浏览 ──→ Zotero SQLite
                               └── 写操作   ──→ Zotero Web API ──→ 同步回 Zotero
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
│   └── install-steps.md            # 手动安装参考
└── src/zotpilot/                   # MCP 服务器源码
```

### 数据存储

```
~/.config/zotpilot/config.json      # 配置文件（Zotero 路径、嵌入模型选择）
~/.local/share/zotpilot/chroma/     # 向量索引
```

---

## 启用写操作

搜索和引用不需要额外配置，装好就能用。打标签、建集合这些写操作需要 Zotero Web API 密钥。

1. 去 [zotero.org/settings/keys](https://www.zotero.org/settings/keys) 建一个 key，勾上 "Allow library access" 和 "Allow write access"
2. 记下页面上的 User ID（是个数字，不是用户名）
3. 告诉 agent：

> 帮我启用 ZotPilot 写操作，我的 Zotero API Key 是 `xxxxx`，User ID 是 `12345`。

<details>
<summary>手动配置</summary>

**Claude Code：**

```bash
claude mcp remove zotpilot
claude mcp add -s user \
  -e GEMINI_API_KEY=<gemini密钥> \
  -e ZOTERO_API_KEY=<zotero密钥> \
  -e ZOTERO_USER_ID=<用户ID> \
  zotpilot -- zotpilot
```

**Codex CLI：**

```bash
codex mcp remove zotpilot
codex mcp add zotpilot \
  --env GEMINI_API_KEY=<gemini密钥> \
  --env ZOTERO_API_KEY=<zotero密钥> \
  --env ZOTERO_USER_ID=<用户ID> \
  -- zotpilot
```

或者直接编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.zotpilot]
command = "zotpilot"
env = { GEMINI_API_KEY = "...", ZOTERO_API_KEY = "...", ZOTERO_USER_ID = "..." }
```

重启 agent。

</details>

不配也行。搜索和引用照样能用，只有标签和集合管理需要这个 key。

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

Claude Code、Codex CLI、OpenCode、OpenClaw。只要支持 Skill + MCP 协议的 agent 都行。

</details>

<details>
<summary>Gemini 嵌入花多少钱？</summary>

免费额度大概 1,000 请求/天。一篇 10 页的论文大约用 1 次请求（每 32 个文本块算 1 次），搜索每次也是 1 次。免费额度够索引几百篇。超出后 $0.15/百万 token，基本可以忽略。Local 模型不花钱。

</details>

<details>
<summary>DashScope/百炼怎么样？</summary>

阿里云百炼的 `text-embedding-v3`，1024 维。国内不用翻墙，¥0.0005/千 token。装的时候选 `--provider dashscope`，key 在 https://bailian.console.aliyun.com/ 拿。

</details>

<details>
<summary>本地模型怎么样？</summary>

`all-MiniLM-L6-v2`，80MB 左右，第一次用自动下载，之后不联网。质量比 Gemini 差一些（384 维 vs 768 维），几百篇以内的库够用。

</details>

<details>
<summary>索引多久？占多大空间？</summary>

每篇 2-5 秒，300 篇大概 15 分钟。索引大小约 1MB / 100 篇。`--limit 10` 可以先试试。跑过的不会重复跑。

</details>

<details>
<summary>扫描版 PDF / 图表 / 特别长的书怎么办？</summary>

- 扫描版：PyMuPDF 有内置 OCR，自动识别
- 图表：提取的是标题文字和上下文段落，不是图片本身。图片 PNG 存在本地
- 超长文献：默认跳过 40 页以上的（`--max-pages` 可以调），也可以用 `--item-key` 单独索引某一篇
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
| 找不到 Skill | `ls ~/.claude/skills/zotpilot/SKILL.md`（Claude Code）或 `ls ~/.agents/skills/zotpilot/SKILL.md`（Codex） |
| `zotpilot: command not found` | `python3 scripts/run.py status`（会自动装） |
| MCP 工具没出来 | 重新注册 MCP 服务器然后重启 |
| 搜出来是空的 | 先跑 `zotpilot index`，或者换个更宽泛的搜索词 |
| `GEMINI_API_KEY not set` | 设环境变量，或 `zotpilot setup --non-interactive --provider local` 换本地模型 |
| 不知道哪出了问题 | 跑 `zotpilot doctor` |

更多见 [references/troubleshooting.md](references/troubleshooting.md)。

---

<details>
<summary>开发 / 贡献</summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest              # 177 个测试
uv run ruff check src/
```

欢迎贡献，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

</details>

---

```bash
git clone https://github.com/xunhe730/ZotPilot.git ~/.claude/skills/zotpilot
# 重启 Claude Code，跟 AI 说"搜一下我的 Zotero"
```

---

<div align="center">
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">报告问题</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/issues">功能建议</a> &middot;
    <a href="https://github.com/xunhe730/ZotPilot/discussions">讨论</a>
  </p>
  <sub>MIT License &copy; 2026 Xiaodong Zhuang</sub>
</div>
