# 发布目录说明（过渡）

`release/` 目录当前仅作为兼容镜像保留，避免旧流程失效。

## 你现在应该优先使用
- Linux：通过 apt / dnf / yum 自动安装
- 主发布目录：`artifacts/release/`
- 主文档入口：`README.md`
- 版本说明目录：`docs/releases/`
- 安装完成后自动检测路径
## 本目录会包含
- 适用于非标准安装位置
- `m3u8ToMp4.exe`
- `README.md`（来自项目主 README）
- 最新的 `RELEASE_v*.short.md`
- `BUILD_INFO.txt`

## 兼容策略

- 打包脚本 `scripts/build_exe.ps1` 会先输出到 `artifacts/release/`
- 随后同步镜像到 `release/`
- 后续建议逐步把外部分发/自动化流程切换到 `artifacts/release/`

### 仅获取可访问 URL（后台保活服务）

如需查看完整使用说明，请直接打开项目根目录的 `README.md`。

---

## 协作与反馈

- 提交 Bug：使用模板 `.github/ISSUE_TEMPLATE/bug_report.yml`
- 提功能建议：使用模板 `.github/ISSUE_TEMPLATE/feature_request.yml`
- 发起 PR：自动载入 `.github/pull_request_template.md`
- 参与贡献流程：参考 `CONTRIBUTING.md`

---

**版本** - 1.3  
**最后更新** - 2026年3月

