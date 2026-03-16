<div align="center">

# ZotPilot

**让 AI 真正读懂你的文献库。**

提问、发现规律、轻松整理。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)

[English](README.md) | 中文

</div>

---

ZotPilot 将你的 Zotero 文献库接入 Claude 等 AI 助手，让你用**自然语言搜索论文内容**，而不只是匹配关键词。它在本地运行，直接读取你的 Zotero 数据库，让 AI 理解你的整个研究收藏。

## 痛点

你的 Zotero 里有几百篇论文。你*记得*读过一篇关于"睡眠纺锤波与记忆巩固的关系"的研究——但 Zotero 搜索只能匹配精确关键词。你没法按*语义*搜索。

## 解决方案

```
你：     "找关于睡眠如何影响记忆形成的论文"
Claude:  找到 8 篇相关论文。匹配度最高的是 Smith et al. (2023)，
         具体在结果部分 (p.12)："Stage 2 NREM 睡眠中的纺锤波密度
         与隔夜记忆改善显著相关 (r=0.67, p<0.001)..."
```

ZotPilot 将你的 PDF 索引为语义向量，即使查询词和原文用词不同，AI 也能找到相关段落。

## 三分钟上手

```bash
# 1. 安装
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 2. 配置（自动检测 Zotero 文献库）
zotpilot setup

# 3. 索引你的论文
zotpilot index

# 4. 添加到 AI 客户端
```

将以下配置添加到你的 MCP 客户端（Claude Code、Cursor、OpenCode 等）：

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

搞定。现在可以用 AI 问任何关于你论文的问题了。

## 能做什么？

### 按语义搜索，不只是关键词

> "找比较深度学习和传统方法在 EEG 分类上的研究"

返回文献库中排名最高的段落，附带完整上下文、引用键和页码。

### 搜索表格和图表

> "展示包含分类准确率的结果表格"

在所有论文的 PDF 中查找数据表格——不仅仅是标题。

### 探索引用网络

> "哪些论文引用了 Smith et al. 2023？它们又引用了什么？"

通过 [OpenAlex](https://openalex.org/) 映射文献库中任意论文的引用图谱。

### 用 AI 整理文献库

> "给所有关于 transformer 的论文打上 'deep-learning' 标签，并添加到 'Neural Networks' 集合"

通过自然语言读写标签和集合。

### 智能排序

搜索结果综合考虑：
- **语义相似度** — 与查询的语义匹配程度
- **章节权重** — 结果/方法 > 引言/参考文献
- **期刊质量** — Q1 期刊权重更高（基于 SCImago）

## 24 个 MCP 工具

| 类别 | 工具 | 功能 |
|------|------|------|
| **搜索** | `search_papers` `search_topic` `search_boolean` `search_tables` `search_figures` | 查找段落、论文、表格、图表 |
| **上下文** | `get_passage_context` | 展开搜索结果的上下文 |
| **文献库** | `list_collections` `get_collection_papers` `list_tags` `get_paper_details` `get_library_overview` | 浏览 Zotero 文献库 |
| **索引** | `index_library` `get_index_stats` | 构建和监控搜索索引 |
| **引用** | `find_citing_papers` `find_references` `get_citation_count` | 通过 OpenAlex 探索引用图谱 |
| **写入** | `set_item_tags` `add_item_tags` `remove_item_tags` `add_to_collection` `remove_from_collection` `create_collection` | 整理文献库 |
| **管理** | `get_reranking_config` `get_vision_costs` | 配置和监控 |

## 工作原理

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│ Zotero      │────>│ PDF      │────>│ 向量嵌入    │────>│ ChromaDB │
│ SQLite 数据库│     │ 提取器   │     │ (Gemini /   │     │ 向量存储 │
│ (只读)      │     │ + OCR    │     │  本地模型)  │     │          │
└─────────────┘     └──────────┘     └─────────────┘     └──────────┘
                                                               │
┌─────────────┐     ┌──────────┐     ┌─────────────┐          │
│ AI 客户端   │<────│ 重排序器 │<────│ 检索器      │<─────────┘
│ (Claude,    │     │ (章节权重│     │ (语义搜索)  │
│  Cursor...) │     │ +期刊质量│     │             │
└─────────────┘     │  )       │     └─────────────┘
                    └──────────┘
```

**关键设计：**
- **本地优先** — 你的论文永远不会离开你的电脑
- **只读 SQLite** — Zotero 运行时也安全
- **非对称嵌入** — 文档和查询使用不同编码（Gemini）
- **章节感知** — 知道段落来自方法、结果还是参考文献

## 嵌入模型选择

| 模型 | API Key | 速度 | 质量 | 离线 |
|------|---------|------|------|------|
| **Gemini** `gemini-embedding-001` | 需要（有免费额度） | 快 | 最佳（MTEB 第一） | 否 |
| **本地** `all-MiniLM-L6-v2` | 不需要 | 中等 | 良好 | 是 |

## 平台支持

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| 核心搜索 | Yes | Yes | Yes |
| Zotero 检测 | Yes | Yes | Yes |
| PDF 提取 | Yes | Yes | Yes |
| PaddleOCR（可选） | Yes | Yes | 部分 |

## 开发

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest              # 106 个测试
uv run ruff check src/     # 代码检查
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md) 了解如何添加嵌入模型、MCP 工具或修复 Bug。

## 路线图

- [x] Gemini + 本地嵌入模型
- [x] 24 个 MCP 工具（搜索、索引、引用、写入）
- [x] 章节感知重排序 + 期刊质量加权
- [x] 跨平台支持（macOS、Linux、Windows）
- [ ] OpenAI 嵌入模型
- [ ] Ollama 嵌入模型（完全本地 LLM）
- [ ] `zotpilot doctor` 诊断命令
- [ ] PyPI 发布（`pip install zotpilot`）
- [ ] 嵌入模型对比指南

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

---

<div align="center">

**为想让 AI 真正理解论文的研究者而建。**

[报告 Bug](https://github.com/xunhe730/ZotPilot/issues) | [功能建议](https://github.com/xunhe730/ZotPilot/issues) | [讨论区](https://github.com/xunhe730/ZotPilot/discussions)

</div>
