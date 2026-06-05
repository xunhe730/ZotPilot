#!/usr/bin/env bash
# HOME-sandboxed end-to-end smoke for the two-layer vendor->model setup path.
#
# Drives the NEW `--provider <vendor>` CLI through setup --verify -> doctor (and,
# with --full, a tiny index + query). Runs in an isolated $HOME so the user's
# REAL ~/.config/zotpilot config and Chroma index are NEVER touched.
#
# Usage:
#   scripts/e2e_setup_smoke.sh --provider siliconflow --embedding-model BAAI/bge-m3 --embedding-key <key>
#   scripts/e2e_setup_smoke.sh --provider ollama            # keyless; verify likely "unreachable" if no server
#   scripts/e2e_setup_smoke.sh --provider gemini --full     # gemini verify is "skipped" by design
#
# The embedding key may also come from $ZOTPILOT_EMBEDDING_API_KEY in the env.
set -euo pipefail

PROVIDER=""
MODEL=""
KEY="${ZOTPILOT_EMBEDDING_API_KEY:-}"
BASE_URL=""
FULL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --provider)            PROVIDER="$2"; shift 2 ;;
    --embedding-model)     MODEL="$2"; shift 2 ;;
    --embedding-key)       KEY="$2"; shift 2 ;;
    --embedding-base-url)  BASE_URL="$2"; shift 2 ;;
    --full)                FULL=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PROVIDER" ]]; then
  echo "ERROR: --provider <vendor> is required (see: zotpilot setup --list-vendors)" >&2
  exit 2
fi

# Run from the repo root so `uv run` resolves the worktree's editable package.
cd "$(dirname "$0")/.."

# Isolated sandbox HOME — the real config/index are off-limits.
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/ztp-e2e.XXXXXX")"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT
export HOME="$SANDBOX"
export XDG_CONFIG_HOME="$SANDBOX/.config"
export XDG_DATA_HOME="$SANDBOX/.local/share"
echo "Sandbox HOME: $SANDBOX"

# Minimal fake Zotero data dir (setup only checks that zotero.sqlite exists).
ZOTERO_DIR="$SANDBOX/zotero"
mkdir -p "$ZOTERO_DIR"
: > "$ZOTERO_DIR/zotero.sqlite"

echo "== zotpilot setup --list-vendors (discovery) =="
uv run zotpilot setup --list-vendors

echo "== zotpilot setup --non-interactive --provider $PROVIDER --verify =="
SETUP_ARGS=(setup --non-interactive --provider "$PROVIDER" --zotero-dir "$ZOTERO_DIR" --verify)
[[ -n "$MODEL" ]]    && SETUP_ARGS+=(--embedding-model "$MODEL")
[[ -n "$KEY" ]]      && SETUP_ARGS+=(--embedding-key "$KEY")
[[ -n "$BASE_URL" ]] && SETUP_ARGS+=(--embedding-base-url "$BASE_URL")
# setup --verify exits non-zero only on a confirmed dim_mismatch; tolerate the
# headless auth/unreachable cases so the smoke completes and prints the verdict.
uv run zotpilot "${SETUP_ARGS[@]}" || echo "(setup --verify returned non-zero — inspect the JSON above)"

echo "== zotpilot doctor =="
uv run zotpilot doctor || echo "(doctor reported issues — expected without a real key/library)"

if [[ "$FULL" -eq 1 ]]; then
  echo "== zotpilot index --limit 1 (best-effort) =="
  uv run zotpilot index --limit 1 || echo "(index skipped/failed — expected with an empty fake library)"
fi

echo "Smoke complete. Real ~/.config/zotpilot was NOT touched (sandbox: $SANDBOX)."
