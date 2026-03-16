<div align="center">
  <h1>🧭 ZotPilot</h1>
  <h3>让 AI 接管你的 Zotero</h3>
  <p>
    按语义搜索、探索引用、用自然语言整理文献。<br>
    <b>一个 MCP 服务器，完整 Zotero 访问，无需插件。</b>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/MCP-Compatible-00B265?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDJMMiA3bDEwIDUgMTAtNXoiIGZpbGw9IiNmZmYiLz48L3N2Zz4=&logoColor=white" alt="MCP">
    <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License">
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-✓-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-✓-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-✓-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>
  <p>
    <img src="https://img.shields.io/github/stars/xunhe730/ZotPilot?style=flat-square&logo=github" alt="GitHub stars">
    <img src="https://img.shields.io/github/forks/xunhe730/ZotPilot?style=flat-square&logo=github" alt="GitHub forks">
    <img src="https://img.shields.io/github/v/release/xunhe730/ZotPilot?style=flat-square&logo=github" alt="Latest version">
  </p>
</div>

<p align="center">
  <a href="#-快速开始">快速开始</a> •
  <a href="#-功能特性">功能特性</a> •
  <a href="#️-24-个工具">工具列表</a> •
  <a href="#️-工作原理">架构</a> •
  <a href="README.md">English</a>
</p>

---

## 👋 为什么选择 ZotPilot？

你的 Zotero 里有几百篇论文。你*记得*读过一篇关于"睡眠纺锤波与记忆巩固"的研究——但 Zotero 只能匹配精确关键词。你没法按*语义*搜索，没法追问，更没法说"按主题整理一下"。

**ZotPilot 改变了这一切。** 它赋予 AI 助手对 Zotero 文献库的完整读写权限——语义搜索、引用探索、表格提取、标签管理……全部通过自然语言完成。

<table>
<tr>
<td width="50%" valign="top">

**没有 ZotPilot**
- 手动猜关键词
- 逐个打开 PDF 找数据
- 一个个复制粘贴标签
- 无法问"谁引用了这篇？"
- 在 Zotero 和 AI 之间来回切换

</td>
<td width="50%" valign="top">

**有了 ZotPilot**
- _"找关于睡眠与记忆的论文"_
- _"展示准确率对比表格"_
- _"给所有 DL 论文打标签并移到集合"_
- _"谁在 Q1 期刊引用了 Wang 2022？"_
- AI 直接读取你的文献库

</td>
</tr>
</table>

---

## ✨ 功能特性

<table>
<tr>
<td width="50%" valign="top">

### 🔍 语义搜索
按含义而非关键词查找段落。结果按章节相关性和期刊质量排序。

### 📊 表格与图表搜索
在整个文献库中搜索提取的表格内容和图表标题。

### 🌐 引用图谱
探索引用关系、查找参考文献、检查影响力——基于 OpenAlex。

</td>
<td width="50%" valign="top">

### 🏷️ 文献库管理
添加/删除标签、在集合间移动论文、创建文件夹——全部通过对话完成。

### 🎯 智能排序
组合评分：语义相似度 × 章节权重 × 期刊质量（SCImago）。

### 🀄 中文支持
自动翻译中文查询，双语并行搜索。

### 🧠 内置 Agent Skill
自带 [Skill 文件](skill/SKILL.md)，教会 AI _如何_使用 ZotPilot——自动选择工具、串联多步工作流、排查错误。无需提示工程。

</td>
</tr>
</table>

### 功能对比

| | Zotero 自带 | 其他 MCP 工具 | **ZotPilot** |
|---|:---:|:---:|:---:|
| 关键词搜索 | ✅ | ✅ | ✅ |
| 语义搜索（按含义） | | | ✅ |
| 搜索表格与图表 | | | ✅ |
| 引用图谱 | | | ✅ |
| 章节感知排序 | | | ✅ |
| 期刊质量加权 | | | ✅ |
| 浏览集合与标签 | ✅ | 部分 | ✅ |
| 管理标签与集合 | ✅ | 部分 | ✅ |
| 中文查询支持 | | | ✅ |
| Agent Skill（引导式工作流） | | | ✅ |
| 100% 本地处理 | ✅ | | ✅ |

