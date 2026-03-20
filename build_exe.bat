 @echo off
setlocal
chcp 65001 >nul

set "ROOT_DIR=%~dp0"
set "PS1_PATH=%ROOT_DIR%scripts\build_exe.ps1"

if /I "%~1"=="--self-test" (
    if not exist "%PS1_PATH%" (
        echo [错误] 未找到脚本: %PS1_PATH%
        exit /b 2
    )
    where powershell >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未找到 powershell.exe
        exit /b 3
    )
    echo [OK] 自检通过，双击可执行打包。
    exit /b 0
)

if not exist "%PS1_PATH%" (
    echo [错误] 未找到脚本: %PS1_PATH%
    echo 请确认仓库结构未变更，且 scripts\build_exe.ps1 存在。
    pause
    exit /b 2
)

echo [信息] 正在启动打包流程，请稍候...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1_PATH%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [失败] 打包未完成，退出码: %EXIT_CODE%
    echo 常见原因:
    echo 1^) Python/venv 不可用
    echo 2^) 网络原因导致依赖安装失败
    echo 3^) 杀毒软件拦截或目录权限不足
    echo.
    echo 你也可以在终端手动运行: powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
    pause
    exit /b %EXIT_CODE%
)

echo.
echo [完成] 打包成功，输出文件: dist\m3u8ToMp4.exe
echo [完成] 发布产物目录: release\
pause
exit /b 0

