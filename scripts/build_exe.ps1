$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (Test-Path ".venv\Scripts\python.exe") {
    $python = ".venv\Scripts\python.exe"
} else {
    $python = "python"
}

& $python -m pip install pyinstaller
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name m3u8ToMp4 `
    --add-data "README.md;." `
    main.py
Write-Host "打包完成，输出目录：dist\\m3u8ToMp4.exe"

