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
    conflict_strategy: str = "auto_rename"
    input_mode: str = "local"
    folder_recursive_scan: bool = False
    folder_first_only_per_dir: bool = False
    preview_before_start: bool = True
    continue_on_error: bool = False
    smart_select_preference: str = "folder"
    delete_to_recycle_bin: bool = True
    enable_drag_drop: bool = True
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
        elif isinstance(value, str) and value.strip():
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

