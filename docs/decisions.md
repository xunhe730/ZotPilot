# 决策记录

记录项目关键决策、理由、行动和时间。按时间倒序排列。

---

## 2026-03-26 | Connector 事件驱动握手 + 闭环 itemType 验证

### 背景
批量导入"流动减阻"18篇文章时发现大量论文被存为 `webpage` 而非 `journalArticle`，根因为 `_pollForTranslators` 超时硬编码（5s），AIP/Elsevier 等出版商页面 translator 注入超时后 Zotero 静默降级保存网页快照。同时发现即便 `progressWindow.done` 触发，Zotero SQLite 写入可能滞后，导致误报成功。

### 决策 1：事件驱动 translator 就绪检测（`agentAPI.js`）
- **问题**：`_pollForTranslators` 用固定 5s 轮询 `_tabInfo[tabId].translators`，超时后无论 translator 是否就绪都调用 `onReady()`，导致 `onZoteroButtonElementClick` 在 translator 缺失时触发 → Zotero 降级为 webpage 保存
- **决策**：完全替换为事件驱动机制
  - 新增模块级 `_translatorWaiters: Map<tabId, resolve>`
  - 在 `init()` 中 monkey-patch `Zotero.Connector_Browser.onTranslators`：translators 到达时若对应 tabId 有 waiter，立即 resolve，无需轮询
  - `_waitForReady` Phase 2 改为 `_waitForTranslatorEvent()`：先检查 fast path（translator 已在 `_tabInfo`），再注册 waiter，设 20s 超时 fallback
  - redirect 时清除 waiter 并重置稳定性窗口
- **弃用方案**：继续延长 `TRANSLATOR_WAIT_MS`——轮询在有些页面天然不可靠，事件是正确抽象
- **状态**：✅ 完成

### 决策 2：第二握手——保存完成后本地 API 轮询确认（`agentAPI.js`）
- **问题**：`progressWindow.done` 触发时 Zotero UI 显示完成，但 SQLite 写入可能滞后，Python 侧 item_key 发现失败或结果不稳定；`success="unconfirmed"`（60s 超时）场景下无法区分保存成功但慢、还是真正失败
- **决策**：新增 `_waitForItemInZotero(entry, tabTitle, beforeItems)`
  - 首次立即尝试（zero-wait），后续 1s 间隔，最多 15 次
  - 通过与 `beforeItems`（save 前快照）做 diff 检测新条目
  - `success=true` 和 `success="unconfirmed"` 都触发；`unconfirmed` 发现条目后升级为 `success`
- **状态**：✅ 完成

### 决策 3：闭环 itemType 验证——webhook 保存后查 Zotero 删除垃圾条目（`ingestion.py` + `zotero_writer.py`）
- **问题**：即使前两层防御（事件驱动 + title 检测）漏过，Connector 仍可能把 webpage 存入 Zotero 库，后续 index/tag 流程使用了错误的条目
- **决策**：在 `_apply_bridge_result_routing` 中，拿到 `item_key` 后调用 `writer.get_item_type(item_key)`，若 itemType 不在 `_ACADEMIC_ITEM_TYPES` 集合中，调用 `writer.delete_item(item_key)` 删除，返回 `success: False, translator_fallback_detected: True`
- **`_ACADEMIC_ITEM_TYPES`**：`journalArticle`, `conferencePaper`, `preprint`, `thesis`, `book`, `bookSection`, `report`, `magazineArticle`, `newspaperArticle`
- **同步新增**（`zotero_writer.py`）：`get_item_type(item_key) → str | None` 和 `delete_item(item_key) → bool` 两个方法
- **状态**：✅ 完成

### 决策 4：明确拒绝 API 元数据 fallback 路径
- **背景**：早期实现在 translator 失败时自动 fallback 到 `add_paper_by_identifier(doi)` 获取元数据（无 PDF）
- **决策**：完全移除此 fallback。理由：有订阅 + 通过 robot 验证的情况下，Connector 一定能抓到完整元数据和 PDF；fallback 往往拿不到想要的数据，反而掩盖根本问题。正确策略是修复 Connector 可靠性，而非绕过失败
- **变更**：`_apply_bridge_result_routing` 的 `translator_fallback_detected`/`no_translator` 分支改为直接报告失败，移除 `url_to_paper` dict 和 `add_paper_by_identifier` 调用
- **状态**：✅ 完成

