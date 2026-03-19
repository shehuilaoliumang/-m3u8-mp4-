#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

"$PYTHON_BIN" -m pip install --upgrade pyinstaller
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --windowed --name m3u8ToMp4 --add-data "README.md:." main.py

APP_PATH="dist/m3u8ToMp4.app"
DMG_PATH="dist/m3u8ToMp4.dmg"

if [[ ! -d "$APP_PATH" ]]; then
  echo "未找到 $APP_PATH，打包失败。"
  exit 1
fi

hdiutil create -volname "m3u8ToMp4" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"
echo "DMG 打包完成：$DMG_PATH"

