# Changelog

## [Unreleased] - 2026-03-26

### Fixed
- **心跳断连（长时间保存时）**：原心跳依赖 `_poll()` 循环计数器（每 5 次 poll = 每 10s），但 `_poll()` 在 `await _handleSave()` 期间（最长 ~95s）被挂起，导致 bridge 在 30s 后判定 extension 断连。改为独立 `setInterval(10000)` 驱动心跳，与 poll 循环完全解耦，保存期间心跳正常发送。
- **translator 就绪检测从轮询改为事件驱动**：原 `_pollForTranslators` 固定 5s 超时后无论 translator 是否就绪都触发保存，导致 AIP/Elsevier 等慢加载页面保存为 webpage。改为 monkey-patch `Zotero.Connector_Browser.onTranslators`，translator 到达时立即通知等待中的 `_handleSave`（`_translatorWaiters` Map），20s 超时兜底。
- **反爬页面产生垃圾条目**：将反爬检测从 bridge 侧（保存后检测 title）移至 `_handleSave` 中，在调用 `onZoteroButtonElementClick` 之前检查 tab title。匹配到 "请稍候…"、"just a moment"、"cloudflare" 等模式时直接返回 `{success: false, error_code: "anti_bot_detected"}`，不执行保存，不在 Zotero 库中产生无效条目。
- **多跳跳转页面过早触发保存（AIP 等出版商）**：AIP 等出版商首次访问时会经历"验证页 → 文章页"两跳，原 `STABILITY_WINDOW_REDIRECT_MS = 2000ms` 不足以等待文章页完全加载及 translator 注入完成，导致保存为 snapshot 而非论文条目。将 `STABILITY_WINDOW_REDIRECT_MS` 从 2000ms 提升至 4000ms，确保多跳场景下文章页稳定后再触发保存。

### Added
- **第二握手：保存完成后本地 API 轮询确认**：新增 `_waitForItemInZotero()`，在 `progressWindow.done` 触发后立即轮询 Zotero 本地 API（`/api/users/0/items/top`），与 save 前快照 diff 检测新条目。最多 15 次、1s 间隔，确认条目真正写入 SQLite 再报 success。`success="unconfirmed"`（60s 超时）场景下发现条目则升级为 success。
- **PDF 下载失败快速退出**：在 `sendMessage` patch 中监听 `progressWindow.itemProgress` 事件。当 `payload.iconSrc` 包含 `cross.png`（Zotero 附件下载失败图标）时，立即 resolve 并返回 `{success: true, pdf_failed: true, error_code: "pdf_download_failed"}`，而非等待 60s 超时。使 Elsevier 等有二次机器人验证的出版商能快速完成元数据入库，PDF 由用户手动补充。

## [0.0.2] - 2026-03-24

质量巩固版本，无新功能。

### Fixed
- **receiveMessage patch**：修复 defense-in-depth 路径长期失效的问题。inject 侧消息以 `["Messaging.sendMessage", [name, args]]` 格式到达，导致原始检查永远无法匹配 `progressWindow.done`。现在正确解包后再检测，备用完成检测路径恢复工作。

### Added
- **单元测试**：新增 `test/tests/agentAPITest.mjs`，15 个测试覆盖 agentAPI.js 全部核心路径（80%+ 行覆盖率）
- `test/agentAPI.mocharc.js` 独立 mocha 配置，`npm run test:unit` 脚本，与现有 E2E 测试完全隔离

### Documentation
- `PROTOCOL.md` 版本号修正（错误的 `0.1.0` → `0.0.2`）
- 明确 `collection_key`/`tags` 在扩展侧是 echo-only，实际 Zotero API 路由由 bridge 负责
- 注明错误响应不包含 `collection_key`/`tags`（有意为之）

## [0.0.1] - 2026-03-24

### Added
- Initial ZotPilot Agent API (`agentAPI.js`) with HTTP polling bridge support
- Dual monkey-patch completion detection (sendMessage primary + receiveMessage defense)
- Heartbeat mechanism (every 10s) with Zotero connectivity check
- `PROTOCOL.md` v1.0.0 HTTP bridge specification
