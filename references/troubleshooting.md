# ZotPilot Troubleshooting

## Quick Fix Table

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `zotpilot: command not found` | CLI not installed | `python3 scripts/run.py status` (auto-installs) |
| `uv: command not found` | uv not installed | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| status shows `config_exists: false` | Never ran setup | `zotpilot setup --non-interactive` |
| status shows `zotero_dir_valid: false` | Wrong Zotero path | `zotpilot setup --non-interactive --zotero-dir /correct/path` |
| `GEMINI_API_KEY not set` | Missing API key | Set env var: `export GEMINI_API_KEY=...` Get key at https://aistudio.google.com/apikey |
| `index_ready: false, doc_count: 0` | Never indexed | `zotpilot index` (full) or `zotpilot index --limit 10` (test) |
| Search returns empty results | Query too specific or index empty | Check `get_index_stats`. Try broader query or `search_boolean` |
| `ZOTERO_API_KEY not set` | Write operation without credentials | See "Enable Write Operations" below |
| `Document has no DOI` | Paper missing DOI in Zotero | Cannot use citation tools — add DOI in Zotero first |
| MCP tools not available | MCP not registered | See "MCP Registration by Platform" below |
| Indexing fails on specific PDFs | Corrupted or scanned PDF | Try `--no-vision` flag, or use `--item-key` to skip |
| ChromaDB lock error | Another process using the index | Stop other zotpilot processes, wait 60s, retry |

## Embedding Provider Issues

**Gemini (default):**
- Free tier: 1,500 requests/day — enough for ~500 papers
- If rate limited: wait 60s or switch to local: `zotpilot setup --non-interactive --provider local`
- Key from: https://aistudio.google.com/apikey

**Local (all-MiniLM-L6-v2):**
- No API key needed, fully offline
- Lower quality than Gemini but works everywhere
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

# With Gemini API key
claude mcp add -s user -e GEMINI_API_KEY=<key> zotpilot -- zotpilot

# Verify
claude mcp list
```

If tools still not showing, restart Claude Code or run `/mcp`.

### OpenCode

```bash
opencode mcp add
# Follow prompts: name=zotpilot, command=zotpilot, transport=stdio
```

### OpenClaw

1. Install MCP bridge plugin:
   ```bash
   openclaw plugins install @aiwerk/openclaw-mcp-bridge
   ```
2. Edit `~/.openclaw/openclaw.json`:
   ```json
   {
     "plugins": {
       "entries": {
         "openclaw-mcp-bridge": {
           "config": {
             "servers": {
               "zotpilot": {
                 "transport": "stdio",
                 "command": "zotpilot",
                 "args": [],
                 "env": { "GEMINI_API_KEY": "${GEMINI_API_KEY}" }
               }
             }
           }
         }
       }
     }
   }
   ```
3. Restart: `openclaw gateway restart`

## Enable Write Operations

Tag and collection management requires Zotero Web API credentials:

1. Go to https://www.zotero.org/settings/keys
2. Create a new key with "Allow library access" and "Allow write access"
3. Find your User ID (number on the same page, not your username)
4. Re-register MCP with additional env vars:

**Claude Code:**
```bash
claude mcp remove zotpilot
claude mcp add -s user -e GEMINI_API_KEY=<key> -e ZOTERO_API_KEY=<key> -e ZOTERO_USER_ID=<id> zotpilot -- zotpilot
```

**OpenCode / OpenClaw:** Add the env vars to the existing MCP config.

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