**测试**：88 个 ingestion bridge 测试全部通过。

---

## 2026-03-26 | ingest_papers 批量并发 + 反爬三层修复

### 背景
实际调研中发现 `ingest_papers` 批量入库时大量文献失败，根因为三个独立问题，逐层修复。

### Bug 1：`ingest_papers` 串行调用导致心跳超时（`ingestion.py`）
- **问题**：`ingest_papers` 在 for 循环中串行调用单URL版 `save_urls`，每次阻塞最多 90s。Chrome extension 在处理保存任务时无法发送心跳，bridge 在 30s 后判定 extension 断连，后续 `/enqueue` 返回 503，最终表现为通用 `"connector save failed"` 错误（`error_code`/`error_message` 字段被 `sub.get("error")` 的 fallback 掩盖）
- **决策**：`ingest_papers` 改为一次性构建 URL 列表，批量调用 `save_urls`（已有并发轮询实现）；`else` 分支透传 `error_code` 和 `error_message`
- **状态**：✅ 完成

### Bug 2：反爬页面在保存后才检测，产生垃圾条目（`agentAPI.js` + `ingestion.py`）
- **问题**：`_apply_bridge_result_routing` 在拿到 `success=True` 结果后检测 title 是否为"请稍候…"，此时 Zotero 已将反爬页面存入库，产生大量无效条目
- **决策**：将检测移至 `agentAPI.js` 的 `_handleSave` 中，在调用 `onZoteroButtonElementClick` 之前检查 tab title。检测到反爬直接返回 `{success: false, error_code: "anti_bot_detected"}`，不执行保存
- **Python 侧联动**：`_poll_one` 改为检查 `error_code == "anti_bot_detected"`，触发 `cancel_event` 立即停止其他并发线程，未处理 URL 标记为 `pending`；`_apply_bridge_result_routing` 移除冗余的 title 检测（修复遗留的 `title` 变量未定义 bug）
- **状态**：✅ 完成

### Bug 3：多跳跳转页面过早触发保存（`agentAPI.js`）
- **问题**：AIP 等出版商首次访问时经历"验证页 → 文章页"两跳。`STABILITY_WINDOW_REDIRECT_MS = 2000ms` 不足以等待文章页稳定及 translator 注入，导致在验证页或跳转中间态触发保存，存为 snapshot 而非论文条目
- **决策**：`STABILITY_WINDOW_REDIRECT_MS` 从 2000ms 提升至 4000ms。`scheduleResolve` 在每次 URL 变化时都会重置计时，所以多跳场景下会在最后一次跳转后再等 4s，确保文章页完全加载
- **状态**：✅ 完成

### Bug 4：PDF 下载失败时等待 60s 超时（`agentAPI.js` + `ingestion.py`）
- **问题**：Elsevier 等出版商在 Zotero translator 下载 PDF 时弹出机器人验证，导致附件下载失败，但 extension 不感知，仍等待 60s 超时
- **决策**：在 `sendMessage` patch 中监听 `progressWindow.itemProgress` 事件，当 `payload.iconSrc` 包含 `cross.png`（Zotero 的失败图标）时立即 resolve，返回 `{success: true, pdf_failed: true, error_code: "pdf_download_failed"}`；Python 侧 `ingest_papers` 将此类结果标记为 `ingested`（元数据成功）但 `pdf: "none"`，并附 `warning` 提示用户手动下载 PDF
- **状态**：✅ 完成

**测试**：477 个测试全部通过。

---

## 2026-03-26 | item_key 发现链三项修复

### 背景
实测发现 `save_urls` 在标准 translator 路径（~95%）无法返回 `item_key`，导致后续 index/tag/collection routing 全部失败。根因追踪至三个独立 bug。

### Bug 1：discovery URL 过滤排除了期刊文章（`zotero_writer.py`）
- **问题**：`find_items_by_url_and_title` 中 `if item_url and self._urls_match(...)` 的条件导致 `item_url` 为空的条目（journalArticle 通常不填 url 字段）被直接排除，discovery 永远返回空
- **决策**：改为 `if not item_url or self._urls_match(...)`——空 url 不能作为排除条件，只有 url 存在且不匹配时才排除
- **状态**：✅ 完成

