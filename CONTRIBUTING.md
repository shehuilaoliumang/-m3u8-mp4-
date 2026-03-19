# 贡献指南

感谢你参与 `m3u8ToMp4` 项目。

## 提交 Issue

- Bug 反馈请使用 `Bug 报告` 模板
- 新功能建议请使用 `功能建议` 模板
- 提交前请先搜索是否已有同类 Issue

## 提交 Pull Request

1. Fork 并创建分支（建议命名：`feature/xxx` 或 `fix/xxx`）
2. 保持单次 PR 聚焦一个主题，便于评审
3. 提交前完成本地自测
4. 填写 PR 模板并关联对应 Issue

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 提交信息建议

建议使用以下风格：

- `feat: 新功能描述`
- `fix: 问题修复描述`
- `docs: 文档更新描述`
- `refactor: 重构描述`
- `test: 测试相关描述`

## 代码风格

- 保持改动最小且聚焦
- 避免无关文件修改
- 与现有中文文案风格保持一致

