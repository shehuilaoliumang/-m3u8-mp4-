from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from app.ui import ConverterApp


class UiHelperTests(unittest.TestCase):
    def test_resolve_readme_path_prefers_exe_directory_when_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "README.md").write_text("help", encoding="utf-8")
            fake_exe = tmp_path / "m3u8ToMp4.exe"

            with mock.patch("app.ui.sys.executable", str(fake_exe)):
                with mock.patch("app.ui.sys.frozen", True, create=True):
                    with mock.patch("app.ui.sys._MEIPASS", None, create=True):
                        resolved = ConverterApp._resolve_readme_path()

            self.assertEqual(resolved, tmp_path / "README.md")

    def test_run_pip_install_returns_clear_message_in_frozen_without_python(self) -> None:
        with mock.patch("app.ui.sys.frozen", True, create=True):
            with mock.patch("app.ui.shutil.which", return_value=None):
                ok, message = ConverterApp._run_pip_install("send2trash>=1.8.3")
        self.assertFalse(ok)
        self.assertIn("已打包 EXE", message)

    def test_parse_drop_data_handles_braced_paths(self) -> None:
        raw = '{C:/Videos/a b.m3u8} {C:/Videos/c.m3u8}'
        items = ConverterApp._parse_drop_data(raw)
        self.assertEqual(items, ["C:/Videos/a b.m3u8", "C:/Videos/c.m3u8"])

    def test_parse_drop_data_handles_single_url(self) -> None:
        raw = "https://example.com/live/index.m3u8"
        items = ConverterApp._parse_drop_data(raw)
        self.assertEqual(items, ["https://example.com/live/index.m3u8"])

    def test_to_preview_text_removes_markdown_marks(self) -> None:
        markdown = "## 标题\n- **项目** `code`\n\n```txt\nline\n```"
        rendered = ConverterApp._to_preview_text(markdown)
        self.assertIn("标题", rendered)
        self.assertIn("- 项目 code", rendered)
        self.assertIn("line", rendered)
        self.assertNotIn("##", rendered)
        self.assertNotIn("```", rendered)

    def test_format_help_preview_has_preview_style_header(self) -> None:
        rendered = ConverterApp._format_help_preview("设置菜单", "## 小节\n- 条目")
        self.assertIn("【设置菜单】", rendered)
        self.assertIn("----------------------------------------", rendered)
        self.assertIn("- 条目", rendered)

    def test_build_dependency_status_text_for_missing_drag_dependency(self) -> None:
        text = ConverterApp._build_dependency_status_text(
            recycle_enabled=True,
            send2trash_available=True,
            drag_enabled=True,
            tkinterdnd2_available=False,
        )
        self.assertIn("回收站可用", text)
        self.assertIn("拖放依赖缺失", text)

    def test_split_help_cards_by_heading(self) -> None:
        content = "简介内容\n\n### 操作步骤\n- 第一步\n\n### 常见问题\n- Q1"
        cards = ConverterApp._split_help_cards(content)
        self.assertEqual(cards[0][0], "本节说明")
        self.assertEqual(cards[1][0], "操作步骤")
        self.assertEqual(cards[2][0], "常见问题")

    def test_build_drag_runtime_status_text_states(self) -> None:
        self.assertEqual(
            ConverterApp._build_drag_runtime_status_text(False, False, False),
            "拖放状态：未启用",
        )
        self.assertEqual(
            ConverterApp._build_drag_runtime_status_text(True, True, True),
            "拖放状态：已启用",
        )
        self.assertIn(
            "重启后启用",
            ConverterApp._build_drag_runtime_status_text(True, False, True),
        )

    def test_filter_help_sections_keeps_parent_chain(self) -> None:
        sections = [
            (1, "设置菜单", ""),
            (2, "依赖中心", ""),
            (3, "一键部署 tkinterdnd2", ""),
            (2, "默认输出目录", ""),
        ]
        filtered = ConverterApp._filter_help_sections(sections, "tkinterdnd2")
        self.assertEqual(
            [title for _level, title, _content in filtered],
            ["设置菜单", "依赖中心", "一键部署 tkinterdnd2"],
        )

    def test_filter_help_sections_returns_all_when_keyword_empty(self) -> None:
        sections = [(1, "A", "a"), (2, "B", "b")]
        self.assertEqual(ConverterApp._filter_help_sections(sections, "  "), sections)

    def test_is_help_match_checks_title_and_preview_content(self) -> None:
        self.assertTrue(ConverterApp._is_help_match("输入设置", "支持 URL", "url"))
        self.assertTrue(ConverterApp._is_help_match("拖放配置", "正文", "拖放"))
        self.assertFalse(ConverterApp._is_help_match("输入设置", "支持 URL", "不存在"))

    def test_resolve_match_index_forward_and_backward(self) -> None:
        self.assertEqual(ConverterApp._resolve_match_index(3, None, True), 0)
        self.assertEqual(ConverterApp._resolve_match_index(3, None, False), 2)
        self.assertEqual(ConverterApp._resolve_match_index(3, 1, True), 2)
        self.assertEqual(ConverterApp._resolve_match_index(3, 1, False), 0)
        self.assertEqual(ConverterApp._resolve_match_index(3, 0, False), 2)

    def test_filter_help_sections_matches_content_text(self) -> None:
        sections = [
            (1, "帮助", ""),
            (2, "输入设置", "支持 URL 和文件夹"),
            (2, "输出设置", "仅文件名"),
        ]
        filtered = ConverterApp._filter_help_sections(sections, "URL")
        self.assertEqual(
            [title for _level, title, _content in filtered],
            ["帮助", "输入设置"],
        )

    def test_filter_help_sections_uses_preview_content_not_raw_markdown(self) -> None:
        sections = [
            (1, "帮助", ""),
            (2, "章节", "### 这是小节标题"),
        ]
        filtered = ConverterApp._filter_help_sections(sections, "###")
        self.assertEqual(filtered, [])

    def test_find_highlight_spans_returns_match_ranges(self) -> None:
        spans = ConverterApp._find_highlight_spans("支持 URL 和 文件", "url")
        self.assertEqual(spans, [(3, 6)])


if __name__ == "__main__":
    unittest.main()