### Bug 2：`_ITEM_DISCOVERY_WINDOW_S` 定义了但从未使用（`ingestion.py`）
- **问题**：常量定义了 60s 时间窗口，但 `_discover_saved_item_key` 从不传入，导致查询不限时间，旧同名文章会干扰结果
- **决策**：`find_items_by_url_and_title` 增加 `window_s` 参数，按 `dateAdded desc` 排序后在 Python 层过滤；`_discover_saved_item_key` 传入 `window_s=_ITEM_DISCOVERY_WINDOW_S`
- **同步删除**：移除了无效的 title-only fallback（`find_items_by_title`）——不加时间窗口的 title 搜索比没有更危险（可能返回旧文章的 key）
- **状态**：✅ 完成

### Bug 3：write_ops 所有 list 参数缺少 JSON string 解析（`write_ops.py`）
- **问题**：Claude Code MCP 客户端有时将 `list[str]` 类型参数序列化为 JSON string 传入，`add_item_tags`、`set_item_tags`、`remove_item_tags`、`create_note`、`batch_tags`、`batch_collections` 全部缺少解析逻辑，实测 `add_item_tags` 报 `Input should be a valid list`
- **决策**：新增 `_coerce_list()` helper（`list` 直接返回，`str` 尝试 `json.loads`），所有接收 list 的工具统一调用
- **参考**：`save_urls`、`ingest_papers` 等已有类似处理，此次补齐 write_ops 模块
- **状态**：✅ 完成

### Connector 端根本修复（2026-03-26 完成）
Connector `agentAPI.js` 增加了保存后本地 API diff 回填逻辑（`_discoverItemKey`），并修复了两个导致回填失败的 bug：
1. **快照时机**：`recentItemsBeforeSave` 移到 `browser.tabs.create` 之前，确保新条目出现在 diff 中
2. **本地 API 403**：请求头加 `Zotero-Allowed-Request: 1`，解决 Zotero 本地 API 认证问题

端到端验证：`item_key` 在标准 translator 路径（arxiv、AIP）单项保存场景已可靠回填。详见 `zotpilot-connector/.docs/decisions.md`。

**测试**：477 个测试全部通过（新增 1 个 `test_window_s_passed_to_writer`）。

---

## 2026-03-25 | v0.4.1 Research Chain 九项修复

### 背景
研究发现工作流在实际使用中暴露了 9 个问题，分两簇：同步/发现类（A/B/C）和工作流效率类（D/E/F）。本次一次性修复，代码变更限定在 `ingestion.py` 3 个函数 + 2 个常量，SKILL.md 补全 agent 侧指令。

### Task 1: `_enrich_oa_url` is_oa 门控
- **决策**：OpenAlex 对订阅期刊也会返回 `oa_url`（即使 `is_oa=False`），必须在返回前检查 `is_oa`，否则会下载到付费墙 HTML 当成 PDF 附件
- **实现**：`if not oa.get("is_oa"): return None`（两行，`ingestion.py:193`）
- **防御纵深**：`zotero_writer.py:300-302` 已有 Content-Type / `%PDF` 字节检查作为第二道防线；`is_oa` 门控是第一道，防止发起请求
- **已提交**：`19ef3e8`（enrich oa_url）+ 本次新测试
- **状态**：✅ 完成

### Task 1b: `search_academic_databases` 结果增加 `publisher`/`journal` 字段
- **决策**：结果 dict 加两个字段，供 agent 对照 ZOTPILOT.md 订阅列表做路由判断；纯展示层变更，不影响现有消费方
- **来源**：OpenAlex `primary_location.source.display_name`（期刊名）和 `host_organization_name`（出版商名）——均在已有 `select` 参数中，零额外 API 成本
- **弃用方案**：只加 `publisher` 不加 `journal`——用户往往用期刊名描述订阅（"JFM"），两个都加
- **状态**：✅ 完成

### Task 2: Anti-bot + 翻译器降级检测
- **决策**：在 `_apply_bridge_result_routing` 成功路径的最顶端，按标题模式检测两类无效保存，立即返回 `success: False` + 具体字段，不进入后续 item_key 发现逻辑
  - **Anti-bot**：标题含 "just a moment"、"cloudflare" 等 → `anti_bot_detected: True`，用户需在 Chrome 完成验证后重试
  - **翻译器降级**：标题以 " | Cambridge Core"、" | SpringerLink" 等结尾 → `translator_fallback_detected: True`，换用 `add_paper_by_identifier(doi)` 路径
