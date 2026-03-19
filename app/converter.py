from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen


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


@dataclass(frozen=True)
class DecryptOptions:
    auto_parse_key: bool = True
    manual_key_hex: str = ""
    manual_iv_hex: str = ""


@dataclass(frozen=True)
class TranscodeOptions:
    mode: str = "preset"
    resolution: str = ""
    video_bitrate: str = ""
    fps: str = ""
    audio_sample_rate: str = ""
    audio_bitrate: str = ""


@dataclass(frozen=True)
class M3U8KeyInfo:
    method: str
    key_uri: str
    iv: str


ProgressCallback = Callable[[float, str], None]


def _normalize_hex_value(value: str, value_name: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    if not re.fullmatch(r"[0-9a-f]+", normalized):
        raise InvalidInputError(f"{value_name} 必须是十六进制字符串。")
    return normalized


def _normalize_manual_key_hex(value: str) -> str:
    normalized = _normalize_hex_value(value, "KEY")
    if normalized and len(normalized) != 32:
        raise InvalidInputError("KEY 必须是 16 字节（32 位十六进制）。")
    return normalized


def _normalize_manual_iv_hex(value: str) -> str:
    normalized = _normalize_hex_value(value, "IV")
    if normalized and len(normalized) != 32:
        raise InvalidInputError("IV 必须是 16 字节（32 位十六进制）。")
    return normalized


def _parse_key_attributes(line: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for key, quoted_value, raw_value in re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]+))', line):
        attributes[key] = quoted_value if quoted_value != "" else raw_value.strip()
    return attributes


def _resolve_key_uri(input_source: str, key_uri: str) -> str:
    if _is_http_source(key_uri):
        return key_uri
    if _is_http_source(input_source):
        from urllib.parse import urljoin

        return urljoin(input_source, key_uri)
    return str((Path(input_source).parent / key_uri).resolve())


def probe_m3u8_key_info(input_source: str) -> M3U8KeyInfo | None:
    text = ""
    if _is_http_source(input_source):
        request = Request(input_source, headers={"User-Agent": "m3u8ToMp4/1.0"})
        with urlopen(request, timeout=8) as response:  # nosec B310
            text = response.read().decode("utf-8", errors="replace")
    else:
        source_path = Path(input_source)
        if source_path.is_file():
            text = source_path.read_text(encoding="utf-8", errors="replace")

    if not text:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#EXT-X-KEY"):
            continue
        attributes = _parse_key_attributes(line)
        method = attributes.get("METHOD", "").strip().upper()
        key_uri = attributes.get("URI", "").strip()
        iv = attributes.get("IV", "").strip()
        if method == "AES-128" and key_uri:
            return M3U8KeyInfo(method=method, key_uri=_resolve_key_uri(input_source, key_uri), iv=iv)
    return None


def _build_input_decrypt_flags(decrypt_options: DecryptOptions) -> list[str]:
    manual_key = _normalize_manual_key_hex(decrypt_options.manual_key_hex)
    manual_iv = _normalize_manual_iv_hex(decrypt_options.manual_iv_hex)
    flags: list[str] = []
    if manual_key:
        flags.extend(["-decryption_key", manual_key])
    if manual_iv:
        flags.extend(["-decryption_iv", manual_iv])
    return flags


def _validate_custom_transcode_options(options: TranscodeOptions) -> TranscodeOptions:
    resolution = options.resolution.strip()
    video_bitrate = options.video_bitrate.strip()
    fps = options.fps.strip()
    audio_sample_rate = options.audio_sample_rate.strip()
    audio_bitrate = options.audio_bitrate.strip()

    if resolution and not re.fullmatch(r"\d{2,5}x\d{2,5}", resolution):
        raise InvalidInputError("分辨率格式应为 宽x高，例如 1920x1080。")
    if video_bitrate and not re.fullmatch(r"\d+(?:\.\d+)?[kKmM]?", video_bitrate):
        raise InvalidInputError("视频码率格式无效，例如 2500k 或 3M。")
    if fps and not re.fullmatch(r"\d+(?:\.\d+)?", fps):
        raise InvalidInputError("帧率格式无效，例如 30 或 29.97。")
    if audio_sample_rate and not re.fullmatch(r"\d{4,6}", audio_sample_rate):
        raise InvalidInputError("音频采样率格式无效，例如 44100 或 48000。")
    if audio_bitrate and not re.fullmatch(r"\d+(?:\.\d+)?[kKmM]?", audio_bitrate):
        raise InvalidInputError("音频码率格式无效，例如 128k。")

    return TranscodeOptions(
        mode=options.mode,
        resolution=resolution,
        video_bitrate=video_bitrate,
        fps=fps,
        audio_sample_rate=audio_sample_rate,
        audio_bitrate=audio_bitrate,
    )


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

    resolved = shutil.which(str(ffmpeg_bin))
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


