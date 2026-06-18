# Changelog

## [Unreleased]

### Added
- **可选 SimpleTex 云端公式 OCR / optional SimpleTex cloud formula OCR**（公式索引 Phase B 首步）—— 在默认本地公式 OCR 之外，可显式把 `formula_ocr_provider` 切到 `simpletex`，调用 SimpleTex 开放平台识别公式（UAT token 或 APP 签名鉴权，标准/轻量 endpoint，可配最小请求间隔与 429/5xx 重试）；仅在显式开启时才把公式裁剪图发往配置的 HTTPS endpoint，`local` 仍为默认、不外发数据。Opt-in cloud formula OCR; formula crops are sent only when `formula_ocr_provider=simpletex` is explicitly set, with `local` remaining the default. Thanks @lwz20210407 (#26).

## [0.5.3] - 2026-06-16

**公式索引 Phase A + 连接器下载增强 / Formula Phase A + connector download** — 新增本地公式语义索引（首阶段），增强连接器 PDF 入库与报错体验，并修复一类索引误报。

### Added
- **公式语义索引 Phase A / formula semantic indexing Phase A** —— 本地 OCR 识别有文字层 PDF 中的 display 公式并入库检索；默认关闭、全程本地（需 `zotpilot[formula]` extra），inline / 纯图片公式等留待后续阶段。
- **直链 / 内嵌 PDF 入库 / direct & embedded PDF ingest** —— 连接器识别直链 PDF 与页面内嵌 iframe PDF 并快速入库（isPDF 短路，无需 translator）。
- **入库报错体验 / ingest error UX** —— 统一错误码字典 + 中文可操作指引；PDF 抓取 / 二次反爬失败以不阻断 notice 提示，`manual_completion` 给 `zotero://select` 跳转链接，同源出版社首篇失败自动跳过其余以避免连环反爬。
- **索引进度 JSONL 流 / indexing progress stream (#24)** —— 输出 append-only 结构化进度事件，便于 GUI / 外部工具读取实时进度。

### Changed
- **预检不再为 translator 空等 / preflight no longer waits for a translator** —— translator 等待从 20s 降到 3s，可达性预检大幅提速，不改变正式入库行为。

### Fixed
- **公式 OCR 依赖与 backfill 隔离 / formula OCR dependency & backfill isolation** —— 缺 `zotpilot[formula]` extra 时快速报错并给安装提示；公式 backfill 先识别再替换、不误删已有公式；单篇公式失败不再误标表格 / 图表 failure。
- **vision-only 配置漂移误报 / vision-only config-drift false alarm** —— `batch_size>0`（或 `--no-vision`）关 vision 触发的假漂移现在引导用 `batch_size=0` 增量索引，不再误导 `force_reindex`（避免重建全部、烧额度）。

## [0.5.2] - 2026-06-08

### Fixed
- **`__version__` 与发布版本一致 / version reporting** —— `zotpilot --version`、`status` 与 skill 版本标记此前在 0.5.1 仍显示 `0.5.0`（`src/zotpilot/__init__.py` 漏同步），现已修正。功能无变化。

## [0.5.1] - 2026-06-08

**单篇精读 + 更自由的嵌入 + 更稳的索引 / Deep reading + flexible embeddings + safer indexing** — 新增单篇论文 AI 导读、嵌入模型选择更自由，并从源头加固了索引的抽取质量、可靠性与数据安全。

### ✨ Highlights
- **`ztp-tutor` 论文导读** —— `/ztp-tutor <标题>` 让 AI 通读本地某篇论文，把彩色高亮、逐句批注（长难句翻译 / 术语解释 / 方法论点评）与图表标注直接写回 Zotero 的 PDF，原地对照查看、全程本地；批注语言与密度随个人「阅读画像」自适应，尚无画像时先征询偏好，尊重已有批注并在写入前自动备份原文。
- **更自由的嵌入模型选择**（Issue #11 / #12）—— 新增通用 OpenAI 兼容嵌入 provider，内置 SiliconFlow（bge-m3 / Qwen3-Embedding）、智谱 GLM、Ollama 等「厂商 → 模型」两步选择，也可填任意端点自定义；Gemini 也支持自定义 endpoint，方便 API 代理或受限网络。可按成本、隐私或网络环境自由切换。取代厂商专用的 Ollama PR #16（感谢 @EconGeo）。
- **更稳更准的索引** —— 从源头修复一类中文 PDF 抽取乱码、嵌入限流自动等待重试、CLI 与 MCP 跨进程加锁，索引更可靠、少烧额度（详见下方 Changed / Fixed）。

**English**
- **`ztp-tutor` deep reading** — `/ztp-tutor <title>` has the agent read one paper from your library and write 5-dimension color highlights, per-sentence notes (long-sentence translation, term explanation, methodology commentary), and figure/table annotations straight into the Zotero PDF, viewed in place, fully local. Annotation language / depth follow your reading persona — with none, the agent asks first; existing annotations are respected and the PDF is backed up before any write.
- **Flexible embeddings** (Issue #11 / #12) — a new generic OpenAI-compatible embedding provider with built-in two-layer vendor → model configs for SiliconFlow (bge-m3 / Qwen3-Embedding), Zhipu GLM, and Ollama, or any custom endpoint; Gemini also accepts a custom endpoint for proxies / restricted networks. Supersedes the vendor-specific Ollama PR #16 (thanks @EconGeo).
- **Safer, more accurate indexing** — a source-level fix for a class of CJK PDF garble (pymupdf4llm internal OCR), automatic embedding rate-limit retry, and a cross-process lock so concurrent CLI / MCP runs can't corrupt the store; plus DashScope native embeddings + vision support (PR #10, thanks @CHENyiru3).

### Added
- **DashScope 原生嵌入 + 视觉表格抽取**（PR #10，感谢 @CHENyiru3）—— 新增 DashScope 原生 embedding endpoint（`dashscope_embedding_endpoint: native`，支持 document / query 非对称检索语义）与 DashScope 视觉表格抽取 provider，并改进 PDF 章节引用启发式等。
- **视觉表格抽取结果缓存** —— 表格 / 图表的视觉抽取结果按内容寻址缓存，重建索引或重跑时不再重复调用付费视觉模型。
- **索引恢复工具** —— 新增 `zotpilot doctor --recover-index`，不消耗嵌入额度即可从现有数据重建向量库；`--reconcile` 可清理已删除文献的残留索引。

### Changed
- **嵌入 429 改为自动等待 + 有限重试**（Issue #15）—— 触发限流时按服务端 `retry_after` 自动等待并有限次重试，不再一遇限流就中断整批；多次重试仍失败才停止，已完成部分完整保留、下次自动续跑。
- **CLI 与 MCP 共用跨进程锁** —— 命令行索引现在与 MCP server 走同一套跨进程租约 + journal，二者并发时不会再互相覆盖、损坏向量库。
- **索引打不开不再自动清空重建**（破坏性变更）—— 过去索引无法打开时会被搬走并重建为空库、可能丢失数据；现在改为保留数据，并提示通过 `doctor --recover-index` 恢复。

### Fixed
- **从源头修复一类中文 PDF 抽取乱码 / 近空白** —— pymupdf4llm 的内部 OCR 会对本有完好文字层的页面（尤以中文论文为甚）误判重抽，整段变乱码或近空白；现在解析时禁用其内部 OCR，并以原生文本层为可靠下限自动回退，真正的扫描件仍走受控 OCR。
- **修复索引可能被误判损坏而清空的严重问题** —— 个别旧索引在打开检测时可能被误判为损坏、进而清空整库；现在检测更稳健，完好数据不会被误删。
- **批量删除保护** —— 清理残留索引时设有安全下限，遇到库读空、数据目录不可达或删除比例过高时会拒绝操作并告警，避免误删。
- **更清晰的索引错误提示** —— 嵌入维度不一致、索引不可用、配置变更等情况会给出明确、可操作的提示，而非直接崩溃中断。

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
