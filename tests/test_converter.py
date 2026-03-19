from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.converter import (
    DeployFailedError,
    FFmpegNotFoundError,
    InvalidInputError,
    TranscodeOptions,
    _iter_ffmpeg_candidates,
    _resolve_input_source,
    _seconds_from_timestamp,
    _with_progress_flags,
    auto_detect_ffmpeg_path,
    build_ffmpeg_custom_command,
    build_ffmpeg_copy_command,
    build_output_path,
    deploy_ffmpeg,
    ensure_ffmpeg_available,
    probe_m3u8_key_info,
    validate_output_dir,
)


class ConverterTests(unittest.TestCase):
    def test_iter_ffmpeg_candidates_includes_exe_dir_when_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "m3u8ToMp4.exe"
            with mock.patch("app.converter.sys.executable", str(fake_exe)):
                with mock.patch("app.converter.sys.frozen", True, create=True):
                    with mock.patch("app.converter.sys._MEIPASS", None, create=True):
                        candidates = _iter_ffmpeg_candidates()

            self.assertIn(Path(tmp) / "ffmpeg.exe", candidates)

    def test_build_output_path_adds_suffix_when_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first_target = tmp_path / "video.mp4"
            first_target.write_bytes(b"x")
            output = build_output_path("video", tmp_path)
            self.assertEqual(output.name, "video_1.mp4")

    def test_build_output_path_uses_custom_name_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = build_output_path("video", tmp_path, output_name="my_output")
            self.assertEqual(output.name, "my_output.mp4")

    def test_build_output_path_supports_mov_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = build_output_path("video", tmp_path, output_format="mov")
            self.assertEqual(output.name, "video.mov")

    def test_build_output_path_rejects_unknown_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(InvalidInputError):
                build_output_path("video", tmp_path, output_format="mkv")

    def test_build_output_path_skip_strategy_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "video.mp4").write_bytes(b"x")
            output = build_output_path("video", tmp_path, conflict_strategy="skip")
            self.assertIsNone(output)

    def test_build_ffmpeg_copy_command_contains_expected_flags(self) -> None:
        cmd = build_ffmpeg_copy_command("ffmpeg", "input.m3u8", Path("output.mp4"))
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)
        self.assertIn("-bsf:a", cmd)

    def test_build_ffmpeg_custom_command_includes_custom_options(self) -> None:
        cmd = build_ffmpeg_custom_command(
            "ffmpeg",
            "input.m3u8",
            Path("output.mp4"),
            TranscodeOptions(
                mode="custom",
                resolution="1280x720",
                video_bitrate="1800k",
                fps="30",
                audio_sample_rate="44100",
                audio_bitrate="128k",
            ),
        )
        self.assertIn("-vf", cmd)
        self.assertIn("scale=1280x720", cmd)
        self.assertIn("-b:v", cmd)
        self.assertIn("1800k", cmd)

    def test_build_ffmpeg_custom_command_rejects_bad_resolution(self) -> None:
        with self.assertRaises(InvalidInputError):
            build_ffmpeg_custom_command(
                "ffmpeg",
                "input.m3u8",
                Path("output.mp4"),
                TranscodeOptions(mode="custom", resolution="bad"),
            )

    def test_validate_output_dir_rejects_invalid_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_dir = Path(tmp) / "missing"
            with self.assertRaises(InvalidInputError):
                validate_output_dir(str(bad_dir))

    def test_ensure_ffmpeg_available_raises_when_missing(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("app.converter._iter_ffmpeg_candidates", return_value=[]):
                with self.assertRaises(FFmpegNotFoundError):
                    ensure_ffmpeg_available("ffmpeg")

    def test_ensure_ffmpeg_available_accepts_custom_executable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg_file = Path(tmp) / "ffmpeg.exe"
            ffmpeg_file.write_text("fake", encoding="utf-8")
            resolved = ensure_ffmpeg_available(str(ffmpeg_file))
            self.assertEqual(Path(resolved), ffmpeg_file.resolve())

    def test_auto_detect_ffmpeg_path_returns_none_when_missing(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("app.converter._iter_ffmpeg_candidates", return_value=[]):
                self.assertIsNone(auto_detect_ffmpeg_path())

    def test_with_progress_flags_inserts_progress_args_before_output(self) -> None:
        command = ["ffmpeg", "-i", "input.m3u8", "output.mp4"]
        result = _with_progress_flags(command)
        self.assertEqual(result[-1], "output.mp4")
        self.assertIn("-progress", result)
        self.assertIn("pipe:1", result)

    def test_seconds_from_timestamp_parses_valid_value(self) -> None:
        seconds = _seconds_from_timestamp("00:01:30.50")
        self.assertEqual(seconds, 90.5)

    def test_resolve_input_source_accepts_url(self) -> None:
        source, stem, is_local = _resolve_input_source("https://example.com/path/test.m3u8")
        self.assertEqual(source, "https://example.com/path/test.m3u8")
        self.assertEqual(stem, "test")
        self.assertFalse(is_local)

    def test_probe_m3u8_key_info_parses_local_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            playlist = Path(tmp) / "index.m3u8"
            playlist.write_text(
                "#EXTM3U\n"
                "#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\",IV=0x00000000000000000000000000000001\n"
                "#EXTINF:10,\n"
                "seg.ts\n",
                encoding="utf-8",
            )
            key_info = probe_m3u8_key_info(str(playlist))
            self.assertIsNotNone(key_info)
            assert key_info is not None
            self.assertEqual(key_info.method, "AES-128")
            self.assertTrue(key_info.key_uri.endswith("enc.key"))

    def test_deploy_ffmpeg_raises_when_winget_missing(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(DeployFailedError):
                deploy_ffmpeg()

    def test_deploy_ffmpeg_returns_success_when_command_ok(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch("shutil.which", return_value="winget"):
            with mock.patch("subprocess.run", return_value=completed):
                result = deploy_ffmpeg()
                self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()