---

## 📥 快速开始

```bash
# 安装
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 配置（自动检测 Zotero）
zotpilot setup

# 索引论文
zotpilot index
```

然后添加到你的 MCP 客户端：

<div align="center">
  <table>
    <tr>
      <td align="center"><b>Claude Code</b></td>
      <td align="center"><b>Cursor</b></td>
      <td align="center"><b>Windsurf</b></td>
    </tr>
    <tr>
      <td><code>~/.claude.json</code></td>
      <td><code>.cursor/mcp.json</code></td>
      <td><code>~/.codeium/windsurf/mcp_config.json</code></td>
    </tr>
  </table>
</div>

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"],
      "env": {
        "GEMINI_API_KEY": "你的密钥"
      }
    }
  }
}
```

> **嵌入模型选择：** Gemini（推荐，有免费额度）或 Local（离线，无需 API key）。在 `zotpilot setup` 中选择。

---

## 🧠 Agent Skill——让 AI 学会做研究

大多数 MCP 服务器给 AI 一堆工具，然后听天由命。ZotPilot 自带 **[Agent Skill](skill/SKILL.md)** ——一份结构化指令文件，教会 AI _如何用你的文献库做研究_。

```
你：     "帮我写一段关于 EEG 脑机接口的 Related Work"

Skill 引导 AI：
  ① 检查索引就绪状态（get_index_stats）
  ② 选择 search_topic（不是 search_papers——这是综述任务）
  ③ 使用 section_weights={"results": 1.0, "conclusion": 1.0} 聚焦发现
  ④ 串联：search_topic → get_paper_details → find_references → search_papers
  ⑤ 将结果格式化为带引用的可读文本，而不是原始 JSON
```

**Skill 编码了什么：**
- **工具选择逻辑** — 用户意图 → 正确工具的决策表
- **参数知识** — 何时用 `required_terms`（缩写词）、`section_weights`（聚焦区域）、`chunk_types`（混合内容）
- **工作流链** — 文献综述、按主题整理、查找特定论文的完整步骤
- **错误恢复** — 索引为空、DOI 缺失、API key 未设置时怎么办
- **输出格式** — 如何呈现结果（引用段落、标注页码、按论文分组）

**安装**（Claude Code）：
```bash
cp -r ZotPilot/skill/ ~/.claude/skills/zotpilot/
```

没有 Skill，AI 仍可调用全部 24 个工具——但不知道先选哪个、哪些参数重要、如何串联。Skill 是"我有工具"和"我会做研究"的区别。

---

## 🛠️ 24 个工具

### 🔍 搜索与发现

| 工具 | 功能 |
|------|------|
| `search_papers` | 语义搜索，支持章节/期刊加权和多维过滤 |
| `search_topic` | 主题级论文发现，按文档去重 |
| `search_boolean` | 精确词匹配（AND/OR），使用 Zotero 全文索引 |
| `search_tables` | 搜索表头、单元格、标题 |
| `search_figures` | 搜索图表标题和描述 |
| `get_passage_context` | 展开任意结果的上下文段落 |

### 📚 浏览与理解

| 工具 | 功能 |
|------|------|
| `get_library_overview` | 分页展示全部论文及索引状态 |
| `get_paper_details` | 完整元数据：标题、作者、摘要、DOI、标签 |
| `list_collections` | 所有 Zotero 文件夹及层级 |
| `get_collection_papers` | 特定集合中的论文 |
| `list_tags` | 所有标签按频率排序 |
| `get_index_stats` | 索引健康：文档数、chunk 数、未索引论文 |

### 🏷️ 整理与写入

| 工具 | 功能 |
|------|------|
| `add_item_tags` / `remove_item_tags` | 添加或删除标签（非破坏性） |
| `set_item_tags` | 替换论文的全部标签 |
| `add_to_collection` / `remove_from_collection` | 在文件夹间移动论文 |
| `create_collection` | 创建新文件夹（支持嵌套） |

### 📈 引用与影响力

| 工具 | 功能 |
|------|------|
| `find_citing_papers` | 谁引用了这篇？（OpenAlex） |
| `find_references` | 这篇引用了什么？ |
| `get_citation_count` | 被引次数和参考文献数 |

### ⚙️ 管理

| 工具 | 功能 |
|------|------|
| `index_library` | 索引新增/变更论文（增量） |
| `get_reranking_config` | 查看排序权重 |
| `get_vision_costs` | 监控视觉 API 用量 |

---

## 🏗️ 工作原理

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│   Zotero     │────▶│ PDF 提取 │────▶│  向量嵌入   │────▶│ ChromaDB │
│  SQLite 数据库│     │ + 表格   │     │  Gemini /   │     │ 向量存储 │
│  (只读)      │     │ + 图表   │     │   本地模型  │     │          │
│              │     │ + OCR    │     └─────────────┘     └────┬─────┘
│              │     └──────────┘                              │
│  Zotero      │     ┌──────────┐     ┌──────────┐            │
│  Web API ◀───┼─────│ 重排序器 │◀────│ 检索器   │◀───────────┘
│  (写操作)    │     │ 章节权重 │     │ 语义搜索 │
└──────────────┘     │ +期刊质量│     │ +上下文  │
                     └────┬─────┘     └──────────┘
                          │
                   ┌──────┴──────┐
                   │  AI 客户端  │
                   │ Claude Code │
                   │   Cursor    │
                   │  Windsurf   │
                   └─────────────┘
```

