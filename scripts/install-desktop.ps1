# Install or update the AIRelays desktop app on Windows (x64) from the GitHub
# release installer.
#
#   irm https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.ps1 | iex
#
# Runs the release's NSIS setup silently as the current user (no admin), after
# verifying it against the SHA-256 digest GitHub publishes. The app embeds its
# own Python and relay; nothing else needs to be installed.
#
# Environment:
#   $env:AIRELAYS_VERSION = "0.14.1"  install that release instead of the newest one
#   $env:AIRELAYS_NO_LAUNCH = "1"     do not start the app after installing
#   $env:GITHUB_TOKEN = "..."         optional; authenticates GitHub API lookups

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell downloads crawl with a progress bar.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = 'lpalbou/AIRelays'
    $api = "https://api.github.com/repos/$repo"
    $headers = @{ 'User-Agent' = 'airelays-installer' }
    $apiHeaders = $headers.Clone()
    if ($env:GITHUB_TOKEN) { $apiHeaders['Authorization'] = "Bearer $env:GITHUB_TOKEN" }

    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
        throw "The AIRelays desktop app is only built for x64 Windows. Install the relay with: py -m pip install airelays"
    }

    if ($env:AIRELAYS_VERSION) {
        $tag = 'v' + $env:AIRELAYS_VERSION.TrimStart('v')
        $releases = @(Invoke-RestMethod -Headers $apiHeaders "$api/releases/tags/$tag")
    } else {
        # Desktop installers are attached a few minutes after a release is
        # created, so fall back to the newest release that has one.
        $releases = @(Invoke-RestMethod -Headers $apiHeaders "$api/releases?per_page=10")
    }
    $asset = $releases | ForEach-Object { $_.assets } |
        Where-Object { $_.name -like '*_x64-setup.exe' } | Select-Object -First 1
    if (-not $asset) { throw "No Windows desktop installer found in the recent AIRelays releases." }

    $setup = Join-Path ([IO.Path]::GetTempPath()) $asset.name
    Write-Host "==> Downloading $($asset.name)"
    Invoke-WebRequest -Headers $headers -UseBasicParsing -Uri $asset.browser_download_url -OutFile $setup

    if ($asset.digest -and $asset.digest.StartsWith('sha256:')) {
        $expected = $asset.digest.Substring(7)
        $actual = (Get-FileHash -Algorithm SHA256 $setup).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            Remove-Item $setup -Force
            throw "Checksum mismatch for $($asset.name): expected $expected, got $actual"
        }
        Write-Host "==> Checksum verified (sha256 $actual)"
    } else {
        Write-Host "==> GitHub published no checksum for this asset; skipping verification."
    }

    # The relay runs in the app's Job Object, so stopping the app stops it too.
    Get-Process -Name 'airelays-desktop' -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host "==> Installing AIRelays"
    $process = Start-Process -FilePath $setup -ArgumentList '/S' -Wait -PassThru
    Remove-Item $setup -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw "The AIRelays installer exited with code $($process.ExitCode)." }

    $exe = Join-Path $env:LOCALAPPDATA 'AIRelays\airelays-desktop.exe'
    if (Test-Path $exe) {
        Write-Host "==> Installed $exe"
        if (-not $env:AIRELAYS_NO_LAUNCH) { Start-Process -FilePath $exe }
    } else {
        Write-Host "==> Installed AIRelays; start it from the Start menu."
    }
    Write-Host "==> Done. The app starts the relay (default http://127.0.0.1:8317) and shares ~/.config/airelays with the CLI."
}
