# ZotPilot Troubleshooting

## Quick Fix Table

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `zotpilot: command not found` | CLI not installed | `python3 scripts/run.py status` (auto-installs) |
| `uv: command not found` | uv not installed | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| status shows `config_exists: false` | Never ran setup | `zotpilot setup --non-interactive` |
| status shows `zotero_dir_valid: false` | Wrong Zotero path | `zotpilot setup --non-interactive --zotero-dir /correct/path` |
| `GEMINI_API_KEY not set` | Missing API key | `zotpilot config set gemini_api_key <key>` or rerun `zotpilot setup` |
| `DASHSCOPE_API_KEY not set` | Missing API key | Set env var: `export DASHSCOPE_API_KEY=...` Get key at https://bailian.console.aliyun.com/ |
| `index_ready: false, doc_count: 0` | Never indexed | `zotpilot index` (full) or `zotpilot index --limit 10` (test) |
| Search returns empty results | Query too specific or index empty | Check `get_index_stats`. Try broader query or `search_boolean` |
| `ZOTERO_API_KEY not set` | Write operation without credentials | `zotpilot config set zotero_api_key <key>` then `zotpilot register` |
| `Document has no DOI` | Paper missing DOI in Zotero | Cannot use citation tools — add DOI in Zotero first |
| MCP tools not available | MCP not registered | See "MCP Registration by Platform" below |
| Indexing fails on specific PDFs | Corrupted or scanned PDF | Try `--no-vision` flag, or use `--item-key` to skip |
| ChromaDB lock error | Another process using the index | Stop other zotpilot processes, wait 60s, retry |
| Not sure what's wrong | Unknown | Run `zotpilot doctor` for detailed diagnostics |

## Embedding Provider Issues

**Gemini (default):**
- Free tier: ~1,000 requests/day (after Dec 2025 reduction)
- If rate limited: wait 60s or switch to dashscope/local
- Key from: https://aistudio.google.com/apikey

**DashScope / Bailian (Alibaba Cloud):**
- Recommended for users in China (no VPN needed)
- Model: text-embedding-v4 (1024 dimensions)
- Pricing: ¥0.0005/1k tokens — very affordable
- Key from: https://bailian.console.aliyun.com/
- Setup: `zotpilot setup --non-interactive --provider dashscope`

**Local (all-MiniLM-L6-v2):**
- No API key needed, fully offline
- Lower quality than Gemini/DashScope but works everywhere
- First run downloads the model (~80MB)

## Indexing Issues

**Indexing is slow:**
- Normal: ~2-5 seconds per paper (PDF extraction + embedding)
- Use `--limit N` to index in batches
- Use `-v` for verbose logging to see progress

**Indexing crashes:**
- Try `--no-vision` to disable vision-based extraction
- Index specific papers with `--item-key KEY`
- Check disk space (ChromaDB needs ~1MB per 100 papers)

**Re-indexing:**
- Force re-index all: `zotpilot index --force`
- Re-index by title pattern: `zotpilot index --title "transformer"`

## MCP Registration by Platform

### Claude Code

```bash
# Without env vars (local embeddings)
claude mcp add -s user zotpilot -- zotpilot

# Current model
claude mcp add -s user zotpilot -- zotpilot mcp serve

# Verify
claude mcp list
```

If tools still not showing, restart Claude Code or run `/mcp`.

### OpenCode

Edit `~/.config/opencode/opencode.json`:

```json
{
  "experimental": {
    "mcp_timeout": 600000
  },
  "mcp": {
    "zotpilot": {
      "type": "local",
      "command": ["zotpilot", "mcp", "serve"]
    }
  }
}
```

> **`experimental.mcp_timeout`（重要）：** OpenCode 的 per-server `timeout` 只控制工具发现（`listTools`），不控制 tool call 执行。ZotPilot 的索引和批量操作可能超过默认 30 秒超时，导致 `MCP Error -32001: Request Timeout`。设 `600000`（10 分钟）可避免此问题。

### Unsupported clients

OpenClaw, Gemini CLI, Cursor, and Windsurf are not first-class supported
targets in the current runtime reconciler. Do not follow older guidance that
suggests `zotpilot register` will manage them.

## Enable Write Operations

Tag and collection management requires Zotero Web API credentials:

1. Go to https://www.zotero.org/settings/keys
2. Create a new key with "Allow library access" and "Allow write access"
3. Find your User ID (number on the same page, not your username)
4. Re-register MCP with additional env vars:

**Claude Code:**
```bash
claude mcp remove zotpilot
zotpilot config set zotero_user_id <id>
zotpilot config set zotero_api_key <key>
zotpilot register
```

**OpenCode / OpenClaw:** Re-run `zotpilot register` after configuring credentials in ZotPilot; do not manually embed secrets into the client config.

5. Restart your AI agent.

## Complete Reset

If nothing works, start fresh:

```bash
# Remove config
rm -rf ~/.config/zotpilot/

# Remove index data
rm -rf ~/.local/share/zotpilot/

# Reinstall CLI
uv tool uninstall zotpilot
python3 scripts/run.py status    # auto-reinstalls

# Re-setup
zotpilot setup
zotpilot index --limit 10
```
