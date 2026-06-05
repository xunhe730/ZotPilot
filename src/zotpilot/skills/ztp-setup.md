---
name: ztp-setup
description: >
  Use for setting up, updating, or repairing ZotPilot.
  Trigger on: "安装ZotPilot", "配置嵌入模型", "注册MCP", "ZotPilot无法启动", "升级ZotPilot",
  "install zotpilot", "setup zotpilot", "configure embedding provider",
  "register MCP", "zotpilot not found", "zotpilot doctor", "update zotpilot", "zotpilot upgrade",
  or when the user is setting up for the first time, after an upgrade, or when commands are broken.
  Covers: setup → vendor/model selection → API key config → MCP/skill registration → initial index → health check, plus upgrade.
---
# Setup Workflow

## Steps
1. Check installation: `python scripts/run.py status --json` or `uv run zotpilot status`
2. If not installed: `pip install zotpilot` or `uv tool install zotpilot`
3. If installed but needing updates (Upgrade workflow): run `zotpilot upgrade`
4. **Embedding vendor & model selection** — never hardcode the vendor or model list; the
   catalog is the single source of truth and changes between versions. Drive it from the CLI:

   **Step 4a — Discover the catalog.** Run `zotpilot setup --list-vendors --json` and parse the
   output. Assert `schema_version == 1` before reading `.vendors`; if it differs, fall back to the
   human-readable `zotpilot setup --list-vendors` and ask the user. Each vendor entry has
   `key`, `label`, `provider`, `base_url`, `requires_key`, `key_url`, `aliases`, and
   `models[{model, dimensions, note, recommended}]`. This mirrors the wizard's two layers: Layer 1
   is the vendor, Layer 2 is the model.

   **Step 4b — Pick the vendor (Layer 1).** Map the user's intent to a vendor `key` (e.g. a Chinese
   user wanting a cheap multilingual cloud endpoint → `siliconflow`; offline/private → `local`;
   a self-hosted endpoint → `ollama` or `custom`). If the intent is ambiguous, show the labels and ask.

   **Step 4c — Pick the model (Layer 2).** Use the vendor's `recommended` model unless the user
   expressed a preference; then pick the matching `models[].model`. For a vendor with
   `allow_custom_model` (or the free-form `custom` vendor), the user may supply a model not in the
   list — but then you MUST also pass `--embedding-dimensions` (it cannot be guessed).

   **Step 4d — Collect the key.** If the chosen vendor has `requires_key: true`, get a key from the
   user (point them at `key_url`). Keyless vendors (e.g. `local`, `ollama`) need none.

5. **API Key Setup**: Prefer interactive `zotpilot setup` on shared machines. API keys are stored in
   `~/.config/zotpilot/config.json`; do not paste or commit that file.
6. **Configure (non-interactive).** Run, substituting the vendor/model you chose above:
   ```
   zotpilot setup --non-interactive --provider <vendor> [--embedding-model <model>] \
     [--embedding-key <key>] --verify
   ```
   - Omit `--embedding-model` to take the vendor's recommended model. For a fixed-base vendor
     (e.g. SiliconFlow/Zhipu/Ollama) the `base_url` and dimensions come from the catalog
     automatically; for the `custom` vendor pass `--embedding-base-url` and `--embedding-dimensions`.
   - `--verify` makes the setup run a one-call self-check and print a single JSON line.
7. **Act on the `--verify` result.** Parse the JSON `verify` field — DO NOT rely on the exit code
   alone (only `dim_mismatch` exits non-zero; `auth`/`unreachable`/`error`/`skipped` all exit 0):
   - `ok` — done; proceed to registration/index.
   - `dim_mismatch` — the JSON names the server's real dimension; re-run step 6 adding
     `--embedding-dimensions <that value>` (this is the self-heal path).
   - `auth` — the key is missing/expired; ask the user for a valid key and re-run.
   - `unreachable` — the endpoint isn't reachable; have the user start the server (e.g. `ollama serve`)
     or fix the base_url, then re-run.
   - `error` — show the `message` to the user and re-run after fixing it.
   - `skipped` — the provider isn't wire-probeable (gemini/dashscope/local); this is expected, treat
     as success.
8. MCP registration and skill deployment are included in `zotpilot setup`. Advanced repair only:
   `zotpilot install` (alias: `zotpilot register`).
9. Initial Index: `zotpilot index --limit 20` (first-time quick index)
10. Verify health: `zotpilot doctor`

## Troubleshooting
- If Zotero is not natively detected at standard paths during setup, instruct the user to explicitly
  define it via the flag: `--zotero-dir /path/to/zotero/data`
- `zotpilot setup --list-vendors` works even before ZotPilot is configured and on machines with no
  Zotero install — it short-circuits before any detection, so it is always safe for discovery.
