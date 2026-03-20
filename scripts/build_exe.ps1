$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

if (Test-Path ".venv\Scripts\python.exe") {
    $python = ".venv\Scripts\python.exe"
} else {
    $python = "python"
}

try {
    Write-Host "[信息] 安装/检查打包依赖..."
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "pip 安装 pyinstaller 失败，退出码: $LASTEXITCODE"
    }

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

    Write-Host "[信息] 开始执行 PyInstaller..."
    & $python -m PyInstaller @pyiArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，退出码: $LASTEXITCODE"
    }

    $distExe = Join-Path $projectRoot "dist\m3u8ToMp4.exe"
    if (-not (Test-Path $distExe)) {
        throw "未找到输出文件: $distExe"
    }

    $releaseDir = Join-Path $projectRoot "release"
    if (Test-Path $releaseDir) {
        Remove-Item -Path $releaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $releaseDir | Out-Null

    Copy-Item -Path $distExe -Destination (Join-Path $releaseDir "m3u8ToMp4.exe") -Force

    $readmePath = Join-Path $projectRoot "README.md"
    if (Test-Path $readmePath) {
        Copy-Item -Path $readmePath -Destination (Join-Path $releaseDir "README.md") -Force
    }

    $latestReleaseNote = Get-ChildItem -Path $projectRoot -Filter "RELEASE_v*.short.md" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latestReleaseNote) {
        Copy-Item -Path $latestReleaseNote.FullName -Destination (Join-Path $releaseDir $latestReleaseNote.Name) -Force
    }

    $buildInfo = @(
        "build_time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "python=$python",
        "exe=release\\m3u8ToMp4.exe"
    )
    Set-Content -Path (Join-Path $releaseDir "BUILD_INFO.txt") -Value $buildInfo -Encoding UTF8

    Write-Host "[完成] 打包成功：$distExe"
    Write-Host "[完成] 发布目录已整理：$releaseDir"
    exit 0
}
catch {
    Write-Host "[错误] 打包失败：$($_.Exception.Message)"
    exit 1
}

