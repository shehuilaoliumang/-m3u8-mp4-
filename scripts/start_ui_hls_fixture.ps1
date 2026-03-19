param(
    [int]$Port = 8000,
    [switch]$ForceRebuild,
    [switch]$NoServe,
    [switch]$NoClipboard,
    [switch]$OpenUI,
    [switch]$OpenHelp,
    [switch]$UrlOnly
)

$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
    return Split-Path -Parent $PSScriptRoot
}

function Get-PythonCommand {
    $projectRoot = Get-ProjectRoot
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }
    return "python"
}

function Get-FreePort {
    param([int]$StartPort)

    for ($p = $StartPort; $p -lt ($StartPort + 200); $p++) {
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
            $listener.Start()
            $listener.Stop()
            return $p
        }
        catch {
            if ($listener) {
                try { $listener.Stop() } catch {}
            }
            continue
        }
    }
    throw "No free port found in range: $StartPort-$($StartPort + 199)."
}

function New-EncryptedHlsSample {
    param(
        [string]$RootPath,
        [switch]$ForceRebuild
    )

    $python = Get-PythonCommand
    $scriptFile = Join-Path $RootPath "tmp_build_hls_fixture.py"

    $script = @'
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

root = Path("tmp_encrypted_verify")
if root.exists() and os.environ.get("FORCE_REBUILD") == "1":
    shutil.rmtree(root)

enc_dir = root / "enc_hls"
out_dir = root / "out"
enc_dir.mkdir(parents=True, exist_ok=True)
out_dir.mkdir(parents=True, exist_ok=True)

src_mp4 = root / "source.mp4"
if not src_mp4.exists():
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
            "-t", "6",
            "-pix_fmt", "yuv420p",
            str(src_mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

key_file = enc_dir / "enc.key"
keyinfo = enc_dir / "enc.keyinfo"
playlist = enc_dir / "index.m3u8"

key_file.write_bytes(os.urandom(16))
iv = "0123456789ABCDEF0123456789ABCDEF"
keyinfo.write_text(f"enc.key\n{key_file.resolve()}\n{iv}\n", encoding="utf-8")

subprocess.run(
    [
        "ffmpeg", "-y",
        "-i", str(src_mp4),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-hls_time", "2",
        "-hls_list_size", "0",
        "-hls_key_info_file", str(keyinfo),
        str(playlist),
    ],
    check=True,
    capture_output=True,
    text=True,
)

print(str(root.resolve()))
'@

    Set-Content -Path $scriptFile -Value $script -Encoding UTF8
    try {
        if ($ForceRebuild) {
            $env:FORCE_REBUILD = "1"
        }
        else {
            $env:FORCE_REBUILD = "0"
        }
        $sampleRoot = & $python -u $scriptFile
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to generate encrypted HLS fixture."
        }
        return ($sampleRoot | Select-Object -Last 1).Trim()
    }
    finally {
        Remove-Item $scriptFile -Force -ErrorAction SilentlyContinue
        Remove-Item Env:FORCE_REBUILD -ErrorAction SilentlyContinue
    }
}

function Test-HlsEndpoints {
    param(
        [string]$BaseUrl
    )

    $playlistUrl = "$BaseUrl/index.m3u8"
    $keyUrl = "$BaseUrl/enc.key"

    $playlistResp = Invoke-WebRequest -Uri $playlistUrl -UseBasicParsing -TimeoutSec 5
    $playlistContent = $playlistResp.Content
    if ($playlistContent -is [byte[]]) {
        $playlistContent = [System.Text.Encoding]::UTF8.GetString($playlistContent)
    }
    elseif ($playlistContent -is [System.Array]) {
        $playlistContent = [System.Text.Encoding]::UTF8.GetString([byte[]]$playlistContent)
    }

    if ($playlistResp.StatusCode -ne 200 -or (-not [string]$playlistContent) -or ([string]$playlistContent -notmatch "#EXTM3U")) {
        throw "Invalid m3u8 response: $playlistUrl"
    }

    $keyResp = Invoke-WebRequest -Uri $keyUrl -UseBasicParsing -TimeoutSec 5
    if ($keyResp.StatusCode -ne 200) {
        throw "Invalid key response: $keyUrl"
    }
}

