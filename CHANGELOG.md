# Changelog

## [Unreleased]

### Added

- **两层「厂商 → 模型」配置 / Two-layer vendor → model setup** —— 配置体验重构为统一的两层模型，由 `providers.py` 中单一 `VENDOR_CATALOG` 驱动，**三个配置面**（交互式向导、非交互式 CLI、Agent skill）共用同一数据源、零漂移。交互式向导先选厂商（Google·Gemini / DashScope / Local / SiliconFlow / Zhipu·GLM / Ollama / Custom）再选模型（预选推荐项，回车即取）。非交互式支持按厂商名一行配置：`zotpilot setup --non-interactive --provider siliconflow --embedding-model BAAI/bge-m3`，固定 base 厂商自动带上 base_url 与维度，省略 `--embedding-model` 取推荐项。新增 `zotpilot setup --list-vendors [--json]`（Agent 查询目录的发现接口，带 `schema_version`）与可选的 `zotpilot setup --non-interactive --verify`（写入后做一次连通自检并打印一行 JSON 分类：`ok`/`dim_mismatch`/`auth`/`unreachable`/`error`/`skipped`，供 Agent 自愈）。旧脚本仍兼容：`--provider gemini|dashscope|local|openai-compatible` 作为别名继续有效；运行时 provider 集合、`config.json` schema 与 `_config_hash` 均未变（不触发重建索引）。`scripts/verify_vendor_catalog.py` 为目录维度改动的强制 live 校验门禁。/ Reworked setup into a unified two-layer vendor → model model backed by a single `VENDOR_CATALOG`, feeding the interactive wizard, the non-interactive CLI, and the `ztp-setup` Agent skill with no drift. Non-interactive now takes a vendor name (`--provider siliconflow --embedding-model BAAI/bge-m3`); fixed-base vendors auto-fill base_url + dimensions, and omitting `--embedding-model` picks the recommended one. Added `setup --list-vendors [--json]` (a versioned discovery contract for Agents) and opt-in `setup --non-interactive --verify` (one JSON line: ok/dim_mismatch/auth/unreachable/error/skipped). Legacy `--provider gemini|dashscope|local|openai-compatible` keep working as aliases; runtime provider set, `config.json` schema and `_config_hash` are unchanged (no forced reindex). `scripts/verify_vendor_catalog.py` is the mandatory drift gate for any catalog dimension change.

- **通用 OpenAI 兼容嵌入 provider / Generic OpenAI-compatible embedding provider** —— 新增 `openai-compatible` 嵌入 provider，可对接任意 OpenAI 兼容的 `/embeddings` 端点（SiliconFlow、Zhipu/GLM、Ollama、vLLM、自建服务等），切换厂商只需配置 `embedding_base_url` + `embedding_model` + `embedding_dimensions`，无需新增代码。维度必须显式指定、永不自动探测（首个响应向量会做长度断言以防索引被静默破坏）。设置向导内置厂商预设（SiliconFlow / Zhipu·GLM / Ollama / Custom）。解决 Issue #12（嵌入部分），取代厂商专用的 Ollama PR #16（感谢 @EconGeo 提出并推动 Ollama 本地嵌入支持）。视觉（vision）的 OpenAI 兼容支持暂缓至后续 issue。/ Added a generic OpenAI-compatible embedding provider (Issue #12, embeddings half); supersedes vendor-specific Ollama PR #16 (thanks to @EconGeo for proposing and driving Ollama local-embedding support). Vision support deferred. 固定维度（非 matryoshka）端点若以 HTTP 400 拒绝 `dimensions` 参数（如 SiliconFlow `BAAI/bge-m3`），现会自动丢弃该参数并重试，无需额外配置。/ Fixed-dimension endpoints that reject the `dimensions` parameter with HTTP 400 (e.g. SiliconFlow bge-m3) are now auto-handled. 设置向导内置一组已 live 实测验证的 SiliconFlow 模型预设（`BAAI/bge-m3` 1024 / `Qwen3-Embedding-0.6B` 1024 / `Qwen3-Embedding-8B` 2048），菜单显示模型维度与定位；通用 Custom 项仍可对接任意端点。/ The setup wizard now seeds a curated, live-verified set of SiliconFlow models (bge-m3, Qwen3-Embedding-0.6B/8B) shown with dimensions, alongside the generic Custom option.

- **自定义 Gemini base URL / Custom Gemini base URL**（Issue #11）—— Gemini 嵌入客户端支持自定义 endpoint，方便处于受限网络或使用 API 代理的用户。通过 `GEMINI_BASE_URL` 环境变量或 `zotpilot config set gemini_base_url <url>` 配置；底层透传给 `genai.Client` 的 `http_options`，未设置时使用 Google 官方 endpoint。

