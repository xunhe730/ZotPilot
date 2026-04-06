---
name: zotpilot
description: Route ZotPilot requests to the right workflow skill
---

# ZotPilot

Choose one workflow and stay inside it:

- `ztp-setup`: install, configure, register MCP, verify first index readiness
- `ztp-research`: discover external papers, ingest them, index them, then organize results
- `ztp-review`: synthesize what the local Zotero library already says about a topic
- `ztp-profile`: analyze library structure, themes, tags, and organization opportunities

Routing rules:

- Setup / install / register / update requests go to `ztp-setup`
- "Find papers", "survey recent work", "collect papers", and ingest requests go to `ztp-research`
- "Review my library", "summarize what I already have", and local-first synthesis go to `ztp-review`
- "Profile my library", "organize tags/collections", and researcher-pattern analysis go to `ztp-profile`

Tool profile defaults:

- `ztp-setup`: `extended`
- `ztp-research`: `research`
- `ztp-review`: `extended`
- `ztp-profile`: `extended`