$projectRoot = Get-ProjectRoot
Set-Location $projectRoot

if ($NoServe -and $UrlOnly) {
    throw "NoServe and UrlOnly cannot be used together."
}

if ($UrlOnly -and ($OpenUI -or $OpenHelp)) {
    Write-Host "UrlOnly mode ignores OpenUI/OpenHelp and only returns a live URL."
    $OpenUI = $false
    $OpenHelp = $false
}

Write-Host "[1/4] Generating encrypted m3u8 fixture..."
$sampleRoot = New-EncryptedHlsSample -RootPath $projectRoot -ForceRebuild:$ForceRebuild
$hlsDir = Join-Path $sampleRoot "enc_hls"
if (-not (Test-Path (Join-Path $hlsDir "index.m3u8"))) {
    throw "Fixture file not found: $hlsDir\index.m3u8"
}

$actualPort = Get-FreePort -StartPort $Port
$baseUrl = "http://127.0.0.1:$actualPort"
$playlistUrl = "$baseUrl/enc_hls/index.m3u8"
$localPlaylist = Join-Path $hlsDir "index.m3u8"

Write-Host "[2/4] Fixture directory: $hlsDir"

if ($NoServe) {
    Write-Host "[3/4] NoServe mode: server not started, HTTP URL is unavailable."
    Write-Host "[4/4] Local playlist path: $localPlaylist"
    if ($OpenUI) {
        Write-Host "OpenUI was requested but NoServe is enabled, so UI will not be auto-opened."
    }
    if ($OpenHelp) {
        Write-Host "OpenHelp was requested but NoServe is enabled, so help window will not be opened."
    }
    exit 0
}

if ($OpenHelp -and -not $OpenUI) {
    Write-Host "OpenHelp requires OpenUI. Enabling OpenUI automatically..."
    $OpenUI = $true
}

Write-Host "Starting local HTTP server (Ctrl+C to stop)..."
Write-Host "Paste into app input source: $playlistUrl"

$python = Get-PythonCommand
Push-Location $sampleRoot
try {
    $uiProcess = $null
    $keepServerAlive = $false
    $process = Start-Process -FilePath $python -ArgumentList @("-m", "http.server", "$actualPort") -PassThru
    Start-Sleep -Milliseconds 600
    Test-HlsEndpoints -BaseUrl "$baseUrl/enc_hls"
    Write-Host "Server is ready."
    Write-Host "PID: $($process.Id)"
    Write-Host "[3/4] Test URL: $playlistUrl"
    if (-not $NoClipboard) {
        try {
            Set-Clipboard -Value $playlistUrl
            Write-Host "[4/4] Copied URL to clipboard."
        }
        catch {
            Write-Host "[4/4] Clipboard copy failed; please copy URL manually."
        }
    }
    Write-Host "When this script exits, the server process will be stopped automatically."

    if ($UrlOnly) {
        $keepServerAlive = $true
        Write-Host "UrlOnly mode: script will exit and keep server running in background."
        Write-Host "Use this to stop server later: Stop-Process -Id $($process.Id)"
        return
    }

    if ($OpenUI) {
        Pop-Location
        try {
            $uiEnv = @{}
            if ($OpenHelp) {
                $uiEnv["M3U8_OPEN_HELP_ON_START"] = "1"
            }
            $uiProcess = Start-Process -FilePath $python -ArgumentList @("main.py") -PassThru -WorkingDirectory $projectRoot -Environment $uiEnv
            Write-Host "UI started. PID: $($uiProcess.Id)"
            Write-Host "Close the UI window to finish one-click self-test."
            if ($OpenHelp) {
                Write-Host "OpenHelp enabled: help window will open automatically after app startup."
            }
        }
        finally {
            Push-Location $sampleRoot
        }
    }

    try {
        while (-not $process.HasExited) {
            if ($uiProcess -ne $null -and $uiProcess.HasExited) {
                Write-Host "UI exited, stopping local HTTP server..."
                break
            }
            Start-Sleep -Seconds 1
        }
    }
    finally {
        if (-not $keepServerAlive -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
finally {
    Pop-Location
}