- **`ztp-tutor` 论文导读 / Deep Reading Guide** —— 新增单篇论文深度导读功能。`/ztp-tutor <标题>` 模糊匹配本地 Zotero 文献后，由 LLM 通读全文，将五维彩色高亮（核心论点 / 关键概念 / 实证证据 / 让步反驳 / 方法论）、逐句中文批注、图表与公式标注，以及第 1 页的论证结构概览便签，直接写入 Zotero 存储的 PDF，可在 Zotero 阅读器中原地打开查看，全程本地。功能会按 `~/.config/zotpilot/ZOTPILOT.md` 中的"阅读画像"自适应调整批注密度与讲解层次（如英文偏弱时补充术语解释与长难句拆解），并尊重 PDF 中已有的人工批注（不重复、不覆盖）。每次写入前自动生成 `.ztpbak` 备份，经独立文件写入、多重校验与原子替换保证原文永不损坏、失败即回滚；跨 macOS / Linux / Windows 均经兼容性加固。配套提供声明式 skill 与 MCP 工具 `get_paper_for_tutor` / `annotate_pdf` / `save_reading_persona`。首次启用后建议在真实 Zotero 阅读器中目视确认中文便签与五色高亮渲染正常。

- **`zotpilot doctor --recover-index` 索引零额度恢复** —— 从完好的 SQLite + HNSW 段（含被旧版本写坏、新核打不开的段）重建向量库，直接复用已有向量、**不产生任何嵌入 API 调用**；先写入新目录并通过校验门（数量 / 维度 / 自最近邻 / 探针）再原子换库，失败则保留原库不动。可用 `--source` 指定某个 `chroma.corrupt-*` 备份（省略则自动发现），`--dry-run` 预览；HNSW 不可读时可回退到从 SQLite 文本重嵌（需确认，会花嵌入额度）。
- **`zotpilot doctor --reconcile` 显式对账** —— 预览 / 清理 Zotero 中已删除的孤儿索引文档；默认受删除下限保护，`--dry-run` 预览，`--force` 越过 25% 下限。
- **可选依赖 extra `recover`**（`chroma-hnswlib`）—— 仅索引恢复路径需要；缺失时给出 `uv sync --extra recover` 提示并可回退重嵌。注意 Python 3.13 暂无预编译 wheel，需本机 C++ 编译器（或改用重嵌回退）。

### Changed

- **索引打不开不再自动「搬走 + 重建空库」**（破坏性行为变更）—— 旧版在探针失败时会把整库移到 `*.corrupt-*` 再原位重建空库，曾导致完好数据被静默清空。现在改为**报错并保留数据**（`IndexUnavailableError`），并引导运行 `zotpilot doctor --recover-index`。依赖「自动重建」的旧行为已移除。

### Fixed

- **P0 数据安全：索引「打不开即静默清空」**（根因 RC1/RC2）—— 旧逻辑用会加载 HNSW 的探针，遇被不同 chromadb 版本写坏的旧段时段错误（exit 139），随即把一个 SQLite 完好、向量在位的库判为损坏并清空。现探针改为**只读、不加载 HNSW、子进程带超时**；段错误判为「不可用」而**不再触发任何搬移 / 清空**，完好库不会被误删。
- **批量误删保护**（RC6）—— 孤儿对账加入常开删除下限：当前库空读、数据目录不可达、或删除量超过索引的 25% 时，拒绝删除并响亮告警；`--force` / `allow_mass_delete` 仅放行比例下限，空读 / 不可达即便显式 override 仍拒删。
- **嵌入维度不匹配**（RC7）—— 在 CLI 与 `doctor` 的索引构造处捕获 `EmbeddingDimensionMismatchError` / `IndexUnavailableError`，给出清晰可操作的提示而非未捕获崩溃。
- **配置漂移**（RC8）—— 嵌入 / 分块 / 视觉等影响索引内容的配置变化且未 `--force` 时**硬阻断**（`ConfigDriftError`），避免静默写入混合嵌入空间的索引。
- **嵌入 429 配额级联**（Issue #15）—— 嵌入服务返回 HTTP 429（配额 / 限流）时不再被当作普通失败逐篇记下并继续硬撞已耗尽的配额、烧掉整批。现在 429 被分类为带 `provider` / `retry_after` 的 `RateLimitError`：索引进入 Phase-3 后立即中止（`break`），当前篇及之后未尝试的论文统一记为 `failed`，运行正常返回（MCP lease 正常释放、已完成的索引完整保留、下次自动续跑）。另加一个与服务商无关的兜底——连续 3 篇相同特征失败也中止（`systemic_abort`，与 `rate_limited_abort` 区分，避免把非配额级联误标成限流）。DashScope 的 `RETRIEVAL_DOCUMENT` 批次遇 429 不再降级成逐条重试；表格 / 图片路径上的 429 也会上抛中止（已提交的正文 chunk 保留）。额外以 `counts["rate_limited_abort"] / ["systemic_abort"] / ["not_indexed_due_to_abort"]` 加性透出，不新增状态、不改退出码语义。

