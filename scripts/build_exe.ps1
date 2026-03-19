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
$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--exclude-module", "tests",
    "--exclude-module", "unittest",
    "--exclude-module", "pip",
    "--exclude-module", "setuptools",
    "--exclude-module", "wheel",
    "--name", "m3u8ToMp4",
    "--add-data", "README.md;.",
    "main.py"
)

$upxDir = Join-Path $projectRoot "tools\upx"
if (Test-Path $upxDir) {
    $pyiArgs += @("--upx-dir", $upxDir)
}

& $python -m PyInstaller @pyiArgs
Write-Host "打包完成，输出目录：dist\\m3u8ToMp4.exe"