<details>
<summary><b>关键设计决策</b></summary>

- **本地优先** — 论文永远不离开你的电脑
- **只读 SQLite** — Zotero 运行时也安全
- **Web API 写入** — 标签/集合变更通过 Zotero 官方 API 同步
- **非对称嵌入** — 文档和查询使用不同编码（Gemini）
- **章节感知** — 知道段落来自方法、结果还是参考文献
- **期刊质量** — Q1 期刊排名更高（SCImago 分区数据）

</details>

---

## 📦 嵌入模型

| 模型 | API Key | 维度 | 质量 | 离线 |
|------|---------|------|------|------|
| **Gemini** `gemini-embedding-001` | 需要（有免费额度） | 768 | 🥇 MTEB 第一 | 否 |
| **本地** `all-MiniLM-L6-v2` | 不需要 | 384 | 良好 | ✅ 是 |

---

## 🗺️ 路线图

- [x] 24 个 MCP 工具——完整覆盖 Zotero 读/写/搜索
- [x] 语义搜索 + 章节感知重排序
- [x] 表格与图表提取和搜索
- [x] 引用图谱（OpenAlex）
- [x] 期刊质量加权（SCImago）
- [x] 中文查询自动翻译
- [x] 跨平台（macOS、Linux、Windows）
- [ ] OpenAI / Ollama 嵌入模型
- [ ] PyPI 发布（`pip install zotpilot`）
- [ ] 基于搜索结果生成文献综述
- [ ] `zotpilot doctor` 诊断命令

---

## 🤝 参与贡献

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

<div align="center">
  <a href="https://www.star-history.com/#xunhe730/ZotPilot&type=Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date&theme=dark" />
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date" />
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=xunhe730/ZotPilot&type=Date" width="600" />
    </picture>
  </a>
</div>

<div align="center">
  <br>
  <p>
    <a href="https://github.com/xunhe730/ZotPilot/issues">报告 Bug</a> ·
    <a href="https://github.com/xunhe730/ZotPilot/issues">功能建议</a> ·
    <a href="https://github.com/xunhe730/ZotPilot/discussions">讨论区</a>
  </p>
  <sub>MIT License © 2026 Xiaodong Zhuang</sub>
</div>
