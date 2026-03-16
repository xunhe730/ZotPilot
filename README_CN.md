<div align="center">

# ZotPilot

**让 AI 接管你的 Zotero。**

阅读、搜索、理解、整理——全部通过自然语言完成。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)

[English](README.md) | 中文

</div>

---

ZotPilot 是一个 MCP 服务器，赋予 AI 助手对 Zotero 文献库的**完整控制权**——不仅是搜索，还包括浏览、整理和深度理解你的整个研究收藏。它把 Zotero 从被动的文件柜变成主动的研究伙伴。

## 它有什么不同

大多数 Zotero 集成只做一件事：关键词搜索。ZotPilot 做所有事：

| | Zotero 自带 | 其他 MCP 工具 | **ZotPilot** |
|---|:---:|:---:|:---:|
| 关键词搜索 | Yes | Yes | Yes |
| 语义搜索（按含义） | | | **Yes** |
| 搜索表格和图表内容 | | | **Yes** |
| 引用图谱探索 | | | **Yes** |
| 章节感知排序 | | | **Yes** |
| 期刊质量加权 | | | **Yes** |
| 浏览集合和标签 | Yes | 部分 | **Yes** |
| 管理标签和集合 | Yes | 部分 | **Yes** |
| 中文查询支持 | | | **Yes** |
| 100% 本地运行 | Yes | | **Yes** |

**一个 MCP 服务器。完整 Zotero 访问。无需插件。**

## 实际效果

### 按语义搜索，不只是关键词

```
你：     "找关于睡眠如何影响记忆形成的论文"
Claude:  找到 8 篇相关论文。匹配度最高的是 Smith et al. (2023)，
         结果部分 (p.12)："Stage 2 NREM 睡眠中的纺锤波密度与隔夜
         记忆改善显著相关 (r=0.67, p<0.001)..."
```

### 对话式文献库整理

```
你：     "给所有深度学习相关论文打上 'DL' 标签，移到 'Neural Networks' 集合"
Claude:  找到 23 篇深度学习相关论文。已为全部 23 篇添加 'DL' 标签。
         19 篇已移至 'Neural Networks'（4 篇之前已在其中）。
```

### 探索引用网络

```
你：     "哪些论文引用了 Wang et al. 2022？其中哪些是 Q1 期刊？"
Claude:  47 篇论文引用了此工作。12 篇来自 Q1 期刊。被引最多的
         （89 次）是 Chen et al. (2023) 发表在 Nature Methods...
```

### 跨论文查找数据

```
你：     "展示比较不同方法分类准确率的表格"
Claude:  在 4 篇论文中找到 6 个包含准确率对比的表格...
         [Li et al. 2024 表 3: CNN 94.2%, Transformer 96.8%, ...]
```

## 三分钟上手

```bash
# 1. 安装
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 2. 配置（自动检测 Zotero 文献库）
zotpilot setup

# 3. 索引论文
zotpilot index

# 4. 添加到 AI 客户端
```

添加到 MCP 客户端配置（Claude Code、Cursor、Windsurf 等）：

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

搞定。你的 AI 现在可以阅读、搜索和整理整个 Zotero 文献库了。

## 24 个工具——完整覆盖 Zotero

### 搜索与发现
| 工具 | 功能 |
|------|------|
| `search_papers` | 语义搜索，支持章节/期刊加权，按作者/年份/标签/集合过滤 |
| `search_topic` | 按主题查找最相关论文，按文档去重 |
| `search_boolean` | 精确词匹配（AND/OR），使用 Zotero 全文索引 |
| `search_tables` | 搜索表格内容——表头、单元格、标题 |
| `search_figures` | 搜索图表标题和描述 |
| `get_passage_context` | 展开任意搜索结果的上下文段落 |

### 浏览与理解
| 工具 | 功能 |
|------|------|
| `get_library_overview` | 分页展示全部论文及索引状态 |
| `get_paper_details` | 完整元数据：标题、作者、年份、摘要、DOI、标签、集合 |
| `list_collections` | 所有 Zotero 文件夹及层级关系 |
| `get_collection_papers` | 特定集合中的论文 |
| `list_tags` | 所有标签按使用频率排序 |
| `get_index_stats` | 索引健康状态：文档数、chunk 数、未索引论文 |