- **关键设计**：检测在 `config.zotero_api_key` 检查之前，无 API key 也能触发；`title=None` 安全处理
- **弃用方案**：在 bridge.py 做检测——bridge 应保持协议中性，检测逻辑属于工具层
- **状态**：✅ 完成

### Task 3: 条目发现指数退避（替代固定 3s 延迟）
- **决策**：`_ITEM_DISCOVERY_DELAY_S = 3.0` 改为 `_DISCOVERY_BACKOFF_DELAYS = [2.0, 4.0, 8.0]`，最多 3 次尝试，总最坏 14s；快路径（首次成功）仍约 2s
- **理由**：Cambridge Core 等慢发布商 3s 内 Zotero Desktop 尚未同步到 Web API，导致 item_key 发现失败；固定增大延迟则惩罚所有保存
- **弃用方案**：直接调高到 6-8s 固定延迟——所有保存都变慢
- **已知后果**：最坏情况延迟从 3s 增至 14s，可接受（发现失败比多等几秒更差）
- **状态**：✅ 完成

### Task 4: SKILL.md agent 指令补全（六项）
六项纯指令层修复，零代码风险：
- **Step 0 订阅询问**：首次运行前读 ZOTPILOT.md，若无 `## Subscription Info` 节则问用户并持久化——订阅信息问一次，后续复用
- **Step 2 强制确认门**：显式 "MUST STOP" + 中文模板，防止 agent 跳过用户确认直接入库
- **路由表 Priority 3/3b**：订阅期刊走 Connector（`save_urls`），无订阅匹配走 `ingest_papers`（元数据无 PDF）；利用新增 `publisher`/`journal` 字段做匹配
- **`save_urls` 批次上限 2**：MCP 超时 90s，每 URL ~30s，>2 个需分批串行调用
- **Step 4 并行索引**：`index_library` 对所有成功入库的 item_key 并发调用（最多 5 个），不串行
- **`pdf_available` vs `pdf: "attached"` 区分**：cloud 上传后本地文件不立即可见，需 Zotero 同步
- **状态**：✅ 完成

**测试覆盖**：新增 16 个测试（4 个 class），57 个 ingestion 测试全部通过；`ingestion.py` 覆盖率 58%。

---

## 2026-03-24 | v0.4.1 前置修复（独立 commits）

### `pyzotero url_params` 二次泄漏修复（`09db7a4`）
- **问题**：`item_template()` 调用后 `url_params` 未清除，导致后续 `items()` 请求携带残留参数（与 `8ee4977` 同类 bug，修的是另一个调用点）
- **决策**：在 `item_template` 调用的 `finally` 块中加 `self._zot.url_params = {}`
- **状态**：✅ 完成

### CrossRef 缺失时从 OpenAlex 补全 `oa_url`（`19ef3e8`）
- **问题**：CrossRef 元数据有时不含 `oa_url`，导致 OA 论文也走"无 PDF"路径
- **决策**：`add_paper_by_identifier` 在 `metadata.oa_url` 为空且无 arxiv_id 时，调用 `_enrich_oa_url(doi)` 向 OpenAlex 补查
- **状态**：✅ 完成

### SKILL.md Step 3 去重检查 + Step 4 工作流扩展（`d22ccdf`、`7cc1e2f`）
- **决策**：Step 3 入库前先用 `advanced_search` 批量查重，已有条目跳过入库直接进 Step 4；Step 4 明确 index→analyze→classify 顺序及笔记条件
- **状态**：✅ 完成

### `profile_library` 新增 `top_journals` 字段（`cb3df2d`）
- **决策**：返回值加入按期刊统计的 top journals 列表，供 agent 和 ZOTPILOT.md 生成器使用
- **状态**：✅ 完成

### `profile_library` 自适应对话式 profiling 工作流（`122d8cb`）
- **决策**：重写 SKILL.md 中 library profiling 工作流——agent 先深度理解文献库再开口，以观察和假设驱动对话，而非固定问卷
- **理由**：问卷式 profiling 产出机械、用户体验差；agent 主动带入理解能生成真正有用的研究档案
- **状态**：✅ 完成

