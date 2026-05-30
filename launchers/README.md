# ZotPilot indexing GUI launchers

These optional Windows launchers run a Tkinter progress window for long ZotPilot semantic indexing jobs.

The GUI shows:

- total / completed / remaining paper counts
- indexed / already indexed / skipped / failed counts
- current paper title and elapsed time
- per-paper status table
- live bilingual log output
- JSONL progress log path for pause/resume

## Required task file

Pass a JSON task list with `--keys` or set:

```powershell
$env:ZOTPILOT_INDEX_KEYS = "C:\path\to\zotpilot_collection_keys.json"
```

Each item should contain at least:

```json
{
  "index": 1,
  "key": "ZOTEROITEMKEY",
  "title": "Paper title",
  "pdf_count": 1
}
```

Optional fields displayed in the table are `collection_path`, `publication`, `date`, and `type`.

## Index root

By default the GUI reads ZotPilot's configured `chroma_db_path` and uses its parent directory as the index root.
You can override it with:

```powershell
$env:ZOTPILOT_INDEX_ROOT = "E:\ZoteroData\ZotPilot"
```

Progress logs are written to `<index-root>\logs\zotpilot_index_progress_*.jsonl`.

## Start commands

Run a two-paper smoke test:

```powershell
.\run_zotpilot_index_sample_gui.ps1 -Keys C:\path\to\tasks.json
```

Run all papers in the task file:

```powershell
.\run_zotpilot_index_full_56_gui.ps1 -Keys C:\path\to\tasks.json
```

Run a full-library task file:

```powershell
.\run_zotpilot_index_full_library_gui.ps1 -Keys C:\path\to\full_library_tasks.json
```

Resume from the newest JSONL progress log under the index root:

```powershell
.\run_zotpilot_index_resume_latest_gui.ps1 -Keys C:\path\to\tasks.json -IndexRoot E:\ZoteroData\ZotPilot
```

Open a read-only monitor for an existing JSONL progress log:

```powershell
.\run_zotpilot_index_monitor_gui.ps1 -Keys C:\path\to\tasks.json
```

## Environment overrides

- `ZOTPILOT_INDEX_KEYS`: default task JSON path.
- `ZOTPILOT_INDEX_ROOT`: default index root.
- `ZOTPILOT_EXE`: path to `zotpilot.exe` used by the executor GUI.
- `ZOTPILOT_PYTHONW`: path to `pythonw.exe` used by the PowerShell launchers.
