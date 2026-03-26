# CLAUDE.md

本文件为 Claude Code 在此仓库中工作时提供指引。

## 项目定位

ZotPilot 是一个 **MCP server**，给本地 Zotero 文献库加上语义搜索、章节感知检索、引用图谱查询和 AI 辅助整理功能。同时附带 Agent Skill（`SKILL.md`），提供跨平台安装引导和工具使用决策树。

**核心价值**：解决 Zotero 原生关键词搜索的局限——按语义搜论文、定位到具体章节、探索引用关系、批量整理标签集合，论文数据全程不离开用户电脑。

**分发方式**：clone 仓库到 agent 的 skills 目录，`SKILL.md` 提供安装引导，`scripts/run.py` 负责自动安装 CLI（`pip`/`uv`）并注册 MCP 服务器。

**支持平台**：macOS / Linux / Windows；Claude Code、Codex CLI、OpenCode、Gemini CLI、Cursor、Windsurf

## 技术栈

| 层次 | 技术 |
|------|------|
| MCP 框架 | FastMCP |
| 向量数据库 | ChromaDB |
| PDF 提取 | PyMuPDF（文本）+ Tesseract（OCR 兜底）+ Claude Haiku（可选视觉表格修复） |
| 嵌入模型 | Gemini `gemini-embedding-001` / DashScope `text-embedding-v4` / Local `all-MiniLM-L6-v2` |
| Zotero 读 | 本地 SQLite（`mode=ro&immutable=1`，只读，不影响 Zotero 客户端） |
| Zotero 写 | pyzotero（官方 Web API v3） |
| 引用图谱 | OpenAlex API（主）+ Semantic Scholar（辅，需 `S2_API_KEY`） |
| 包管理 | uv |
| 代码检查 | ruff + mypy |
| 测试 | pytest（覆盖率阈值 29%） |
| 运行时 | Python 3.10+ |

## 常用命令

```bash
# 安装（可编辑开发模式）
uv pip install -e ".[dev]"

# 直接运行 MCP 服务器
uv run zotpilot

# CLI 子命令
uv run zotpilot setup --non-interactive --provider gemini
uv run zotpilot index [--force] [--limit N] [--item-key KEY] [--max-pages N]
uv run zotpilot status [--json]
uv run zotpilot doctor [--full]
uv run zotpilot config set <key> <value>
uv run zotpilot register [--gemini-key KEY] [--zotero-api-key KEY] [--zotero-user-id ID]

# 测试
uv run pytest                          # 全部测试（覆盖率阈值 29%）
uv run pytest tests/test_config.py    # 单文件
uv run pytest -k test_name            # 单个测试

# 代码检查 / 类型检查
uv run ruff check src tests
uv run mypy src
```

## 代码搜索

**首选 `mgrep`**（语义搜索，比 grep/ripgrep 更精准）：

```bash
mgrep "heartbeat 断连原因"          # 自然语言语义搜索
mgrep "writer.check_has_pdf"        # 精确符号搜索
mgrep "translator fallback logic"   # 跨文件概念搜索
```

仅在 mgrep 不可用或需要精确正则时退回到 `Grep` 工具。

## 内部文档

> `docs/` 目录已加入 `.gitignore`，仅供本地参考，不提交到公开仓库。

- `docs/architecture.md` — 系统架构、组件说明、已知局限
- `docs/decisions.md` — 关键决策记录（按时间倒序）

**文档更新规则**：每次架构决策或涉及多个文件的实现变更，必须同步更新相应文档：
- 新增功能 / 重构 → 更新 `docs/architecture.md`
- 重要技术决策 → 在 `docs/decisions.md` 新增条目

## 架构

ZotPilot 是一个 **FastMCP 服务器**，提供 32 个工具用于对本地 Zotero 文献库进行语义搜索。架构分四层：

### 1. 入口点
- `cli.py` — argparse CLI，子命令：`setup`、`index`、`status`、`doctor`、`config`、`register`。无子命令时启动 MCP 服务器。
- `server.py` — 薄层 shim：import `state.mcp` 和 `tools/` 包（import 副作用注册所有工具），然后调用 `mcp.run()`。

### 2. MCP 状态与懒加载单例（`state.py`）
所有共享对象（`VectorStore`、`Retriever`、`Reranker`、`ZoteroClient`、`ZoteroWriter`、`ZoteroApiReader`、`IdentifierResolver`）均为懒加载单例，由单一 `threading.Lock` 保护。工具在每次请求时调用 `_get_retriever()`、`_get_zotero()` 等。`switch_library` 调用 `_reset_singletons()` 拆除所有单例。后台线程监控父进程 PID，父进程退出时调用 `os._exit(0)` 防止孤儿服务器进程。

### 3. 工具模块（`tools/`）
八个模块，均由 `tools/__init__.py` import 触发 `@mcp.tool` 装饰器注册：

| 模块 | 职责 |
|------|------|
| `search.py` | `search_papers`、`search_topic`、`search_boolean`、`search_tables`、`search_figures` |
| `context.py` | `get_passage_context`、`get_paper_details` |
| `library.py` | `get_library_overview`、`advanced_search`、`get_notes`、`list_tags`、`list_collections` 等 |
| `indexing.py` | `index_library`、`get_index_stats` |
| `citations.py` | `find_references`、`find_citing_papers`、`get_citation_count` |
| `write_ops.py` | `create_note`、`add_item_tags`、`set_item_tags`、`create_collection`、`add_to_collection` 等 |
| `admin.py` | `switch_library`、`get_reranking_config`、`get_vision_costs` |
| `ingestion.py` | `search_academic_databases`、`add_paper_by_identifier`、`ingest_papers` |

