from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.m3u8_tools import check_integrity, get_first_segment, parse_m3u8


class M3U8ToolsTests(unittest.TestCase):
    def test_parse_m3u8_detects_encryption_and_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playlist = root / "index.m3u8"
            playlist.write_text(
                "#EXTM3U\n"
                "#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\"\n"
                "#EXTINF:10,\n"
                "seg0.ts\n"
                "#EXTINF:10,\n"
                "seg1.ts\n",
                encoding="utf-8",
            )
            parsed = parse_m3u8(str(playlist))
            self.assertTrue(parsed.encrypted)
            self.assertEqual(parsed.encryption_method, "AES-128")
            self.assertEqual(len(parsed.segments), 2)

    def test_check_integrity_reports_missing_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "seg0.ts").write_bytes(b"x")
            playlist = root / "index.m3u8"
            playlist.write_text(
                "#EXTM3U\n"
                "#EXTINF:10,\n"
                "seg0.ts\n"
                "#EXTINF:10,\n"
                "seg_missing.ts\n",
                encoding="utf-8",
            )
            report = check_integrity(str(playlist))
            self.assertEqual(report.total_segments, 2)
            self.assertEqual(len(report.missing_segments), 1)

    def test_get_first_segment_returns_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            playlist = root / "index.m3u8"
            playlist.write_text(
                "#EXTM3U\n"
                "#EXTINF:10,\n"
                "seg0.ts\n",
                encoding="utf-8",
            )
            first = get_first_segment(str(playlist))
            self.assertIsNotNone(first)
            assert first is not None
            self.assertTrue(first.resolved_uri.endswith("seg0.ts"))

    def test_parse_m3u8_rejects_html_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html_file = root / "not_playlist.m3u8"
            html_file.write_text("<!DOCTYPE html>\n<html></html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_m3u8(str(html_file))


if __name__ == "__main__":
    unittest.main()

