param(
    [string]$Keys = $env:ZOTPILOT_INDEX_KEYS,
    [string]$IndexRoot = $env:ZOTPILOT_INDEX_ROOT,
    [string]$Pythonw = $(if ($env:ZOTPILOT_PYTHONW) { $env:ZOTPILOT_PYTHONW } else { "pythonw.exe" }),
    [int]$MaxPages = 0
)

$ErrorActionPreference = "Stop"
$Script = Join-Path $PSScriptRoot "zotpilot_collection_index_gui.py"
if (-not $Keys) {
    throw "Pass -Keys or set ZOTPILOT_INDEX_KEYS to the task JSON path."
}
if (-not $IndexRoot) {
    $ConfigLine = & zotpilot config get chroma_db_path
    $ChromaPath = ($ConfigLine -replace "^chroma_db_path:\s*", "") -replace "\s+\[[^\]]+\]\s*$", ""
    $IndexRoot = Split-Path -Parent $ChromaPath
}
$LogDir = Join-Path $IndexRoot "logs"
$LatestLog = Get-ChildItem -LiteralPath $LogDir -Filter "zotpilot_index_progress_*.jsonl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if ($LatestLog) {
    & $Pythonw $Script --keys $Keys --index-root $IndexRoot --limit 0 --max-pages $MaxPages --auto-start --resume-log $LatestLog.FullName
} else {
    & $Pythonw $Script --keys $Keys --index-root $IndexRoot --limit 0 --max-pages $MaxPages --auto-start
}
