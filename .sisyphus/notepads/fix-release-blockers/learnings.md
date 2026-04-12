# Fix Release Blockers - Notepad

## Plan Overview
- 10 tasks, all independent, all can run in parallel
- Current state: 1 test failure (test_profile_library_with_items), 535 passed
- workflow/ directory exists with batch.py, batch_store.py (T2 target)

## Conventions Discovered
- Tests use `uv run pytest`
- Lint uses `uv run ruff check src tests`
- Coverage fail-under: 29%
- Python 3.13, pytest 9.0.2

## Task Assignments
- T1: Test fix — doc_count assertion failure (assert 0 == 2)
- T2: Delete orphaned workflow/ package
- T3: Add concurrency lock to index_library
- T4: Fix 9 bare except in connector.py
- T5: Deduplicate _parse_json_string_list
- T6: Update/fix incident fixture
- T7: Config.save() should not persist API keys
- T8: MCP config file permissions (0600)
- T9: OpenAlex rate limiter
- T10: LOW cleanup (dead code, comments, naming)

## T7: Config.save() API Key Exclusion — COMPLETED
- `Config.save()` at `config.py:182-192` was writing all 5 API keys to disk
- Fixed by commenting out: `gemini_api_key`, `dashscope_api_key`, `anthropic_api_key`, `zotero_api_key`, `semantic_scholar_api_key`
- Added SECURITY comment explaining the rationale
- Renamed test `test_save_persists_api_keys` → `test_save_does_not_persist_api_keys` with inverted assertions
- All 13 config tests pass
- `Config.load()` backward compat preserved: it still reads keys from file if present (env vars still take priority)
## T8: MCP Config File Permissions (0600)
- Applied `os.chmod(path, 0o600)` after config writes in `_platforms.py:954-958` and `cli.py:196-198`
- Guarded with `if sys.platform != "win32":` to avoid affecting Windows
- `os` and `sys` already imported in `_platforms.py`; added `import os` to `cli.py` (ruff auto-fixed import order)
- Pre-existing test failures unrelated to this change: `test_save_persists_api_keys` (T7), workflow import tests, profile tests
