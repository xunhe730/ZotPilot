param(
    [string]$Keys = $env:ZOTPILOT_INDEX_KEYS,
    [string]$Pythonw = $(if ($env:ZOTPILOT_PYTHONW) { $env:ZOTPILOT_PYTHONW } else { "pythonw.exe" })
)

$ErrorActionPreference = "Stop"
$Script = Join-Path $PSScriptRoot "zotpilot_index_monitor_gui.py"
if (-not $Keys) {
    throw "Pass -Keys or set ZOTPILOT_INDEX_KEYS to the task JSON path."
}

& $Pythonw $Script --keys $Keys
