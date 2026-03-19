from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Sequence
from urllib.parse import urlparse


class ConverterError(Exception):
    """转换模块的基础异常。"""


class FFmpegNotFoundError(ConverterError):
    """当 PATH 中找不到 ffmpeg 时抛出。"""


class InvalidInputError(ConverterError):
    """当输入或输出路径不合法时抛出。"""


class ConvertFailedError(ConverterError):
    """当 ffmpeg 返回非零退出码时抛出。"""


class CancelledError(ConverterError):
    """当用户主动取消转换时抛出。"""


class DeployFailedError(ConverterError):
    """当 ffmpeg 一键部署失败时抛出。"""


@dataclass(frozen=True)
class ConvertResult:
    output_file: Path
    used_fallback: bool
    skipped: bool = False


@dataclass(frozen=True)
class DeployResult:
    success: bool
    message: str


ProgressCallback = Callable[[float, str], None]


def _iter_ffmpeg_candidates() -> list[Path]:
    project_root = Path(__file__).resolve().parents[1]
    app_dirs: list[Path] = [project_root]

    # 兼容 PyInstaller: 优先尝试 EXE 所在目录和 _MEIPASS 提取目录
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        app_dirs.insert(0, exe_dir)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            app_dirs.append(Path(meipass))

    candidates = [
        Path.home() / "scoop" / "shims" / "ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]

    for app_dir in app_dirs:
        candidates.extend(
            [
                app_dir / "ffmpeg.exe",
                app_dir / "ffmpeg" / "bin" / "ffmpeg.exe",
                app_dir / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe",
            ]
        )

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        win_get_links = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        candidates.append(win_get_links)

        win_get_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if win_get_packages.exists():
            candidates.extend(win_get_packages.rglob("ffmpeg.exe"))

    choco_install = os.environ.get("ChocolateyInstall")
    if choco_install:
        candidates.append(Path(choco_install) / "bin" / "ffmpeg.exe")

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        unique_candidates.append(candidate)
    return unique_candidates


def auto_detect_ffmpeg_path() -> str | None:
    try:
        return ensure_ffmpeg_available("ffmpeg")
    except FFmpegNotFoundError:
        return None


def ensure_ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> str:
    custom_path = Path(ffmpeg_bin).expanduser()
    if custom_path.is_file():
        return str(custom_path.resolve())

    resolved = shutil.which(f"{ffmpeg_bin}")
    if resolved:
        return resolved

    for candidate in _iter_ffmpeg_candidates():
        if candidate.is_file():
            return str(candidate.resolve())

    raise FFmpegNotFoundError(
        "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，或在界面中手动选择 ffmpeg.exe。"
    )


def _seconds_from_timestamp(value: str) -> float | None:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _is_http_source(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _resolve_input_source(input_source: str) -> tuple[str, str, bool]:
    source = input_source.strip()
    if not source:
        raise InvalidInputError("输入源不能为空。")

    if _is_http_source(source):
        parsed = urlparse(source)
        base_name = Path(parsed.path).stem or "m3u8_output"
        return source, base_name, False

    input_path = Path(source).expanduser().resolve()
    if not input_path.exists() or not input_path.is_file():
        raise InvalidInputError(f"输入文件不存在：{input_path}")
    if input_path.suffix.lower() != ".m3u8":
        raise InvalidInputError("输入文件必须是 .m3u8 格式。")
    return str(input_path), input_path.stem, True


def _probe_duration_seconds(ffmpeg_bin: str, input_path: str, is_local: bool) -> float | None:
    if not is_local:
        return None

    ffmpeg_path = Path(ffmpeg_bin)
    if ffmpeg_path.is_file():
        ffprobe_candidate = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
        ffprobe_bin = str(ffprobe_candidate) if ffprobe_candidate.is_file() else "ffprobe"
    else:
        ffprobe_bin = "ffprobe"

    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None

    if result.returncode != 0:
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def _with_progress_flags(command: Sequence[str]) -> list[str]:
    cmd = list(command)
    if len(cmd) < 2:
        return cmd
    output_file = cmd[-1]
    return [
        *cmd[:-1],
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        output_file,
    ]


def _run_command_with_progress(
    command: Sequence[str],
    duration_seconds: float | None,
    progress_callback: ProgressCallback | None,
    cancel_event: object | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    output_lines: list[str] = []
    processed_seconds = 0.0

    if process.stdout is not None:
        for raw_line in process.stdout:
            if cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set():
                process.terminate()
                process.wait(timeout=5)
                return subprocess.CompletedProcess(
                    list(command),
                    -1,
                    stdout="\n".join(output_lines),
                    stderr="转换已取消",
                )

            line = raw_line.strip()
            output_lines.append(line)
            if not progress_callback:
                continue

            if line.startswith("out_time_us="):
                try:
                    processed_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
            elif line.startswith("out_time_ms="):
                try:
                    processed_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
            elif line.startswith("out_time="):
                parsed = _seconds_from_timestamp(line.split("=", 1)[1])
                if parsed is None:
                    continue
                processed_seconds = parsed
            elif line == "progress=end":
                progress_callback(100.0, "转换完成，正在收尾...")
                continue
            else:
                continue

            if duration_seconds and duration_seconds > 0:
                percent = min(99.0, processed_seconds / duration_seconds * 100)
                progress_callback(percent, f"已处理 {processed_seconds:.1f}s / {duration_seconds:.1f}s")
            else:
                progress_callback(0.0, f"已处理 {processed_seconds:.1f}s")

    return_code = process.wait()
    stdout_text = "\n".join(output_lines)
    return subprocess.CompletedProcess(list(command), return_code, stdout=stdout_text, stderr=stdout_text)


def validate_output_dir(output_dir: str) -> Path:
    output_path = Path(output_dir).expanduser().resolve()
    if not output_path.exists() or not output_path.is_dir():
        raise InvalidInputError(f"输出目录无效：{output_path}")
    return output_path


def _normalize_output_name(output_name: str | None) -> str | None:
    if output_name is None:
        return None
    normalized = output_name.strip()
    if not normalized:
        return None

    invalid_chars = set('\\/:*?"<>|')
    if any(ch in invalid_chars for ch in normalized):
        raise InvalidInputError("输出文件名包含非法字符：\\ / : * ? \" < > |")

    name_without_suffix = Path(normalized).stem
    if not name_without_suffix:
        raise InvalidInputError("输出文件名不能为空。")
    return name_without_suffix


def build_output_path(
    input_path: Path | str,
    output_dir: Path,
    output_name: str | None = None,
    conflict_strategy: str = "auto_rename",
) -> Path | None:
    default_name = input_path.stem if isinstance(input_path, Path) else Path(input_path).stem
    base_name = _normalize_output_name(output_name) or default_name
    candidate = output_dir / f"{base_name}.mp4"

    if conflict_strategy == "overwrite":
        return candidate
    if conflict_strategy == "skip" and candidate.exists():
        return None
    if conflict_strategy not in {"auto_rename", "skip", "overwrite"}:
        raise InvalidInputError(f"未知重名策略：{conflict_strategy}")
    if not candidate.exists():
        return candidate

    index = 1
    while True:
        candidate = output_dir / f"{base_name}_{index}.mp4"
        if not candidate.exists():
            return candidate
        index += 1


def build_ffmpeg_copy_command(ffmpeg_bin: str, input_source: str, output_path: Path) -> list[str]:
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        input_source,
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(output_path),
    ]


def build_ffmpeg_reencode_command(
    ffmpeg_bin: str,
    input_source: str,
    output_path: Path,
    x264_preset: str,
    crf: str,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-y",
        "-i",
        input_source,
        "-c:v",
        "libx264",
        "-preset",
        x264_preset,
        "-crf",
        crf,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]


def _resolve_preset(preset: str) -> tuple[str, str, bool]:
    options = {
        "fast_copy": ("medium", "23", True),
        "compatibility": ("medium", "23", False),
        "high_quality": ("slow", "18", False),
    }
    if preset not in options:
        raise InvalidInputError(f"未知转换预设：{preset}")
    return options[preset]


def convert_m3u8_to_mp4(
    input_file: str,
    output_dir: str,
    ffmpeg_bin: str = "ffmpeg",
    fallback_reencode: bool = True,
    progress_callback: ProgressCallback | None = None,
    output_name: str | None = None,
    preset: str = "fast_copy",
    conflict_strategy: str = "auto_rename",
    cancel_event: object | None = None,
) -> ConvertResult:
    ffmpeg_bin = ensure_ffmpeg_available(ffmpeg_bin)
    output_folder = validate_output_dir(output_dir)
    input_source, default_name, is_local = _resolve_input_source(input_file)
    output_path = build_output_path(default_name, output_folder, output_name, conflict_strategy)

    if output_path is None:
        return ConvertResult(output_file=output_folder / f"{default_name}.mp4", used_fallback=False, skipped=True)

    x264_preset, crf, allow_copy_first = _resolve_preset(preset)
    duration_seconds = _probe_duration_seconds(ffmpeg_bin, input_source, is_local)

    if progress_callback:
        progress_callback(0.0, "准备开始转换...")

    copy_result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    if allow_copy_first:
        copy_cmd = build_ffmpeg_copy_command(ffmpeg_bin, input_source, output_path)
        copy_result = _run_command_with_progress(
            _with_progress_flags(copy_cmd),
            duration_seconds,
            progress_callback,
            cancel_event,
        )
        if copy_result.returncode == -1:
            raise CancelledError("转换已取消。")
        if copy_result.returncode == 0 and output_path.exists():
            if progress_callback:
                progress_callback(100.0, "转换完成")
            return ConvertResult(output_file=output_path, used_fallback=False)
        if not fallback_reencode:
            raise ConvertFailedError(copy_result.stderr.strip() or "ffmpeg 转换失败。")
        if progress_callback:
            progress_callback(0.0, "流拷贝失败，尝试重编码...")

    reencode_cmd = build_ffmpeg_reencode_command(ffmpeg_bin, input_source, output_path, x264_preset, crf)
    reencode_result = _run_command_with_progress(
        _with_progress_flags(reencode_cmd),
        duration_seconds,
        progress_callback,
        cancel_event,
    )
    if reencode_result.returncode == -1:
        raise CancelledError("转换已取消。")
    if reencode_result.returncode == 0 and output_path.exists():
        if progress_callback:
            progress_callback(100.0, "转换完成")
        return ConvertResult(output_file=output_path, used_fallback=allow_copy_first)

    details = (
        "ffmpeg 转换失败。\n"
        f"流拷贝错误输出：{copy_result.stderr.strip()}\n"
        f"重编码错误输出：{reencode_result.stderr.strip()}"
    )
    raise ConvertFailedError(details)


def deploy_ffmpeg() -> DeployResult:
    winget_path = shutil.which("winget")
    if not winget_path:
        raise DeployFailedError("未检测到 winget，无法一键部署。请手动安装 ffmpeg。")

    command = [
        winget_path,
        "install",
        "--id",
        "Gyan.FFmpeg",
        "-e",
        "--accept-source-agreements",
        "--accept-package-agreements",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise DeployFailedError(f"ffmpeg 一键部署失败：{details}")

    return DeployResult(success=True, message="ffmpeg 已完成安装，请在设置中重新检测路径。")