### 整理与写入
| 工具 | 功能 |
|------|------|
| `add_item_tags` / `remove_item_tags` | 添加或删除标签，不影响已有标签 |
| `set_item_tags` | 替换论文的全部标签 |
| `add_to_collection` / `remove_from_collection` | 在文件夹间移动论文 |
| `create_collection` | 创建新文件夹（支持嵌套） |

### 引用与影响力
| 工具 | 功能 |
|------|------|
| `find_citing_papers` | 谁引用了这篇论文？（通过 OpenAlex） |
| `find_references` | 这篇论文引用了什么？ |
| `get_citation_count` | 被引次数和参考文献数 |

### 索引与管理
| 工具 | 功能 |
|------|------|
| `index_library` | 索引新增/变更的论文（增量更新） |
| `get_reranking_config` | 查看和理解排序权重 |
| `get_vision_costs` | 监控视觉 API 表格提取的用量 |

## 工作原理

```
┌─────────────┐     ┌──────────┐     ┌─────────────┐     ┌──────────┐
│ Zotero      │────>│ PDF 提取 │────>│ 向量嵌入    │────>│ ChromaDB │
│ SQLite 数据库│     │ + 表格   │     │ (Gemini /   │     │ 向量存储 │
│ (只读)      │     │ + 图表   │     │  本地模型)  │     │          │
│             │     │ + OCR    │     └─────────────┘     └────┬─────┘
│             │     └──────────┘                               │
│ Zotero      │     ┌──────────┐     ┌─────────────┐          │
│ Web API     │<────│ 重排序器 │<────│ 检索器      │<─────────┘
│ (写操作)    │     │ 章节权重 │     │ 语义搜索    │
└─────────────┘     │ +期刊质量│     │ +上下文扩展 │
                    └──────────┘     └─────────────┘
                          │
                    ┌─────┴─────┐
                    │ AI 客户端 │
                    │ Claude    │
                    │ Cursor    │
                    │ Windsurf  │
                    └───────────┘
```

### 关键设计

- **本地优先** — 论文永远不离开你的电脑
- **只读 SQLite** — Zotero 运行时也安全
- **Web API 写入** — 标签/集合变更同步回 Zotero
- **章节感知** — 知道段落来自方法、结果还是参考文献
- **期刊质量** — Q1 期刊结果排名更高（SCImago 数据）
- **中文支持** — 自动翻译中文查询，双语并行搜索

## 嵌入模型

| 模型 | API Key | 速度 | 质量 | 离线 |
|------|---------|------|------|------|
| **Gemini** `gemini-embedding-001` | 需要（有免费额度） | 快 | 最佳（MTEB 第一） | 否 |
| **本地** `all-MiniLM-L6-v2` | 不需要 | 中等 | 良好 | 是 |

## 平台支持

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| 核心功能（搜索、索引、整理） | Yes | Yes | Yes |
| Zotero 自动检测 | Yes | Yes | Yes |
| PDF + OCR 提取 | Yes | Yes | Yes |
| 视觉表格提取 | Yes | Yes | Yes |
| PaddleOCR（可选） | Yes | Yes | 部分 |

## 开发

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest              # 106 个测试
uv run ruff check src/     # 代码检查
```

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 路线图

- [x] 24 个 MCP 工具——完整覆盖 Zotero 读/写/搜索
- [x] 语义搜索 + 章节感知重排序
- [x] 表格和图表提取与搜索
- [x] 引用图谱（OpenAlex）
- [x] 期刊质量加权（SCImago）
- [x] 中文查询自动翻译
- [x] 跨平台（macOS、Linux、Windows）
- [ ] OpenAI / Ollama 嵌入模型
- [ ] PyPI 发布（`pip install zotpilot`）
- [ ] 基于搜索结果生成文献综述
- [ ] `zotpilot doctor` 诊断命令

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

---

<div align="center">

**一个 MCP 服务器，掌控你的整个 Zotero 文献库。**

[报告 Bug](https://github.com/xunhe730/ZotPilot/issues) · [功能建议](https://github.com/xunhe730/ZotPilot/issues) · [讨论区](https://github.com/xunhe730/ZotPilot/discussions)

</div>
