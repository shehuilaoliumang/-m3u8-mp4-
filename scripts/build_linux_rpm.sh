#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

"$PYTHON_BIN" -m pip install --upgrade pyinstaller
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --onefile --name m3u8ToMp4 main.py

if ! command -v fpm >/dev/null 2>&1; then
  echo "未检测到 fpm，请先安装：gem install --no-document fpm"
  exit 1
fi

mkdir -p dist/pkg/usr/local/bin
cp dist/m3u8ToMp4 dist/pkg/usr/local/bin/m3u8ToMp4

fpm -s dir -t rpm -n m3u8ToMp4 -v 1.10.0 \
  --description "m3u8 转 mp4 工具" \
  -C dist/pkg usr/local/bin/m3u8ToMp4

echo "RPM 打包完成（输出在当前目录）。"