def _format_bytes(size_value: int) -> str:
    size = float(size_value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size_value}B"


def _format_eta(seconds_value: float | None) -> str:
    if seconds_value is None or seconds_value < 0:
        return "--:--"
    total = int(seconds_value)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _parse_speed_factor(line: str) -> float | None:
    if not line.startswith("speed="):
        return None
    raw = line.split("=", 1)[1].strip().lower().rstrip("x")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


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
    total_size_bytes = 0
    speed_factor: float | None = None

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
            elif line.startswith("total_size="):
                try:
                    total_size_bytes = int(line.split("=", 1)[1])
                except ValueError:
                    continue
            elif line.startswith("speed="):
                speed_factor = _parse_speed_factor(line)
            elif line == "progress=end":
                progress_callback(100.0, "转换完成，正在收尾...")
                continue
            else:
                continue

            if duration_seconds and duration_seconds > 0:
                percent = min(99.0, processed_seconds / duration_seconds * 100)
                remain_seconds = max(0.0, duration_seconds - processed_seconds)
                eta = remain_seconds / speed_factor if speed_factor and speed_factor > 0 else None
                progress_callback(
                    percent,
                    " | ".join(
                        [
                            f"已处理 {processed_seconds:.1f}s/{duration_seconds:.1f}s",
                            f"速度 {speed_factor:.2f}x" if speed_factor else "速度 --",
                            f"剩余 {_format_eta(eta)}",
                            f"已输出 {_format_bytes(total_size_bytes)}",
                        ]
                    ),
                )
            else:
                progress_callback(
                    0.0,
                    " | ".join(
                        [
                            f"已处理 {processed_seconds:.1f}s",
                            f"速度 {speed_factor:.2f}x" if speed_factor else "速度 --",
                            f"已输出 {_format_bytes(total_size_bytes)}",
                        ]
                    ),
                )

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


def _normalize_output_format(output_format: str) -> str:
    normalized = output_format.strip().lower().lstrip(".")
    if normalized not in {"mp4", "mov", "avi"}:
        raise InvalidInputError("输出格式仅支持 mp4 / mov / avi。")
    return normalized


def build_output_path(
    input_path: Path | str,
    output_dir: Path,
    output_name: str | None = None,
    conflict_strategy: str = "auto_rename",
    output_format: str = "mp4",
) -> Path | None:
    extension = _normalize_output_format(output_format)
    default_name = input_path.stem if isinstance(input_path, Path) else Path(input_path).stem
    base_name = _normalize_output_name(output_name) or default_name
    candidate = output_dir / f"{base_name}.{extension}"

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
        candidate = output_dir / f"{base_name}_{index}.{extension}"
        if not candidate.exists():
            return candidate
        index += 1