写操作（`write_ops.py`）需要 `ZOTERO_API_KEY` + `ZOTERO_USER_ID` 环境变量，使用 `ZoteroWriter`（pyzotero Web API）。只读工具使用 `ZoteroClient`（本地 SQLite）。

### 4. RAG 流水线
```
PDF 文件
  └─ pdf/extractor.py          （PyMuPDF 文本提取，OCR 兜底）
  └─ feature_extraction/       （视觉 API 处理图表，可选 PaddleOCR）
  └─ pdf/chunker.py            （文本 → 分块，含章节分类）
  └─ pdf/section_classifier.py （标注分块：摘要、方法、结果等）
  └─ embeddings/               （base.py 接口；gemini.py、dashscope.py、local.py 实现）
  └─ vector_store.py           （ChromaDB 封装；存储分块及元数据）

查询路径：
  retriever.py → vector_store.py → reranker.py（RRF + 章节/期刊权重）
```

### 无 RAG 模式
配置 `embedding_provider = "none"` 禁用向量索引。`_get_store_optional()` 返回 `None`，工具降级为 SQLite 元数据搜索。`advanced_search`、notes、tags、collections 在无索引时仍可工作。

## 配置

配置文件：`~/.config/zotpilot/config.json`（Unix）/ `%APPDATA%\zotpilot\config.json`（Windows）。
ChromaDB 数据：`~/.local/share/zotpilot/chroma/`（Unix）。

API 密钥始终优先从环境变量读取，其次从配置文件。`Config.save()` 永不将 API 密钥持久化到磁盘。

| 环境变量 | 用途 |
|---------|------|
| `GEMINI_API_KEY` | 嵌入（gemini 提供商） |
| `DASHSCOPE_API_KEY` | 嵌入（dashscope 提供商） |
| `ANTHROPIC_API_KEY` | 视觉提取（图表/表格） |
| `ZOTERO_API_KEY` | 写操作 |
| `ZOTERO_USER_ID` | Zotero 数字用户 ID |
| `S2_API_KEY` | Semantic Scholar（可选，更高速率限制） |

## Git 工作流

### 分支策略

- **`main`** — 生产分支，仅接受来自 `dev` 的 PR 合并，**禁止直接 push**
- **`dev`** — 日常开发分支，所有功能和修复都在此分支提交

### 规则

- **绝不** 直接 `git push origin main`
- 所有变更通过 PR 从 `dev` → `main` 合并
- 发版时：在 `dev` 完成发版清单 → 提 PR → 合并到 `main` → 在 `main` 打 tag

### 日常工作流

```bash
# 确保在 dev 分支
git checkout dev

# 功能开发完毕后推送
git push origin dev

# 需要发版时，提 PR
gh pr create --base main --head dev --title "release: vX.Y.Z"
```

## 版本管理

Claude 负责此项目的版本管理。当用户说"发版"、"release"或类似指令时，直接执行完整流程，无需逐步确认：

### 发版流程
1. **Commit** 所有暂存变更，使用规范 commit 消息（`feat:` / `fix:` / `docs:` 等）
2. **Tag** `vX.Y.Z` — 必须与 `pyproject.toml` 版本一致（CI 会校验）
3. **Push** commit + tag：`git push && git push --tags`
4. CI（`release.yml`）自动发布到 PyPI 并从 CHANGELOG 创建 GitHub Release

### 版本号规则
- `patch`（0.x.**Z**）：bug 修复、文档更新、测试新增
- `minor`（0.**Y**.0）：新用户功能（新 CLI 子命令、新 MCP 工具）
- `major`（**X**.0.0）：MCP 工具签名或配置格式的破坏性变更

### 发版清单
- [ ] `pyproject.toml` 版本已更新
- [ ] `src/zotpilot/__init__.py` `__version__` 与之同步
- [ ] `CHANGELOG.md` 顶部有 `## [X.Y.Z] - YYYY-MM-DD` 条目
- [ ] `README.md` 已反映新命令或功能
- [ ] `uv run pytest -q` 通过（覆盖率 ≥ 29%）
- [ ] commit → tag → push

### CHANGELOG 格式
遵循 CHANGELOG.md 中已建立的中英双语格式。
CI 的 `awk` 提取器读取前两个 `## [` 标题之间的内容——保持该结构不变。

## 关键设计模式

- **双重检查锁定的单例模式**：所有昂贵对象每个服务器进程初始化一次，`switch_library` 时重置。
- **无 RAG 降级**：`embedding_provider="none"` 使仅元数据工具在无 ChromaDB 或嵌入 API 时也能工作。
- **嵌入提供商抽象**：`embeddings/base.py` 定义 `Embedder` 接口；`embeddings/__init__.py:create_embedder(config)` 返回正确实现。
- **通过 import 副作用注册工具**：`server.py` 执行 `from . import tools`，import 所有 8 个工具模块，每个模块对其函数调用 `@mcp.tool`。
- **Filter 和 result utils 从 `state.py` 重导出**，向后兼容（`filters.py`、`result_utils.py`）。