### `profile_library` 只读 SQLite URI + 日期字段修复（`80b5c6c`、`a18f60b`）
- **决策**：SQLite 连接改用 `?mode=ro` URI；year_distribution 查询改用正确字段名；计数从 PDF 附件数改为总条目数
- **状态**：✅ 完成

---

## 2026-03-24 v0.4.0 误发事故记录

**事故：** v0.4.0 在未经用户确认的情况下发布到 PyPI。

**经过：** 用户问"你怎么都成v0.5了"（命名问题），Claude 误读为发版指令，执行了完整 release flow（版本 bump、CHANGELOG、tag、push），CI 自动发布到 PyPI。PyPI 版本无法撤回。

**影响：** v0.4.0 已公开发布。内容本身完整（426 tests pass），但发布时机未经用户决定，且细节尚未最终确认。

**结论：** `发版` / `release` 必须是用户的**显式指令**，不能由 Claude 从上下文推断。发版前需明确告知用户将要执行的操作并等待确认。

---

## v0.4 计划（待实现）

### 目标：Agent Research 全自动文献入库流水线

**用户场景**：AI agent 在做 research 时，发现一篇相关论文 → 一行指令完成：抓取入库 + 索引 + 分析 + 写笔记 + 自动归类打标。

**流水线步骤：**

```
save_from_url(url)                    ← MCP tool → Bridge(:2619) → ZotPilot Connector
       ↓                                  Chrome 扩展打开页面 → Zotero 翻译器提取元数据+PDF
       ↓                                  → 写入 Zotero Desktop（条目 + PDF 附件）
index_library(item_key=...)           ← 仅索引该条目（增量，不重建全库）
       ↓
search_papers / get_paper_details     ← 分析内容（摘要、方法、结论）
       ↓
create_note(item_key, content)        ← 写结构化笔记（研究问题、方法、结论、与当前研究的关联）
       ↓
add_to_collection + add_item_tags     ← 移入合适分类 + 打标签
```

**抓取机制说明（依赖 zotpilot-connector）：**
- `save_from_url` 不做元数据提取，完全委托给 **ZotPilot Connector**（Chrome 扩展）
- Connector 在用户的真实浏览器中打开 URL（携带机构 Cookie），触发 Zotero 翻译器
- 翻译器负责结构化元数据提取 + PDF 下载 + 写入 Zotero Desktop（`localhost:23119`）
- 这条链路保证了元数据质量（与用户手动点击 Zotero 按钮等效）和机构访问权限（PDF）
- 前提条件：ZotPilot Connector 已安装且 Chrome 已打开（bridge auto_start 会自动拉起 Python 侧）

**工具就绪状态：**

| 步骤 | 工具 | 状态 |
|------|------|------|
| 抓取入库 | `save_from_url(url)` | ✅ 已有，经 Connector → Zotero |
| `item_key` 补全 | `_apply_bridge_result_routing` 内发现 | ✅ v0.4 实现（title+URL 查 Web API） |
| 增量索引 | `index_library(item_key=...)` | ✅ MCP tool 已暴露 `item_key` 参数 |
| 内容分析 | `get_paper_details(item_key)` | ✅ 已有 |
| 写笔记 | `create_note(item_key, content, title, tags)` | ✅ 已有 |
| 归集合 | `add_to_collection(item_key, collection_key)` | ✅ 已有 |
| 打标签 | `add_item_tags(item_key, tags)` | ✅ 已有 |

**`item_key` 补全（已在 v0.4 实现）**

所有后续步骤都需要 `item_key`，Connector 只有 ~5% 情况能直接返回它。

- **根本原因**：Connector 走 Zotero 翻译器路径时，扩展侧无法从 Zotero Desktop 的保存响应中拿到 `item_key`（只有 `saveAsWebpage` 路径例外）
- **实现方案**：重构 `_apply_bridge_result_routing`（`tools/ingestion.py`），save 成功后始终调用 `_discover_saved_item_key`（Zotero Web API 通过 title+URL 查询），并将结果写入返回 dict
- **修复的两个 gap**：①无路由参数时跳过 discovery；②发现的 key 没有写回返回值

