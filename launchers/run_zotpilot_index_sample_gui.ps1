param(
    [string]$Keys = $env:ZOTPILOT_INDEX_KEYS,
    [string]$Pythonw = $(if ($env:ZOTPILOT_PYTHONW) { $env:ZOTPILOT_PYTHONW } else { "pythonw.exe" }),
    [int]$MaxPages = 0
)

$ErrorActionPreference = "Stop"
$Script = Join-Path $PSScriptRoot "zotpilot_collection_index_gui.py"
if (-not $Keys) {
    throw "Pass -Keys or set ZOTPILOT_INDEX_KEYS to the task JSON path."
}

& $Pythonw $Script --keys $Keys --limit 2 --max-pages $MaxPages --auto-start
