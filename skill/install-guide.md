---
name: zotpilot-install
description: Guided installation of ZotPilot MCP server for Claude Code, OpenCode, and OpenClaw
---

# ZotPilot Installation Guide

## Prerequisites

Check these before starting:
1. **Python ≥ 3.10**: `python3 --version`
2. **uv** (recommended): `uv --version` — install from https://docs.astral.sh/uv/
3. **Zotero** installed with a library containing PDFs

## Quick Install

```bash
# Clone the repo
git clone https://github.com/xdzhuang/ZotPilot.git
cd ZotPilot

# Install as a uv tool (isolated environment)
uv tool install .

# Run interactive setup
zotpilot setup
```

The setup wizard will:
1. Auto-detect your Zotero data directory
2. Choose embedding provider (Gemini recommended, or local/offline)
3. Configure API key if using Gemini
4. Migrate settings from deep-zotero if present
5. Write config to `~/.config/zotpilot/config.json`

## Client Configuration

### Claude Code

Add to `~/.claude/settings.json`:

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

### OpenCode

Add to your OpenCode MCP configuration:

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

### OpenClaw

Add to your OpenClaw configuration:

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

## Index Your Library

After installation, index your Zotero library:

```bash
# Index all papers (first run takes a while)
zotpilot index

# Or index with options
zotpilot index --limit 10          # Index only 10 papers (test run)
zotpilot index --force             # Re-index everything
zotpilot index --verbose           # Debug logging
zotpilot index --no-vision         # Skip vision-based table extraction
```

## Verify Installation

```bash
# Check config and index status
zotpilot status

# Run MCP server directly (for testing)
python -m zotpilot
```

## Optional Dependencies

```bash
# Vision-based table extraction (requires Anthropic API key)
uv tool install ".[vision]"

# PaddleOCR for scanned PDFs
uv tool install ".[paddle]"

# Zotero write operations (tagging, collections via Web API)
uv tool install ".[write]"

# Everything
uv tool install ".[all]"
```

## Migrating from deep-zotero

If you have an existing deep-zotero installation:
- `zotpilot setup` will detect and offer to migrate your config
- Your existing ChromaDB index is compatible — just point to it
- No re-indexing needed if you keep the same embedding provider