**Connector 使用前提（`save_from_url` 调用方需满足）：**
1. Chrome 已打开（Connector 在用户真实浏览器中运行）
2. ZotPilot Connector 扩展已安装且已启用（`chrome://extensions/`）
3. Bridge 无需手动启动（`save_from_url` 自动拉起 `:2619`）
4. `ZOTERO_API_KEY` + `ZOTERO_USER_ID` 已配置（`item_key` 发现 + 集合/标签路由需要；未配置时 save 仍成功但不返回 `item_key`）

**设计取舍：**
- **分步调用**：agent 分步执行每个工具（可见、可调试、可中断），不封装成单一复合 tool
- **笔记内容**：由 agent 根据阅读生成，ZotPilot 不做 AI 内容生成
- **集合路由**：agent 先 `list_collections` 了解现有结构再决策，不做自动算法

**状态**：🔄 进行中（v0.4）

---

## 2026-03-24

### 分支策略建立
- **决策**：新建 `dev` 作为日常开发分支，`main` 仅接受来自 `dev` 的 PR 合并，禁止直接 push main
- **理由**：规范化发版流程，防止未经验证的变更直接进入生产分支；与 zotpilot-connector 保持一致的工程规范
- **行动**：创建并推送 `origin/dev`，在 CLAUDE.md 写入 Git Workflow 规范
- **状态**：✅ 完成

### docs/ 文档体系建立
- **决策**：新建 `docs/` 存放内部架构文档和决策记录，从 `.omc/` 提炼历史决策
- **理由**：与 zotpilot-connector 的 `.docs/` 保持平行的文档结构；`.omc/` 是运维记录，`docs/` 是工程决策知识库
- **行动**：创建 `docs/architecture.md` 和 `docs/decisions.md`
- **状态**：✅ 完成

---

## 2026-03-23 | v0.3.1（规划中）

### SKILL.md 重构：Hybrid 精简方案
- **决策**：将安装/配置/更新内容迁移到 `references/setup-guide.md`，SKILL.md 保留 readiness check + 3 行 inline setup 兜底
- **理由**：SKILL.md 217 行中 51% 是一次性 setup 内容，每次 agent 会话都加载但从不使用；纯重构无 fallback 风险高
- **弃用方案**：纯增量（膨胀不可持续）、纯重构（无 inline fallback 有平台兼容风险）
- **行动**：提取 setup/config/update 到 `references/setup-guide.md`；删除 `references/install-steps.md`（合并入 setup-guide）
- **状态**：🔄 进行中

### Windows 升级错误：catch-the-error vs 进程检测
- **决策**：捕获 `CalledProcessError`，检查 stderr 关键词（PermissionError、WinError、Access is denied 等），匹配则输出友好提示
- **理由**：`tasklist` 进程检测不可靠（漏掉 python.exe 启动的进程、locale 依赖、wmic 已弃用）
- **已知限制**：stderr 关键词可能随 pip/uv 版本变化（silent decay），最差情况回退到显示原始 stderr
- **行动**：提取共享 helper `_is_windows_lock_error(stderr)`，修改两处 `CalledProcessError` handler
- **状态**：🔄 进行中

### Cursor/Windsurf 升级为 Tier 1
- **决策**：两个平台都支持 skills，从 Tier 2 升级为 Tier 1
- **理由**：Cursor `~/.cursor/skills/`、Windsurf `~/.codeium/windsurf/skills/` 均已验证支持 skill 目录
- **行动**：`_platforms.py` + `scripts/platforms.py` 中 tier 改为 1，加 `skills_dir`；README/SKILL.md 同步更新
- **已提交**：`de709bd`
- **状态**：✅ 完成

### 写操作配置方式统一
- **决策**：统一用 `config set` 持久化 key，而非 "tell agent" 临时传入
- **理由**：`register` 写的是 MCP 客户端配置，换客户端就丢；`config.json` 是 ZotPilot 自己的，本地持久
- **流程**：agent 引导获取 key → `zotpilot config set zotero_api_key` → `zotpilot config set zotero_user_id` → `zotpilot register`
- **状态**：✅ 完成

---

## 2026-03-23 | v0.3.0（已发布）

