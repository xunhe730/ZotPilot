# Changelog

## 如何更新 / How to Update

```bash
zotpilot update              # 自动探测安装方式，更新 CLI + skill 目录
zotpilot update --check      # 只查版本，不安装
zotpilot update --dry-run    # 预览操作，不执行
```

手动更新：`uv tool upgrade zotpilot` 或 `pip install --upgrade zotpilot`

---

## [Unreleased]

### Fixed
- **ingest_papers collection 路由竞态修复**：Connector 通过本地 API 路由后，后端不再通过 Web API 重复写入（解决双重路由覆写问题）
- **item_key 发现窗口扩大**：`RECENT_ITEMS_LIMIT` 10→50，`MAX_ATTEMPTS` 15→45，批量入库时 item 发现更稳定
- **出版商自动标签清理**：arXiv/bioRxiv/medRxiv 来源论文入库后自动清除出版商注入的 subject tags
- **Post-batch reconciliation**：批次完成后对未路由条目通过本地 API 补路由，兜底 Web API

### Added
- `routing_applied` 协议：Connector 显式标记路由状态，后端据此决定是否跳过 Web API 路由
- `routing_status` 字段：`get_ingest_status` 返回每个条目的路由状态（`routed_by_connector`/`routed_by_backend`/`routing_failed` 等）
- SKILL.md Step 8 新增 INBOX 清理步骤：归类后必须从 INBOX 移除（closes #3）

---

## [0.4.0] - 2026-03-24

### Added
- `bridge` CLI 子命令：`zotpilot bridge [--port N]` 手动启动 HTTP bridge 服务（为后续浏览器扩展集成做基础设施准备）

### Fixed
- pyzotero `url_params` 泄漏
- Zotero API `qmode` 参数修复

---

## [0.3.1] - 2026-03-23

### Added
- `status --json` 新增 version 字段
- `--version` flag
- Cursor / Windsurf 升级为 Tier 1

### Fixed
- Windows `zotpilot update` 文件锁定时输出友好提示
- 收窄异常类型、路径比较安全性、文件编码显式指定
- ruff lint / mypy 全部通过

---

## [0.3.0] - 2026-03-23

### Added
- `zotpilot update` 一键更新命令，自动探测安装方式（uv / pip / editable），同时更新 CLI 和所有平台 skill 目录
- `--check` / `--dry-run` / `--cli-only` / `--skill-only` 标志
- Skill 目录升级安全检查：跳过符号链接、脏工作树、非 ZotPilot 仓库

---

## [0.2.1] - 2026-03-23

### Added
- 论文摄取：`search_academic_databases`、`add_paper_by_identifier`、`ingest_papers`（Semantic Scholar 搜索 + Zotero 导入）
- `config` CLI 子命令：`set` / `get` / `list` / `unset` / `path`
- Semantic Scholar API key 支持（`S2_API_KEY`）
- `switch_library` 工具：切换用户/群组文献库
- `get_annotations` 工具：读取高亮和评论

### Fixed
- API key 优先级：环境变量现在优先于配置文件

---

## [0.2.0] - 2026-03-19

### Added
- No-RAG 模式：`embedding_provider: "none"` 可在不配置 embedding 的情况下使用元数据搜索、笔记、标签等基础功能

---

## [0.1.5] - 2026-03-19

### Added
- `get_feeds` 工具：列出 RSS 订阅或获取订阅条目

---

## [0.1.4] - 2026-03-19

### Added
- `get_notes` / `create_note` 笔记工具
- `advanced_search` 高级元数据搜索（年份/作者/标签/集合等，无需索引）

---

## [0.1.3] - 2026-03-19

### Changed
- 批量工具合并：`batch_tags(action="add|set|remove")`、`batch_collections(action="add|remove")`，工具数 29 → 26
- 所有工具 docstring 精简

---

## [0.1.2] - 2026-03-19

### Added
- 查询缓存：相同查询不再重复调用 embedding API
- 批量写操作工具（最多 100 条）

### Removed
- 内置中英翻译（改由 Agent 负责）

---

## [0.1.1] - 2026-03-19

### Fixed
- 线程安全：所有单例初始化使用双重检查锁
- ReDoS 漏洞修复
- API key 不再打印到终端
- Collection 缓存在写操作后正确失效

---

## [0.1.0] - 2026-03-16

### Added
- 初始版本：26 个 MCP 工具
- Gemini / Local 嵌入提供方
- 章节感知重排序 + 期刊质量加权
- PDF 提取（文本 + 表格 + 图表 + OCR）
