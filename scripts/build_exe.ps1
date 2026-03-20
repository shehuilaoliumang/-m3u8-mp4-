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

    $artifactsRoot = Join-Path $projectRoot "artifacts"
    if (-not (Test-Path $artifactsRoot)) {
        New-Item -ItemType Directory -Path $artifactsRoot | Out-Null
    }

    $releaseDir = Join-Path $artifactsRoot "release"
    if (Test-Path $releaseDir) {
        Remove-Item -Path $releaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $releaseDir | Out-Null

    Copy-Item -Path $distExe -Destination (Join-Path $releaseDir "m3u8ToMp4.exe") -Force

    $readmePath = Join-Path $projectRoot "README.md"
    if (Test-Path $readmePath) {
        Copy-Item -Path $readmePath -Destination (Join-Path $releaseDir "README.md") -Force
    }

    $releaseNotesDir = Join-Path $projectRoot "docs\releases"
    if (-not (Test-Path $releaseNotesDir)) {
        $releaseNotesDir = $projectRoot
    }
    $latestReleaseNote = Get-ChildItem -Path $releaseNotesDir -Filter "RELEASE_v*.short.md" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latestReleaseNote) {
        Copy-Item -Path $latestReleaseNote.FullName -Destination (Join-Path $releaseDir $latestReleaseNote.Name) -Force
    }

    $buildInfo = @(
        "build_time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "python=$python",
        "exe=artifacts\\release\\m3u8ToMp4.exe"
    )
    Set-Content -Path (Join-Path $releaseDir "BUILD_INFO.txt") -Value $buildInfo -Encoding UTF8

    # 兼容旧目录：继续输出一份镜像，避免外部脚本立刻失效。
    $legacyReleaseDir = Join-Path $projectRoot "release"
    if (Test-Path $legacyReleaseDir) {
        Remove-Item -Path $legacyReleaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $legacyReleaseDir | Out-Null
    Copy-Item -Path (Join-Path $releaseDir "*") -Destination $legacyReleaseDir -Recurse -Force

    Write-Host "[完成] 打包成功：$distExe"
    Write-Host "[完成] 主发布目录：$releaseDir"
    Write-Host "[完成] 兼容镜像目录：$legacyReleaseDir"
    exit 0
}
catch {
    Write-Host "[错误] 打包失败：$($_.Exception.Message)"
    exit 1
}

