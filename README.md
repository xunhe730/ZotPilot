<div align="center">
  <h2>🧭 ZotPilot</h2>
  <img src="assets/banner.jpg" alt="ZotPilot" width="100%">

  <p>
    <a href="https://www.zotero.org/">
      <img src="https://img.shields.io/badge/Zotero-CC2936?style=for-the-badge&logo=zotero&logoColor=white" alt="Zotero">
    </a>
    <a href="https://claude.ai/code">
      <img src="https://img.shields.io/badge/Claude_Code-6849C3?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude Code">
    </a>
    <a href="https://github.com/openai/codex">
      <img src="https://img.shields.io/badge/Codex-74AA9C?style=for-the-badge&logo=openai&logoColor=white" alt="Codex">
    </a>
    <a href="https://modelcontextprotocol.io/">
      <img src="https://img.shields.io/badge/MCP-0175C2?style=for-the-badge&logoColor=white" alt="MCP">
    </a>
    <a href="https://pypi.org/project/zotpilot/">
      <img src="https://img.shields.io/pypi/v/zotpilot?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI">
    </a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows">
  </p>

  <p><b>让 AI 读懂你的 Zotero 文献库，论文数据不离家。</b></p>

  <p>
    <a href="#快速开始">快速开始</a> &bull;
    <a href="#能做什么">能做什么</a> &bull;
    <a href="#使用模式与示例">使用模式与示例</a> &bull;
    <a href="#工作原理">工作原理</a> &bull;
    <a href="#更新">更新</a> &bull;
    <a href="#faq">FAQ</a> &bull;
    <a href="README_EN.md">English</a>
  </p>
</div>

---

## 快速开始

```bash
pip install zotpilot
zotpilot setup                 # 交互式配置 + 自动部署 skills + 注册 MCP
# 重启你的 AI 客户端
```

然后跟 agent 说「搜我库里关于 X 的论文」或「帮我调研 Y 方向」。它会按流程把 18 个 MCP 工具和 4 个 packaged skill 串起来完成任务。

