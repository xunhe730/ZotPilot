# Security

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it via GitHub Issues.

## Security Model

- **Zotero database**: Read-only access (`?mode=ro&immutable=1`)
- **ChromaDB**: Local persistent storage, no network access
- **Write operations**: Require explicit Zotero Web API credentials
- **MCP transport**: stdio only (no HTTP endpoints)

## Secrets

Never commit API keys. Required secrets vary by feature:
- `GEMINI_API_KEY` — for Gemini embeddings
- `DASHSCOPE_API_KEY` — for DashScope embeddings (alternative)
- `ANTHROPIC_API_KEY` — for vision table extraction (optional)
- `ZOTERO_API_KEY` + `ZOTERO_USER_ID` — for write operations (optional)

### Where keys are stored

| Location | How it gets there | Exposure risk |
|----------|-------------------|---------------|
| Environment variables | User sets manually | Low — not persisted to disk |
| MCP client config (JSON) | `register` writes `env` section | Medium — plaintext in `~/.claude/settings.local.json`, Cursor `settings.json`, etc. |
| Shell history | `register --gemini-key <key>` | Medium — `~/.bash_history` / `~/.zsh_history` |
| `~/.config/zotpilot/config.json` | `setup` writes embedding config | Low — only `embedding_provider`, no API keys |

### Recommendations

1. **Prefer environment variables** over CLI flags where possible — set `GEMINI_API_KEY`, `ZOTERO_API_KEY`, `ZOTERO_USER_ID` in your shell profile, then run `register` without key flags. The MCP server reads env vars at startup.
2. **If using CLI flags**: be aware they appear in shell history. Run `history -d <n>` (bash) or edit `~/.zsh_history` after.
3. **MCP config files** store keys as plaintext `env` entries. This is by design (MCP clients inject them at server startup). Ensure these files have appropriate permissions (`chmod 600`).
4. **Rotate keys** if you suspect exposure. Zotero API keys can be revoked at [zotero.org/settings/keys](https://www.zotero.org/settings/keys). Gemini keys at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