## 如何更新 / How to Update

```bash
zotpilot update              # 自动探测安装方式，更新 CLI + skill 目录
zotpilot update --check      # 只查版本，不安装
zotpilot update --dry-run    # 预览操作，不执行
```

手动更新：`uv tool upgrade zotpilot` 或 `pip install --upgrade zotpilot`

---

## [0.5.0] - 2026-04-28

**架构重构 / Architectural Refactor** — 重新设计入库流程、精简工具层、新增浏览器扩展。

### ✨ Highlights
- **Connector 浏览器扩展**：AI agent 可通过你的浏览器保存论文到 Zotero，自动带上机构订阅的 PDF
- **一步入库**：给 agent 一组 DOI / arXiv ID / URL，它帮你全部存进 Zotero 并验证 PDF
- **18 个精简工具**（原 33 个）：合并冗余，每个工具做一件事
- **Research 工作流**：4 个声明式 Skill 引导 agent 完成"搜索 → 入库 → 整理 → 报告"全流程
- **索引可靠性大修**（Issue #7）：增量索引、中断恢复、不再丢失已完成的索引数据

### Added
- **`zotpilot install` 命令别名** — 与 `zotpilot register` 等价，用作统一的多平台安装/注册入口
- **Connector 浏览器扩展** — 基于 Zotero Connector fork，加入 AI agent 调用路径。Agent 通过本地 bridge 触发浏览器保存，带机构权限下载 PDF。从 [GitHub Release](https://github.com/xunhe730/ZotPilot/releases) 下载 zip，加载到 Chrome 即可
- **`ingest_by_identifiers` 工具** — 给 DOI / arXiv ID / URL 即可入库，自动去重、验证 PDF、失败时走 API fallback。返回每篇论文的最终状态（`saved_with_pdf` / `saved_metadata_only` / `duplicate` / `failed`）
- **`profile_library` 工具** — 分析文献库的主题分布、期刊结构、时间跨度，帮助 agent 理解你的研究方向
- **`search_academic_databases` 全参数搜索** — OpenAlex 检索支持 `min_citations`、`concepts`、`institutions`、`source` 等 filter，cursor-based 分页
- **`zotpilot update` 命令** — 一键升级 CLI + skill 目录
- **版本漂移检测** — MCP server 启动时检查已部署 skill 版本，不匹配时提示更新
- **增量索引** — 基于 PDF hash 跳过已索引文档，中断后从断点恢复，不重复处理
- **索引并发保护** — 防止多个 agent 同时索引导致重复数据
- **入库即时验证** — Connector 保存后通过本地 Zotero API 验证 itemType + title，自动识别并清理出版商 translator 产生的网页快照垃圾 item，失败时走 DOI API fallback

### Changed
- **安装/注册用户入口收敛** — 推荐入口统一为 `zotpilot setup`（首次配置）和 `zotpilot install` / `zotpilot register`（重注册 / 修复 drift），不再向终端用户暴露 `register --dev`
- **多平台注册失败传播** — `update` / `sync` 遇到部分平台注册失败时会显式失败并列出平台，不再假成功
- **Claude Code 注册语法修正** — stdio 注册改为 `claude mcp add ... -- <command>`，兼容 `uv run --directory ...`
- **AGENTS.md / CLAUDE.md** — 同步到 v0.5.0 三 Agent 协作模型（Claude / OpenCode / Codex），更新架构描述和文档维护规则
- **MCP 工具从 33 个精简到 18 个**：
  - `search_papers` 新增 `section_type` 参数，可搜表格和图表（替代 `search_tables` / `search_figures`）
  - `ingest_by_identifiers` 支持 URL 输入（替代 `save_urls`）
  - `manage_collections` 支持 `action="create"`（替代 `create_collection`）
  - `index_library` 支持 `item_keys` 参数局部重索引（替代 `reindex_degraded`）
- **入库流程同步化** — 不再需要轮询状态或多步确认，一次调用返回完整结果
- **Skill 系统** — 4 个声明式 skill（`ztp-research` / `ztp-review` / `ztp-profile` / `ztp-setup`）替代旧的路由器模式，由平台原生机制自动选择
- **平台支持收敛到 3 个** — Claude Code / Codex CLI / OpenCode 为官方支持平台（Gemini CLI / Cursor / Windsurf 不再维护适配，MCP 工具仍可用但不保证）

### Removed
- **状态机工具** — `confirm_candidates` / `approve_ingest` / `get_batch_status` 等 7 个多步确认工具，被 `ingest_by_identifiers` 一步替代
- **`switch_library`** — 多文献库切换推迟到未来版本
- **旧工具别名** — `search_tables`、`search_figures`、`save_urls`、`create_collection`、`reindex_degraded` 等已合并到对应工具

### Fixed
- **Bridge 认证改为 Origin 白名单** — 原 `X-ZotPilot-Token` 方案存在根本缺陷：`/status`
  公开下发 token + `Access-Control-Allow-Origin: *` 导致任意网页都能两步拿到 token
  并调用 `/enqueue`。同时扩展与 bridge 的 token 契约跨仓库未同步（`f0d8c96` 只改了
  主仓库，发布用的 fork 仓库扩展从未跟进），造成 v0.5.0 内测期所有 Connector 保存
  全部 401。改为 Origin 白名单：浏览器强制附加不可伪造的 `Origin` header，bridge 只
  放行 `chrome-extension://` / `moz-extension://` / `safari-web-extension://` 前缀
  和无 Origin（CLI/MCP）的请求，其他一律 403。安全上真正防住了"恶意网页调用 bridge
  写入 Zotero"的攻击面；架构上无共享 secret，扩展与 bridge 可独立升级
- **Preflight 真正阻塞 + 分级 blocking** — 检测到反爬页面时阻塞整个批次要求用户介入，不再悄然降级为 API fallback；分级策略：`anti_bot_detected` / `subscription_required` 封 publisher 域，`preflight_timeout` / `preflight_failed` 只封单 URL（不误伤 IEEE / Springer SPA 慢 hydration 的无关条目）
- **DOI suffix 接受 `.` 字符** — `identifier_resolver._DOI_RE` 从 `[^\s\)\"\',;\.\?]+` 改为 `\S+`，不再误拒 Elsevier / IEEE 风格 DOI（如 `10.1016/j.jcp.2022.111902`、`10.1109/jas.2023.123537`）。与上游 `search.is_doi_query` 对齐
- **OpenAlex SSL 首连重试** — `_request` 现在捕获 `httpx.RequestError` 并按现有 backoff 重试（原代码仅 429 走重试路径，TLS 首连抖动会直接挂）
- **`state._init_lock` 自死锁** — `_get_library_override()` 去掉无意义的 lock acquire（持有者二次 acquire 非 `RLock` 导致 MCP `tools/call` 永不返回）
- **active_candidates 对象一致性** — `run_preflight_check` 接收 `active_candidates` 引用，保证 preflight 操作的对象与后续处理的对象为同一实例
- **ArXiv API 改用 HTTPS** — `identifier_resolver` 中 ArXiv API 端点从 `http://` 改为 `https://`
- **代码质量（P0–P2）** — 修复 `section_type` 验证、`chunk_index` 边界保护、`year_min=0` 过滤异常、消除死代码赋值
- **Issue #7：索引中断丢数据** — 增量索引基于 PDF hash，中断后自动从断点恢复；清理 ChromaDB 中的 stale 孤儿记录
- **arXiv DOI 路由** — `10.48550/arXiv.xxx` 格式的 DOI 正确路由到 arXiv API（CrossRef 不索引这类 DOI）
- **PDF 提取冷启动** — 硬化 PDF fallback 链，修复首次索引时的提取失败
- **API 密钥不再写入配置文件** — `config save()` 跳过所有 API key 字段
- **MCP 配置文件权限** — Unix 上自动设为 0600，防止其他用户读取
- **OpenAlex 请求限流** — 添加 rate limiter 和 429 重试，避免触发 API 封禁

### 从 v0.4 升级 / Upgrading from v0.4

```bash
pip install --upgrade zotpilot     # 或 uv tool upgrade zotpilot
zotpilot install                   # 必须：工具签名变了，需重新注册
```

Connector 浏览器扩展是 Research 工作流的核心组件，从 [GitHub Release](https://github.com/xunhe730/ZotPilot/releases) 下载安装到 Chrome。没有 Connector，入库功能降级为 metadata-only（无 PDF），纯 URL 入库会失败。搜索、引用、整理功能不受影响。

如果你之前通过 `register --gemini-key` 传入 API 密钥，升级后改用 `zotpilot config set gemini_api_key <key>` 保存（更安全，不进 shell history）。

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
