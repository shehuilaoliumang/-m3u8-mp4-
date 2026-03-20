# 构建产物目录

本目录用于集中存放可分发的构建产物。

## 目录约定

- `artifacts/release/`：Windows EXE 打包产物（主发布目录）

## 说明

- 打包脚本 `scripts/build_exe.ps1` 会在这里输出最新构建。
- 根目录 `release/` 仍会保留一份兼容镜像，后续可逐步迁移外部流程到 `artifacts/release/`。

