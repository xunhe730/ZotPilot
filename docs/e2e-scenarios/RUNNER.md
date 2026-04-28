# ZotPilot E2E Test Runner Protocol

本文件是 AI agent 执行 E2E 测试的完整操作手册。
每次运行前阅读此文件和 `scenarios.md`。

---

## 工具契约（agent 必读）

| 工具 | 关键字段 |
|------|---------|
| `get_paper_details(item_key)` | `doc_id, title, authors, year, publication, doi, tags, collections, pdf_available (bool), date_added` — **无 itemType，无 attachments 数组** |
| `advanced_search(conditions)` | 支持 field：`title, author, year, tag, collection, publication, doi` — **不支持 url 字段** |
| `get_collection_papers(collection_key)` | 返回 collection 内条目列表，用于验证 collection 是否为空 |
| 无 MCP delete 工具 | agent 打 `e2e-test` 标签，运行结束后人工在 Zotero 批量删除 |

---

## 运行前准备

1. **确认 MCP 可达**：
   - 调用 `get_index_stats()` — 返回 dict 且无异常即为 MCP 可达
   - **不要**用 `uv run zotpilot status`（只检查本地 config，不验证 Bridge/Connector）

2. **确认 Connector 可达**：
   - 调用 `save_urls(["https://arxiv.org/abs/9999.99999"])` — 此 URL 必然 404/失败，但能区分"bridge 连接错误"和"正常失败"
   - 若返回顶层 `error`（如 bridge not running / connection refused）或 `results` 为空，停止并提示用户检查 Chrome + Connector
   - 若返回非空 `results`，且 `results[0]` 为正常失败（如 `success=False`，并带 `error` / `error_code` / `status` 任一失败信号），则 Connector 可达 ✓（无条目入库，无需清理）

3. **确认环境**：
   - 确认 Zotero Desktop 已开启
   - 确认 ZotPilot Connector 已加载（Chrome 扩展图标可见）
   - 记录版本信息（Zotero、Chrome、Connector、ZotPilot 版本）

4. **选取运行时 URL**（MEDIUM/LOW 场景）：
   - 各出版商首页"最新发表"列表，或用 `search_academic_databases` 找近期论文
   - 每次运行选不同的具体 URL

5. **初始化报告文件**：
   - 路径：`docs/test-runs/YYYY-MM-DD-HHmm-e2e.md`（使用实际时间）
   - 按下方"报告模板"初始化

---

## 执行顺序

```
DEDUP (4) → ITEMKEY (5) → ITYPE (7) → PDF (8) → ANTIBOT (5) → PUBLISHER (18) → CONCUR (2)
```

---

## 每个场景的执行步骤

### 1. 前置清理检查

```python
advanced_search([{"field": "doi", "op": "is", "value": "<doi>"}], limit=1)
```

若有结果，记录"残留条目 item_key=XXXX"，并将该场景判定为 **SKIP**（原因：前置条件不满足，需人工清理后重跑）。
若无结果，正常执行场景。

### 2. 调用工具

按场景描述的"输入"调用对应 MCP 工具。**所有入库调用都加 `tags=["e2e-test"]`**（方便后续清理查询）。

### 3. 断言返回值

逐字段对比"预期返回值"与实际返回值，记录每个断言是 ✓ 还是 ✗。

**字段映射提醒**：
- 验证"有 PDF"→ `get_paper_details` 的 `pdf_available=true`（不是 `attachments`）
- 验证"添加时间"→ `date_added`（不是 `dateAdded`）
- 验证"itemType"→ 通过 `doi` 模式 + `publication` 字段间接推断

### 4. 验证库状态

按场景描述的"预期库状态"调用验证工具，记录断言结果。

### 5. 判定结果

| 条件 | 判定 |
|------|------|
| 所有断言 ✓ | PASS |
| 任一断言 ✗ | FAIL |
| 场景前提不成立 | SKIP（必须注明原因） |

### 6. 后置清理

- **PASS 场景**：不做自动清理（条目保留 `e2e-test` 标签，运行结束后由人工在 Zotero 批量删除）
- **FAIL 场景**：保留 `e2e-test` 标签，追加 `e2e-fail-SCENID` 标签；在报告中注明 `item_key=XXXX（保留供人工复查）`

