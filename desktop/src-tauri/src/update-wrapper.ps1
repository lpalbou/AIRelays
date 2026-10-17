# Runs an AIRelays desktop update. Written and started by the app
# (src/updates.rs), which passes everything through AIRELAYS_* variables so
# no path or version is ever spliced into this script.
#
# Waits for the app to exit, runs the release installer pinned to the
# approved version and SHA-256, checks that the installed app now carries
# that version, then reopens AIRelays whether the update succeeded or not.
# A failure is recorded for the app to report.

$log = $env:AIRELAYS_UPDATE_LOG
$utf8 = New-Object System.Text.UTF8Encoding $false
# Logging must never decide the outcome: another program holding the log
# open only costs log lines.
function Write-Log([string]$text) {
    try { [IO.File]::AppendAllText($log, "$text`r`n", $utf8) } catch { }
}
function Write-Failure([string]$reason) {
    Write-Log "Update failed: $reason"
    # UTF-8 explicitly: Windows PowerShell 5.1 writes Set-Content in the
    # ANSI code page, which the app cannot read back.
    [IO.File]::WriteAllText($env:AIRELAYS_UPDATE_FAILED, $reason, $utf8)
}

try { [IO.File]::WriteAllText($log, "", $utf8) } catch { }
Write-Log "AIRelays update to $env:AIRELAYS_VERSION started $(Get-Date -Format o)"
# Tells the app the updater is running, so it may quit.
[IO.File]::WriteAllText($env:AIRELAYS_UPDATE_READY, "$PID", $utf8)

$appPid = [int]$env:AIRELAYS_UPDATE_PID
try { Wait-Process -Id $appPid -Timeout 90 -ErrorAction Stop } catch { }
if (Get-Process -Id $appPid -ErrorAction SilentlyContinue) {
    Write-Failure "AIRelays did not close in time, so the update was cancelled."
    exit 1
}

$app = $env:AIRELAYS_UPDATE_RELAUNCH
$version = $env:AIRELAYS_VERSION
# This wrapper reopens the app itself, after success or failure.
$env:AIRELAYS_NO_LAUNCH = '1'
try {
    & $env:AIRELAYS_UPDATE_INSTALLER *>&1 | ForEach-Object { Write-Log "$_" }
    # The installed file is the only reliable verdict: an installer that
    # stopped early or did not replace the app leaves the old version.
    $installed = (Get-Item -LiteralPath $app -ErrorAction Stop).VersionInfo.ProductVersion
    if ($installed -ne $version) {
        throw "the installed app reports version $installed instead of $version."
    }
    Write-Log "Update finished: AIRelays $installed."
} catch {
    Write-Failure "$_"
}

# The reopened app must not inherit this update's settings.
Get-ChildItem Env: | Where-Object { $_.Name -like 'AIRELAYS_UPDATE_*' } |
    ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
Remove-Item Env:AIRELAYS_NO_LAUNCH, Env:AIRELAYS_SHA256, Env:AIRELAYS_VERSION -ErrorAction SilentlyContinue
try {
    Start-Process -FilePath $app -ArgumentList '--after-update'
} catch {
    Write-Log "Could not reopen AIRelays: $_"
}
