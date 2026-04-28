# ZotPilot E2E 场景清单

**版本**：v1.1（2026-03-28）
**维护说明**：每修复一个回归 bug，在对应域新增一条场景。ID 永不复用。

## 清理策略

**无 MCP delete 工具**，清理方式：
- 每个测试条目在入库时打 `["e2e-test"]` 标签
- PASS 后：**不调用 `set_item_tags`**（会抹掉用户原有标签）；条目保留在库中，由人工在 Zotero 批量删除带 `e2e-test` 标签的条目
- FAIL 后：追加 `e2e-fail-SCENID` 标签（用 `add_item_tags`，不覆盖已有标签），供人工复查
- 运行前置条件（"库中不存在"）依赖 `advanced_search` 按 DOI 查询；若前置条件不满足（条目已存在），标注 SKIP 并注明"需人工清理后重跑"
- 跑完后：`advanced_search([{"field": "tag", "op": "is", "value": "e2e-test"}])` 列出所有测试条目，报告中附上列表供人工清理

---

## 场景 ID 格式

`<域>-NNN`，域前缀：

| 前缀 | 问题域 |
|---|---|
| `DEDUP` | DOI 去重 |
| `ITEMKEY` | item_key 发现链 |
| `ITYPE` | itemType 验证（通过 `publication` 字段间接推断） |
| `ANTIBOT` | 反爬检测 |
| `PDF` | PDF 状态 |
| `PUBLISHER` | 出版商探针 |
| `CONCUR` | 并发与线程安全 |

---

## 域 1：DOI 去重（DEDUP）

### DEDUP-001 同一 arXiv 论文入库两次应被去重

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2301.07041"}])` 返回空
**输入**：
- 第一次：`ingest_papers([{"arxiv_id": "2301.07041", "landing_page_url": "https://arxiv.org/abs/2301.07041"}], tags=["e2e-test"])`
- 第二次：同上

**预期返回值**：
- 第一次：`ingested=1, skipped_duplicates=0`
- 第二次：`ingested=0, skipped_duplicates=1, results[0].status=already_in_library`

**预期库状态**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2301.07041"}])` 返回恰好 1 条
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### DEDUP-002 先用 arXiv URL 入库，再用期刊 DOI 入库同一篇，应去重

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.1706.03762"}])` 返回空
**输入**：
- 第一次：`ingest_papers([{"arxiv_id": "1706.03762", "landing_page_url": "https://arxiv.org/abs/1706.03762"}], tags=["e2e-test"])`
- 第二次：`ingest_papers([{"doi": "10.48550/arxiv.1706.03762", "landing_page_url": "https://arxiv.org/abs/1706.03762"}], tags=["e2e-test"])`

**预期返回值**：第二次 `skipped_duplicates=1`
**预期库状态**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.1706.03762"}])` 返回 1 条
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### DEDUP-003 批量 8 篇中有 2 篇已在库，去重计数正确

**可信度**：HIGH
**前置条件**：库中已存在 arXiv:2301.07041 和 arXiv:1706.03762（来自 DEDUP-001/002）
**输入**：`ingest_papers([8 个条目，其中 2 个为上述已有论文，另外 6 个为新 arXiv OA 论文], tags=["e2e-test"])`
**预期返回值**：`ingested=6, skipped_duplicates=2, total=8`
**预期库状态**：已有 2 篇仍为 1 条（`advanced_search` 按 DOI 验证），无重复
**清理**：人工在 Zotero 中删除新入库的 6 条（带 `e2e-test` 标签）

---

### DEDUP-004 5 分钟内 in-memory cache 拦截重复

**可信度**：HIGH
**前置条件**：库中不存在目标论文（选一篇新 arXiv OA 论文）
**输入**：
- 第一次：`ingest_papers([{"arxiv_id": "XXXX", ...}], tags=["e2e-test"])`，记录调用开始/结束时间 T1
- 第二次（10s 内）：相同调用，记录耗时 T2

**预期返回值**：第二次 `skipped_duplicates=1`
**时间断言**：T2 < T1（cache 命中路径更快）
**预期库状态**：`advanced_search` 按 DOI 返回 1 条
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

## 域 2：item_key 发现（ITEMKEY）

