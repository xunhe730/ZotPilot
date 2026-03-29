# ZotPilot Connector：面向 Zotero 的浏览器保存执行层

<div align="center">
  <h2>给 AI agent 用的 Chrome 保存桥接扩展</h2>

  <p>
    <img src="https://img.shields.io/badge/Chrome-MV3-4285F4?style=flat-square&logo=googlechrome&logoColor=white" alt="Chrome MV3">
    <img src="https://img.shields.io/badge/Bridge-HTTP%20localhost%3A2619-0A7E5C?style=flat-square" alt="HTTP bridge">
    <img src="https://img.shields.io/badge/Base-Zotero%20Connector-BD2C00?style=flat-square" alt="Based on Zotero Connector">
    <img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="License">
  </p>

  <p>
    <a href="#这是什么">这是什么</a> &bull;
    <a href="#为什么要做这个">为什么</a> &bull;
    <a href="#快速开始">快速开始</a> &bull;
    <a href="#工作原理">架构</a> &bull;
    <a href="#api">API</a> &bull;
    <a href="#开发">开发</a> &bull;
    <a href="README.md">English</a>
  </p>
</div>

---

## 这是什么

ZotPilot Connector 是一个基于 [Zotero Connector](https://www.zotero.org/support/dev/client_coding/connector_http_server) fork 出来的 Chrome 扩展，在上游保存能力的基础上，加了一条面向 AI agent 的桥接调用路径。

它的职责很聚焦：

- 接收本地 HTTP bridge 发来的保存命令
- 用你真实的 Chrome 会话打开目标页面
- 走 Zotero Connector 原本的保存流程
- 把保存结果以结构化方式返回给调用方

它是 [ZotPilot](https://github.com/xunhe730/ZotPilot) 的浏览器侧配套组件。真正的 MCP server、本地索引、Zotero API 写操作和 `save_from_url()` 工具都在 ZotPilot 主仓库里。

一句话概括：

```text
Agent -> ZotPilot MCP tool -> 本地 bridge -> Chrome 扩展 -> Zotero Desktop
```

---

## 为什么要做这个

标准的 Zotero Connector 默认假设“有人在浏览器里点一下插件按钮”。

但 AI agent 做不到这一点：

- 它不能可靠地操作浏览器工具栏 UI
- 它又需要真实浏览器里的 cookie、机构登录状态和 translator 行为
- 它还需要一个机器可读的完成信号，而不是浏览器弹窗

ZotPilot Connector 的目标，就是在不重写 Zotero Connector 保存逻辑的前提下，把原本只能“人工点击”的能力，变成 agent 可以调用的本地接口。

相对上游 Zotero Connector，它额外提供了：

- **Agent 触发路径**：通过 `/enqueue` 发命令，不需要人手点按钮
- **真实浏览器上下文**：复用你当前 Chrome 会话和页面状态
- **诚实的完成状态**：超时返回 `"unconfirmed"`，而不是误报成功
- **快速失败**：扩展没连上时，bridge 立即返回 `extension_not_connected`

---

## 它和 ZotPilot 是什么关系

这个仓库不是完整产品，只是 ZotPilot 系统中的一个组件。

| 组件 | 仓库 | 职责 |
|------|------|------|
| MCP server、搜索、索引、Zotero API 写操作 | `ZotPilot` | 面向 agent 的工具和编排层 |
| 浏览器执行层 | `zotpilot-connector` | 打开页面、跑 translator、触发保存 |
| 本地 bridge | `ZotPilot` | 在 `127.0.0.1:2619` 上排队命令、收集结果 |

如果你只想要 Zotero 语义搜索，这个仓库本身不够。

如果你想让 `save_from_url()` 通过真实浏览器会话去保存网页或论文，这个仓库就是必需的浏览器侧组件。

---

## 快速开始

### 1. 构建扩展

```bash
git clone https://github.com/xunhe730/zotpilot-connector.git
cd zotpilot-connector
npm install
./build.sh -d
```

构建完成后，MV3 的解压目录在 `build/manifestv3`。

### 2. 加载到 Chrome

1. 打开 `chrome://extensions/`
2. 开启**开发者模式**
3. 点击**加载已解压的扩展程序**
4. 选择 `build/manifestv3`

### 3. 启动本地 bridge

bridge 由 ZotPilot 主项目提供，不在本仓库里：

```bash
zotpilot bridge
```

或者：

```bash
python -m zotpilot.cli bridge
```

### 4. 验证连接状态

```bash
curl http://127.0.0.1:2619/status
```

预期返回大致如下：

```json
{
  "bridge": "running",
  "port": 2619,
  "extension_connected": true,
  "zotero_running": true
}
```

### 5. 发起一次保存

直接调用 bridge：

```bash
curl -X POST http://127.0.0.1:2619/enqueue \
  -H "Content-Type: application/json" \
  -d '{"action":"save","url":"https://arxiv.org/abs/2401.00001"}'
```

通过 ZotPilot 的 MCP 工具调用：

```python
save_from_url(
    url="https://arxiv.org/abs/2401.00001",
    collection_key="MYCOLL",
    tags=["ml"]
)
```

---

## 一次保存实际会发生什么

1. 客户端向 bridge 提交一个保存命令。
2. 扩展轮询 `/pending`，拿到待执行任务。
3. 扩展在后台打开目标页面。
4. 等待页面就绪和 translator 检测完成。
5. 调用与人工点击完全相同的保存入口。
6. 通过 Connector 内部消息观察保存是否完成。
7. 把结构化结果 POST 回 bridge。

这里最重要的设计点是：

这个扩展没有为 agent 单独再造一套保存流水线，而是直接复用 Zotero Connector 现有的保存逻辑，只是在外围补了自动触发和结果回报。

所以它天然继承了这些能力：

- Zotero translators
- 各类 publisher 页面兼容性
- 机构登录和 cookie
- `saveAsWebpage` 回退路径
- 与 Zotero Desktop 的本地连接能力 `127.0.0.1:23119`

---

## 工作原理

```text
┌─────────────┐    HTTP/JSON     ┌──────────────┐   轮询 / 心跳   ┌──────────────────┐
│ ZotPilot    │◄──────────────►  │ BridgeServer │◄───────────────►│ ZotPilot         │
│ MCP server  │                  │ localhost    │                  │ Connector        │
│ / 其他客户端 │                  │ :2619        │                  │ Chrome 扩展      │
└─────────────┘                  └──────────────┘                  └────────┬─────────┘
                                                                            │
                                                                            ▼
                                                                   ┌──────────────────┐
                                                                   │ Zotero Desktop   │
                                                                   │ localhost:23119  │
                                                                   └──────────────────┘
```

这套设计里的几个关键取舍：

- **用本地 HTTP bridge，不用 native messaging**：任何本地 HTTP 客户端都能接入
- **轮询同时承担 MV3 保活**：扩展的 poll loop 顺带让 service worker 持续存活
- **通过观察消息判断完成**：不是猜测保存成功，而是监听 Connector 内部完成信号
- **条目关联是 best-effort**：ZotPilot 后续可能根据标题或 item key 做 collection/tag 路由

完整协议见 [PROTOCOL.md](PROTOCOL.md)。

---

## API

bridge 在 `http://127.0.0.1:2619` 上暴露一组很小的 HTTP API。

| 端点 | 方法 | 作用 |
|------|------|------|
| `/pending` | `GET` | 扩展获取下一条待执行命令 |
| `/enqueue` | `POST` | 客户端提交保存命令 |
| `/result` | `POST` | 扩展提交保存结果 |
| `/result/<request_id>` | `GET` | 客户端轮询执行结果 |
| `/heartbeat` | `POST` | 扩展上报在线状态和 Zotero 状态 |
| `/status` | `GET` | 查询 bridge 和连接健康状态 |

命令示例：

```json
{
  "action": "save",
  "url": "https://www.nature.com/articles/s41586-024-00001-x",
  "collection_key": "MYCOLL1",
  "tags": ["climate", "2024"]
}
```

典型成功结果：

```json
{
  "request_id": "abc123def456",
  "success": true,
  "url": "https://arxiv.org/abs/2401.00001",
  "title": "A Neural Network for Everything",
  "item_key": null,
  "_detected_via": "sendMessage"
}
```

超时结果：

```json
{
  "request_id": "abc123def456",
  "success": "unconfirmed",
  "error_code": "completion_unconfirmed",
  "error_message": "Save was triggered but no completion signal arrived within timeout."
}
```

---

## 和上游 Zotero Connector 的区别

| 能力 | Zotero Connector | ZotPilot Connector |
|------|------------------|-------------------|
| 从浏览器 UI 触发保存 | 是 | 是 |
| 从 agent 或 HTTP 客户端触发保存 | 否 | 是 |
| 在真实 Chrome 会话里运行 | 是 | 是 |
| 显式暴露 bridge 健康状态 | 否 | 是 |
| 提供机器可读的完成结果 | 有限 | 是 |
| 超时明确返回 `"unconfirmed"` | 否 | 是 |

---

## 开发

### 构建命令

```bash
# 调试构建
./build.sh -d

# 生产构建
./build.sh -p

# 监听并自动重建
npx gulp watch
```

### 测试

```bash
npm test
```

### 代码结构

- 上游 Connector 代码仍然是主体实现
- ZotPilot 相关浏览器集成主要集中在 `src/browserExt/agentAPI.js`
- MV3 入口是 `src/browserExt/background-worker.js`
- 后台初始化逻辑在 `src/browserExt/background.js`

---

## 故障排除

### `extension_not_connected`

bridge 最近没有收到扩展的 heartbeat。

检查：

- Chrome 是否正在运行
- 解压扩展是否已经加载
- 加载的是否是 `build/manifestv3`

### `zotero_running: false`

扩展无法访问 Zotero Desktop 的 `http://127.0.0.1:23119/connector/ping`。

检查：

- Zotero Desktop 是否已打开
- Zotero Connector 集成是否正常

### 保存结果是 `"unconfirmed"`

表示扩展已经触发保存，但在超时窗口内没有观察到完成信号。

这不一定意味着保存失败。请直接检查 Zotero。

### collection 或 tags 没有应用

collection/tag 路由不是由扩展直接完成，而是 ZotPilot 在保存后补做的。

检查：

- ZotPilot 是否配置了 `ZOTERO_API_KEY` 和 `ZOTERO_USER_ID`
- 保存后的条目是否能被唯一识别出来

---

## 常见问题

### 这个仓库能单独使用吗？

严格说不太适合。你可以直接用 `curl` 调 bridge，但它的设计目标是作为 ZotPilot 的浏览器执行层。

### 为什么不直接让 Python 去调用 Zotero？

因为很多网页保存依赖真实浏览器状态，比如 cookie、跳转链、动态页面和 translator 执行。

### 为什么用轮询？

因为这是一个本地 MV3 扩展集成场景，轮询简单、稳定，而且能顺带让 background service worker 保活。

### 它会替代 Zotero Connector 吗？

不会。它本质上还是 Zotero Connector，只是加了一条可以被 agent 触发的桥接路径。

---

## 环境要求

- 已加载 ZotPilot Connector 的 Chrome
- 正在运行的 Zotero Desktop
- 运行在 `2619` 端口上的 ZotPilot bridge
- 如果你需要保存后自动补 collection/tag，需要在 ZotPilot 中配置 `ZOTERO_API_KEY` 和 `ZOTERO_USER_ID`

---

## License

AGPL-3.0，与上游 Zotero Connector 代码库保持一致。