### 7. 跑完后验证清理

```python
advanced_search([{"field": "tag", "op": "is", "value": "e2e-test"}])
```

若返回非空，列出残留条目（PASS/FAIL 场景均可能保留，属当前策略预期）。

对专为本次测试创建的 E2E collection（如存在），调用：
```python
get_collection_papers(collection_key)  # 列出 collection 内测试条目，供人工核对并统一清理
```

---

## FAIL 后自动探查

FAIL 场景不停止，继续跑完所有场景。FAIL 后立即执行：

1. `get_paper_details(item_key)` — 查看实际入库内容
2. 对比 `docs/decisions.md` 中记录的根因
3. 在报告中写出：
   - 实际返回值 vs 预期返回值（diff）
   - 可能的回归原因
   - 是否需要人工复查

---

## SKIP 条件

- URL 已失效（HTTP 404 或域名不存在）
- 出版商页面结构变化导致场景前提不成立
- 场景依赖特定网络状态（机构 Cookie 未登录）
- LOW 可信度场景的特定前提未满足（如 CF 未触发）
- ITEMKEY-005：无法在当前环境重启 MCP server

SKIP 必须注明：原因 + 是否需要人工补测。

---

## 报告模板

```markdown
# E2E Test Run — YYYY-MM-DD HH:MM

## 环境
- Zotero Desktop: x.x.x
- Chrome: xxx
- ZotPilot Connector: vX.Y.Z
- ZotPilot: vX.Y.Z
- 机构网络: 是 / 否
- MCP 可达确认: get_index_stats() → OK

## 汇总
| 域 | 总计 | PASS | FAIL | SKIP |
|---|---|---|---|---|
| DEDUP | 4 | | | |
| ITEMKEY | 5 | | | |
| ITYPE | 7 | | | |
| ANTIBOT | 5 | | | |
| PDF | 8 | | | |
| PUBLISHER | 18 | | | |
| CONCUR | 2 | | | |
| **合计** | **49** | | | |

## 详情

### ✅ DEDUP-001 PASS
**工具返回值**：`ingested=1, skipped_duplicates=0` ✓
**库状态**：`advanced_search` 返回 1 条 ✓
**耗时**：x.xs
**清理**：保留 `e2e-test` 标签，运行结束后人工批量删除

### ❌ ITEMKEY-002 FAIL
**工具返回值**：`item_key=null` ✗（预期非空）
**库状态**：条目存在 ✓
**失败原因**：（agent 填写）
**探查结论**：（agent 填写）
**保留条目**：`item_key=XXXX`（已打 e2e-fail-ITEMKEY-002 标签，保留供人工复查）

### ⚠️ ANTIBOT-001 SKIP
**原因**：目标 URL 当前无 CF 保护，preflight 返回 all_clear=True
**是否需人工补测**：否（LOW 可信度，下次运行时重试）
```

---

## 快速验证模式（默认可执行的 HIGH 场景子集，共 20 条）

首次运行或 CI 中，只跑以下场景：

**DEDUP**（4 条）：DEDUP-001, 002, 003, 004

**ITEMKEY**（2 条）：ITEMKEY-001, 003

**ITYPE**（1 条）：ITYPE-001

**ANTIBOT**（2 条）：ANTIBOT-004, 005

**PDF**（5 条）：PDF-001, 002, 003, 004, 005

**PUBLISHER**（5 条）：PUB-001, 011, 012, 017, 018

**CONCUR**（1 条）：CONCUR-001

合计 **20 条**（4+2+1+2+5+5+1）。完整运行（49 条）建议在有机构网络时进行。

**说明**：
- 该模式覆盖“默认在同一 MCP 会话内可直接执行”的 HIGH 场景子集。
- `ITEMKEY-005` 虽标记为 HIGH，但要求独立 MCP 进程重启验证，因此**不纳入**默认 quick mode。
- PUB-011、012、017、018 均为 HIGH（MDPI、Frontiers、bioRxiv、PMC），加上 PUB-001（arXiv）共 5 条 PUBLISHER；再加 CONCUR-001，总计 4+2+1+2+5+5+1 = **20 条**。
