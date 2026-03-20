from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


CONFIG_PATH = Path.home() / ".m3u8_to_mp4_config.json"


@dataclass
class AppConfig:
    last_output_dir: str
    default_output_dir: str = ""
    ffmpeg_path: str = "ffmpeg"
    preset: str = "fast_copy"
    output_format: str = "mp4"
    output_prefix: str = ""
    output_suffix: str = ""
    output_use_timestamp: bool = False
    conflict_strategy: str = "auto_rename"
    input_mode: str = "local"
    folder_recursive_scan: bool = False
    folder_first_only_per_dir: bool = False
    preview_before_start: bool = True
    continue_on_error: bool = False
    smart_select_preference: str = "folder"
    delete_to_recycle_bin: bool = True
    delete_scope_mode: str = "with_related_and_dirs"
    delete_include_related_files: bool = True
    delete_cleanup_empty_dirs: bool = True
    delete_preview_before_execute: bool = True
    enable_drag_drop: bool = True
    decrypt_auto_parse_key: bool = True
    manual_decrypt_key_hex: str = ""
    manual_decrypt_iv_hex: str = ""
    transcode_mode: str = "preset"
    custom_video_resolution: str = ""
    custom_video_bitrate: str = ""
    custom_video_fps: str = ""
    custom_audio_sample_rate: str = ""
    custom_audio_bitrate: str = ""
    custom_templates_json: str = "{}"
    enable_resume: bool = True
    persist_resume_json: bool = True
    enable_sound_notify: bool = False
    cleanup_preview_temp_on_exit: bool = True
    max_workers: str = "1"
    log_level_filter: str = "全部"
    log_task_filter: str = "全部任务"
    theme_mode: str = "light"
    progressbar_color_mode: str = "green"
    window_geometry: str = "900x620"


def load_config(default_output_dir: str) -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig(last_output_dir=default_output_dir)

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig(last_output_dir=default_output_dir)

    config = AppConfig(last_output_dir=default_output_dir)
    template = asdict(config)
    for key, default_value in template.items():
        value = raw.get(key)
        if isinstance(default_value, bool):
            if isinstance(value, bool):
                setattr(config, key, value)
            elif isinstance(value, str) and value.lower() in {"true", "false"}:
                setattr(config, key, value.lower() == "true")
            continue

        if isinstance(default_value, str) and isinstance(value, str):
            # 允许空字符串覆盖，方便用户清空自定义参数。
            setattr(config, key, value)
    return config


def save_config(config: AppConfig) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # 配置写入失败不影响核心转换流程。
        return

