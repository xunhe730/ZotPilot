# Supported MCP Clients

## Claude Code

**Config file:** `~/.claude/settings.json`

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"]
    }
  }
}
```

**Skill installation:** Copy the `skill/` directory to `~/.claude/skills/zotpilot/` for guided workflows.

## OpenCode

**Config:** Add to your OpenCode MCP server configuration:

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"]
    }
  }
}
```

## OpenClaw

**Config:** Add to your OpenClaw configuration:

```json
{
  "mcpServers": {
    "zotpilot": {
      "command": "uv",
      "args": ["tool", "run", "zotpilot"]
    }
  }
}
```

## Generic MCP Client

ZotPilot uses stdio transport. Any MCP client that supports stdio can connect:

```bash
# The server reads from stdin and writes to stdout
uv tool run zotpilot
```

## Environment Variables

Set these before starting the MCP server:

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | For Gemini embeddings | Google Gemini API key |
| `OPENALEX_EMAIL` | Optional | Email for OpenAlex polite pool (10 req/s vs 1 req/s) |
| `ANTHROPIC_API_KEY` | Optional | For vision-based table extraction |
| `ZOTERO_API_KEY` | Optional | For write operations (tags, collections) |
| `ZOTERO_USER_ID` | Optional | Zotero user ID for write operations |