### ITEMKEY-001 标准 translator 路径（arXiv），item_key 能被发现

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2301.07041"}])` 返回空；ZOTERO_API_KEY 已配置
**输入**：`save_urls(["https://arxiv.org/abs/2301.07041"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`results[0].item_key` 非空非 null
**预期库状态**：`get_paper_details(item_key)` 成功返回，`date_added` 字段非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITEMKEY-002 慢发布商（Cambridge Core），指数退避后发现 item_key

**可信度**：MEDIUM
**前置条件**：库中不存在目标论文，有机构订阅
**输入**：`save_urls(["<当前可访问的 Cambridge Core 文章 URL>"], tags=["e2e-test"])`（运行时选择）
**预期返回值**：`results[0].success=True`，`results[0].item_key` 非空（允许最多 30s 延迟）
**预期库状态**：`get_paper_details(item_key)` 返回 `publication` 字段非空（期刊名）
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITEMKEY-003 item_key 发现后 collection/tag routing 正确应用

**可信度**：HIGH
**前置条件**：ZOTERO_API_KEY 已配置；通过 `list_collections()` 获取 INBOX collection_key
**步骤**：
1. `list_collections()` → 找到 INBOX 的 key
2. `save_urls(["https://arxiv.org/abs/2310.06825"], collection_key=<inbox_key>, tags=["e2e-test", "e2e-routing-test"])`

**预期返回值**：`results[0].success=True`，`results[0].item_key` 非空，`results[0].warning` 为 null 或缺失
**预期库状态**：`get_paper_details(item_key)` 显示 `collections` 字段（分号分隔的 collection **名称**字符串）包含 "INBOX"，`tags` 字段包含 `e2e-routing-test`
**清理**：无需额外操作（入库时已带 `e2e-test` / `e2e-routing-test` 标签，运行结束后由人工统一删除）

---

### ITEMKEY-004 journalArticle 的 url 字段为空，发现链不因此失败

**可信度**：MEDIUM
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.1038/s41586-021-03819-2"}])` 返回空
**输入**：`save_urls(["https://www.nature.com/articles/s41586-021-03819-2"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`results[0].item_key` 非空
**预期库状态**：`get_paper_details(item_key)` 返回 `doi=10.1038/s41586-021-03819-2`，`publication` 非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITEMKEY-005 无 ZOTERO_API_KEY 时 save 成功但不崩溃

**可信度**：HIGH
**重要限制**：此场景**必须在独立 MCP 进程中运行**（writer singleton 已缓存，同 session 内清除 env var 无效）。运行方式：重启 ZotPilot MCP server 并设 `ZOTERO_API_KEY=` 为空，再执行此场景。
**前置条件**：ZOTERO_API_KEY 未配置（空字符串或缺失），MCP 进程刚启动
**输入**：`save_urls(["https://arxiv.org/abs/2301.07041"])`
**预期返回值**：`results[0].success=True`，`results[0].item_key` 为 null 或缺失，无异常
**预期库状态**：`advanced_search([{"field": "title", "op": "contains", "value": "A Survey of Large Language Models"}])` 能找到该条目
**清理**：恢复 ZOTERO_API_KEY；重启 MCP server；若条目已入库，则保留其状态并在报告中注明，后续由人工按 `e2e-test` 标签统一删除
**SKIP 条件**：无法在当前环境重启 MCP server，标注 SKIP 并注明"需独立进程"

---

## 域 3：itemType 验证（ITYPE）

**说明**：`get_paper_details` 不返回 `itemType`。通过以下字段间接推断：
- preprint：`publication` 通常为空或含 "arXiv"，`doi` 含 `10.48550/arxiv`
- journalArticle：`publication` 为期刊名（非空），`doi` 为正式期刊 DOI
- 反爬检测/fallback：工具返回 `translator_fallback_detected=True`

### ITYPE-001 arXiv 论文保存后为 preprint，不被误删

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2301.07041"}])` 返回空
**输入**：`save_urls(["https://arxiv.org/abs/2301.07041"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`results[0].translator_fallback_detected` 不为 True
**预期库状态**：`get_paper_details(item_key)` 显示 `doi` 含 `10.48550/arxiv`（preprint 标志）
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITYPE-002 AIP 出版商页面保存后为期刊文章

**可信度**：MEDIUM
**前置条件**：库中不存在目标论文，有机构订阅
**输入**：`save_urls(["<当前可访问的 pubs.aip.org 文章 URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`translator_fallback_detected` 不为 True
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 非空且不含 "arXiv"
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITYPE-003 Elsevier/ScienceDirect 页面保存后为期刊文章

**可信度**：MEDIUM
**前置条件**：库中不存在目标论文，有机构订阅
**输入**：`save_urls(["<当前可访问的 sciencedirect.com 文章 URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITYPE-004 webpage 降级被检测并不留垃圾条目

**可信度**：MEDIUM
**前置条件**：库中无 sciencedirect.com 首页条目
**输入**：`save_urls(["https://www.sciencedirect.com/"])`
**预期返回值**：`results[0].success=False`，`results[0].translator_fallback_detected=True`
**预期库状态**：`advanced_search([{"field": "title", "op": "contains", "value": "ScienceDirect"}])` 无新增条目
**清理**：无需清理

---

### ITYPE-005 SpringerLink 论文保存后为期刊文章

**可信度**：MEDIUM
**前置条件**：库中不存在目标论文，有机构订阅
**输入**：`save_urls(["<当前可访问的 link.springer.com 文章 URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITYPE-006 Nature 论文保存后为期刊文章

**可信度**：MEDIUM
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.1038/s41586-021-03819-2"}])` 返回空
**输入**：`save_urls(["https://www.nature.com/articles/s41586-021-03819-2"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 含 "Nature"
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ITYPE-007 PNAS 论文保存后为期刊文章

**可信度**：MEDIUM
**前置条件**：库中不存在目标论文
**输入**：`save_urls(["<当前可访问的 pnas.org DOI URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 含 "PNAS" 或 "National Academy"
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

## 域 4：反爬检测（ANTIBOT）

### ANTIBOT-001 Cloudflare 保护页面被 preflight 拦截，不产生垃圾条目

**可信度**：LOW（依赖出版商当前反爬状态）
**输入**：`ingest_papers([{"landing_page_url": "<已知 CF 保护 URL>"}])`
**预期返回值**：`ingest_complete=False` 且（`preflight_report` 含 `blocked` 条目，或 `results[0].anti_bot_detected=True`）
**预期库状态**：`advanced_search([{"field": "tag", "op": "is", "value": "e2e-test"}])` 无新增
**清理**：无需清理
**SKIP 条件**：preflight 返回 `all_clear=True`（CF 未触发），标注 SKIP

---

### ANTIBOT-002 AIP 多跳跳转，preflight 不误报

**可信度**：MEDIUM
**前置条件**：有机构订阅
**输入**：`ingest_papers([{"doi": "10.1063/5.0030305", "landing_page_url": "https://doi.org/10.1063/5.0030305"}], tags=["e2e-test"])`
**预期返回值**：`preflight_report.all_clear=True`（若有 preflight），`ingested=1`
**预期库状态**：`get_paper_details(item_key)` 显示 `publication` 非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ANTIBOT-003 不存在的 ScienceDirect URL 不入库

**可信度**：MEDIUM
**输入**：`ingest_papers([{"landing_page_url": "https://www.sciencedirect.com/science/article/pii/S0000000000000000"}])`
**预期返回值**：`results[0].status=failed` 且含 `error` 或 `error_code`
**预期库状态**：`advanced_search([{"field": "tag", "op": "is", "value": "e2e-test"}])` 无新增
**清理**：无需清理

---

### ANTIBOT-004 正常 OA 页面不被误判为 anti-bot

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2310.06825"}])` 返回空
**输入**：`ingest_papers([{"arxiv_id": "2310.06825", "landing_page_url": "https://arxiv.org/abs/2310.06825"}], tags=["e2e-test"])`
**预期返回值**：`preflight_report.all_clear=True`（若有 preflight），`ingested=1`
**预期库状态**：`get_paper_details(item_key)` 成功返回
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### ANTIBOT-005 404 URL 不入库

**可信度**：HIGH
**输入**：`ingest_papers([{"landing_page_url": "https://arxiv.org/abs/9999.99999"}])`
**预期返回值**：`results[0].status=failed`，含 error 信息
**预期库状态**：`advanced_search([{"field": "tag", "op": "is", "value": "e2e-test"}])` 无新增
**清理**：无需清理

---

## 域 5：PDF 状态（PDF）

### PDF-001 arXiv OA 论文，PDF 自动附加

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2301.07041"}])` 返回空
**输入**：`ingest_papers([{"arxiv_id": "2301.07041", "landing_page_url": "https://arxiv.org/abs/2301.07041"}], tags=["e2e-test"])`
**预期返回值**：`results[0].pdf=attached`
**预期库状态**：`get_paper_details(item_key)` 显示 `pdf_available=true`
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### PDF-002 批量 8 篇 OA，PDF 轮询预算足够，不误报 none

**可信度**：HIGH
**前置条件**：8 篇均为 OA arXiv，库中均不存在
**输入**：`ingest_papers([8 个 arXiv 条目], tags=["e2e-test"])`
**预期返回值**：`pdf_summary.attached >= 6`，`total=8`，无异常
**预期库状态**：对 `pdf_summary.attached` 个条目调用 `get_paper_details` 验证 `pdf_available=true`
**清理**：人工在 Zotero 中删除新入库条目（带 `e2e-test` 标签）

---

### PDF-003 MDPI OA，PDF 直链不阻断条目入库

**可信度**：HIGH
**前置条件**：库中不存在目标 MDPI 论文
**输入**：`save_urls(["<当前可访问的 mdpi.com OA 文章 URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`（`pdf` 字段为 attached 或 none 均可）
**预期库状态**：`get_paper_details(item_key)` 成功返回，`publication` 非空
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### PDF-004 单篇入库，PDF 就绪后提前退出

**可信度**：HIGH
**前置条件**：`advanced_search([{"field": "doi", "op": "is", "value": "10.48550/arxiv.2310.06825"}])` 返回空
**输入**：记录开始时间；`ingest_papers([{"arxiv_id": "2310.06825", "landing_page_url": "https://arxiv.org/abs/2310.06825"}], tags=["e2e-test"])`；记录结束时间
**预期返回值**：`results[0].pdf=attached`，总耗时 < 60s
**预期库状态**：`get_paper_details(item_key)` 显示 `pdf_available=true`
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### PDF-005 12 篇批量，PDF 预算自动上限，不超时崩溃

**可信度**：HIGH
**前置条件**：12 篇均为 OA arXiv，库中均不存在
**输入**：`ingest_papers([12 个 arXiv 条目], tags=["e2e-test"])`（工具自动分块，每块 ≤ 10）
**预期返回值**：`total=12`，有 `pdf_summary` 字段，无异常，总耗时 ≤ 180s
**预期库状态**：12 条均存在（`advanced_search` 按 tag 验证）
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### PDF-006 Elsevier PDF 下载触发机器人验证，元数据成功但 PDF 失败

**可信度**：LOW（依赖出版商当前反爬状态）
**前置条件**：Elsevier URL 当前触发 PDF 下载验证，有机构订阅
**输入**：`save_urls(["<当前触发 PDF robot 验证的 sciencedirect.com URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`results[0].pdf=none`，有 `warning` 字段
**预期库状态**：`get_paper_details(item_key)` 显示 `pdf_available=false`
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）
**SKIP 条件**：`pdf=attached`（robot 验证未触发），标注 SKIP

---

### PDF-007 订阅期刊，机构 Cookie 有效，PDF 成功附加

**可信度**：MEDIUM
**前置条件**：Chrome 已登录机构账号；库中不存在目标论文
**输入**：`save_urls(["<有订阅的期刊文章 URL>"], tags=["e2e-test"])`（运行时选择）
**预期返回值**：`results[0].success=True`，`results[0].pdf=attached`
**预期库状态**：`get_paper_details(item_key)` 显示 `pdf_available=true`
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### PDF-008 Cookie 复用：同出版商第二篇，PDF 正常下载

**可信度**：MEDIUM
**前置条件**：紧接 PDF-007 之后（< 5 分钟），Cookie 仍有效；同一出版商另一篇
**输入**：`save_urls(["<同出版商另一篇 URL>"], tags=["e2e-test"])`
**预期返回值**：`results[0].success=True`，`results[0].pdf=attached`
**预期库状态**：`get_paper_details(item_key)` 显示 `pdf_available=true`
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

## 域 6：出版商探针（PUBLISHER）

**通用流程**：
1. `advanced_search([{"field": "doi", "op": "is", "value": "<doi>"}])` 确认无重复（已知 DOI 时）
2. `save_urls([URL], tags=["e2e-test"])`
3. 断言 `results[0].success=True`，`translator_fallback_detected` 不为 True
4. `get_paper_details(item_key)` 验证 `doi` 非空 且 `publication` 非空（preprint 除外）
5. 清理：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

### PUB-001 arXiv

**可信度**：HIGH
**URL**：`https://arxiv.org/abs/2301.07041`
**验证点**：`doi` 含 `10.48550/arxiv`，`pdf_available=true`

---

### PUB-002 AIP Publishing

**可信度**：MEDIUM
**URL**：运行时选当前可访问的 `pubs.aip.org` 文章 URL
**验证点**：`publication` 含 "Journal of" 或 "Physics"，`item_key` 非空

---

### PUB-003 Elsevier / ScienceDirect

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `sciencedirect.com` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-004 Springer / SpringerLink

**可信度**：MEDIUM
**URL**：`https://link.springer.com/article/10.1007/s10994-021-06056-y`（或当前可访问的替代）
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-005 Nature

**可信度**：MEDIUM
**URL**：`https://www.nature.com/articles/s41586-021-03819-2`
**验证点**：`publication` 含 "Nature"，`doi=10.1038/s41586-021-03819-2`

---

### PUB-006 PNAS

**可信度**：MEDIUM
**URL**：运行时选当前可访问的 `pnas.org` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-007 Cambridge Core

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `cambridge.org/core` 文章 URL
**验证点**：`item_key` 非空（允许退避延迟），`publication` 非空

---

### PUB-008 Wiley Online Library

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `onlinelibrary.wiley.com` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-009 Taylor & Francis

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `tandfonline.com` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-010 Oxford Academic

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `academic.oup.com` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-011 MDPI（OA）

**可信度**：HIGH
**URL**：运行时选当前可访问的 `mdpi.com` OA 文章 URL
**验证点**：`publication` 非空，`pdf_available=true`

---

### PUB-012 Frontiers（OA）

**可信度**：HIGH
**URL**：运行时选当前可访问的 `frontiersin.org` OA 文章 URL
**验证点**：`publication` 非空，`pdf_available=true`

---

### PUB-013 IEEE Xplore

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `ieeexplore.ieee.org` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-014 ACS Publications

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `pubs.acs.org` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-015 SAGE Journals

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `journals.sagepub.com` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-016 Annual Reviews

**可信度**：MEDIUM
**URL**：运行时选当前有机构访问权限的 `annualreviews.org` 文章 URL
**验证点**：`publication` 非空，`doi` 非空

---

### PUB-017 bioRxiv / medRxiv

**可信度**：HIGH
**URL**：`https://www.biorxiv.org/content/10.1101/2021.11.01.466702v2`（或当前可访问的预印本）
**验证点**：`doi` 含 `10.1101`，`pdf_available=true`

---

### PUB-018 PubMed Central（OA）

**可信度**：HIGH
**URL**：`https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8279538/`（或当前可访问的 PMC OA 文章）
**验证点**：`publication` 非空，`pdf_available=true`

---

## 域 7：并发与线程安全（CONCUR）

### CONCUR-001 批量 5 篇并发保存，无竞态，无重复条目

**可信度**：HIGH
**前置条件**：5 篇均不在库中（选 5 篇不同 arXiv OA 论文）
**输入**：`ingest_papers([5 个不同 arXiv 条目], tags=["e2e-test"])`
**预期返回值**：`ingested=5, skipped_duplicates=0, total=5`
**预期库状态**：对每个 item_key 调用 `get_paper_details` 确认存在，共 5 条
**清理**：人工在 Zotero 中删除（带 `e2e-test` 标签的条目）

---

### CONCUR-002 anti-bot 触发后 cancel_event 停止其他线程

**可信度**：LOW（依赖出版商当前反爬状态）
**行为说明**：`save_urls` 代码（ingestion.py:1437）说明：anti-bot 触发**前**已完成的 save 会正常返回 success；触发后的 URL 标为 `pending`。"库中无新增条目"断言**不成立**。
**前置条件**：批量中第 1 个 URL 会触发 anti-bot（运行时确认）
**输入**：`save_urls(["https://www.sciencedirect.com/", "https://arxiv.org/abs/2301.07041", "https://arxiv.org/abs/1706.03762", "https://arxiv.org/abs/2310.06825", "https://arxiv.org/abs/2205.01068"])`
**预期返回值**：`results[0]` 含 `anti_bot_detected=True` 或 `status=failed`；其余条目 `status=pending`（未处理）或 `success=True`（已在 anti-bot 前完成）
**预期库状态**：仅验证 anti-bot URL 本身无垃圾条目（`advanced_search` 按 title 搜索）
**清理**：人工在 Zotero 中删除任何意外入库的 e2e-test 条目
**SKIP 条件**：ScienceDirect 首页当前未触发 anti-bot，标注 SKIP