### `zotpilot update` 子命令架构
- **决策**：实现为 CLI 包内的薄包装协调器，委托给现有包管理器（uv/pip）执行实际升级，skill 目录通过 `git pull` 更新
- **理由**：不同安装方式（uv/pip/editable）的用户需要一个统一命令；shell script 不够可发现
- **弃用方案**：外部 shell script（不够可发现）、仅 `--check`（不解决用户不更新的问题）
- **关键设计**：
  - installer 检测锚定当前运行实例（`sys.argv[0]`），不用 `shutil.which("zotpilot")`（PATH 优先级不可靠）
  - uv 检测通过 `uv tool dir --bin` 动态获取，不硬编码路径
  - 返回 `(installer, uv_cmd)` 元组，调用方复用 `uv_cmd`，不独立 re-resolve
  - skill dir 更新前必须通过：本地 identity check（SKILL.md name + scripts/run.py）、dirty tree 检查、symlink 检查
  - remote URL 不作为 skip gate（允许 fork/镜像），只打 info note
- **已提交**：`303f675`
- **状态**：✅ 完成

### CI 全面修复
- **决策**：ruff 114 errors 全修（auto-fix + noqa）；mypy 136 errors 通过 per-module `[[tool.mypy.overrides]]` 抑制
- **理由**：历史遗留问题，之前 CI 未强制执行；一次性清零让后续 CI 持续有效
- **已提交**：`74b62e0`（ruff）、`63d3dc8`（mypy）
- **状态**：✅ 完成（CI 首次全绿）

### `main` 分支保护
- **决策**：禁止 force push、禁止删除；不要求 PR review（用户保留直接 push 权限）、不要求 CI 通过
- **理由**：防止历史误删，同时保留开发灵活性
- **状态**：✅ 完成

---

## 2026-03-22 | v0.2.1（已发布）

### 论文收录功能（ingestion 工具组）
- **决策**：新增 `search_academic_databases`、`add_paper_by_identifier`、`ingest_papers` 三个工具
- **数据来源**：Semantic Scholar API（`S2_API_KEY` 可选，提升限速）
- **状态**：✅ 完成

### `zotpilot config` CLI 子命令
- **决策**：新增 `config set/get/list/path/remove` 管理配置文件
- **理由**：之前只能手动编辑 `config.json`，配置体验差
- **状态**：✅ 完成

### API key 优先级规则
- **决策**：环境变量 > config 文件；`Config.save()` 设计上不持久化 API key（安全考虑）；`config set` 直接写 JSON 可以持久化
- **状态**：✅ 完成

---

## 早期开发（从 git history / .omc 提炼）

### Bridge + Connector 联动架构（Phase 1 生产可用）

**两个 repo 的职责分工：**

| 侧 | Repo | 核心文件 |
|---|---|---|
| Python / MCP | ZotPilot | `src/zotpilot/bridge.py`, `tools/ingestion.py` |
| Chrome 扩展 | zotpilot-connector | `src/browserExt/agentAPI.js` |

**完整数据流：**
```
1. AI Agent 调用 save_from_url(url, collection_key, tags)
2. save_from_url → BridgeServer.auto_start()（若 bridge 未运行则自动拉起）
3. POST /enqueue → Bridge 返回 request_id
4. MCP tool 每 2s 轮询 GET /result/<request_id>，最多等 90s
5. Chrome 扩展（agentAPI.js）每 2s GET /pending
6. 扩展收到命令 → 打开新标签页加载 url
7. 触发 onZoteroButtonElementClick，Zotero Connector 运行翻译器
8. agentAPI.js 双重 monkey-patch 检测保存完成（sendMessage 主路径 + receiveMessage 防御）
9. 扩展 POST /result {success, title, item_key, ...}
10. MCP tool 收到结果 → _apply_bridge_result_routing 处理 collection/tag 路由
```

**架构决策：WebSocket → HTTP 轮询**
- **初始方案**：WebSocket（`ws://localhost:2618`）
- **实际实现**：HTTP 轮询（`localhost:2619`，`ThreadingHTTPServer`）
- **理由**：MV3 Service Worker 不支持持久 WebSocket 连接，随时可能被浏览器休眠；HTTP 轮询天然兼容
- **ThreadingHTTPServer 而非 asyncio**：MCP tool 轮询 `/result` 时会阻塞；若用单线程 server，扩展同时 POST `/result` 会死锁