def build_ffmpeg_copy_command(
    ffmpeg_bin: str,
    input_source: str,
    output_path: Path,
    input_options: Sequence[str] | None = None,
) -> list[str]:
    in_opts = list(input_options or [])
    return [
        ffmpeg_bin,
        "-y",
        *in_opts,
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
    input_options: Sequence[str] | None = None,
) -> list[str]:
    in_opts = list(input_options or [])
    return [
        ffmpeg_bin,
        "-y",
        *in_opts,
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


def build_ffmpeg_custom_command(
    ffmpeg_bin: str,
    input_source: str,
    output_path: Path,
    options: TranscodeOptions,
    input_options: Sequence[str] | None = None,
) -> list[str]:
    custom = _validate_custom_transcode_options(options)
    in_opts = list(input_options or [])
    command = [
        ffmpeg_bin,
        "-y",
        *in_opts,
        "-i",
        input_source,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        custom.audio_bitrate or "192k",
    ]
    if custom.resolution:
        command.extend(["-vf", f"scale={custom.resolution}"])
    if custom.video_bitrate:
        command.extend(["-b:v", custom.video_bitrate])
    if custom.fps:
        command.extend(["-r", custom.fps])
    if custom.audio_sample_rate:
        command.extend(["-ar", custom.audio_sample_rate])
    command.append(str(output_path))
    return command


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
    decrypt_options: DecryptOptions | None = None,
    transcode_options: TranscodeOptions | None = None,
    output_format: str = "mp4",
) -> ConvertResult:
    ffmpeg_bin = ensure_ffmpeg_available(ffmpeg_bin)
    output_folder = validate_output_dir(output_dir)
    input_source, default_name, is_local = _resolve_input_source(input_file)
    decrypt_opts = decrypt_options or DecryptOptions()
    transcode_opts = transcode_options or TranscodeOptions()
    input_decrypt_flags = _build_input_decrypt_flags(decrypt_opts)

    key_info: M3U8KeyInfo | None = None
    if decrypt_opts.auto_parse_key:
        try:
            key_info = probe_m3u8_key_info(input_source)
        except Exception:
            key_info = None

    normalized_output_format = _normalize_output_format(output_format)
    output_path = build_output_path(
        default_name,
        output_folder,
        output_name,
        conflict_strategy,
        normalized_output_format,
    )

    if output_path is None:
        return ConvertResult(
            output_file=output_folder / f"{default_name}.{normalized_output_format}",
            used_fallback=False,
            skipped=True,
        )

    x264_preset, crf, allow_copy_first = _resolve_preset(preset)
    duration_seconds = _probe_duration_seconds(ffmpeg_bin, input_source, is_local)

    if progress_callback:
        if key_info is not None:
            progress_callback(0.0, f"检测到 AES-128 加密，KEY URL：{key_info.key_uri}")
        elif input_decrypt_flags:
            progress_callback(0.0, "已启用手动 KEY/IV 解密参数")
        else:
            progress_callback(0.0, "准备开始转换...")

    copy_result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
    if allow_copy_first:
        copy_cmd = build_ffmpeg_copy_command(ffmpeg_bin, input_source, output_path, input_decrypt_flags)
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

    if transcode_opts.mode == "custom":
        reencode_cmd = build_ffmpeg_custom_command(
            ffmpeg_bin,
            input_source,
            output_path,
            transcode_opts,
            input_decrypt_flags,
        )
    else:
        reencode_cmd = build_ffmpeg_reencode_command(
            ffmpeg_bin,
            input_source,
            output_path,
            x264_preset,
            crf,
            input_decrypt_flags,
        )
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


def _run_install_command(command: list[str], hint: str) -> DeployResult:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise DeployFailedError(f"ffmpeg 一键部署失败：{details}\n建议：{hint}")
    return DeployResult(success=True, message="ffmpeg 已完成安装，请在设置中重新检测路径。")


def deploy_ffmpeg() -> DeployResult:
    platform = sys.platform.lower()

    if platform.startswith("win"):
        winget_path = shutil.which("winget")
        if not winget_path:
            raise DeployFailedError("未检测到 winget，无法一键部署。请手动安装 ffmpeg。")
        return _run_install_command(
            [
                str(winget_path),
                "install",
                "--id",
                "Gyan.FFmpeg",
                "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            "请确认 winget 可用，或在设置中手动指定 ffmpeg 路径。",
        )

    if platform == "darwin":
        brew_path = shutil.which("brew")
        if not brew_path:
            raise DeployFailedError("未检测到 brew。请先安装 Homebrew，再执行一键部署。")
        return _run_install_command(
            [str(brew_path), "install", "ffmpeg"],
            "请确认 Homebrew 可用并有安装权限。",
        )

    if platform.startswith("linux"):
        apt_path = shutil.which("apt-get")
        dnf_path = shutil.which("dnf")
        yum_path = shutil.which("yum")
        if apt_path:
            return _run_install_command(
                [str(apt_path), "install", "-y", "ffmpeg"],
                "若提示权限不足，请在终端使用 sudo apt-get install -y ffmpeg。",
            )
        if dnf_path:
            return _run_install_command(
                [str(dnf_path), "install", "-y", "ffmpeg"],
                "若提示权限不足，请在终端使用 sudo dnf install -y ffmpeg。",
            )
        if yum_path:
            return _run_install_command(
                [str(yum_path), "install", "-y", "ffmpeg"],
                "若提示权限不足，请在终端使用 sudo yum install -y ffmpeg。",
            )
        raise DeployFailedError("未检测到 apt-get/dnf/yum，无法自动安装 ffmpeg。")

    raise DeployFailedError(f"暂不支持当前平台自动安装：{sys.platform}")
