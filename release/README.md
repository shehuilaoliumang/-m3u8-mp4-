# 发布目录说明（过渡）

`release/` 目录当前仅作为兼容镜像保留，避免旧流程失效。

## 你现在应该优先使用

- 主发布目录：`artifacts/release/`
- 主文档入口：`README.md`
- 版本说明目录：`docs/releases/`

## 本目录会包含

- `m3u8ToMp4.exe`
- `README.md`（来自项目主 README）
- 最新的 `RELEASE_v*.short.md`
- `BUILD_INFO.txt`

## 兼容策略

- 打包脚本 `scripts/build_exe.ps1` 会先输出到 `artifacts/release/`
- 随后同步镜像到 `release/`
- 后续建议逐步把外部分发/自动化流程切换到 `artifacts/release/`

---

如需查看完整使用说明，请直接打开项目根目录的 `README.md`。