**前置**：[Zotero 8](https://www.zotero.org/download/)（已安装并至少启动过一次）· Python 3.10+ · 支持的 AI Agent 客户端（Claude Code / Codex / OpenCode）。入库工作流还需要 [Connector 浏览器扩展](#安装详情)。

ZotPilot 会把 Codex packaged skills 部署到 `~/.agents/skills`。旧的 `$CODEX_HOME/skills`（默认通常是 `~/.codex/skills`）只是 Codex 兼容路径；如果你的 Codex 桌面环境显示该路径，它不是 ZotPilot 的部署目标。

---

## 能做什么

ZotPilot 由三部分组成：

| 组件 | 作用 |
|------|------|
| **MCP Server** | 18 个原子工具：语义搜索、引用图谱、入库、标签 / 集合 / 笔记管理 |
| **Connector** | Chrome 扩展。agent 通过你的浏览器会话保存论文，保留机构订阅 PDF |
| **Agent Skills** | 把工具串成完整研究流程，不只是单次调用 |

### 4 个 skill 覆盖研究流程

| Skill | 做什么 |
|-------|--------|
| `ztp-research` | 本地库 + OpenAlex 检索 → 候选确认 → Connector 入库 → 自动打标签 / 分集合 → 逐篇报告 |
| `ztp-review` | 基于库内论文做综述、聚类、比较、初稿整理 |
| `ztp-profile` | 分析文献库主题分布、期刊层次、时间跨度、标签使用 |
| `ztp-setup` | 指导 agent 调用 `zotpilot setup` / `upgrade` / `doctor` 做安装、更新、排错。它不是 CLI 命令本身 |

### 5 个核心能力

| 能力 | 不一样在哪 |
|------|--------|
| **语义搜索** | 按意思搜，不只关键词匹配。结果精确到章节段落 |
| **一步入库** | DOI / arXiv / URL 混合输入 → Connector 浏览器保存 → 验证 → 必要时回退 |
| **引用图谱** | OpenAlex 查引用链，并在引用论文里搜特定观点 |
| **批量整理** | 语义匹配 → 打标签、分集合、写笔记，同步回 Zotero |
| **学术检索** | OpenAlex 全参数检索，可直接喂给入库流程 |

---

## 和其他方案的区别

| | 语义搜索 | 章节定位 | 入库 + 整理 | 引用图谱 | 安装 |
|------|:-:|:-:|:-:|:-:|--------|
| Zotero 原生 | ✗ | ✗ | ✗ | ✗ | — |
| 把 PDF 喂给 AI | ✓ | ✗ | ✗ | ✗ | 手动 |
| 自己搭 RAG | ✓ | 看实现 | ✗ | ✗ | 数小时 |
| [zotero-mcp](https://github.com/54yyyu/zotero-mcp) | ✓ | ✗ | 部分 | ✗ | ~5 min |
| **ZotPilot** | ✓ | ✓ | ✓（Connector） | ✓ | ~5 min |

不一样的地方主要在于：入库走真实浏览器会话和 Zotero translator，机构订阅的 PDF 也能一起拿到；引用数据来自 OpenAlex；排序和重排细节放在下面的工作原理里。

---

## 安装详情

<details>
<summary><b>嵌入模型 provider 选择</b></summary>

| Provider | 体验 | 离线 | 获取 API Key |
|----------|------|:---:|-------------|
| Gemini | 高质量默认 | ✗ | [Google AI Studio](https://aistudio.google.com/apikey) |
| DashScope | 适合中国网络环境 | ✗ | [阿里云百炼](https://bailian.console.aliyun.com/) |
| Local | 基本够用 | ✓ | 不需要 |

> 选定后不建议换。向量维度不同，换模型要 `zotpilot index --force` 重建。
> 选 `local` 只把 ZotPilot 切到本地嵌入模式；本地模型在首次实际调用 embeddings 时才下载，不在 `setup` 阶段预下载。

非交互式（agent 驱动）：

```bash
zotpilot setup --non-interactive --provider gemini   # 或 dashscope / local
```

</details>

<details>
<summary><b>API Key 与环境变量</b></summary>

配置模型分两层：

- `zotpilot setup` 写共享本地配置到 macOS / Linux 的 `~/.config/zotpilot/config.json`，或 Windows 的 `%APPDATA%\zotpilot\config.json`，并自动部署 skills / 注册 MCP
- `zotpilot config set` 管理共享配置；API key 也会写入同一个 `config.json`
- `zotpilot upgrade` 升级 CLI，并刷新 packaged skills / 同步 MCP runtime
- API key 不写入 Claude / Codex / OpenCode 的客户端配置

环境变量仍可作为临时 override，优先级高于 `config.json`：

```bash
export GEMINI_API_KEY=<your-key>           # 或 DASHSCOPE_API_KEY
export ANTHROPIC_API_KEY=<your-key>        # 可选：复杂表格视觉提取
```

`config.json` 可能包含 API key。不要提交、公开粘贴或同步到不可信位置；共享机器上优先用交互式 `zotpilot setup` 输入密钥，避免把 key 留在 shell history。

推荐顺序：

```bash
zotpilot setup                         # 交互式：会询问 embedding key 以及 Zotero User ID / API key（可跳过）
# 或
zotpilot setup --non-interactive --provider gemini
```

之后如果需要修改配置：

```bash
zotpilot config set gemini_api_key <key>
zotpilot config set zotero_user_id <id>
zotpilot config set zotero_api_key <key>
zotpilot setup
```

可选：`openalex_email` 不是密钥，只是 OpenAlex 联系邮箱。配置后 OpenAlex 相关搜索 / 引文查询能走 polite pool（约 10 req/s；未配置通常约 1 req/s）：

```bash
zotpilot config set openalex_email you@example.com
```

</details>

<details>
<summary><b>Connector 浏览器扩展</b></summary>

入库工作流默认只说明 Chrome：

1. 打开 [最新 Release](https://github.com/xunhe730/ZotPilot/releases/latest)，下载 `zotpilot-connector-v*.zip` 并解压
2. Chrome 地址栏打开 `chrome://extensions/`
3. 打开右上角**开发者模式**
4. 点击**加载已解压的扩展程序**
5. 选择包含 `manifest.json` 的目录
6. 工具栏出现 Zotero 图标即安装成功

> ZotPilot Connector 是官方 Zotero Connector 的 fork。两者可共存：官方扩展处理手动保存，ZotPilot Connector 处理 agent 调用。

Connector 升级：

1. 重新下载最新 Release zip
2. 打开 `chrome://extensions/`
3. 在已加载的 ZotPilot Connector 上点刷新

</details>

<details>
<summary><b>启用写操作（标签 / 集合 / 笔记）</b></summary>

搜索和引用无需额外凭据。写操作需要 Zotero Web API 密钥：

1. 打开 [zotero.org/settings/keys](https://www.zotero.org/settings/keys)
2. 记下页面顶部的**数字 User ID**
3. 创建 private key，勾选 "Allow library access" + "Allow write access"

```bash
zotpilot config set zotero_user_id 12345678
zotpilot config set zotero_api_key YOUR_KEY
zotpilot setup
zotpilot doctor
```

迁移旧的客户端内嵌 secret：

```bash
zotpilot config migrate-secrets
```

</details>

<details>
<summary><b>验证安装</b></summary>

```bash
zotpilot doctor     # 诊断配置 / 环境 / MCP 注册
zotpilot status     # 索引状态
```

MCP 工具或 Skill 没出现？重新运行 `zotpilot setup` 并重启 agent。高级用户如果只想刷新 agent 集成，可用 `zotpilot install`。

</details>

---

## 使用模式与示例

### 直接自然语言交互

适合简单、单步、目标清晰的任务，直接对 agent 说即可：

- “搜我库里关于 X 的论文”
- “哪些论文的 Results 里提到 Y？”
- “谁引用了这篇？”
- “索引了多少论文？”

这类请求通常会直接调用单个或少量 MCP 工具完成。

常见例子：

| 你说 | agent 做 |
|------|---------|
| 「搜我的论文，关于 X」 | 语义搜索已索引论文 |
| 「哪些论文的 Results 里提到 Y？」 | 按章节 + 关键词定位段落 |
| 「找比较模型准确率的表格」 | 搜 PDF 提取的表格内容 |
| 「谁引用了这篇？怎么评价？」 | OpenAlex 查引用 → 搜观点段落 |
| 「索引了多少论文？」 | 索引状态检查 |

### `ztp-*` workflow

适合多阶段、容易跑偏、需要 agent 按顺序推进的任务，建议显式触发 workflow：

- `ztp-research`
  - “/ztp-research 帮我调研 X 方向最近的重要论文，并把值得收的入到 Zotero 里”
- `ztp-review`
  - “/ztp-review 基于我库里的论文整理一版关于 X 的综述框架”
- `ztp-profile`
  - “/ztp-profile 看看我这个库主要在研究什么，再决定怎么整理”
- `ztp-setup`
  - “/ztp-setup 检查 ZotPilot 配置”

这类任务更适合让 agent 显式进入 skill 工作流，因为它们通常涉及搜索、筛选、入库、整理、汇报等多个阶段。
---

## 工作原理

```text
索引（跑一次）
Zotero SQLite ──→ PDF 提取 ──→ 分块 + 章节分类 ──→ 向量嵌入 ──→ ChromaDB

查询（每次）
Agent ──→ MCP 工具 ───┬── 语义搜索 ──→ ChromaDB ──→ 章节感知重排序
                      ├── 引用图谱 ──→ OpenAlex
                      ├── 文献浏览 ──→ Zotero SQLite（只读）
                      ├── 写操作   ──→ Zotero Web API ──→ 同步回 Zotero
                      └── 入库     ──→ Bridge + Connector ──→ Zotero Desktop
```

- **索引**：SQLite 以 `mode=ro&immutable=1` 只读打开；PyMuPDF 提取 PDF 全文、表格、图表；按学术章节分块；嵌入存入 ChromaDB。增量索引会跳过已完成项目。
- **搜索**：查询向量化 → ChromaDB 余弦相似度 → 章节感知重排序 + 期刊质量加权。
- **入库**：Agent → 本地 bridge (127.0.0.1:2619) → Chrome Connector → Zotero Desktop。
- **写操作**：标签 / 集合 / 笔记通过 Zotero Web API（Pyzotero），自动同步回客户端。

<details>
<summary><b>MCP 工具列表（18 个）</b></summary>

| 类别 | 工具 |
|------|------|
| 搜索 | `search_papers`、`search_topic`、`search_boolean`、`advanced_search` |
| 阅读 | `get_passage_context`、`get_paper_details`、`get_notes`、`get_annotations`、`browse_library`、`profile_library` |
| 发现 | `search_academic_databases` |
| 入库 | `ingest_by_identifiers` |
| 整理 | `manage_tags`、`manage_collections`、`create_note` |
| 引用 | `get_citations` |
| 索引 | `index_library`、`get_index_stats` |

`search_papers` 支持 `section_type` 参数搜表格 / 图表。`ingest_by_identifiers` 接受 DOI / arXiv ID / URL 混合输入。

</details>

<details>
<summary><b>文件结构 & 数据位置</b></summary>

```text
PyPI 安装的 zotpilot（wheel 内含 skills + references）
├── src/zotpilot/skills/
├── references/
└── connector/

# 配置 / 索引位置
# macOS / Linux
~/.config/zotpilot/config.json
~/.local/share/zotpilot/chroma/

# Windows
%APPDATA%\zotpilot\config.json
%APPDATA%\zotpilot\chroma\
```

</details>

---

## 更新

```bash
zotpilot upgrade
```

升级当前 ZotPilot CLI、刷新 packaged skill 文件、同步 MCP runtime 配置。

<details>
<summary><b>常用更新命令</b></summary>

| 命令 / 选项 | 用途 |
|------|------|
| `upgrade` 或 `update`（不带参数） | 升级 CLI + 刷新 skills + 同步 runtime |
| `--check` | 只检查版本（始终 exit 0） |
| `--dry-run` | 预览 runtime drift 和更新动作 |
| `--cli-only` | 只升级 CLI 包 |
| `--skill-only` | 只刷新 skills 和 runtime 注册 |
| `--re-register` | 即使没有 drift，也强制刷新客户端注册 |
| `--migrate-secrets` | 同步 runtime 前，迁移旧客户端内嵌 secrets |

</details>

---

## FAQ

<details>
<summary><b>会改我的 Zotero 数据库吗？</b></summary>

不会。SQLite 用 `mode=ro&immutable=1` 打开，物理上写不进去。标签 / 集合 / 笔记走 Zotero 官方 Web API，变更正常同步回客户端。

</details>

<details>
<summary><b>Zotero 开着能用吗？</b></summary>

能，只读模式不冲突。

</details>

<details>
<summary><b>支持哪些 agent？</b></summary>

Claude Code、Codex、OpenCode。这三家是我们官方支持的客户端，Skill 部署、MCP 注册、升级同步都针对它们做了适配。

</details>

<details>
<summary><b>嵌入模型花多少钱？</b></summary>

Gemini 免费额度约 1,000 请求/天，够索引几百篇；超出后 $0.15/百万 token。DashScope 新用户 100 万 token 免费。Local 模型完全离线免费。

</details>

<details>
<summary><b>索引多久？</b></summary>

每篇 2–5 秒，300 篇约 15 分钟。用 `zotpilot index --limit 10` 先试试，跑过的自动跳过。

</details>

<details>
<summary><b>扫描版 PDF / 超长文献？</b></summary>

- 扫描版自动 OCR（需装 Tesseract：macOS `brew install tesseract tesseract-lang`，Ubuntu `sudo apt install tesseract-ocr`）
- 超过 40 页默认跳过（`--max-pages` 可调），`--item-key` 可单独索引
- 可选：Claude Haiku 修复复杂表格（需 `ANTHROPIC_API_KEY`）

</details>

<details>
<summary><b>能完全离线用吗？</b></summary>

能。嵌入选 `--provider local`，不配写操作 key，全部本地跑。搜索、浏览、索引都不需要网络。

</details>

<details>
<summary><b>引用数据从哪来？</b></summary>

[OpenAlex](https://openalex.org/)。没 DOI 的论文无法查引用，但语义搜索和标签管理不受影响。

</details>

---

## 出了问题

| 症状 | 怎么办 |
|------|------|
| 找不到 Skill | `zotpilot setup` 然后重启 agent |
| `zotpilot: command not found` | 先 `pip install zotpilot` |
| MCP 工具没出来 | `zotpilot setup` 然后重启 agent |
| 搜出来是空的 | 先跑 `zotpilot index` |
| `GEMINI_API_KEY not set` | `export GEMINI_API_KEY=<key>` 或改用 `setup --provider local` |
| 不知道哪出了问题 | `zotpilot doctor` |

更多见 [troubleshooting.md](references/troubleshooting.md)。

---

<details>
<summary><b>开发 / 贡献</b></summary>

```bash
git clone https://github.com/xunhe730/ZotPilot.git
cd ZotPilot
pip install -e ".[dev]"

zotpilot setup
python -m pytest
python -m ruff check src/ tests/
```

Connector 开发：

```bash
cd connector
npm install
./build.sh -d
```

</details>

---

<div align="center">
  <code>pip install zotpilot &amp;&amp; zotpilot setup</code>
  <br><br>
  <sub>Claude Code &middot; Codex &middot; OpenCode</sub>
  <br><br>
  <a href="https://github.com/xunhe730/ZotPilot/issues">报告问题</a> &middot;
  <a href="https://github.com/xunhe730/ZotPilot/discussions">讨论</a>
  <br>
  <sub>MIT License &copy; 2026 xunhe</sub>
</div>