**关键参数：**
- 心跳超时：30s（扩展每 10s 发一次，容忍 3 次丢失）
- Bridge 未收到心跳时 `/enqueue` 返回 503（fast-fail，不入队）
- Result TTL：5 分钟；最大存 100 条（防无限增长）
- `save_from_url` 轮询超时：90s

**Bridge Server 归属：ZotPilot 主 repo**
- **理由**：Bridge 是 `save_from_url` MCP tool 的基础设施，自然属于 Python 侧；connector 只负责扩展侧逻辑
- **auto_start**：`save_from_url` 调用时若 bridge 未运行，自动以子进程拉起，无需用户手动 `zotpilot bridge`

- **状态**：✅ 完成（`f12beac` Phase 1 生产可用）

### FastMCP 框架选型
- **决策**：使用 FastMCP 而非手写 MCP 协议层
- **理由**：`@mcp.tool` 装饰器 + import side-effects 注册，极大简化工具模块的添加和维护
- **状态**：✅ 完成

### 懒加载单例架构（`state.py`）
- **决策**：所有昂贵对象使用懒加载单例，受单一 `threading.Lock` 保护；`switch_library` 全部重置
- **理由**：MCP server 启动时不知道用户是否需要 RAG；避免无效初始化
- **关键**：后台线程监控父进程 PID，父进程退出时 `os._exit(0)` 防孤儿进程
- **状态**：✅ 完成

### No-RAG 降级模式
- **决策**：`embedding_provider="none"` 时完全跳过向量索引，工具退化为 SQLite 元数据搜索
- **理由**：部分用户无法访问 Gemini/DashScope API，不应因此无法使用基础功能
- **状态**：✅ 完成

### 嵌入提供方抽象
- **决策**：`embeddings/base.py` 定义 `Embedder` 接口，`create_embedder(config)` 工厂函数按配置返回实现
- **理由**：支持 Gemini、DashScope、本地模型，对工具层完全透明
- **状态**：✅ 完成

### `switch_library` 工具禁用（P1-2/3）
- **决策**：移除 `switch_library` 的 `@mcp.tool()` 装饰器，从 MCP 工具列表中去除
- **理由**：该工具承诺了跨库 RAG 能力，但实现上存在数据完整性风险（cross-library RAG leaks）；功能未完成前不暴露
- **保留**：`_set_library_override`、`_reset_singletons` 等内部函数保留，等多库索引实现后重新暴露
- **状态**：✅ 完成

### `_zotpilot_command()` fallback 策略（P1-1）
- **决策**：config-writing 调用方使用 `allow_fallback=False`，找不到 binary 时抛 RuntimeError 并终止注册
- **理由**：原来的 `return "zotpilot"` 兜底会写入损坏的配置文件，数据完整性风险
- **保留**：`_print_manual_fallback` 保持 `allow_fallback=True`（只打印帮助文本，不写配置）
- **状态**：✅ 完成

### `python -m zotpilot` 不能作为 CLI fallback（P1-5）
- **决策**：不推荐 `python -m zotpilot` 作为 Windows PATH 问题的替代方案
- **理由**：`__main__.py` 启动的是 MCP server（`server.main()`），不是 CLI；会误导用户
- **修正**：只推荐修 PATH，文档更新为 `zotpilot register` 替代 `scripts/run.py register`
- **状态**：✅ 完成

---

## 版本管理约定

- **Claude 负责版本管理**（见 CLAUDE.md `## Version Management`）
- **patch** (0.x.Z)：bug fixes, doc updates, test additions
- **minor** (0.Y.0)：new user-facing features (new CLI subcommand, new MCP tool)
- **major** (X.0.0)：breaking changes to MCP tool signatures or config format
- **CHANGELOG**：双语格式（中文 / English），CI 用 awk 提取 release notes
- **发版流程**：`dev` → PR → `main` → commit → tag vX.Y.Z → push + push tags → CI 自动发 PyPI + GitHub Release

---

<!-- 新决策追加在上方，格式：
## YYYY-MM-DD
### 标题
- **决策**：
- **理由**：
- **行动**：
- **状态**：🔄 进行中 / ✅ 完成 / ❌ 撤销
-->
