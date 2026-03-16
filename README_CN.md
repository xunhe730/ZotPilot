# ZotPilot

基于 AI 的 Zotero 文献库语义搜索工具。ZotPilot 是一个 MCP 服务器，让 AI 助手能够搜索、分析和管理你的学术论文。

[English](README.md) | 中文

## 为什么选择 ZotPilot？

Zotero 自带搜索只支持关键词匹配。ZotPilot 在此基础上增加了：

- **语义搜索** — 按语义而非关键词查找论文
- **表格与图表搜索** — 在整个文献库中检索数据
- **引用图谱** — 通过 OpenAlex 发现引用关系
- **章节感知排序** — 优先展示方法、结果或结论部分的内容
- **期刊质量加权** — 提升高影响力期刊论文的排名
- **文献库管理** — 通过 AI 使用标签和集合组织论文

## 快速开始

### 研究人员

```bash
# 安装
git clone https://github.com/xunhe730/ZotPilot.git
uv tool install ./ZotPilot

# 配置（自动检测 Zotero，配置嵌入模型）
zotpilot setup

# 索引文献库
zotpilot index

# 添加到 Claude Code（编辑 ~/.claude/settings.json，见下方）
```

### 开发者

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
uv sync --extra dev
uv run pytest
```

## MCP 客户端配置

### Claude Code

添加到 `~/.claude/settings.json`：

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"]
    }
  }
}
```

### OpenCode / OpenClaw

使用相同的配置格式，将 `zotpilot` MCP 服务器添加到客户端配置中。

## 功能特性

### 24 个 MCP 工具

| 类别 | 工具 |
|------|------|
| **搜索** | `search_papers`（论文搜索）、`search_topic`（主题搜索）、`search_boolean`（布尔搜索）、`search_tables`（表格搜索）、`search_figures`（图表搜索） |
| **上下文** | `get_passage_context`（获取段落上下文） |
| **文献库** | `list_collections`（列出集合）、`get_collection_papers`（获取集合论文）、`list_tags`（列出标签）、`get_paper_details`（获取论文详情）、`get_library_overview`（文献库概览） |
| **索引** | `index_library`（索引文献库）、`get_index_stats`（获取索引统计） |
| **引用** | `find_citing_papers`（查找引用论文）、`find_references`（查找参考文献）、`get_citation_count`（获取引用数） |
| **写入** | `set_item_tags`（设置标签）、`add_item_tags`（添加标签）、`remove_item_tags`（删除标签）、`add_to_collection`（添加到集合）、`remove_from_collection`（从集合移除）、`create_collection`（创建集合） |
| **管理** | `get_reranking_config`（获取重排序配置）、`get_vision_costs`（获取视觉成本） |

### 嵌入模型

| 模型 | API Key | 维度 | 质量 |
|------|---------|------|------|
| Gemini (`gemini-embedding-001`) | 需要 | 768 | 最佳（MTEB 排名第一） |
| 本地 (`all-MiniLM-L6-v2`) | 无需 | 384 | 良好（离线可用） |

### Agent Skill（智能体技能）

安装 `skill/` 目录以获取引导式工作流：
- 自动选择搜索策略
- 文献综述模板
- 文献库组织工作流

## 架构

```
Zotero SQLite → zotero_client → indexer → pdf/ → embeddings → vector_store (ChromaDB)
                                                                      ↓
查询 → embeddings → vector_store → reranker → 响应
```

详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 环境变量

| 变量 | 是否必需 | 说明 |
|------|----------|------|
| `GEMINI_API_KEY` | Gemini 嵌入时需要 | Google Gemini API 密钥 |
| `OPENALEX_EMAIL` | 可选 | 用于 OpenAlex 高速访问（10 请求/秒 vs 1 请求/秒） |
| `ANTHROPIC_API_KEY` | 可选 | 用于基于视觉的表格提取 |
| `ZOTERO_API_KEY` | 可选 | 用于写入操作（标签、集合） |
| `ZOTERO_USER_ID` | 可选 | Zotero 用户 ID，用于写入操作 |

## 从 deep-zotero 迁移

如果你之前使用 deep-zotero：
- `zotpilot setup` 会自动检测并提示迁移配置
- 已有的 ChromaDB 索引完全兼容，无需重新索引
- 保持相同嵌入模型即可直接使用

## 许可证

MIT — 详见 [LICENSE](LICENSE)。
