# Security

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it via GitHub Issues.

## Security Model

- **Zotero database**: Read-only access (`?mode=ro&immutable=1`)
- **API keys**: Stored in config file or environment variables, never logged
- **ChromaDB**: Local persistent storage, no network access
- **Write operations**: Require explicit Zotero Web API credentials
- **MCP transport**: stdio only (no HTTP endpoints)

## Secrets

Never commit API keys. Use environment variables or `~/.config/zotpilot/config.json`.

Required secrets vary by feature:
- `GEMINI_API_KEY` — for Gemini embeddings
- `ANTHROPIC_API_KEY` — for vision table extraction (optional)
- `ZOTERO_API_KEY` + `ZOTERO_USER_ID` — for write operations (optional)
