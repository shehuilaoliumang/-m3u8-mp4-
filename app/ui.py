from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
import os
import tempfile
import threading
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

try:
    import winsound
except ImportError:  # pragma: no cover
    winsound = None

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover
    send2trash = None

from app.config import AppConfig, load_config, save_config
from app.converter import (
    CancelledError,
    ConvertFailedError,
    DecryptOptions,
    DeployFailedError,
    FFmpegNotFoundError,
    InvalidInputError,
    TranscodeOptions,
    auto_detect_ffmpeg_path,
    convert_m3u8_to_mp4,
    deploy_ffmpeg,
    sanitize_ffmpeg_error_text,
)
from app.m3u8_tools import check_integrity, get_first_segment, parse_m3u8
from app.resume_store import ResumeStore


@dataclass(frozen=True)
class LogEntry:
    level: str
    task: str
    message: str


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Misc, auto_pack: bool = True) -> None:
        super().__init__(master, padding=12)
        self.master = master
        if auto_pack:
            self.pack(fill=tk.BOTH, expand=True)

        self.config_model = load_config(default_output_dir=str(Path.cwd()))
        self.master.geometry(self.config_model.window_geometry)

        self.default_output_dir = self.config_model.default_output_dir or self.config_model.last_output_dir
        if not self.default_output_dir:
            self.default_output_dir = str(Path.cwd())

        self.input_mode_var = tk.StringVar(value="自动")
        self.source_var = tk.StringVar()
        self.local_files: list[str] = []
        self.output_var = tk.StringVar(value=self.default_output_dir)
        self.output_name_var = tk.StringVar()
        self.ffmpeg_var = tk.StringVar(value=self.config_model.ffmpeg_path)
        self.preset_var = tk.StringVar(value=self._preset_label(self.config_model.preset))
        self.output_format_var = tk.StringVar(value=(self.config_model.output_format or "mp4").lower())
        self.output_prefix_var = tk.StringVar(value=self.config_model.output_prefix)
        self.output_suffix_var = tk.StringVar(value=self.config_model.output_suffix)
        self.output_timestamp_var = tk.BooleanVar(value=self.config_model.output_use_timestamp)
        self.conflict_var = tk.StringVar(value=self._conflict_label(self.config_model.conflict_strategy))
        self.folder_recursive_var = tk.BooleanVar(value=self.config_model.folder_recursive_scan)
        self.folder_first_only_var = tk.BooleanVar(value=self.config_model.folder_first_only_per_dir)
        self.preview_before_start_var = tk.BooleanVar(value=self.config_model.preview_before_start)
        self.continue_on_error_var = tk.BooleanVar(value=self.config_model.continue_on_error)
        self.smart_select_preference_var = tk.StringVar(value=self.config_model.smart_select_preference)
        self.delete_to_recycle_var = tk.BooleanVar(value=self.config_model.delete_to_recycle_bin)
        self.delete_scope_var = tk.StringVar(value=self._resolve_delete_scope_mode())
        self.delete_preview_before_execute_var = tk.BooleanVar(value=self.config_model.delete_preview_before_execute)
        self.enable_drag_drop_var = tk.BooleanVar(value=self.config_model.enable_drag_drop)
        self.decrypt_auto_parse_var = tk.BooleanVar(value=self.config_model.decrypt_auto_parse_key)
        self.decrypt_key_var = tk.StringVar(value=self.config_model.manual_decrypt_key_hex)
        self.decrypt_iv_var = tk.StringVar(value=self.config_model.manual_decrypt_iv_hex)
        self.transcode_mode_var = tk.StringVar(value=self.config_model.transcode_mode)
        self.custom_resolution_var = tk.StringVar(value=self.config_model.custom_video_resolution)
        self.custom_video_bitrate_var = tk.StringVar(value=self.config_model.custom_video_bitrate)
        self.custom_fps_var = tk.StringVar(value=self.config_model.custom_video_fps)
        self.custom_audio_sample_rate_var = tk.StringVar(value=self.config_model.custom_audio_sample_rate)
        self.custom_audio_bitrate_var = tk.StringVar(value=self.config_model.custom_audio_bitrate)
        self.enable_resume_var = tk.BooleanVar(value=self.config_model.enable_resume)
        self.enable_sound_notify_var = tk.BooleanVar(value=self.config_model.enable_sound_notify)
        self.cleanup_preview_temp_on_exit_var = tk.BooleanVar(value=self.config_model.cleanup_preview_temp_on_exit)
        self.max_workers_var = tk.StringVar(value=self.config_model.max_workers or "1")
        self.log_level_filter_var = tk.StringVar(value=self.config_model.log_level_filter or "全部")
        self.log_task_filter_var = tk.StringVar(value=self.config_model.log_task_filter or "全部任务")
        self.status_var = tk.StringVar(value="就绪")
        self.dependency_status_var = tk.StringVar(value="依赖状态：检测中...")
        self.drag_runtime_status_var = tk.StringVar(value="拖放状态：检测中...")
        self.delete_scope_status_var = tk.StringVar(value="删除范围：检测中...")
        self.progress_text_var = tk.StringVar(value="等待开始")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.cleanup_progress_text_var = tk.StringVar(value="清理进度：等待开始")
        self.cleanup_progress_var = tk.DoubleVar(value=0.0)
        self.ffmpeg_hint_var = tk.StringVar(value="FFmpeg：自动检测中...")
        self.theme_mode_var = tk.StringVar(value=self._normalize_theme_mode(self.config_model.theme_mode))
        self.progress_color_var = tk.StringVar(
            value=self._normalize_progress_color_mode(self.config_model.progressbar_color_mode)
        )

        self.cancel_event: threading.Event | None = None
        self.delete_source_after_success = False
        self.drag_drop_runtime_enabled = False
        self._working = False
        self.transcode_templates = self._load_transcode_templates(self.config_model.custom_templates_json)
        self.preview_temp_files: set[str] = set()
        self.log_entries: list[LogEntry] = []
        self.log_tasks: set[str] = {"全部任务", "全局"}
        self._log_render_after_id: str | None = None
        self._max_log_render_lines = 800
        self._progress_state_lock = threading.Lock()
        self._progress_update_scheduled = False
        self._pending_progress: tuple[float, str] = (0.0, "等待开始")
        self._ffmpeg_detecting = False
        self._last_log_filter: tuple[str, str] = ("全部", "全部任务")
        self._last_log_rendered_count = 0
        self._help_sections_cache: list[tuple[int, str, str]] | None = None
        self._help_sections_cache_path = ""
        self._help_sections_cache_mtime: float | None = None
        self._help_loading_token = 0
        self._classic_checkbuttons: list[tk.Checkbutton] = []
        self._progressbar_style_name = "Green.Horizontal.TProgressbar"
        self._cleanup_progressbar_style_name = "Cleanup.Horizontal.TProgressbar"

        self._build_widgets()
        self._defer_bootstrap_init()

    def _defer_bootstrap_init(self) -> None:
        # 将启动阶段拆分到多个 after 周期，降低首帧卡顿与黑屏概率。
        self.status_var.set("初始化中...")
        self._apply_theme(self.theme_mode_var.get())
        self._bind_live_validation()
        self._update_folder_option_state(False)
        self._refresh_action_state()
        self._refresh_delete_scope_status()
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.master.after(0, self._setup_drag_drop)
        self.master.after(30, self._refresh_drag_runtime_status)
        self.master.after(60, self._refresh_dependency_status)
        self.master.after(80, lambda: self.auto_detect_ffmpeg(show_message=False, log_start=False))
        self.master.after(160, self._preload_help_sections_cache)
        self.master.after(120, lambda: self.status_var.set("就绪"))

    def _preload_help_sections_cache(self) -> None:
        readme_path = self._resolve_readme_path()
        if readme_path is None:
            return

        try:
            current_mtime = readme_path.stat().st_mtime
            if (
                self._help_sections_cache is not None
                and self._help_sections_cache_path == str(readme_path)
                and self._help_sections_cache_mtime == current_mtime
            ):
                return
        except OSError:
            current_mtime = None

        def worker() -> None:
            try:
                readme_text = readme_path.read_text(encoding="utf-8")
                sections = self._parse_readme_sections(readme_text)
            except Exception:
                return
            self._help_sections_cache = sections
            self._help_sections_cache_path = str(readme_path)
            self._help_sections_cache_mtime = current_mtime

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _preset_label(value: str) -> str:
        mapping = {
            "fast_copy": "极速封装（先拷贝）",
            "compatibility": "兼容模式（重编码）",
            "high_quality": "高质量（慢速重编码）",
        }
        return mapping.get(value, "极速封装（先拷贝）")

    @staticmethod
    def _preset_value(label: str) -> str:
        mapping = {
            "极速封装（先拷贝）": "fast_copy",
            "兼容模式（重编码）": "compatibility",
            "高质量（慢速重编码）": "high_quality",
        }
        return mapping.get(label, "fast_copy")

    @staticmethod
    def _normalize_theme_mode(value: str) -> str:
        return value if value in {"light", "dark"} else "light"

    @staticmethod
    def _normalize_progress_color_mode(value: str) -> str:
        return value if value in {"green", "blue", "orange", "purple"} else "green"

    @staticmethod
    def _progressbar_color_hex(mode: str) -> str:
        mapping = {
            "green": "#2dbf4f",
            "blue": "#2f7df6",
            "orange": "#f08a24",
            "purple": "#8a5cf6",
        }
        return mapping.get(mode, "#2dbf4f")

    @staticmethod
    def _theme_toggle_button_text(mode: str) -> str:
        return "白日模式" if mode == "dark" else "夜间模式"

    def toggle_theme(self) -> None:
        new_mode = "dark" if self.theme_mode_var.get() == "light" else "light"
        self._apply_theme(new_mode)
        self._save_config()
        self._append_log(f"已切换为{'夜间' if new_mode == 'dark' else '白日'}模式", level="INFO", task="全局")

    def _on_progress_color_change(self) -> None:
        normalized = self._normalize_progress_color_mode(self.progress_color_var.get())
        self.progress_color_var.set(normalized)
        self._apply_theme(self.theme_mode_var.get())
        self._save_config()
        self._append_log(f"进度条颜色已设置为：{normalized}", level="INFO", task="全局")

    def _apply_theme(self, mode: str) -> None:
        normalized = self._normalize_theme_mode(mode)
        self.theme_mode_var.set(normalized)

        style = ttk.Style(self.master)
        # 在 Windows 原生主题下，部分控件会忽略 foreground/background，导致白底白字。
        # 统一切换到 clam 以确保按钮、输入框、下拉框等颜色可控。
        if style.theme_use() != "clam":
            style.theme_use("clam")

        if normalized == "dark":
            bg = "#1f1f1f"
            panel = "#2a2a2a"
            fg = "#e8e8e8"
            input_bg = "#333333"
            active_bg = "#3a3a3a"
            disabled_fg = "#9a9a9a"
            select_bg = "#2f2f2f"
        else:
            bg = "#f7f7f7"
            panel = "#ffffff"
            fg = "#222222"
            input_bg = "#ffffff"
            active_bg = "#f0f0f0"
            disabled_fg = "#8a8a8a"
            select_bg = "#ffffff"

        style.configure(".", background=bg, foreground=fg)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, foreground=fg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.map("TCheckbutton", foreground=[("disabled", disabled_fg), ("!disabled", fg)])
        style.map("TRadiobutton", foreground=[("disabled", disabled_fg), ("!disabled", fg)])
        style.configure("TButton", background=panel, foreground=fg)
        style.map(
            "TButton",
            background=[("active", active_bg), ("disabled", panel)],
            foreground=[("disabled", disabled_fg), ("!disabled", fg)],
        )
        style.configure("TMenubutton", background=panel, foreground=fg)
        style.map(
            "TMenubutton",
            background=[("active", active_bg), ("disabled", panel)],
            foreground=[("disabled", disabled_fg), ("!disabled", fg)],
        )
        style.configure("TEntry", fieldbackground=input_bg, background=input_bg, foreground=fg)
        style.map("TEntry", foreground=[("disabled", disabled_fg), ("!disabled", fg)])
        style.configure("TCombobox", fieldbackground=input_bg, background=panel, foreground=fg)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", input_bg), ("!readonly", input_bg)],
            foreground=[("readonly", fg), ("!readonly", fg), ("disabled", disabled_fg)],
        )
        style.configure("Vertical.TScrollbar", background=panel, troughcolor=bg, arrowcolor=fg)
        style.map("Vertical.TScrollbar", background=[("active", active_bg)])
        style.configure("Treeview", background=input_bg, fieldbackground=input_bg, foreground=fg)
        style.map("Treeview", background=[("selected", "#4a6984")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=panel, foreground=fg)
        style.map("Treeview.Heading", background=[("active", active_bg)], foreground=[("active", fg)])
        style.configure(
            self._progressbar_style_name,
            background=self._progressbar_color_hex(self.progress_color_var.get()),
            darkcolor=self._progressbar_color_hex(self.progress_color_var.get()),
            lightcolor=self._progressbar_color_hex(self.progress_color_var.get()),
            troughcolor=panel,
            bordercolor=panel,
        )
        style.configure(
            self._cleanup_progressbar_style_name,
            background="#26a0da",
            darkcolor="#26a0da",
            lightcolor="#26a0da",
            troughcolor=panel,
            bordercolor=panel,
        )

        self.master.configure(bg=bg)
        self.configure(style="TFrame")

        if hasattr(self, "theme_toggle_btn"):
            self.theme_toggle_btn.configure(text=self._theme_toggle_button_text(normalized))

        for name in ("preview_box", "log_box"):
            if hasattr(self, name):
                widget = getattr(self, name)
                widget.configure(bg=input_bg, fg=fg, insertbackground=fg)

        if hasattr(self, "help_canvas"):
            self.help_canvas.configure(background=bg)

        for check in self._classic_checkbuttons:
            check.configure(
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                selectcolor=select_bg,
                disabledforeground=disabled_fg,
                highlightthickness=0,
            )

        try:
            self.event_generate("<<ThemeChanged>>", when="tail")
        except tk.TclError:
            return

    @staticmethod
    def _conflict_label(value: str) -> str:
        mapping = {
            "auto_rename": "自动重命名",
            "overwrite": "覆盖同名文件",
            "skip": "跳过同名文件",
        }
        return mapping.get(value, "自动重命名")

    @staticmethod
    def _conflict_value(label: str) -> str:
        mapping = {
            "自动重命名": "auto_rename",
            "覆盖同名文件": "overwrite",
            "跳过同名文件": "skip",
        }
        return mapping.get(label, "auto_rename")

    @staticmethod
    def _load_transcode_templates(raw_json: str) -> dict[str, dict[str, str]]:
        if not raw_json.strip():
            return {}
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        templates: dict[str, dict[str, str]] = {}
        for name, payload in parsed.items():
            if not isinstance(name, str) or not isinstance(payload, dict):
                continue
            templates[name] = {
                "resolution": str(payload.get("resolution", "")),
                "video_bitrate": str(payload.get("video_bitrate", "")),
                "fps": str(payload.get("fps", "")),
                "audio_sample_rate": str(payload.get("audio_sample_rate", "")),
                "audio_bitrate": str(payload.get("audio_bitrate", "")),
            }
        return templates

    def _templates_to_json(self) -> str:
        return json.dumps(self.transcode_templates, ensure_ascii=False)

    def _collect_decrypt_options(self) -> DecryptOptions:
        return DecryptOptions(
            auto_parse_key=self.decrypt_auto_parse_var.get(),
            manual_key_hex=self.decrypt_key_var.get().strip(),
            manual_iv_hex=self.decrypt_iv_var.get().strip(),
        )

    def _collect_transcode_options(self) -> TranscodeOptions:
        return TranscodeOptions(
            mode=self.transcode_mode_var.get(),
            resolution=self.custom_resolution_var.get().strip(),
            video_bitrate=self.custom_video_bitrate_var.get().strip(),
            fps=self.custom_fps_var.get().strip(),
            audio_sample_rate=self.custom_audio_sample_rate_var.get().strip(),
            audio_bitrate=self.custom_audio_bitrate_var.get().strip(),
        )

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0)
        self.columnconfigure(3, weight=0)

        self.theme_toggle_btn = ttk.Button(
            self,
            text=self._theme_toggle_button_text(self.theme_mode_var.get()),
            command=self.toggle_theme,
            width=10,
        )
        self.theme_toggle_btn.grid(row=0, column=0, sticky="w", pady=(0, 8))

        settings_btn = ttk.Menubutton(self, text="设置")
        settings_menu = tk.Menu(settings_btn, tearoff=False)
        dependency_menu = tk.Menu(settings_menu, tearoff=False)
        ffmpeg_menu = tk.Menu(dependency_menu, tearoff=False)
        ffmpeg_menu.add_command(label="一键检测 FFmpeg", command=self.auto_detect_ffmpeg)
        ffmpeg_menu.add_command(label="一键部署 FFmpeg", command=self.install_ffmpeg)
        ffmpeg_menu.add_command(label="手动选择 ffmpeg.exe", command=self.select_ffmpeg)
        ffmpeg_menu.add_command(label="查看当前 FFmpeg 路径", command=self.show_ffmpeg_path)
        ffmpeg_menu.add_separator()
        ffmpeg_menu.add_command(label="恢复为默认 ffmpeg", command=self.reset_ffmpeg_path)

        recycle_menu = tk.Menu(dependency_menu, tearoff=False)
        recycle_menu.add_checkbutton(
            label="源文件删除改为回收站",
            variable=self.delete_to_recycle_var,
            command=lambda: (self._save_config(), self._refresh_dependency_status(), self._refresh_delete_scope_status()),
        )
        delete_scope_menu = tk.Menu(settings_menu, tearoff=False)
        delete_scope_menu.add_radiobutton(
            label="仅 m3u8 文件（最保守）",
            variable=self.delete_scope_var,
            value="playlist_only",
            command=lambda: (self._save_config(), self._refresh_delete_scope_status()),
        )
        delete_scope_menu.add_radiobutton(
            label="m3u8 + 关联分片/KEY/子清单（推荐）",
            variable=self.delete_scope_var,
            value="with_related_files",
            command=lambda: (self._save_config(), self._refresh_delete_scope_status()),
        )
        delete_scope_menu.add_radiobutton(
            label="m3u8 + 关联分片/KEY/子清单 + 空目录（最彻底）",
            variable=self.delete_scope_var,
            value="with_related_and_dirs",
            command=lambda: (self._save_config(), self._refresh_delete_scope_status()),
        )
        recycle_menu.add_command(label="检测回收站依赖状态", command=self.check_send2trash_status)
        recycle_menu.add_command(label="一键部署回收站依赖", command=self.install_send2trash)

        drag_menu = tk.Menu(dependency_menu, tearoff=False)
        drag_menu.add_command(label="检测 tkinterdnd2 状态", command=self.check_tkinterdnd2_status)
        drag_menu.add_command(label="一键部署 tkinterdnd2", command=self.install_tkinterdnd2)

        dependency_menu.add_command(label="一键部署全部可选依赖", command=self.install_all_optional_dependencies)
        dependency_menu.add_separator()
        dependency_menu.add_cascade(label="FFmpeg", menu=ffmpeg_menu)
        dependency_menu.add_cascade(label="回收站依赖", menu=recycle_menu)
        dependency_menu.add_cascade(label="拖放依赖", menu=drag_menu)
        settings_menu.add_cascade(label="依赖中心", menu=dependency_menu)
        settings_menu.add_separator()
        smart_menu = tk.Menu(settings_menu, tearoff=False)
        smart_menu.add_radiobutton(
            label="自动判断",
            variable=self.smart_select_preference_var,
            value="auto",
            command=self._save_config,
        )
        smart_menu.add_radiobutton(
            label="总是文件选择器",
            variable=self.smart_select_preference_var,
            value="file",
            command=self._save_config,
        )
        smart_menu.add_radiobutton(
            label="总是文件夹选择器",
            variable=self.smart_select_preference_var,
            value="folder",
            command=self._save_config,
        )
        settings_menu.add_cascade(label="智能选择偏好", menu=smart_menu)
        settings_menu.add_cascade(label="源文件删除范围", menu=delete_scope_menu)
        progress_color_menu = tk.Menu(settings_menu, tearoff=False)
        progress_color_menu.add_radiobutton(
            label="绿色（默认）",
            variable=self.progress_color_var,
            value="green",
            command=self._on_progress_color_change,
        )
        progress_color_menu.add_radiobutton(
            label="蓝色",
            variable=self.progress_color_var,
            value="blue",
            command=self._on_progress_color_change,
        )
        progress_color_menu.add_radiobutton(
            label="橙色",
            variable=self.progress_color_var,
            value="orange",
            command=self._on_progress_color_change,
        )
        progress_color_menu.add_radiobutton(
            label="紫色",
            variable=self.progress_color_var,
            value="purple",
            command=self._on_progress_color_change,
        )
        settings_menu.add_cascade(label="进度条颜色", menu=progress_color_menu)
        settings_menu.add_checkbutton(
            label="删除前显示预演清单",
            variable=self.delete_preview_before_execute_var,
            command=self._save_config,
        )
        settings_menu.add_checkbutton(
            label="启用拖放（需重启生效）",
            variable=self.enable_drag_drop_var,
            command=self._on_toggle_drag_drop,
        )
        settings_menu.add_separator()
        settings_menu.add_command(label="设置默认输出目录", command=self.set_default_output_dir)
        settings_menu.add_command(label="重置默认输出目录", command=self.reset_default_output_dir)
        settings_menu.add_command(label="设置并发数", command=self.set_max_workers)
        settings_menu.add_checkbutton(
            label="启用提示音（完成/失败/取消）",
            variable=self.enable_sound_notify_var,
            command=self._save_config,
        )
        settings_menu.add_checkbutton(
            label="会话结束自动清理临时预览文件",
            variable=self.cleanup_preview_temp_on_exit_var,
            command=self._save_config,
        )
        settings_menu.add_separator()
        settings_menu.add_command(label="加密解密设置", command=self.open_decrypt_settings)
        settings_menu.add_command(label="自定义转码参数", command=self.open_custom_transcode_settings)
        settings_menu.add_command(label="输出格式与命名规则", command=self.open_naming_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="导出配置", command=self.export_config_file)
        settings_menu.add_command(label="导入配置", command=self.import_config_file)
        settings_btn.configure(menu=settings_menu)
        settings_btn.grid(row=0, column=3, sticky="e", pady=(0, 8))
        self.settings_btn = settings_btn

        self.help_btn = ttk.Button(self, text="帮助", command=self.open_help_window)
        self.help_btn.grid(row=0, column=2, sticky="e", padx=(0, 8), pady=(0, 8))

        ttk.Label(self, textvariable=self.ffmpeg_hint_var).grid(
            row=0,
            column=1,
            columnspan=1,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Label(self, text="输入来源（自动识别）：").grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(self, text="支持文件 / 文件夹 / URL").grid(row=1, column=1, sticky="w", pady=(0, 8))

        self.recursive_check = self._make_stateful_classic_checkbutton(
            base_text="递归扫描子目录",
            variable=self.folder_recursive_var,
        )
        self.recursive_check.grid(row=1, column=2, sticky="w", pady=(0, 8))

        self.first_only_check = self._make_stateful_classic_checkbutton(
            base_text="每目录仅首个",
            variable=self.folder_first_only_var,
        )
        self.first_only_check.grid(row=1, column=3, sticky="w", pady=(0, 8))

        ttk.Label(self, text="输入源：").grid(row=2, column=0, sticky="w", pady=(0, 8))
        self.source_entry = ttk.Entry(self, textvariable=self.source_var)
        self.source_entry.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(0, 8)
        )
        self.source_btn = ttk.Button(self, text="智能选择", command=self.select_source_auto)
        self.source_btn.grid(row=2, column=3, sticky="ew", pady=(0, 8))

        ttk.Label(self, text="输出目录：").grid(row=3, column=0, sticky="w", pady=(0, 8))
        self.output_entry = ttk.Entry(self, textvariable=self.output_var)
        self.output_entry.grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(0, 8)
        )
        self.output_browse_btn = ttk.Button(self, text="浏览", command=self.select_output_dir)
        self.output_browse_btn.grid(
            row=3, column=3, sticky="ew", pady=(0, 8)
        )

        ttk.Label(self, text="输出文件名（可选）：").grid(row=4, column=0, sticky="w", pady=(0, 8))
        self.output_name_entry = ttk.Entry(self, textvariable=self.output_name_var)
        self.output_name_entry.grid(
            row=4, column=1, columnspan=3, sticky="ew", padx=(8, 8), pady=(0, 8)
        )

        ttk.Label(self, text="转换预设：").grid(row=5, column=0, sticky="w", pady=(0, 8))
        preset_box = ttk.Combobox(
            self,
            textvariable=self.preset_var,
            values=["极速封装（先拷贝）", "兼容模式（重编码）", "高质量（慢速重编码）"],
            state="readonly",
        )
        preset_box.grid(row=5, column=1, sticky="w", pady=(0, 8))

        ttk.Label(self, text="重名策略：").grid(row=5, column=2, sticky="e", pady=(0, 8))
        conflict_box = ttk.Combobox(
            self,
            textvariable=self.conflict_var,
            values=["自动重命名", "覆盖同名文件", "跳过同名文件"],
            state="readonly",
        )
        conflict_box.grid(row=5, column=3, sticky="ew", pady=(0, 8))

        self.custom_mode_check = self._make_stateful_classic_checkbutton(
            base_text="自定义模式（分辨率/码率/帧率/采样率）",
            variable=self.transcode_mode_var,
            onvalue="custom",
            offvalue="preset",
            command=self._save_config,
        )
        self.custom_mode_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.custom_settings_btn = ttk.Button(self, text="编辑自定义参数", command=self.open_custom_transcode_settings)
        self.custom_settings_btn.grid(row=6, column=2, sticky="ew", padx=(8, 8), pady=(0, 8))

        self.decrypt_settings_btn = ttk.Button(self, text="加密解密设置", command=self.open_decrypt_settings)
        self.decrypt_settings_btn.grid(row=6, column=3, sticky="ew", pady=(0, 8))

        self.preview_before_check = self._make_stateful_classic_checkbutton(
            base_text="开始前弹窗预览",
            variable=self.preview_before_start_var,
        )
        self.preview_before_check.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.continue_on_error_check = self._make_stateful_classic_checkbutton(
            base_text="单项失败继续后续",
            variable=self.continue_on_error_var,
        )
        self.continue_on_error_check.grid(row=7, column=2, sticky="w", pady=(0, 8))

        self.resume_check = self._make_stateful_classic_checkbutton(
            base_text="断点续传（任务级）",
            variable=self.enable_resume_var,
            command=self._save_config,
        )
        self.resume_check.grid(row=7, column=3, sticky="w", pady=(0, 8))

        self.preview_btn = ttk.Button(self, text="预览任务", command=self.preview_tasks)
        self.preview_btn.grid(row=8, column=0, sticky="ew", pady=(0, 8))

        self.convert_btn = ttk.Button(self, text="开始转换", command=self.start_convert)
        self.convert_btn.grid(row=8, column=1, sticky="ew", pady=(0, 8))

        self.cancel_btn = ttk.Button(self, text="取消转换", command=self.cancel_convert, state=tk.DISABLED)
        self.cancel_btn.grid(row=8, column=2, sticky="ew", padx=(8, 8), pady=(0, 8))

        self.export_log_btn = ttk.Button(self, text="导出日志", command=self.export_log)
        self.export_log_btn.grid(row=8, column=3, sticky="ew", pady=(0, 8))

        tool_frame = ttk.LabelFrame(self, text="诊断与预览工具")
        tool_frame.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        tool_frame.columnconfigure(0, weight=1)
        tool_frame.columnconfigure(1, weight=1)
        tool_frame.columnconfigure(2, weight=1)
        tool_frame.columnconfigure(3, weight=1)

        self.integrity_btn = ttk.Button(tool_frame, text="完整性校验", command=self.run_integrity_check)
        self.integrity_btn.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=8)

        self.preview_segment_btn = ttk.Button(tool_frame, text="预览首片段", command=self.preview_first_segment)
        self.preview_segment_btn.grid(row=0, column=1, sticky="ew", padx=4, pady=8)

        self.cleanup_preview_btn = ttk.Button(tool_frame, text="清理临时预览", command=self.cleanup_preview_temp_files_manually)
        self.cleanup_preview_btn.grid(row=0, column=2, sticky="ew", padx=(4, 8), pady=8)

        self.quick_check_btn = ttk.Button(tool_frame, text="快速自查", command=self.run_quick_self_check)
        self.quick_check_btn.grid(row=0, column=3, sticky="ew", padx=(4, 8), pady=8)

        ttk.Label(self, text="任务预览：").grid(row=10, column=0, sticky="w")
        self.preview_box = scrolledtext.ScrolledText(self, height=7, state=tk.DISABLED)
        self.preview_box.grid(row=11, column=0, columnspan=4, sticky="nsew", pady=(4, 8))

        ttk.Progressbar(
            self,
            style=self._progressbar_style_name,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        ).grid(row=12, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        ttk.Label(self, textvariable=self.progress_text_var).grid(
            row=13,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Progressbar(
            self,
            style=self._cleanup_progressbar_style_name,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.cleanup_progress_var,
        ).grid(row=14, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        ttk.Label(self, textvariable=self.cleanup_progress_text_var).grid(
            row=15,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Label(self, text="日志：").grid(row=16, column=0, sticky="w")
        log_toolbar = ttk.Frame(self)
        log_toolbar.grid(row=16, column=1, columnspan=3, sticky="e")
        ttk.Label(log_toolbar, text="级别：").pack(side=tk.LEFT)
        self.log_level_box = ttk.Combobox(
            log_toolbar,
            textvariable=self.log_level_filter_var,
            values=["全部", "信息", "成功", "警告", "失败", "调试"],
            state="readonly",
            width=8,
        )
        self.log_level_box.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(log_toolbar, text="任务：").pack(side=tk.LEFT)
        self.log_task_box = ttk.Combobox(
            log_toolbar,
            textvariable=self.log_task_filter_var,
            values=["全部任务", "全局"],
            state="readonly",
            width=12,
        )
        self.log_task_box.pack(side=tk.LEFT, padx=(4, 8))
        self.only_failed_btn = ttk.Button(log_toolbar, text="仅失败", command=self.filter_only_failed_logs)
        self.only_failed_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.copy_error_btn = ttk.Button(log_toolbar, text="复制错误", command=self.copy_error_logs)
        self.copy_error_btn.pack(side=tk.LEFT)

        self.log_box = scrolledtext.ScrolledText(self, height=12, state=tk.DISABLED)
        self.log_box.grid(row=17, column=0, columnspan=4, sticky="nsew", pady=(4, 8))
        self.rowconfigure(17, weight=1)
        self.log_box.tag_configure("success", foreground="#1f7a1f")
        self.log_box.tag_configure("warning", foreground="#b36b00")
        self.log_box.tag_configure("error", foreground="#cc0000")
        self.log_box.tag_configure("debug", foreground="#6b6b6b")

        self.log_level_var_trace = self.log_level_filter_var.trace_add("write", self._on_log_filter_change)
        self.log_task_var_trace = self.log_task_filter_var.trace_add("write", self._on_log_filter_change)

        ttk.Label(self, textvariable=self.status_var).grid(row=18, column=0, columnspan=2, sticky="w")
        ttk.Label(self, textvariable=self.dependency_status_var).grid(row=18, column=2, columnspan=2, sticky="e")
        ttk.Label(self, textvariable=self.drag_runtime_status_var).grid(row=19, column=0, columnspan=2, sticky="w")
        ttk.Label(self, textvariable=self.delete_scope_status_var).grid(row=19, column=2, columnspan=2, sticky="e")

    def _make_classic_checkbutton(self, **kwargs: object) -> tk.Checkbutton:
        check = tk.Checkbutton(self, anchor="w", indicatoron=True, takefocus=True, **kwargs)
        self._classic_checkbuttons.append(check)
        return check

    @staticmethod
    def _format_checkbutton_text(base_text: str, enabled: bool) -> str:
        return f"{base_text}（{'已开启' if enabled else '未开启'}）"

    def _make_stateful_classic_checkbutton(
        self,
        base_text: str,
        variable: tk.Variable,
        onvalue: object = True,
        offvalue: object = False,
        command: Callable[[], None] | None = None,
    ) -> tk.Checkbutton:
        def update_text() -> None:
            current = variable.get()
            check.configure(text=self._format_checkbutton_text(base_text, current == onvalue))

        def on_toggle() -> None:
            update_text()
            if command is not None:
                command()

        check = self._make_classic_checkbutton(
            text=base_text,
            variable=variable,
            onvalue=onvalue,
            offvalue=offvalue,
            command=on_toggle,
        )
        variable.trace_add("write", lambda *_args: update_text())
        update_text()
        return check

    def _update_folder_option_state(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.recursive_check.configure(state=state)
        self.first_only_check.configure(state=state)

    def _bind_live_validation(self) -> None:
        self.source_var.trace_add("write", self._on_input_change)
        self.output_var.trace_add("write", self._on_input_change)
        self.output_name_var.trace_add("write", self._on_input_change)

    def _on_input_change(self, *_args: object) -> None:
        self._refresh_action_state()

    def _refresh_action_state(self) -> None:
        has_source = bool(self.local_files) or bool(self.source_var.get().strip())
        has_output = bool(self.output_var.get().strip())
        can_start = has_source and has_output and not self._working
        self.convert_btn.configure(state=tk.NORMAL if can_start else tk.DISABLED)
        self.preview_btn.configure(state=tk.NORMAL if can_start else tk.DISABLED)

    @staticmethod
    def _parse_drop_data(raw_data: str) -> list[str]:
        # tkinterdnd2 may wrap paths in braces to preserve spaces.
        tokens = re.findall(r"\{[^}]*\}|\"[^\"]*\"|\S+", raw_data)
        items: list[str] = []
        for token in tokens:
            value = token.strip().strip("{}\"").strip()
            if value:
                items.append(value)
        return items

    @staticmethod
    def _to_preview_text(markdown_text: str) -> str:
        text = markdown_text.replace("\r\n", "\n")
        text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0).replace("```", ""), text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*[-*]\s+", "- ", text, flags=re.MULTILINE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "（暂无内容）"

    @staticmethod
    def _format_help_preview(title: str, markdown_content: str) -> str:
        body = ConverterApp._to_preview_text(markdown_content)
        lines = [
            f"【{title}】",
            "",
            "----------------------------------------",
            "",
            body,
        ]
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _split_help_cards(markdown_content: str) -> list[tuple[str, str]]:
        cards: list[tuple[str, str]] = []
        current_title = "本节说明"
        current_lines: list[str] = []

        for line in markdown_content.splitlines():
            stripped = line.lstrip()
            heading_match = re.match(r"^(#{3,6})\s+(.+)$", stripped)
            if heading_match:
                if current_lines:
                    cards.append((current_title, ConverterApp._to_preview_text("\n".join(current_lines))))
                current_title = heading_match.group(2).strip()
                current_lines = []
                continue
            current_lines.append(line)

        if current_lines or not cards:
            cards.append((current_title, ConverterApp._to_preview_text("\n".join(current_lines))))

        return [(title, body or "（暂无内容）") for title, body in cards]

    @staticmethod
    def _build_drag_runtime_status_text(
        drag_enabled: bool,
        drag_runtime_enabled: bool,
        tkinterdnd2_available: bool,
    ) -> str:
        if not drag_enabled:
            return "拖放状态：未启用"
        if drag_runtime_enabled:
            return "拖放状态：已启用"
        if tkinterdnd2_available:
            return "拖放状态：依赖已安装，重启后启用"
        return "拖放状态：依赖缺失（tkinterdnd2）"

    def _setup_drag_drop(self) -> None:
        if not self.config_model.enable_drag_drop:
            self.drag_drop_runtime_enabled = False
            self._refresh_drag_runtime_status()
            return

        support_widgets: list[object] = [self, self.source_entry]
        enabled_count = 0
        for widget in support_widgets:
            if not hasattr(widget, "drop_target_register") or not hasattr(widget, "dnd_bind"):
                continue
            try:
                widget.drop_target_register("DND_Files", "DND_Text")
                widget.dnd_bind("<<Drop>>", self._on_drop)
                enabled_count += 1
            except Exception:
                continue

        if enabled_count > 0:
            self.drag_drop_runtime_enabled = True
            self._append_log("拖放已启用：可将 m3u8 文件、文件夹或 URL 拖入输入框。")
        else:
            self.drag_drop_runtime_enabled = False
            self._append_log("拖放开关已开启，但当前环境未启用 tkinterdnd2。")
        self._refresh_drag_runtime_status()

    def _on_drop(self, event: object) -> str:
        if self._working:
            return "break"

        raw_data = str(getattr(event, "data", "") or "").strip()
        dropped_items = self._parse_drop_data(raw_data)
        if not dropped_items:
            return "break"

        if len(dropped_items) == 1 and self._is_url_source(dropped_items[0]):
            self.local_files = []
            self.source_var.set(dropped_items[0])
            self._update_folder_option_state(False)
            self._refresh_action_state()
            self._append_log(f"已拖入 URL：{dropped_items[0]}")
            self._quick_check_source()
            return "break"

        local_files: list[str] = []
        folder_candidate: str | None = None
        for item in dropped_items:
            path = Path(item).expanduser()
            if not path.exists():
                continue
            if path.is_dir() and folder_candidate is None:
                folder_candidate = str(path.resolve())
                continue
            if path.is_file() and path.suffix.lower() == ".m3u8":
                local_files.append(str(path.resolve()))

        if local_files:
            self.local_files = local_files
            first_name = Path(local_files[0]).name
            self.source_var.set(f"已拖入 {len(local_files)} 个文件，首个：{first_name}")
            self._update_folder_option_state(False)
            self._refresh_action_state()
            self._append_log(f"拖放导入文件成功：{len(local_files)} 个 m3u8")
            self._quick_check_source()
            self.config_model.input_mode = "local"
            self._save_config()
            return "break"

        if folder_candidate is not None:
            self.local_files = []
            self.source_var.set(folder_candidate)
            self._update_folder_option_state(True)
            self._refresh_action_state()
            self._append_log(f"拖放导入文件夹：{folder_candidate}")
            self._quick_check_source()
            self.config_model.input_mode = "folder"
            self._save_config()
            return "break"

        messagebox.showwarning("拖放失败", "仅支持拖入 .m3u8 文件、包含 m3u8 的文件夹或 URL。")
        self._append_log("拖放内容无效：未识别到 m3u8 文件/文件夹/URL。")
        return "break"

    @staticmethod
    def _is_url_source(source: str) -> bool:
        parsed = urlparse(source)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _is_local_file_source(self, source: str) -> bool:
        if self._is_url_source(source):
            return False
        path = Path(source)
        return path.exists() and path.is_file()

    @staticmethod
    def _extract_uri_from_tag(line: str) -> str | None:
        match = re.search(r'URI="([^"]+)"', line)
        if not match:
            return None
        uri = match.group(1).strip()
        return uri or None

    @staticmethod
    def _is_path_under(base: Path, target: Path) -> bool:
        try:
            target.relative_to(base)
            return True
        except ValueError:
            return False

    @staticmethod
    def _resolve_local_m3u8_reference(base_playlist: Path, raw_uri: str) -> Path | None:
        uri = raw_uri.strip()
        if not uri:
            return None

        if uri.lower().startswith(("http://", "https://")):
            return None

        if uri.lower().startswith("file://"):
            local = unquote(uri[7:])
            if not local:
                return None
            return Path(local).expanduser().resolve()

        parsed = urlparse(uri)
        if parsed.scheme and parsed.netloc:
            return None

        candidate = Path(uri).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()

        relative_path = parsed.path or uri
        relative_path = relative_path.split("?", 1)[0].split("#", 1)[0].strip()
        if not relative_path:
            return None
        return (base_playlist.parent / relative_path).resolve()

    @classmethod
    def _collect_related_source_files(cls, source_file: Path) -> set[Path]:
        root_playlist = source_file.expanduser().resolve()
        if not root_playlist.exists() or not root_playlist.is_file():
            return set()

        related_files: set[Path] = {root_playlist}
        visited_playlists: set[Path] = set()
        pending_playlists: list[Path] = [root_playlist]
        scope_root = root_playlist.parent

        while pending_playlists:
            playlist = pending_playlists.pop()
            if playlist in visited_playlists:
                continue
            visited_playlists.add(playlist)
            if not playlist.exists() or not playlist.is_file():
                continue

            try:
                text = playlist.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                ref_uri: str | None = None
                if line.startswith("#"):
                    if "URI=" in line:
                        ref_uri = cls._extract_uri_from_tag(line)
                else:
                    ref_uri = line

                if not ref_uri:
                    continue
                resolved = cls._resolve_local_m3u8_reference(playlist, ref_uri)
                if resolved is None:
                    continue
                if not cls._is_path_under(scope_root, resolved):
                    continue
                if not resolved.exists() or not resolved.is_file():
                    continue

                related_files.add(resolved)
                if resolved.suffix.lower() == ".m3u8":
                    pending_playlists.append(resolved)

        return related_files

    @classmethod
    def _collect_cleanup_dirs(cls, root_playlist: Path, related_files: set[Path]) -> list[Path]:
        scope_root = root_playlist.parent.resolve()
        dirs: set[Path] = {scope_root}
        for file_path in related_files:
            current = file_path.parent.resolve()
            while cls._is_path_under(scope_root, current):
                dirs.add(current)
                if current == scope_root:
                    break
                current = current.parent
        return sorted(dirs, key=lambda item: len(item.parts), reverse=True)

    @classmethod
    def _collect_companion_config_files(cls, root_playlist: Path, related_files: set[Path]) -> set[Path]:
        root_dir = root_playlist.parent.resolve()
        companions: set[Path] = set()
        for entry in root_dir.iterdir():
            if not entry.is_file():
                continue
            resolved = entry.resolve()
            if resolved in related_files:
                continue
            name = resolved.name.lower()
            stem = resolved.stem.lower()
            suffix = resolved.suffix.lower()
            if name == "config" or name.startswith("config."):
                companions.add(resolved)
                continue
            if stem in {"config", "settings", "setting", "cfg", "keyinfo"}:
                companions.add(resolved)
                continue
            if suffix in {".keyinfo", ".cfg", ".conf", ".config"}:
                companions.add(resolved)
                continue
        return companions

    @classmethod
    def _try_fast_delete_parent_dir(
        cls,
        source_path: Path,
        related_files: set[Path],
        use_recycle: bool,
        force_delete_any: bool = False,
    ) -> tuple[int, int] | None:
        root_dir = source_path.parent.resolve()
        if not root_dir.exists() or not root_dir.is_dir():
            return None

        files_on_disk: set[Path] = set()
        dir_count = 0
        for current_root, dir_names, file_names in os.walk(root_dir):
            current_dir = Path(current_root).resolve()
            if not cls._is_path_under(root_dir, current_dir):
                return None
            dir_count += len(dir_names)
            for file_name in file_names:
                files_on_disk.add((current_dir / file_name).resolve())

        if not files_on_disk:
            return None
        if not force_delete_any and not files_on_disk.issubset(related_files):
            return None

        try:
            if use_recycle and send2trash is not None:
                send2trash(str(root_dir))
            else:
                shutil.rmtree(root_dir)
        except OSError:
            return None
        return len(files_on_disk), dir_count + 1

    @staticmethod
    def _normalize_delete_scope_mode(value: str) -> str:
        return value if value in {"playlist_only", "with_related_files", "with_related_and_dirs"} else "with_related_and_dirs"

    def _resolve_delete_scope_mode(self) -> str:
        configured = self._normalize_delete_scope_mode(getattr(self.config_model, "delete_scope_mode", ""))
        if configured != "with_related_and_dirs" or getattr(self.config_model, "delete_scope_mode", ""):
            return configured

        # 兼容旧配置：由两个布尔值推导删除策略。
        include_related = getattr(self.config_model, "delete_include_related_files", True)
        cleanup_dirs = getattr(self.config_model, "delete_cleanup_empty_dirs", True)
        if not include_related:
            return "playlist_only"
        if not cleanup_dirs:
            return "with_related_files"
        return "with_related_and_dirs"

    def _delete_scope_flags(self) -> tuple[bool, bool]:
        mode = self._normalize_delete_scope_mode(self.delete_scope_var.get())
        if mode == "playlist_only":
            return False, False
        if mode == "with_related_files":
            return True, False
        return True, True

    @staticmethod
    def _is_high_risk_delete_scope(mode: str) -> bool:
        return ConverterApp._normalize_delete_scope_mode(mode) == "with_related_and_dirs"

    def _refresh_delete_scope_status(self) -> None:
        self.delete_scope_status_var.set(f"删除范围：{self._delete_scope_label()}")

    def _delete_scope_label(self) -> str:
        mode = self._normalize_delete_scope_mode(self.delete_scope_var.get())
        if mode == "playlist_only":
            return "仅 m3u8 文件（最保守）"
        if mode == "with_related_and_dirs":
            return "m3u8 + 关联分片/KEY/子清单 + 空目录（最彻底）"
        return "m3u8 + 关联分片/KEY/子清单（推荐）"

    def _build_delete_preview_text(self, sources: list[tuple[str, str | None, Path | None]]) -> str:
        include_related_files, cleanup_empty_dirs = self._delete_scope_flags()
        local_sources = [source for source, _, _ in sources if self._is_local_file_source(source)]

        all_files: set[Path] = set()
        all_dirs: set[Path] = set()
        for source in local_sources:
            src_path = Path(source).expanduser().resolve()
            if not src_path.exists() or not src_path.is_file():
                continue
            if include_related_files:
                related = self._collect_related_source_files(src_path)
                related.update(self._collect_companion_config_files(src_path, related))
            else:
                related = {src_path}
            all_files.update(path for path in related if path.exists() and path.is_file())
            if include_related_files and cleanup_empty_dirs and not self.delete_to_recycle_var.get():
                all_dirs.update(self._collect_cleanup_dirs(src_path, related))

        sample_files = sorted(all_files, key=lambda p: str(p))[:12]
        lines = [
            f"本次删除策略：{self._delete_scope_label()}",
            f"删除方式：{'回收站' if self.delete_to_recycle_var.get() and send2trash is not None else '永久删除'}",
            f"本地输入数量：{len(local_sources)}",
            f"预计删除文件：{len(all_files)} 个",
            f"预计清理空目录候选：{len(all_dirs)} 个",
            "",
            "删除文件示例（最多 12 条）：",
        ]
        if include_related_files and cleanup_empty_dirs and not self.delete_to_recycle_var.get():
            lines.insert(5, "性能优化：若目录仅含可识别关联文件，将直接删除 m3u8 上级目录。")
        if sample_files:
            lines.extend(f"- {item}" for item in sample_files)
        else:
            lines.append("- （未识别到可删除文件）")
        return "\n".join(lines)

    @staticmethod
    def _build_cleanup_verify_note(source_path: Path) -> str:
        parent = source_path.parent.resolve()
        if not parent.exists():
            return "核验：上级目录已清理完成（目录不存在）"

        try:
            remaining = list(parent.iterdir())
        except OSError as exc:
            return f"核验：无法读取上级目录（{exc}）"

        if not remaining:
            return "核验：上级目录为空（可手动删除）"

        sample = ", ".join(item.name for item in sorted(remaining, key=lambda p: p.name.lower())[:3])
        more = "" if len(remaining) <= 3 else " ..."
        return f"核验：上级目录仍有 {len(remaining)} 项 -> {sample}{more}"

    @staticmethod
    def _is_cleanup_verify_success(note: str) -> bool:
        text = note.strip()
        return text.startswith("核验：上级目录已清理完成") or text.startswith("核验：上级目录为空")

    def clear_source(self) -> None:
        self.local_files = []
        self.source_var.set("")
        self._update_folder_option_state(False)
        self._refresh_action_state()

    def _detect_source_kind(self) -> str:
        preference = self.smart_select_preference_var.get()
        if preference in {"file", "folder"}:
            return preference

        source = self.source_var.get().strip()
        if self.local_files:
            return "file"
        if not source:
            if self.config_model.input_mode == "folder" or self.folder_recursive_var.get():
                return "folder"
            return "file"

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return "url"

        path = Path(source).expanduser()
        if path.exists() and path.is_dir():
            return "folder"
        return "file"

    def _delete_source_file(
        self,
        file_path: str,
        include_related_files: bool = True,
        cleanup_empty_dirs: bool = True,
        allow_fast_parent_remove: bool = False,
        force_remove_parent_dir: bool = False,
    ) -> tuple[int, int]:
        source_path = Path(file_path).expanduser().resolve()
        related_files = (
            self._collect_related_source_files(source_path)
            if include_related_files
            else ({source_path} if source_path.exists() and source_path.is_file() else set())
        )
        if include_related_files and source_path.exists() and source_path.is_file():
            related_files.update(self._collect_companion_config_files(source_path, related_files))
        if not related_files and source_path.exists() and source_path.is_file():
            related_files = {source_path}

        use_recycle = self.delete_to_recycle_var.get() and send2trash is not None
        if allow_fast_parent_remove and include_related_files and cleanup_empty_dirs:
            fast_deleted = self._try_fast_delete_parent_dir(
                source_path,
                related_files,
                use_recycle=use_recycle,
                force_delete_any=force_remove_parent_dir,
            )
            if fast_deleted is not None:
                return fast_deleted

        deleted_file_count = 0
        for target in sorted(related_files, key=lambda item: len(item.parts), reverse=True):
            if not target.exists() or not target.is_file():
                continue
            if use_recycle:
                send2trash(str(target))
            else:
                target.unlink()
            deleted_file_count += 1

        deleted_dir_count = 0
        if include_related_files and cleanup_empty_dirs:
            for folder in self._collect_cleanup_dirs(source_path, related_files):
                if not folder.exists() or not folder.is_dir():
                    continue
                try:
                    folder.rmdir()
                    deleted_dir_count += 1
                except OSError:
                    continue

        return deleted_file_count, deleted_dir_count

    def _quick_check_source(self) -> None:
        try:
            collected = self._collect_sources()
            self._append_log(f"输入源检测通过，共识别 {len(collected)} 个任务")
        except InvalidInputError as exc:
            self._append_log(f"输入源检测失败：{exc}")

    @staticmethod
    def _suggest_name_for_m3u8(file_path: Path) -> str:
        generic_names = {"video", "index", "playlist", "master"}
        if file_path.stem.lower() in generic_names and file_path.parent.name:
            return file_path.parent.name
        return file_path.stem

    @staticmethod
    def _suggest_name_for_url(source: str) -> str | None:
        parsed = urlparse(source)
        stem = Path(parsed.path).stem
        if not stem or stem.lower() in {"video", "index", "playlist", "master"}:
            return parsed.netloc.replace(":", "_") or None
        return None

    def select_source_auto(self) -> None:
        source_kind = self._detect_source_kind()

        if source_kind in {"file", "url"}:
            file_paths = filedialog.askopenfilenames(
                title="选择一个或多个 m3u8 文件",
                filetypes=[("M3U8 文件", "*.m3u8"), ("所有文件", "*.*")],
            )
            if not file_paths:
                return
            self.local_files = list(file_paths)
            first_name = Path(self.local_files[0]).name
            self.source_var.set(f"已选择 {len(self.local_files)} 个文件，首个：{first_name}")
            self._update_folder_option_state(False)
            self._quick_check_source()
            self.config_model.input_mode = "local"
            self._save_config()
            self._refresh_action_state()
            return

        folder = filedialog.askdirectory(title="选择包含 m3u8 的文件夹")
        if not folder:
            return
        self.local_files = []
        self.source_var.set(folder)
        self._update_folder_option_state(True)
        self._quick_check_source()
        self.config_model.input_mode = "folder"
        self._save_config()
        self._refresh_action_state()

    def select_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_var.set(folder)
            self._refresh_action_state()

    def _compose_output_name(self, core_name: str | None) -> str | None:
        base = (core_name or "").strip()
        if not base:
            return None
        prefix = self.output_prefix_var.get().strip()
        suffix = self.output_suffix_var.get().strip()
        if self.output_timestamp_var.get():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"{prefix}{base}{suffix}_{stamp}"
        return f"{prefix}{base}{suffix}"

    def open_naming_settings(self) -> None:
        window = tk.Toplevel(self.master)
        window.title("输出格式与命名规则")
        window.geometry("520x260")
        window.transient(self.master)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        format_var = tk.StringVar(value=self.output_format_var.get())
        prefix_var = tk.StringVar(value=self.output_prefix_var.get())
        suffix_var = tk.StringVar(value=self.output_suffix_var.get())
        timestamp_var = tk.BooleanVar(value=self.output_timestamp_var.get())

        ttk.Label(frame, text="输出格式：").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Combobox(frame, textvariable=format_var, values=["mp4", "mov", "avi"], state="readonly", width=12).grid(
            row=0,
            column=1,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Label(frame, text="文件名前缀（批量可用）：").grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Entry(frame, textvariable=prefix_var).grid(row=1, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="文件名后缀（批量可用）：").grid(row=2, column=0, sticky="w", pady=(0, 8))
        ttk.Entry(frame, textvariable=suffix_var).grid(row=2, column=1, sticky="ew", pady=(0, 8))

        ttk.Checkbutton(frame, text="按时间戳命名（追加 YYYYMMDD_HHMMSS）", variable=timestamp_var).grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 10),
        )

        btns = ttk.Frame(frame)
        btns.grid(row=4, column=0, columnspan=2, sticky="e")

        def on_save() -> None:
            self.output_format_var.set(format_var.get().strip().lower() or "mp4")
            self.output_prefix_var.set(prefix_var.get())
            self.output_suffix_var.set(suffix_var.get())
            self.output_timestamp_var.set(timestamp_var.get())
            self._save_config()
            self._append_log("已更新输出格式与命名规则。", level="INFO", task="全局")
            window.destroy()

        ttk.Button(btns, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="保存", command=on_save).pack(side=tk.RIGHT, padx=(0, 8))

    def run_integrity_check(self) -> None:
        try:
            sources = self._collect_sources()
        except InvalidInputError as exc:
            messagebox.showwarning("校验失败", str(exc))
            return

        reports = []
        for source, _name, _subdir in sources:
            try:
                report = check_integrity(source)
                reports.append(report)
            except Exception as exc:
                self._append_log(f"完整性校验异常：{source} -> {exc}", level="ERROR", task="全局")

        if not reports:
            messagebox.showwarning("校验结果", "没有可用的 m3u8 校验结果。")
            return

        for report in reports:
            encrypt_text = "是" if report.encrypted else "否"
            missing_count = len(report.missing_segments)
            level = "WARNING" if missing_count > 0 else "SUCCESS"
            self._append_log(
                f"完整性校验：{report.source} | 缺失 {missing_count} | 加密 {encrypt_text}",
                level=level,
                task="全局",
            )
        self._open_integrity_report_window(reports)

    @staticmethod
    def _check_resource_reachable(uri: str) -> tuple[bool, str]:
        parsed = urlparse(uri)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            try:
                request = Request(uri, headers={"User-Agent": "m3u8ToMp4/1.0"})
                with urlopen(request, timeout=6) as response:  # nosec B310
                    status = int(getattr(response, "status", 200))
                    if 200 <= status < 400:
                        return True, f"HTTP {status}"
                    return False, f"HTTP {status}"
            except Exception as exc:
                return False, str(exc)

        path = Path(uri).expanduser()
        if path.exists():
            return True, "本地文件存在"
        return False, "本地文件不存在"

    @staticmethod
    def _build_quick_check_report_text(
        source: str,
        segment_count: int,
        encrypted: bool,
        method: str,
        key_uri: str,
        first_segment_uri: str,
        segment_status: str,
        key_status: str,
    ) -> str:
        return (
            "快速自查结果\n\n"
            f"输入源：{source}\n"
            f"分片数量：{segment_count}\n"
            f"加密状态：{'是' if encrypted else '否'}\n"
            f"加密方式：{method or '无'}\n"
            f"KEY 地址：{key_uri or '无'}\n"
            f"首分片：{first_segment_uri or '无'}\n"
            f"首分片可达性：{segment_status}\n"
            f"KEY 可达性：{key_status}\n"
        )

    def run_quick_self_check(self) -> None:
        try:
            sources = self._collect_sources()
        except InvalidInputError as exc:
            messagebox.showwarning("快速自查", str(exc))
            return

        if not sources:
            messagebox.showwarning("快速自查", "未找到可检查的输入源。")
            return

        source = sources[0][0]
        try:
            parsed = parse_m3u8(source)
        except Exception as exc:
            messagebox.showwarning("快速自查", f"清单解析失败：{exc}")
            self._append_log(f"快速自查失败：{exc}", level="ERROR", task="全局")
            return

        first_segment_uri = ""
        segment_status = "无分片"
        if parsed.segments:
            first_segment_uri = parsed.segments[0]
            first_segment_resolved = (
                first_segment_uri
                if self._is_url_source(first_segment_uri)
                else str((Path(source).expanduser().resolve().parent / first_segment_uri).resolve())
                if not self._is_url_source(source)
                else urljoin(source, first_segment_uri)
            )
            seg_ok, seg_detail = self._check_resource_reachable(first_segment_resolved)
            segment_status = f"{'可达' if seg_ok else '不可达'}（{seg_detail}）"

        key_status = "无密钥"
        if parsed.encrypted and parsed.key_uri:
            key_ok, key_detail = self._check_resource_reachable(parsed.key_uri)
            key_status = f"{'可达' if key_ok else '不可达'}（{key_detail}）"

        report = self._build_quick_check_report_text(
            source=source,
            segment_count=len(parsed.segments),
            encrypted=parsed.encrypted,
            method=parsed.encryption_method,
            key_uri=parsed.key_uri,
            first_segment_uri=first_segment_uri,
            segment_status=segment_status,
            key_status=key_status,
        )
        self._append_log("已完成快速自查。", level="SUCCESS", task="全局")
        messagebox.showinfo("快速自查", report)

    @staticmethod
    def _format_integrity_report_text(reports: list[object]) -> str:
        lines: list[str] = []
        for report in reports:
            # duck typing to avoid tight coupling for tests
            source = str(getattr(report, "source", ""))
            encrypted = bool(getattr(report, "encrypted", False))
            method = str(getattr(report, "encryption_method", ""))
            key_uri = str(getattr(report, "key_uri", ""))
            checked_segments = int(getattr(report, "checked_segments", 0))
            total_segments = int(getattr(report, "total_segments", 0))
            missing_segments = list(getattr(report, "missing_segments", []))
            lines.append(
                f"源：{source}\n"
                f"- 加密：{'是' if encrypted else '否'} {method}\n"
                f"- KEY：{key_uri or '无'}\n"
                f"- 分片：{checked_segments}/{total_segments}\n"
                f"- 缺失数量：{len(missing_segments)}"
            )
            if missing_segments:
                lines.append("- 缺失明细：")
                lines.extend([f"  * {item}" for item in missing_segments])
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _open_integrity_report_window(self, reports: list[object]) -> None:
        window = tk.Toplevel(self.master)
        window.title("m3u8 完整性校验结果")
        window.geometry("860x560")
        window.transient(self.master)

        frame = ttk.Frame(window, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(1, weight=1)

        tree = ttk.Treeview(frame, columns=("encrypt", "segments", "missing"), show="tree headings")
        tree.heading("#0", text="输入源")
        tree.heading("encrypt", text="加密")
        tree.heading("segments", text="分片")
        tree.heading("missing", text="缺失")
        tree.column("#0", width=420)
        tree.column("encrypt", width=110, anchor="center")
        tree.column("segments", width=110, anchor="center")
        tree.column("missing", width=90, anchor="center")
        tree.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        right_panel = ttk.Frame(frame)
        right_panel.grid(row=1, column=1, sticky="nsew")
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(1, weight=1)

        ttk.Label(right_panel, text="缺失分片明细：").grid(row=0, column=0, sticky="w", pady=(0, 6))
        missing_box = scrolledtext.ScrolledText(right_panel, state=tk.DISABLED, wrap=tk.WORD)
        missing_box.grid(row=1, column=0, sticky="nsew")

        report_map: dict[str, object] = {}
        for idx, report in enumerate(reports):
            missing_segments = list(getattr(report, "missing_segments", []))
            item_id = tree.insert(
                "",
                tk.END,
                text=str(getattr(report, "source", "")),
                values=(
                    "是" if bool(getattr(report, "encrypted", False)) else "否",
                    f"{int(getattr(report, 'checked_segments', 0))}/{int(getattr(report, 'total_segments', 0))}",
                    str(len(missing_segments)),
                ),
            )
            report_map[item_id] = report
            if idx == 0:
                tree.selection_set(item_id)

        def render_missing(item_id: str) -> None:
            report = report_map.get(item_id)
            missing_segments = list(getattr(report, "missing_segments", [])) if report is not None else []
            missing_box.configure(state=tk.NORMAL)
            missing_box.delete("1.0", tk.END)
            if not missing_segments:
                missing_box.insert(tk.END, "未发现缺失分片。")
            else:
                missing_box.insert(tk.END, "\n".join(missing_segments))
            missing_box.configure(state=tk.DISABLED)

        def on_select(_event: object | None = None) -> None:
            selected = tree.selection()
            if not selected:
                return
            render_missing(selected[0])

        tree.bind("<<TreeviewSelect>>", on_select)
        selected_items = tree.selection()
        if selected_items:
            render_missing(selected_items[0])

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def export_report() -> None:
            file_path = filedialog.asksaveasfilename(
                title="导出完整性报告",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
            if not file_path:
                return
            content = self._format_integrity_report_text(reports)
            Path(file_path).write_text(content, encoding="utf-8")
            self._append_log(f"完整性报告已导出：{file_path}", level="SUCCESS", task="全局")

        ttk.Button(btns, text="导出报告", command=export_report).pack(side=tk.RIGHT)
        ttk.Button(btns, text="关闭", command=window.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def preview_first_segment(self) -> None:
        try:
            sources = self._collect_sources()
        except InvalidInputError as exc:
            messagebox.showwarning("预览失败", str(exc))
            return
        if not sources:
            messagebox.showwarning("预览失败", "未找到可预览的输入源。")
            return

        source = sources[0][0]
        preview_source = source
        guessed_playlist = self._guess_m3u8_from_segment_source(source)
        if guessed_playlist:
            preview_source = guessed_playlist
            self._append_log(
                f"检测到分片地址，预览目标已自动切换为清单：{preview_source}",
                level="WARNING",
                task="全局",
            )
        try:
            segment = get_first_segment(preview_source)
        except Exception as exc:
            messagebox.showwarning("预览失败", f"无法解析 m3u8：{exc}")
            self._append_log(f"首片段预览失败：{exc}", level="ERROR", task="全局")
            return
        if segment is None:
            messagebox.showwarning("预览失败", "当前 m3u8 未找到可预览分片。")
            return

        ffplay_bin = shutil.which("ffplay")
        if ffplay_bin:
            try:
                # 对加密流，直接播放分片会失败，优先让 ffplay 打开 m3u8 清单。
                ffplay_args = [ffplay_bin, "-autoexit", "-t", "8"]
                source_path = Path(preview_source)
                if source_path.suffix.lower() == ".m3u8":
                    ffplay_args.extend(["-allowed_extensions", "ALL"])
                ffplay_args.append(preview_source)
                subprocess.Popen(ffplay_args)
                self._append_log(f"已启动 m3u8 预览：{preview_source}", level="INFO", task="全局")
                return
            except OSError as exc:
                self._append_log(f"启动 ffplay 预览失败：{exc}", level="ERROR", task="全局")

        # ffplay 不可用时，回退为 ffmpeg 生成 5 秒临时视频。
        temp_preview = self._build_preview_temp_mp4(preview_source)
        if temp_preview is not None:
            if self._open_with_system_default(temp_preview):
                self._append_log(f"已生成并打开临时预览：{temp_preview}", level="INFO", task="全局")
                messagebox.showinfo("首分片预览", f"已生成 5 秒临时预览：\n{temp_preview}")
                return
            messagebox.showinfo("首分片预览", f"已生成临时预览，请手动打开：\n{temp_preview}")
            return

        messagebox.showinfo(
            "首分片预览",
            "当前环境未检测到 ffplay，且无法生成临时预览。\n\n"
            f"首分片路径：\n{segment.resolved_uri}",
        )

    @staticmethod
    def _guess_m3u8_from_segment_source(source: str) -> str | None:
        parsed = urlparse(source.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            path = parsed.path or ""
            if not path.lower().endswith((".ts", ".m4s", ".aac")):
                return None
            playlist_path = str(Path(path).with_name("index.m3u8")).replace("\\", "/")
            return urlunparse((parsed.scheme, parsed.netloc, playlist_path, "", "", ""))

        local_path = Path(source).expanduser()
        if local_path.suffix.lower() not in {".ts", ".m4s", ".aac"}:
            return None
        return str(local_path.with_name("index.m3u8"))

    def _build_preview_temp_mp4(self, source: str) -> str | None:
        ffmpeg_bin = self.ffmpeg_var.get().strip() or "ffmpeg"
        ffmpeg_path = shutil.which(ffmpeg_bin) if ffmpeg_bin != "ffmpeg" else shutil.which("ffmpeg")
        if not ffmpeg_path and Path(ffmpeg_bin).exists():
            ffmpeg_path = ffmpeg_bin
        if not ffmpeg_path:
            return None

        preview_dir = self._preview_temp_dir()
        preview_dir.mkdir(parents=True, exist_ok=True)
        output_file = preview_dir / f"preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

        command = [
            str(ffmpeg_path),
            "-y",
            "-i",
            source,
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(output_file),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            self._append_log(f"生成临时预览失败：{exc}", level="ERROR", task="全局")
            return None
        if result.returncode != 0 or not output_file.exists():
            details = result.stderr.strip() or result.stdout.strip() or "未知错误"
            self._append_log(f"生成临时预览失败：{details}", level="ERROR", task="全局")
            return None
        self.preview_temp_files.add(str(output_file))
        return str(output_file)

    @staticmethod
    def _preview_temp_dir() -> Path:
        return Path(tempfile.gettempdir()) / "m3u8ToMp4_preview"

    def _cleanup_preview_temp_files(self) -> int:
        removed = 0
        for file_path in list(self.preview_temp_files):
            try:
                path = Path(file_path)
                if path.exists():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        self.preview_temp_files.clear()
        return removed

    def cleanup_preview_temp_files_manually(self) -> None:
        removed = self._cleanup_preview_temp_files()
        if removed > 0:
            self._append_log(f"已手动清理临时预览文件：{removed} 个", level="SUCCESS", task="全局")
            messagebox.showinfo("清理完成", f"已清理 {removed} 个临时预览文件。")
            return
        self._append_log("当前没有可清理的临时预览文件。", level="INFO", task="全局")
        messagebox.showinfo("清理完成", "当前没有可清理的临时预览文件。")

    @staticmethod
    def _open_with_system_default(file_path: str) -> bool:
        try:
            if sys.platform.startswith("win"):
                os.startfile(file_path)  # type: ignore[attr-defined]
                return True
            if sys.platform == "darwin":
                subprocess.Popen(["open", file_path])
                return True
            subprocess.Popen(["xdg-open", file_path])
            return True
        except Exception:
            return False

    def open_decrypt_settings(self) -> None:
        window = tk.Toplevel(self.master)
        window.title("加密解密设置")
        window.geometry("560x280")
        window.transient(self.master)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        auto_var = tk.BooleanVar(value=self.decrypt_auto_parse_var.get())
        key_var = tk.StringVar(value=self.decrypt_key_var.get())
        iv_var = tk.StringVar(value=self.decrypt_iv_var.get())

        ttk.Checkbutton(
            frame,
            text="自动解析 m3u8 中的 AES-128 KEY URL",
            variable=auto_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="手动 KEY（16字节十六进制，可选）：").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=key_var).grid(row=1, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="手动 IV（16字节十六进制，可选）：").grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=iv_var).grid(row=2, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(
            frame,
            text="说明：优先使用手动 KEY/IV；留空时由 FFmpeg 按 m3u8 内 KEY URI 自动获取。",
            foreground="#666666",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 12))

        btns = ttk.Frame(frame)
        btns.grid(row=4, column=0, columnspan=2, sticky="e")

        def on_save() -> None:
            self.decrypt_auto_parse_var.set(auto_var.get())
            self.decrypt_key_var.set(key_var.get().strip())
            self.decrypt_iv_var.set(iv_var.get().strip())
            self._save_config()
            self._append_log("已更新加密解密设置。")
            window.destroy()

        ttk.Button(btns, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="保存", command=on_save).pack(side=tk.RIGHT, padx=(0, 8))

    def open_custom_transcode_settings(self) -> None:
        window = tk.Toplevel(self.master)
        window.title("自定义转码参数")
        window.geometry("620x360")
        window.transient(self.master)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        resolution_var = tk.StringVar(value=self.custom_resolution_var.get())
        video_bitrate_var = tk.StringVar(value=self.custom_video_bitrate_var.get())
        fps_var = tk.StringVar(value=self.custom_fps_var.get())
        audio_rate_var = tk.StringVar(value=self.custom_audio_sample_rate_var.get())
        audio_bitrate_var = tk.StringVar(value=self.custom_audio_bitrate_var.get())
        template_var = tk.StringVar(value="")

        ttk.Label(frame, text="模板：").grid(row=0, column=0, sticky="w", pady=(0, 8))
        template_names = sorted(self.transcode_templates.keys())
        template_box = ttk.Combobox(frame, textvariable=template_var, values=template_names, state="readonly")
        template_box.grid(row=0, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="分辨率（例 1920x1080）：").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=resolution_var).grid(row=1, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="视频码率（例 2500k）：").grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=video_bitrate_var).grid(row=2, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="帧率（例 30 / 29.97）：").grid(row=3, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=fps_var).grid(row=3, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="音频采样率（例 44100）：").grid(row=4, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=audio_rate_var).grid(row=4, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="音频码率（例 128k）：").grid(row=5, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(frame, textvariable=audio_bitrate_var).grid(row=5, column=1, sticky="ew", pady=(0, 6))

        def apply_template() -> None:
            name = template_var.get().strip()
            payload = self.transcode_templates.get(name)
            if not payload:
                return
            resolution_var.set(payload.get("resolution", ""))
            video_bitrate_var.set(payload.get("video_bitrate", ""))
            fps_var.set(payload.get("fps", ""))
            audio_rate_var.set(payload.get("audio_sample_rate", ""))
            audio_bitrate_var.set(payload.get("audio_bitrate", ""))

        def save_as_template() -> None:
            name = simpledialog.askstring("保存模板", "请输入模板名称：", parent=window)
            if not name:
                return
            clean_name = name.strip()
            if not clean_name:
                return
            self.transcode_templates[clean_name] = {
                "resolution": resolution_var.get().strip(),
                "video_bitrate": video_bitrate_var.get().strip(),
                "fps": fps_var.get().strip(),
                "audio_sample_rate": audio_rate_var.get().strip(),
                "audio_bitrate": audio_bitrate_var.get().strip(),
            }
            template_box.configure(values=sorted(self.transcode_templates.keys()))
            template_var.set(clean_name)
            self._save_config()
            self._append_log(f"已保存自定义转码模板：{clean_name}")

        action_frame = ttk.Frame(frame)
        action_frame.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 8))
        ttk.Button(action_frame, text="应用模板", command=apply_template).pack(side=tk.LEFT)
        ttk.Button(action_frame, text="另存为模板", command=save_as_template).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            frame,
            text="提示：开启主界面“自定义模式”后，本参数将覆盖预设编码参数。",
            foreground="#666666",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 10))

        btns = ttk.Frame(frame)
        btns.grid(row=8, column=0, columnspan=2, sticky="e")

        def on_save() -> None:
            self.custom_resolution_var.set(resolution_var.get().strip())
            self.custom_video_bitrate_var.set(video_bitrate_var.get().strip())
            self.custom_fps_var.set(fps_var.get().strip())
            self.custom_audio_sample_rate_var.set(audio_rate_var.get().strip())
            self.custom_audio_bitrate_var.set(audio_bitrate_var.get().strip())
            self._save_config()
            self._append_log("已更新自定义转码参数。")
            window.destroy()

        ttk.Button(btns, text="取消", command=window.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="保存", command=on_save).pack(side=tk.RIGHT, padx=(0, 8))

    @staticmethod
    def _ensure_send2trash_module() -> bool:
        global send2trash
        if send2trash is not None:
            return True
        try:
            send2trash_module = importlib.import_module("send2trash")
            send2trash = send2trash_module.send2trash
            return True
        except Exception:
            return False

    @staticmethod
    def _ensure_tkinterdnd2_module() -> bool:
        try:
            importlib.import_module("tkinterdnd2")
            return True
        except Exception:
            return False

    @staticmethod
    def _build_dependency_status_text(
        recycle_enabled: bool,
        send2trash_available: bool,
        drag_enabled: bool,
        tkinterdnd2_available: bool,
    ) -> str:
        recycle_text = (
            "回收站可用"
            if recycle_enabled and send2trash_available
            else "回收站依赖缺失"
            if recycle_enabled
            else "回收站关闭"
        )
        drag_text = (
            "拖放可用"
            if drag_enabled and tkinterdnd2_available
            else "拖放依赖缺失"
            if drag_enabled
            else "拖放关闭"
        )
        return f"依赖状态：{recycle_text} | {drag_text}"

    def _refresh_dependency_status(self) -> None:
        send2trash_available = self._ensure_send2trash_module()
        tkinterdnd2_available = self._ensure_tkinterdnd2_module()
        self.dependency_status_var.set(
            self._build_dependency_status_text(
                recycle_enabled=self.delete_to_recycle_var.get(),
                send2trash_available=send2trash_available,
                drag_enabled=self.enable_drag_drop_var.get(),
                tkinterdnd2_available=tkinterdnd2_available,
            )
        )

    def _refresh_drag_runtime_status(self) -> None:
        self.drag_runtime_status_var.set(
            self._build_drag_runtime_status_text(
                drag_enabled=self.enable_drag_drop_var.get(),
                drag_runtime_enabled=self.drag_drop_runtime_enabled,
                tkinterdnd2_available=self._ensure_tkinterdnd2_module(),
            )
        )

    def _on_toggle_drag_drop(self) -> None:
        self._save_config()
        self._refresh_drag_runtime_status()
        self._refresh_dependency_status()
        messagebox.showinfo("设置已保存", "拖放开关将在下次启动时生效。")

    def check_send2trash_status(self) -> None:
        if self._ensure_send2trash_module():
            self._append_log("可选依赖 send2trash 已可用。")
            self._refresh_dependency_status()
            messagebox.showinfo("依赖状态", "send2trash 已安装，可用回收站删除模式。")
            return
        self._append_log("可选依赖 send2trash 未安装。")
        self._refresh_dependency_status()
        messagebox.showwarning(
            "依赖状态",
            "send2trash 未安装，当前仅支持永久删除。\n可在设置中点击“一键部署回收站依赖”。",
        )

    def install_send2trash(self) -> None:
        if self._working:
            return
        if self._ensure_send2trash_module():
            messagebox.showinfo("提示", "send2trash 已安装，无需重复部署。")
            return
        self._set_busy(True)
        self.status_var.set("部署中...")
        self._append_log("开始部署可选依赖 send2trash...")
        thread = threading.Thread(target=self._install_send2trash_worker, daemon=True)
        thread.start()

    @staticmethod
    def _run_pip_install(package_spec: str) -> tuple[bool, str]:
        commands: list[list[str]] = []
        install_args = [
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            package_spec,
        ]

        if getattr(sys, "frozen", False):
            for launcher in ("py", "python"):
                if shutil.which(launcher):
                    commands.append([launcher, *install_args])
        else:
            commands.append([sys.executable, *install_args])

        if not commands:
            return (
                False,
                "当前是已打包 EXE 运行环境，且未检测到可用 Python 解释器（py/python）。"
                "请先安装 Python，或在源码环境中安装该依赖。",
            )

        last_error = ""
        for command in commands:
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
            except OSError as exc:
                last_error = f"调用 pip 失败（{command[0]}）：{exc}"
                continue

            if result.returncode == 0:
                return True, result.stdout.strip() or "安装完成"
            last_error = result.stderr.strip() or result.stdout.strip() or "未知错误"

        return False, last_error or "依赖安装失败。"

    @staticmethod
    def _resolve_readme_path() -> Path | None:
        candidates: list[Path] = []

        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "README.md")
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "README.md")

        candidates.append(Path(__file__).resolve().parents[1] / "README.md")

        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def install_all_optional_dependencies(self) -> None:
        if self._working:
            return
        self._set_busy(True)
        self.status_var.set("部署中...")
        self._append_log("开始一键部署全部可选依赖（send2trash + tkinterdnd2）...")
        thread = threading.Thread(target=self._install_all_optional_dependencies_worker, daemon=True)
        thread.start()

    def _install_all_optional_dependencies_worker(self) -> None:
        packages = [
            ("send2trash", "send2trash>=1.8.3", self._ensure_send2trash_module),
            ("tkinterdnd2", "tkinterdnd2>=0.4.2", self._ensure_tkinterdnd2_module),
        ]
        installed: list[str] = []
        skipped: list[str] = []
        failed: list[tuple[str, str]] = []

        for display_name, package_spec, ensure_func in packages:
            if ensure_func():
                skipped.append(display_name)
                continue

            success, details = self._run_pip_install(package_spec)
            if not success:
                failed.append((display_name, details))
                continue

            if ensure_func():
                installed.append(display_name)
            else:
                failed.append((display_name, "安装命令成功，但模块导入失败，请重启程序后重试。"))

        self.master.after(0, self._on_install_all_optional_dependencies_done, installed, skipped, failed)

    def _on_install_all_optional_dependencies_done(
        self,
        installed: list[str],
        skipped: list[str],
        failed: list[tuple[str, str]],
    ) -> None:
        self._set_busy(False)
        self._refresh_dependency_status()
        self._refresh_drag_runtime_status()

        if failed and not installed:
            self.status_var.set("失败")
        elif failed:
            self.status_var.set("部分完成")
        else:
            self.status_var.set("就绪")

        if installed:
            self._append_log(f"可选依赖安装成功：{', '.join(installed)}")
        if skipped:
            self._append_log(f"可选依赖已存在，跳过：{', '.join(skipped)}")
        if failed:
            for name, reason in failed:
                self._append_log(f"可选依赖安装失败：{name} -> {reason}")

        lines: list[str] = []
        if installed:
            lines.append(f"安装成功：{', '.join(installed)}")
        if skipped:
            lines.append(f"已安装跳过：{', '.join(skipped)}")
        if failed:
            lines.append("安装失败：")
            lines.extend([f"- {name}: {reason}" for name, reason in failed])

        message = "\n".join(lines) if lines else "未执行任何依赖变更。"
        if failed:
            messagebox.showwarning("可选依赖部署结果", message)
            return
        messagebox.showinfo("可选依赖部署完成", message)

    def _install_send2trash_worker(self) -> None:
        success, details = self._run_pip_install("send2trash>=1.8.3")
        if not success:
            self.master.after(0, self._on_install_send2trash_error, details)
            return

        if not self._ensure_send2trash_module():
            self.master.after(0, self._on_install_send2trash_error, "安装命令成功，但模块导入失败，请重启程序后重试。")
            return
        self.master.after(0, self._on_install_send2trash_success)

    def _on_install_send2trash_success(self) -> None:
        self._set_busy(False)
        self.status_var.set("就绪")
        self._append_log("可选依赖 send2trash 部署成功。")
        self._refresh_dependency_status()
        messagebox.showinfo("部署成功", "send2trash 已安装，后续删除可使用回收站模式。")

    def _on_install_send2trash_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set("失败")
        self._append_log(f"send2trash 部署失败：{message}")
        self._refresh_dependency_status()
        messagebox.showerror(
            "部署失败",
            "send2trash 安装失败。\n\n"
            f"{message}\n\n"
            "建议：\n1. 检查网络连接\n2. 以管理员身份运行\n3. 在虚拟环境中重试",
        )

    def check_tkinterdnd2_status(self) -> None:
        available = self._ensure_tkinterdnd2_module()
        self._refresh_drag_runtime_status()
        self._refresh_dependency_status()
        if available:
            runtime = "已启用" if self.drag_drop_runtime_enabled else "已安装（重启后按开关生效）"
            self._append_log("可选依赖 tkinterdnd2 已可用。")
            messagebox.showinfo("依赖状态", f"tkinterdnd2 已安装，拖放状态：{runtime}。")
            return
        self._append_log("可选依赖 tkinterdnd2 未安装。")
        messagebox.showwarning(
            "依赖状态",
            "tkinterdnd2 未安装，当前无法使用拖放。\n可在设置中点击“一键部署 tkinterdnd2”。",
        )

    def install_tkinterdnd2(self) -> None:
        if self._working:
            return
        if self._ensure_tkinterdnd2_module():
            messagebox.showinfo("提示", "tkinterdnd2 已安装，无需重复部署。")
            return
        self._set_busy(True)
        self.status_var.set("部署中...")
        self._append_log("开始部署可选依赖 tkinterdnd2...")
        thread = threading.Thread(target=self._install_tkinterdnd2_worker, daemon=True)
        thread.start()

    def _install_tkinterdnd2_worker(self) -> None:
        success, details = self._run_pip_install("tkinterdnd2>=0.4.2")
        if not success:
            self.master.after(0, self._on_install_tkinterdnd2_error, details)
            return

        if not self._ensure_tkinterdnd2_module():
            self.master.after(0, self._on_install_tkinterdnd2_error, "安装命令成功，但模块导入失败，请重启程序后重试。")
            return
        self.master.after(0, self._on_install_tkinterdnd2_success)

    def _on_install_tkinterdnd2_success(self) -> None:
        self._set_busy(False)
        self.status_var.set("就绪")
        self._append_log("可选依赖 tkinterdnd2 部署成功。")
        self._refresh_drag_runtime_status()
        self._refresh_dependency_status()
        messagebox.showinfo("部署成功", "tkinterdnd2 已安装。请重启应用后使用拖放功能。")

    def _on_install_tkinterdnd2_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set("失败")
        self._append_log(f"tkinterdnd2 部署失败：{message}")
        self._refresh_drag_runtime_status()
        self._refresh_dependency_status()
        messagebox.showerror(
            "部署失败",
            "tkinterdnd2 安装失败。\n\n"
            f"{message}\n\n"
            "建议：\n1. 检查网络连接\n2. 以管理员身份运行\n3. 在虚拟环境中重试",
        )

    def export_log(self) -> None:
        entries = self._filtered_log_entries()
        content = "\n".join(
            [f"[{self._log_prefix(entry.level)}][{entry.task}] {entry.message}" for entry in entries]
        ).strip()
        if not content:
            messagebox.showinfo("提示", "当前没有可导出的日志。")
            return

        file_path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not file_path:
            return

        Path(file_path).write_text(content + "\n", encoding="utf-8")
        self._append_log(f"日志已导出（当前筛选）：{file_path}", level="SUCCESS", task="全局")

    def filter_only_failed_logs(self) -> None:
        self.log_level_filter_var.set("失败")
        # 保留当前任务筛选，仅快速切换级别。
        self._append_log("日志筛选已切换为：仅失败", level="DEBUG", task="全局")

    def cancel_convert(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.status_var.set("正在取消...")
            self._append_log("收到取消请求，正在停止 ffmpeg...")

    def select_ffmpeg(self) -> None:
        ffmpeg_file = filedialog.askopenfilename(
            title="选择 ffmpeg 可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if ffmpeg_file:
            self.ffmpeg_var.set(ffmpeg_file)
            self.ffmpeg_hint_var.set("FFmpeg：已手动配置")
            self._append_log(f"已手动设置 ffmpeg：{ffmpeg_file}")
            self._save_config()

    def show_ffmpeg_path(self) -> None:
        current = self.ffmpeg_var.get().strip() or "ffmpeg"
        messagebox.showinfo("FFmpeg 路径", f"当前 FFmpeg 配置：\n{current}")

    def reset_ffmpeg_path(self) -> None:
        self.ffmpeg_var.set("ffmpeg")
        self.ffmpeg_hint_var.set("FFmpeg：使用系统默认命令")
        self._append_log("FFmpeg 配置已恢复为默认值：ffmpeg")
        self._save_config()

    def set_default_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="设置默认输出目录")
        if not folder:
            return

        self.default_output_dir = folder
        self.output_var.set(folder)
        self._append_log(f"默认输出目录已设置为：{folder}")
        self._save_config()
        messagebox.showinfo("设置完成", f"默认输出目录已更新：\n{folder}")

    def reset_default_output_dir(self) -> None:
        self.default_output_dir = str(Path.cwd())
        self.output_var.set(self.default_output_dir)
        self._append_log(f"默认输出目录已重置为：{self.default_output_dir}")
        self._save_config()

    def set_max_workers(self) -> None:
        current = self.max_workers_var.get().strip() or "1"
        value = simpledialog.askstring("设置并发数", "请输入并发数（1-8）：", initialvalue=current, parent=self.master)
        if value is None:
            return
        text = value.strip()
        if not text.isdigit() or not (1 <= int(text) <= 8):
            messagebox.showwarning("输入无效", "并发数必须是 1 到 8 的整数。")
            return
        self.max_workers_var.set(text)
        self._append_log(f"并发数已设置为：{text}", level="INFO")
        self._save_config()

    def export_config_file(self) -> None:
        file_path = filedialog.asksaveasfilename(
            title="导出配置",
            defaultextension=".json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not file_path:
            return
        self._save_config()
        Path(file_path).write_text(
            json.dumps(asdict(self.config_model), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_log(f"配置导出成功：{file_path}", level="SUCCESS")

    def import_config_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not file_path:
            return
        try:
            raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置格式错误")
            merged = asdict(self.config_model)
            merged.update(raw)
            self.config_model = AppConfig(**merged)
        except Exception as exc:
            messagebox.showerror("导入失败", f"配置导入失败：{exc}")
            self._append_log(f"配置导入失败：{exc}", level="ERROR")
            return

        self._apply_loaded_config_to_ui()
        save_config(self.config_model)
        self._append_log(f"配置导入成功：{file_path}", level="SUCCESS")
        messagebox.showinfo("导入成功", "配置已导入并应用。")

    def _apply_loaded_config_to_ui(self) -> None:
        self.default_output_dir = self.config_model.default_output_dir or self.default_output_dir
        self.output_var.set(self.config_model.last_output_dir or self.output_var.get())
        self.ffmpeg_var.set(self.config_model.ffmpeg_path or "ffmpeg")
        self.preset_var.set(self._preset_label(self.config_model.preset))
        self.output_format_var.set((self.config_model.output_format or "mp4").lower())
        self.output_prefix_var.set(self.config_model.output_prefix)
        self.output_suffix_var.set(self.config_model.output_suffix)
        self.output_timestamp_var.set(self.config_model.output_use_timestamp)
        self.conflict_var.set(self._conflict_label(self.config_model.conflict_strategy))
        self.folder_recursive_var.set(self.config_model.folder_recursive_scan)
        self.folder_first_only_var.set(self.config_model.folder_first_only_per_dir)
        self.preview_before_start_var.set(self.config_model.preview_before_start)
        self.continue_on_error_var.set(self.config_model.continue_on_error)
        self.smart_select_preference_var.set(self.config_model.smart_select_preference)
        self.delete_to_recycle_var.set(self.config_model.delete_to_recycle_bin)
        self.delete_scope_var.set(self._resolve_delete_scope_mode())
        self.delete_preview_before_execute_var.set(self.config_model.delete_preview_before_execute)
        self.enable_drag_drop_var.set(self.config_model.enable_drag_drop)
        self.decrypt_auto_parse_var.set(self.config_model.decrypt_auto_parse_key)
        self.decrypt_key_var.set(self.config_model.manual_decrypt_key_hex)
        self.decrypt_iv_var.set(self.config_model.manual_decrypt_iv_hex)
        self.transcode_mode_var.set(self.config_model.transcode_mode)
        self.custom_resolution_var.set(self.config_model.custom_video_resolution)
        self.custom_video_bitrate_var.set(self.config_model.custom_video_bitrate)
        self.custom_fps_var.set(self.config_model.custom_video_fps)
        self.custom_audio_sample_rate_var.set(self.config_model.custom_audio_sample_rate)
        self.custom_audio_bitrate_var.set(self.config_model.custom_audio_bitrate)
        self.enable_resume_var.set(self.config_model.enable_resume)
        self.enable_sound_notify_var.set(self.config_model.enable_sound_notify)
        self.cleanup_preview_temp_on_exit_var.set(self.config_model.cleanup_preview_temp_on_exit)
        self.max_workers_var.set(self.config_model.max_workers or "1")
        self.log_level_filter_var.set(self.config_model.log_level_filter or "全部")
        self.log_task_filter_var.set(self.config_model.log_task_filter or "全部任务")
        self.theme_mode_var.set(self._normalize_theme_mode(self.config_model.theme_mode))
        self.progress_color_var.set(self._normalize_progress_color_mode(self.config_model.progressbar_color_mode))
        self.transcode_templates = self._load_transcode_templates(self.config_model.custom_templates_json)
        self._apply_theme(self.theme_mode_var.get())
        self._refresh_delete_scope_status()
        self._refresh_action_state()
        self._refresh_dependency_status()
        self._refresh_drag_runtime_status()

    def _play_notify_sound(self, mode: str) -> None:
        if not self.enable_sound_notify_var.get():
            return
        try:
            if winsound is not None and sys.platform.startswith("win"):
                tone = {
                    "success": winsound.MB_ICONASTERISK,
                    "error": winsound.MB_ICONHAND,
                    "cancel": winsound.MB_ICONEXCLAMATION,
                }.get(mode, winsound.MB_OK)
                winsound.MessageBeep(tone)
                return
            self.master.bell()
        except Exception:
            return

    def install_ffmpeg(self) -> None:
        if self._working:
            return
        self._set_busy(True)
        self.status_var.set("部署中...")
        self._append_log("开始一键部署 ffmpeg，请稍候...")
        thread = threading.Thread(target=self._install_ffmpeg_worker, daemon=True)
        thread.start()

    def _install_ffmpeg_worker(self) -> None:
        try:
            result = deploy_ffmpeg()
            self.master.after(0, self._on_install_success, result.message)
        except DeployFailedError as exc:
            self.master.after(0, self._on_install_error, str(exc))
        except Exception as exc:  # pragma: no cover
            self.master.after(0, self._on_install_error, f"未预期错误：{exc}")

    def _on_install_success(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set("就绪")
        self._append_log(message)
        self.auto_detect_ffmpeg(show_message=False)
        messagebox.showinfo(
            "FFmpeg 部署成功",
            f"✓ FFmpeg 已成功部署\n\n{message}\n\n"
            "现在可以开始使用工具转换视频。"
        )

    def _on_install_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set("失败")
        self._append_log(f"部署失败：{message}", level="ERROR", task="全局")
        messagebox.showerror(
            "FFmpeg 部署失败",
            f"✗ FFmpeg 部署过程出现错误：\n\n{message}\n\n"
            "请尝试以下方式：\n"
            "1. 手动选择 ffmpeg.exe 文件\n"
            "2. 确保系统安装管理器可用（Windows: winget / macOS: brew / Linux: apt 或 dnf 或 yum）\n"
            "3. 查看日志了解更多详情"
        )

    def auto_detect_ffmpeg(self, show_message: bool = True, log_start: bool = True) -> None:
        if self._ffmpeg_detecting:
            return
        self._ffmpeg_detecting = True
        self.ffmpeg_hint_var.set("FFmpeg：检测中...")
        if log_start:
            self._append_log("正在检测 ffmpeg 路径...", level="DEBUG", task="全局")
        thread = threading.Thread(
            target=self._auto_detect_ffmpeg_worker,
            args=(show_message,),
            daemon=True,
        )
        thread.start()

    def _auto_detect_ffmpeg_worker(self, show_message: bool) -> None:
        detected = auto_detect_ffmpeg_path()
        try:
            self.master.after(0, self._on_auto_detect_ffmpeg_done, detected, show_message)
        except tk.TclError:
            return

    def _on_auto_detect_ffmpeg_done(self, detected: str | None, show_message: bool) -> None:
        self._ffmpeg_detecting = False
        if detected:
            self.ffmpeg_var.set(detected)
            self.ffmpeg_hint_var.set("FFmpeg：已自动检测")
            self._append_log(f"自动检测到 ffmpeg：{detected}")
            self._save_config()
            if show_message:
                messagebox.showinfo("检测成功", f"已自动填充 ffmpeg 路径：\n{detected}")
            return

        self.ffmpeg_hint_var.set("FFmpeg：未检测到，请在设置中手动选择")
        self._append_log("自动检测未找到 ffmpeg，请手动选择 ffmpeg.exe")
        if show_message:
            messagebox.showwarning("检测失败", "未检测到 ffmpeg，请手动选择 ffmpeg.exe")

    @staticmethod
    def _log_prefix(level: str) -> str:
        mapping = {
            "INFO": "信息",
            "SUCCESS": "成功",
            "WARNING": "警告",
            "ERROR": "失败",
            "DEBUG": "调试",
        }
        return mapping.get(level, "信息")

    @staticmethod
    def _log_tag(level: str) -> str | None:
        return {
            "SUCCESS": "success",
            "WARNING": "warning",
            "ERROR": "error",
            "DEBUG": "debug",
        }.get(level)

    def _append_log(self, text: str, level: str = "INFO", task: str = "全局") -> None:
        entry = LogEntry(level=level, task=task, message=text)
        self.log_entries.append(entry)
        if task not in self.log_tasks:
            self.log_tasks.add(task)
            if hasattr(self, "log_task_box"):
                self.log_task_box.configure(values=sorted(self.log_tasks, key=lambda x: (x != "全部任务", x)))
        self._schedule_log_render()

    def _on_log_filter_change(self, *_args: object) -> None:
        if self._log_render_after_id is not None:
            try:
                self.master.after_cancel(self._log_render_after_id)
            except Exception:
                pass
            self._log_render_after_id = None
        self._last_log_rendered_count = 0
        self._render_log_entries()

    def _filtered_log_entries(self) -> list[LogEntry]:
        level_filter = self.log_level_filter_var.get().strip()
        task_filter = self.log_task_filter_var.get().strip()
        level_map = {
            "信息": "INFO",
            "成功": "SUCCESS",
            "警告": "WARNING",
            "失败": "ERROR",
            "调试": "DEBUG",
        }
        target_level = level_map.get(level_filter, "") if level_filter != "全部" else ""
        target_task = task_filter if task_filter and task_filter != "全部任务" else ""

        filtered: list[LogEntry] = []
        for entry in self.log_entries:
            if target_level and entry.level != target_level:
                continue
            if target_task and entry.task != target_task:
                continue
            filtered.append(entry)
        if len(filtered) > self._max_log_render_lines:
            return filtered[-self._max_log_render_lines :]
        return filtered

    def _schedule_log_render(self) -> None:
        if self._log_render_after_id is not None:
            return
        # 合并短时间内大量日志写入，避免 Text 组件频繁整块重绘。
        self._log_render_after_id = self.master.after(60, self._flush_log_render)

    def _flush_log_render(self) -> None:
        self._log_render_after_id = None
        self._render_log_entries()

    def _render_log_entries(self) -> None:
        if not hasattr(self, "log_box"):
            return
        current_filter = (self.log_level_filter_var.get().strip(), self.log_task_filter_var.get().strip())
        if current_filter != self._last_log_filter:
            self._last_log_filter = current_filter
            self._last_log_rendered_count = 0

        can_incremental = (
            current_filter == ("全部", "全部任务")
            and self._last_log_rendered_count > 0
            and len(self.log_entries) <= self._max_log_render_lines
            and self._last_log_rendered_count <= len(self.log_entries)
        )

        if can_incremental:
            new_entries = self.log_entries[self._last_log_rendered_count :]
            if not new_entries:
                return
            self.log_box.configure(state=tk.NORMAL)
            for entry in new_entries:
                prefix = self._log_prefix(entry.level)
                line = f"[{prefix}][{entry.task}] {entry.message}\n"
                tag = self._log_tag(entry.level)
                if tag:
                    self.log_box.insert(tk.END, line, (tag,))
                else:
                    self.log_box.insert(tk.END, line)
            self.log_box.see(tk.END)
            self.log_box.configure(state=tk.DISABLED)
            self._last_log_rendered_count = len(self.log_entries)
            return

        filtered_entries = self._filtered_log_entries()
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        for entry in filtered_entries:
            prefix = self._log_prefix(entry.level)
            line = f"[{prefix}][{entry.task}] {entry.message}\n"
            tag = self._log_tag(entry.level)
            if tag:
                self.log_box.insert(tk.END, line, (tag,))
            else:
                self.log_box.insert(tk.END, line)
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)
        self._last_log_rendered_count = len(self.log_entries) if current_filter == ("全部", "全部任务") else 0

    def copy_error_logs(self) -> None:
        errors = [f"[{entry.task}] {entry.message}" for entry in self.log_entries if entry.level == "ERROR"]
        if not errors:
            messagebox.showinfo("复制错误", "当前没有失败日志可复制。")
            return
        content = "\n".join(errors)
        self.master.clipboard_clear()
        self.master.clipboard_append(content)
        self._append_log(f"已复制 {len(errors)} 条失败日志到剪贴板。", level="INFO")

    def _set_preview_text(self, text: str) -> None:
        self.preview_box.configure(state=tk.NORMAL)
        self.preview_box.delete("1.0", tk.END)
        self.preview_box.insert(tk.END, text)
        self.preview_box.configure(state=tk.DISABLED)

    @staticmethod
    def _parse_readme_sections(readme_text: str) -> list[tuple[int, str, str]]:
        sections: list[tuple[int, str, list[str]]] = []
        current_level = 1
        current_title = "概览"
        current_lines: list[str] = []

        for line in readme_text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                hash_count = len(stripped) - len(stripped.lstrip("#"))
                title = stripped[hash_count:].strip()
                if title:
                    if current_lines:
                        sections.append((current_level, current_title, current_lines.copy()))
                    current_level = min(max(hash_count, 1), 6)
                    current_title = title
                    current_lines = []
                    continue
            current_lines.append(line)

        if current_lines or not sections:
            sections.append((current_level, current_title, current_lines.copy()))

        return [(level, title, "\n".join(lines).strip() or "（暂无内容）") for level, title, lines in sections]

    @staticmethod
    def _filter_help_sections(
        sections: list[tuple[int, str, str]],
        keyword: str,
    ) -> list[tuple[int, str, str]]:
        query = keyword.strip().lower()
        if not query:
            return sections

        parent_indices: list[int | None] = [None] * len(sections)
        level_stack: dict[int, int] = {}
        for index, (level, _title, _content) in enumerate(sections):
            parent_indices[index] = level_stack.get(level - 1)
            level_stack[level] = index
            for deeper in list(level_stack.keys()):
                if deeper > level:
                    del level_stack[deeper]

        keep_indices: set[int] = set()
        for index, (_level, title, content) in enumerate(sections):
            if not ConverterApp._is_help_match(title, content, query):
                continue
            cursor: int | None = index
            while cursor is not None:
                keep_indices.add(cursor)
                cursor = parent_indices[cursor]

        return [entry for idx, entry in enumerate(sections) if idx in keep_indices]

    @staticmethod
    def _is_help_match(title: str, content: str, keyword: str) -> bool:
        query = keyword.strip().lower()
        if not query:
            return False
        preview_content = ConverterApp._to_preview_text(content).lower()
        return query in title.lower() or query in preview_content

    def _collect_help_tree_item_ids(self, parent: str = "") -> list[str]:
        if not hasattr(self, "help_tree"):
            return []
        item_ids: list[str] = []
        for item_id in self.help_tree.get_children(parent):
            item_ids.append(item_id)
            item_ids.extend(self._collect_help_tree_item_ids(item_id))
        return item_ids

    def _rebuild_help_tree(self, sections: list[tuple[int, str, str]]) -> None:
        if not hasattr(self, "help_tree"):
            return

        self.help_tree.delete(*self.help_tree.get_children(""))
        self.help_sections = {}

        parents: dict[int, str] = {0: ""}
        for level, title, content in sections:
            parent_id = parents.get(level - 1, "")
            item_id = self.help_tree.insert(parent_id, tk.END, text=title, open=True)
            self.help_sections[item_id] = content
            parents[level] = item_id
            for key in list(parents.keys()):
                if key > level:
                    del parents[key]

        first_items = self.help_tree.get_children("")
        if first_items:
            self.help_tree.selection_set(first_items[0])
            self._on_help_tree_select()
            return

        self._render_help_cards("搜索结果", "未找到匹配的章节，请尝试其他关键词。")

    def _on_help_search_change(self, *_args: object) -> None:
        if not hasattr(self, "help_all_sections") or not hasattr(self, "help_search_var"):
            return
        keyword = self.help_search_var.get()
        filtered = self._filter_help_sections(self.help_all_sections, keyword)
        self._rebuild_help_tree(filtered)

    def _jump_to_next_help_match(self, _event: object | None = None) -> str:
        return self._jump_to_help_match(forward=True)

    @staticmethod
    def _resolve_match_index(total: int, current_index: int | None, forward: bool) -> int:
        if total <= 0:
            return 0
        if current_index is None:
            return 0 if forward else total - 1
        step = 1 if forward else -1
        return (current_index + step) % total

    def _jump_to_prev_help_match(self, _event: object | None = None) -> str:
        return self._jump_to_help_match(forward=False)

    def _jump_to_help_match(self, forward: bool) -> str:
        if not hasattr(self, "help_tree") or not hasattr(self, "help_search_var"):
            return "break"
        keyword = self.help_search_var.get().strip()
        if not keyword:
            return "break"

        ordered_ids = self._collect_help_tree_item_ids()
        matched_ids = [
            item_id
            for item_id in ordered_ids
            if self._is_help_match(
                str(self.help_tree.item(item_id, "text") or ""),
                self.help_sections.get(item_id, ""),
                keyword,
            )
        ]
        if not matched_ids:
            return "break"

        selected = self.help_tree.selection()
        current = selected[0] if selected else None
        current_index = matched_ids.index(current) if current in matched_ids else None
        next_index = self._resolve_match_index(len(matched_ids), current_index, forward)

        target_id = matched_ids[next_index]
        self.help_tree.selection_set(target_id)
        self.help_tree.focus(target_id)
        self.help_tree.see(target_id)
        self._on_help_tree_select()
        return "break"

    @staticmethod
    def _find_highlight_spans(text: str, keyword: str) -> list[tuple[int, int]]:
        query = keyword.strip()
        if not query:
            return []
        pattern = re.compile(re.escape(query), flags=re.IGNORECASE)
        return [(match.start(), match.end()) for match in pattern.finditer(text)]

    @staticmethod
    def _insert_highlighted_text(widget: tk.Text, text: str, keyword: str) -> None:
        widget.delete("1.0", tk.END)
        spans = ConverterApp._find_highlight_spans(text, keyword)
        if not spans:
            widget.insert(tk.END, text)
            return

        cursor = 0
        for start, end in spans:
            if start > cursor:
                widget.insert(tk.END, text[cursor:start])
            widget.insert(tk.END, text[start:end], ("help_highlight",))
            cursor = end
        if cursor < len(text):
            widget.insert(tk.END, text[cursor:])

    def _render_help_cards(self, title: str, content: str) -> None:
        if not hasattr(self, "help_cards_container"):
            return
        for child in self.help_cards_container.winfo_children():
            child.destroy()

        keyword = self.help_search_var.get() if hasattr(self, "help_search_var") else ""
        header_text = tk.Text(
            self.help_cards_container,
            height=1,
            wrap=tk.NONE,
            relief=tk.FLAT,
            highlightthickness=0,
            bd=0,
            bg=self.help_canvas.cget("bg") if hasattr(self, "help_canvas") else "#f0f0f0",
        )
        header_text.tag_configure("help_highlight", background="#ffe58f", foreground="#111111")
        self._insert_highlighted_text(header_text, f"【{title}】", keyword)
        header_text.configure(state=tk.DISABLED)
        header_text.pack(fill=tk.X, padx=8, pady=(8, 4))

        cards = self._split_help_cards(content)
        for index, (card_title, card_body) in enumerate(cards, start=1):
            card = ttk.LabelFrame(self.help_cards_container, text=f"{index}. {card_title}")
            card.pack(fill=tk.X, padx=8, pady=6)
            card_text = tk.Text(
                card,
                height=max(2, min(12, card_body.count("\n") + 1)),
                wrap=tk.WORD,
                relief=tk.FLAT,
                highlightthickness=0,
                bd=0,
            )
            card_text.tag_configure("help_highlight", background="#ffe58f", foreground="#111111")
            self._insert_highlighted_text(card_text, card_body, keyword)
            card_text.configure(state=tk.DISABLED)
            card_text.pack(fill=tk.X, padx=10, pady=8)

        if hasattr(self, "help_canvas"):
            self.help_canvas.update_idletasks()
            self.help_canvas.yview_moveto(0.0)

    def _on_help_tree_select(self, _event: object | None = None) -> None:
        if not hasattr(self, "help_tree"):
            return
        selected = self.help_tree.selection()
        if not selected:
            return
        item_id = selected[0]
        content = self.help_sections.get(item_id)
        if content is not None:
            title = str(self.help_tree.item(item_id, "text") or "帮助")
            self._render_help_cards(title, content)

    def open_help_window(self) -> None:
        if hasattr(self, "help_window") and self.help_window.winfo_exists():
            self.help_window.lift()
            self.help_window.focus_force()
            return

        readme_path = self._resolve_readme_path()
        if readme_path is None:
            messagebox.showwarning(
                "帮助",
                "未找到 README 文档，暂时无法打开帮助。\n"
                "若你在 EXE 中运行，请确认发布目录包含 README.md。",
            )
            return

        help_window = tk.Toplevel(self.master)
        help_window.title("帮助")
        help_window.geometry("980x660")
        help_window.minsize(760, 500)
        self.help_window = help_window

        container = ttk.Panedwindow(help_window, orient=tk.HORIZONTAL)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(container, width=260)
        right_frame = ttk.Frame(container)
        container.add(left_frame, weight=1)
        container.add(right_frame, weight=3)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_frame, text="搜索：").pack(side=tk.LEFT)
        self.help_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.help_search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self.help_search_var.trace_add("write", self._on_help_search_change)
        search_entry.bind("<Return>", self._jump_to_next_help_match)
        search_entry.bind("<KP_Enter>", self._jump_to_next_help_match)
        search_entry.bind("<Shift-Return>", self._jump_to_prev_help_match)
        search_entry.bind("<Shift-KP_Enter>", self._jump_to_prev_help_match)

        tree = ttk.Treeview(left_frame, show="tree")
        tree.pack(fill=tk.BOTH, expand=True)
        self.help_tree = tree

        canvas = tk.Canvas(right_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        cards_container = ttk.Frame(canvas)
        cards_window = canvas.create_window((0, 0), window=cards_container, anchor="nw")
        cards_container.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(cards_window, width=event.width),
        )
        self.help_canvas = canvas
        self.help_cards_container = cards_container

        self.help_sections: dict[str, str] = {}
        self.help_all_sections: list[tuple[int, str, str]] = []

        tree.bind("<<TreeviewSelect>>", self._on_help_tree_select)
        self._render_help_cards("帮助", "正在加载帮助文档，请稍候...")
        self._load_help_sections_async(readme_path)

    def _load_help_sections_async(self, readme_path: Path) -> None:
        self._help_loading_token += 1
        token = self._help_loading_token
        cache_hit = False
        try:
            current_mtime = readme_path.stat().st_mtime
            cache_hit = (
                self._help_sections_cache is not None
                and self._help_sections_cache_path == str(readme_path)
                and self._help_sections_cache_mtime == current_mtime
            )
        except OSError:
            current_mtime = None

        if cache_hit and self._help_sections_cache is not None:
            self._on_help_sections_loaded(token, self._help_sections_cache, None)
            return

        def worker() -> None:
            try:
                readme_text = readme_path.read_text(encoding="utf-8")
                sections = self._parse_readme_sections(readme_text)
                error = None
            except Exception as exc:
                sections = []
                error = str(exc)

            try:
                self.master.after(0, self._on_help_sections_loaded, token, sections, error, str(readme_path), current_mtime)
            except tk.TclError:
                return

        threading.Thread(target=worker, daemon=True).start()

    def _on_help_sections_loaded(
        self,
        token: int,
        sections: list[tuple[int, str, str]],
        error: str | None,
        path: str = "",
        mtime: float | None = None,
    ) -> None:
        if token != self._help_loading_token:
            return
        if not hasattr(self, "help_window") or not self.help_window.winfo_exists():
            return

        if error is not None:
            self._render_help_cards("帮助", f"帮助文档加载失败：\n{error}")
            return

        if path:
            self._help_sections_cache = sections
            self._help_sections_cache_path = path
            self._help_sections_cache_mtime = mtime

        self.help_all_sections = sections
        self._rebuild_help_tree(self.help_all_sections)

    def _set_busy(self, busy: bool) -> None:
        self._working = busy
        self.cancel_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.settings_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.preview_before_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.continue_on_error_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.resume_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.custom_mode_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.custom_settings_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.decrypt_settings_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        editable_state = tk.DISABLED if busy else tk.NORMAL
        self.source_entry.configure(state=editable_state)
        self.output_entry.configure(state=editable_state)
        self.output_name_entry.configure(state=editable_state)
        self.source_btn.configure(state=editable_state)
        self.output_browse_btn.configure(state=editable_state)
        self.export_log_btn.configure(state=editable_state)
        if hasattr(self, "integrity_btn"):
            self.integrity_btn.configure(state=editable_state)
        if hasattr(self, "preview_segment_btn"):
            self.preview_segment_btn.configure(state=editable_state)
        if hasattr(self, "cleanup_preview_btn"):
            self.cleanup_preview_btn.configure(state=editable_state)
        if hasattr(self, "quick_check_btn"):
            self.quick_check_btn.configure(state=editable_state)
        if hasattr(self, "log_level_box"):
            self.log_level_box.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "log_task_box"):
            self.log_task_box.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "copy_error_btn"):
            self.copy_error_btn.configure(state=editable_state)
        if hasattr(self, "only_failed_btn"):
            self.only_failed_btn.configure(state=editable_state)
        self._refresh_action_state()

    def _update_progress(self, percent: float, message: str) -> None:
        value = max(0.0, min(100.0, percent))
        self.progress_var.set(value)
        self.progress_text_var.set(f"{int(value)}% - {message}")

    def _update_cleanup_progress(self, percent: float, message: str) -> None:
        value = max(0.0, min(100.0, percent))
        self.cleanup_progress_var.set(value)
        self.cleanup_progress_text_var.set(f"{int(value)}% - {message}")

    def _queue_progress_update(self, percent: float, message: str) -> None:
        with self._progress_state_lock:
            self._pending_progress = (percent, message)
            if self._progress_update_scheduled:
                return
            self._progress_update_scheduled = True
        # 约 15fps，足够流畅并显著降低主线程 UI 重绘压力。
        self.master.after(66, self._flush_progress_update)

    def _flush_progress_update(self) -> None:
        with self._progress_state_lock:
            percent, message = self._pending_progress
            self._progress_update_scheduled = False
        self._update_progress(percent, message)

    def _collect_sources(self) -> list[tuple[str, str | None, Path | None]]:
        if self.local_files:
            self._update_folder_option_state(False)
            return [
                (file_path, self._suggest_name_for_m3u8(Path(file_path)), None)
                for file_path in self.local_files
            ]

        source = self.source_var.get().strip()
        if not source:
            raise InvalidInputError("请输入 URL，或使用智能选择选择文件/文件夹。")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            self._update_folder_option_state(False)
            return [(source, self._suggest_name_for_url(source), None)]

        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise InvalidInputError(f"输入源不存在：{source_path}")

        if source_path.is_file():
            if source_path.suffix.lower() != ".m3u8":
                raise InvalidInputError("输入文件必须是 .m3u8 格式。")
            self._update_folder_option_state(False)
            return [(str(source_path), self._suggest_name_for_m3u8(source_path), None)]

        if not source_path.is_dir():
            raise InvalidInputError("输入源既不是文件也不是文件夹。")

        self._update_folder_option_state(True)
        m3u8_files = (
            sorted(source_path.rglob("*.m3u8"))
            if self.folder_recursive_var.get()
            else sorted(source_path.glob("*.m3u8"))
        )
        if not m3u8_files:
            raise InvalidInputError("所选文件夹中未找到 .m3u8 文件。")

        if self.folder_recursive_var.get() and self.folder_first_only_var.get():
            first_by_dir: dict[Path, Path] = {}
            for file_path in m3u8_files:
                rel_parent = file_path.parent.relative_to(source_path)
                if rel_parent not in first_by_dir:
                    first_by_dir[rel_parent] = file_path
            m3u8_files = [first_by_dir[key] for key in sorted(first_by_dir.keys(), key=lambda p: str(p))]

        used_names: dict[str, int] = {}
        entries: list[tuple[str, str | None, Path | None]] = []
        for m3u8_file in m3u8_files:
            base_name = self._suggest_name_for_m3u8(m3u8_file)
            serial = used_names.get(base_name, 0) + 1
            used_names[base_name] = serial
            suggested_name = base_name if serial == 1 else f"{base_name}_{serial}"
            rel_parent = m3u8_file.parent.relative_to(source_path)
            subdir = None if rel_parent == Path(".") else rel_parent
            entries.append((str(m3u8_file), suggested_name, subdir))
        return entries


    def _build_preview_lines(
        self,
        sources: list[tuple[str, str | None, Path | None]],
        output_dir: str,
        base_output_name: str | None,
    ) -> list[str]:
        extension = self.output_format_var.get().strip().lower().lstrip(".") or "mp4"
        lines = [f"总任务数：{len(sources)}", ""]
        for index, (source, suggested_name, rel_subdir) in enumerate(sources, start=1):
            current_output_name = base_output_name or suggested_name
            if base_output_name and len(sources) > 1:
                current_output_name = f"{base_output_name}_{index}"

            if not current_output_name:
                parsed = urlparse(source)
                current_output_name = Path(parsed.path).stem or f"task_{index}"

            current_output_name = self._compose_output_name(current_output_name) or current_output_name

            target_output_dir = Path(output_dir)
            if rel_subdir is not None:
                target_output_dir = target_output_dir / rel_subdir

            target_file = target_output_dir / f"{current_output_name}.{extension}"
            lines.append(f"[{index}] 输入：{source}")
            lines.append(f"    输出：{target_file}")
        return lines

    def _collect_sources_async(
        self,
        on_success: Callable[[list[tuple[str, str | None, Path | None]]], None],
        action_name: str,
    ) -> None:
        if self._working:
            return
        self._set_busy(True)
        self.status_var.set("扫描中...")
        self._append_log(f"开始异步扫描输入源（{action_name}）...", level="DEBUG", task="全局")

        def worker() -> None:
            try:
                sources = self._collect_sources()
            except InvalidInputError as exc:
                self.master.after(0, self._on_collect_sources_error, str(exc))
                return
            self.master.after(0, on_success, sources)

        threading.Thread(target=worker, daemon=True).start()

    def _on_collect_sources_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set("就绪")
        messagebox.showwarning("输入无效", message)
        self._append_log(message, level="WARNING")

    def _on_preview_sources_ready(self, sources: list[tuple[str, str | None, Path | None]]) -> None:
        self._set_busy(False)
        self.status_var.set("就绪")
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("缺少目录", "请先选择输出目录。")
            return
        base_output_name = self.output_name_var.get().strip() or None
        lines = self._build_preview_lines(sources, output_dir, base_output_name)
        self._set_preview_text("\n".join(lines) + "\n")
        self._append_log("已刷新任务预览", level="INFO", task="全局")

    def _on_start_sources_ready(self, sources: list[tuple[str, str | None, Path | None]]) -> None:
        output_dir = self.output_var.get().strip()
        if not output_dir:
            self._set_busy(False)
            self.status_var.set("就绪")
            messagebox.showwarning("缺少目录", "请先选择输出目录。")
            return

        base_output_name = self.output_name_var.get().strip() or None
        preview_lines = self._build_preview_lines(sources, output_dir, base_output_name)
        self._set_preview_text("\n".join(preview_lines) + "\n")

        local_source_count = sum(1 for source, _, _ in sources if self._is_local_file_source(source))
        self.delete_source_after_success = False
        if local_source_count > 0:
            delete_mode = "回收站" if self.delete_to_recycle_var.get() else "永久删除"
            if self.delete_to_recycle_var.get() and send2trash is None:
                delete_mode = "永久删除（未安装 send2trash）"
            delete_scope = self._delete_scope_label()

            delete_choice = messagebox.askyesnocancel(
                "源文件处理",
                f"检测到 {local_source_count} 个本地源文件\n\n"
                f"删除方式：{delete_mode}\n\n"
                f"删除范围：{delete_scope}\n\n"
                "转换成功后是否执行删除？",
            )
            if delete_choice is None:
                self._set_busy(False)
                self.status_var.set("就绪")
                self._append_log("用户取消了转换任务", level="WARNING")
                return
            self.delete_source_after_success = delete_choice
            self._append_log(
                "本次任务源文件处理：转换成功后删除" if delete_choice else "本次任务源文件处理：保留源文件",
                level="INFO",
            )

            if delete_choice and self.delete_preview_before_execute_var.get():
                preview_text = self._build_delete_preview_text(sources)
                preview_confirm = messagebox.askyesno(
                    "删除预演确认",
                    f"以下为执行前预演结果：\n\n{preview_text}\n\n是否按上述策略继续转换并在成功后删除源文件？",
                    icon="warning",
                )
                if not preview_confirm:
                    self._set_busy(False)
                    self.status_var.set("就绪")
                    self._append_log("用户取消了删除预演确认，任务未开始", level="WARNING")
                    return

            if (
                delete_choice
                and self._is_high_risk_delete_scope(self.delete_scope_var.get())
                and not self.delete_to_recycle_var.get()
            ):
                danger_confirm = messagebox.askyesno(
                    "高风险删除确认",
                    "当前删除策略为“最彻底”，且为永久删除模式。\n"
                    "转换成功后将删除 m3u8、关联分片/KEY，并尽可能清理空目录。\n\n"
                    "此操作不可恢复，是否继续？",
                    icon="warning",
                )
                if not danger_confirm:
                    self._set_busy(False)
                    self.status_var.set("就绪")
                    self._append_log("用户取消了高风险删除策略，任务未开始", level="WARNING")
                    return

        if self.preview_before_start_var.get():
            preview_text = "\n".join(preview_lines[:20])
            if len(preview_lines) > 20:
                preview_text += "\n...（预览已截断，可在界面任务预览区查看完整内容）"
            proceed = messagebox.askyesno(
                "开始转换确认",
                f"确认以下转换任务？\n\n{preview_text}\n\n"
                "确定：开始转换 | 取消：返回修改",
            )
            if not proceed:
                self._set_busy(False)
                self.status_var.set("就绪")
                self._append_log("用户取消了转换任务", level="WARNING")
                return

        self._start_convert_with_sources(sources, output_dir)

    def _start_convert_with_sources(self, sources: list[tuple[str, str | None, Path | None]], output_dir: str) -> None:
        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.status_var.set("转换中...")
        self.progress_var.set(0.0)
        self.progress_text_var.set("0% - 开始转换")
        self.cleanup_progress_var.set(0.0)
        self.cleanup_progress_text_var.set("0% - 等待清理阶段")
        self._append_log(f"任务开始，共 {len(sources)} 个输入源", level="INFO")

        decrypt_options = self._collect_decrypt_options()
        transcode_options = self._collect_transcode_options()

        thread = threading.Thread(
            target=self._convert_worker,
            args=(
                sources,
                output_dir,
                self.ffmpeg_var.get().strip() or "ffmpeg",
                decrypt_options,
                transcode_options,
                self.enable_resume_var.get(),
            ),
            daemon=True,
        )
        thread.start()

    def preview_tasks(self) -> None:
        self._collect_sources_async(self._on_preview_sources_ready, "预览")

    def start_convert(self) -> None:
        if self._working:
            return
        self._collect_sources_async(self._on_start_sources_ready, "开始转换")

    def _convert_worker(
        self,
        sources: list[tuple[str, str | None, Path | None]],
        output_dir: str,
        ffmpeg_bin: str,
        decrypt_options: DecryptOptions,
        transcode_options: TranscodeOptions,
        resume_enabled: bool,
    ) -> None:
        total = len(sources)
        preset = self._preset_value(self.preset_var.get())
        conflict_strategy = self._conflict_value(self.conflict_var.get())
        base_output_name = self.output_name_var.get().strip() or None
        continue_on_error = self.continue_on_error_var.get()
        failed_items: list[str] = []
        cleanup_verify_issues: list[str] = []
        resume_store = ResumeStore(Path(output_dir)) if resume_enabled else None
        resume_lock = threading.Lock()
        parent_remaining_lock = threading.Lock()
        cleanup_verify_lock = threading.Lock()
        progress_map: dict[int, float] = {idx: 0.0 for idx in range(1, total + 1)}
        progress_lock = threading.Lock()
        max_workers = max(1, min(8, int(self.max_workers_var.get().strip() or "1")))
        cleanup_total = (
            sum(1 for source, _, _ in sources if self._is_local_file_source(source))
            if self.delete_source_after_success
            else 0
        )
        cleanup_state = {"done": 0}
        cleanup_lock = threading.Lock()
        parent_remaining: dict[Path, int] = {}
        for source, _, _ in sources:
            if not self._is_local_file_source(source):
                continue
            parent = Path(source).expanduser().resolve().parent
            parent_remaining[parent] = parent_remaining.get(parent, 0) + 1

        def calc_output_name(index: int, suggested_name: str | None) -> str | None:
            current_output_name = base_output_name or suggested_name
            if base_output_name and total > 1:
                current_output_name = f"{base_output_name}_{index}"
            return self._compose_output_name(current_output_name)

        def progress_callback_factory(index: int) -> Callable[[float, str], None]:
            def callback(percent: float, message: str) -> None:
                with progress_lock:
                    progress_map[index] = max(0.0, min(100.0, percent))
                    overall = sum(progress_map.values()) / total
                self._queue_progress_update(overall, f"[{index}/{total}] {message}")

            return callback

        def process_single(index: int, source: str, suggested_name: str | None, rel_subdir: Path | None) -> tuple[int, str, str | None]:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise CancelledError("用户取消了任务。")

            task_label = f"{index}/{total}"
            current_output_name = calc_output_name(index, suggested_name)
            task_id: str | None = None
            if resume_store is not None:
                with resume_lock:
                    task_id = resume_store.build_task_id(
                        source=source,
                        output_name=current_output_name,
                        output_subdir=str(rel_subdir) if rel_subdir is not None else None,
                    )
                    record = resume_store.get(task_id)
                if record is not None and record.status == "completed" and record.output_file and Path(record.output_file).exists():
                    self.master.after(
                        0,
                        self._append_log,
                        f"断点续传：跳过已完成任务 -> {record.output_file}",
                        "DEBUG",
                        task_label,
                    )
                    with progress_lock:
                        progress_map[index] = 100.0
                    return index, "skipped_resume", record.output_file

            target_output_dir = Path(output_dir)
            if rel_subdir is not None:
                target_output_dir = target_output_dir / rel_subdir
                target_output_dir.mkdir(parents=True, exist_ok=True)

            if resume_store is not None and task_id is not None:
                with resume_lock:
                    resume_store.mark(task_id, status="in_progress", note="开始处理")

            # 分片级续传：记录本地 m3u8 的已校验前缀，避免重复全量检查。
            if resume_store is not None and task_id is not None:
                checked_prefix = resume_store.get_segments_checked_prefix(task_id)
            else:
                checked_prefix = 0
            try:
                integrity = check_integrity(source, skip_checked_prefix=checked_prefix)
            except Exception as exc:
                return index, "failed", f"完整性校验失败：{exc}"
            if integrity.missing_segments:
                missing_hint = integrity.missing_segments[0]
                return index, "failed", f"检测到分片缺失（示例）：{missing_hint}"
            if resume_store is not None and task_id is not None and integrity.checked_segments > checked_prefix:
                with resume_lock:
                    resume_store.mark_segments_checked_prefix(task_id, integrity.checked_segments)

            self.master.after(0, self._append_log, f"开始处理：{source}", "INFO", task_label)
            if rel_subdir is not None:
                self.master.after(0, self._append_log, f"输出子目录：{target_output_dir}", "DEBUG", task_label)

            try:
                result = convert_m3u8_to_mp4(
                    input_file=source,
                    output_dir=str(target_output_dir),
                    ffmpeg_bin=ffmpeg_bin,
                    progress_callback=progress_callback_factory(index),
                    output_name=current_output_name,
                    preset=preset,
                    conflict_strategy=conflict_strategy,
                    cancel_event=self.cancel_event,
                    decrypt_options=decrypt_options,
                    transcode_options=transcode_options,
                    output_format=self.output_format_var.get().strip() or "mp4",
                )
            except (FFmpegNotFoundError, InvalidInputError, ConvertFailedError) as exc:
                if resume_store is not None and task_id is not None:
                    with resume_lock:
                        resume_store.mark(task_id, status="failed", note=str(exc))
                return index, "failed", str(exc)

            if resume_store is not None and task_id is not None:
                with resume_lock:
                    resume_store.mark(
                        task_id,
                        status="completed",
                        output_file=str(result.output_file),
                        note="完成" if not result.skipped else "跳过",
                    )

            source_parent = Path(source).expanduser().resolve().parent
            is_last_in_parent = False
            if self._is_local_file_source(source):
                with parent_remaining_lock:
                    remain = parent_remaining.get(source_parent, 0)
                    if remain > 0:
                        remain -= 1
                    parent_remaining[source_parent] = remain
                    is_last_in_parent = remain == 0

            if (
                self.delete_source_after_success
                and not result.skipped
                and self._is_local_file_source(source)
            ):
                try:
                    delete_scope_mode = self._normalize_delete_scope_mode(self.delete_scope_var.get())
                    include_related_files, cleanup_empty_dirs = self._delete_scope_flags()
                    with progress_lock:
                        overall = sum(progress_map.values()) / total
                    self.master.after(0, self.status_var.set, f"清理源文件中（{task_label}）...")
                    self._queue_progress_update(overall, f"[{task_label}] 转换完成，正在清理源文件...")

                    if delete_scope_mode == "with_related_and_dirs" and not is_last_in_parent:
                        self.master.after(0, self._append_log, f"同目录任务未完成，延迟到最后统一清理：{source_parent}", "DEBUG", task_label)
                        with cleanup_lock:
                            cleanup_state["done"] += 1
                            done = cleanup_state["done"]
                        if cleanup_total > 0:
                            self.master.after(
                                0,
                                self._update_cleanup_progress,
                                done / cleanup_total * 100,
                                f"清理排队 {done}/{cleanup_total}（{task_label}）",
                            )
                        self.master.after(0, self.status_var.set, "转换中...")
                        return index, "success", str(result.output_file)

                    deleted_files, deleted_dirs = self._delete_source_file(
                        source,
                        include_related_files=include_related_files,
                        cleanup_empty_dirs=cleanup_empty_dirs,
                        allow_fast_parent_remove=is_last_in_parent,
                        force_remove_parent_dir=(delete_scope_mode == "with_related_and_dirs"),
                    )
                    mode = "回收站" if self.delete_to_recycle_var.get() and send2trash is not None else "永久删除"
                    scope = self._delete_scope_label()
                    self.master.after(
                        0,
                        self._append_log,
                        f"已处理源文件（{mode}/{scope}）：{source} | 删除文件 {deleted_files} 个，清理空目录 {deleted_dirs} 个",
                        "DEBUG",
                        task_label,
                    )
                    verify_note = self._build_cleanup_verify_note(Path(source).expanduser().resolve())
                    self.master.after(0, self._append_log, verify_note, "INFO", task_label)
                    if not self._is_cleanup_verify_success(verify_note):
                        with cleanup_verify_lock:
                            cleanup_verify_issues.append(f"[{task_label}] {source} -> {verify_note}")
                    with cleanup_lock:
                        cleanup_state["done"] += 1
                        done = cleanup_state["done"]
                    if cleanup_total > 0:
                        self.master.after(
                            0,
                            self._update_cleanup_progress,
                            done / cleanup_total * 100,
                            f"清理中 {done}/{cleanup_total}（{task_label}）",
                        )
                    self.master.after(0, self.status_var.set, "转换中...")
                except OSError as exc:
                    self.master.after(0, self._append_log, f"删除源文件失败：{source} ({exc})", "WARNING", task_label)
                    with cleanup_lock:
                        cleanup_state["done"] += 1
                        done = cleanup_state["done"]
                    if cleanup_total > 0:
                        self.master.after(
                            0,
                            self._update_cleanup_progress,
                            done / cleanup_total * 100,
                            f"清理异常 {done}/{cleanup_total}（{task_label}）",
                        )

            if result.skipped:
                return index, "skipped", str(result.output_file)
            return index, "success", str(result.output_file)

        try:
            indexed_sources = list(enumerate(sources, start=1))
            if max_workers <= 1:
                for index, (source, suggested_name, rel_subdir) in indexed_sources:
                    idx, status, payload = process_single(index, source, suggested_name, rel_subdir)
                    task_label = f"{idx}/{total}"
                    if status == "failed":
                        if continue_on_error:
                            failed_items.append(f"[{task_label}] {source} -> {payload}")
                            self.master.after(0, self._append_log, f"失败，继续：{payload}", "ERROR", task_label)
                            continue
                        raise ConvertFailedError(payload or "转换失败")
                    if status in {"success", "skipped", "skipped_resume"}:
                        log_level = "SUCCESS" if status == "success" else "WARNING"
                        log_text = (
                            f"完成：{payload}"
                            if status == "success"
                            else f"跳过：{payload}"
                        )
                        self.master.after(0, self._append_log, log_text, log_level, task_label)
            else:
                self.master.after(0, self._append_log, f"并发模式已启用：{max_workers} 线程", "INFO", "全局")
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {
                        executor.submit(process_single, idx, src, name, sub): (idx, src)
                        for idx, (src, name, sub) in indexed_sources
                    }
                    for future in as_completed(future_map):
                        idx, src = future_map[future]
                        task_label = f"{idx}/{total}"
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            raise CancelledError("用户取消了任务。")
                        try:
                            _idx, status, payload = future.result()
                        except CancelledError:
                            raise
                        except Exception as exc:
                            status = "failed"
                            payload = str(exc)

                        if status == "failed":
                            failed_items.append(f"[{task_label}] {src} -> {payload}")
                            self.master.after(0, self._append_log, f"失败：{payload}", "ERROR", task_label)
                            if not continue_on_error:
                                raise ConvertFailedError(payload or "转换失败")
                            continue

                        log_level = "SUCCESS" if status == "success" else "WARNING"
                        log_text = f"完成：{payload}" if status == "success" else f"跳过：{payload}"
                        self.master.after(0, self._append_log, log_text, log_level, task_label)

            self.master.after(0, self._on_success_batch, failed_items, cleanup_verify_issues)
        except (FFmpegNotFoundError, InvalidInputError, ConvertFailedError, CancelledError) as exc:
            self.master.after(0, self._on_error, str(exc))
        except Exception as exc:  # pragma: no cover
            self.master.after(0, self._on_error, f"未预期错误：{exc}")

    def _on_success_batch(self, failed_items: list[str], cleanup_verify_issues: list[str]) -> None:
        self._set_busy(False)
        if self.delete_source_after_success:
            self._update_cleanup_progress(100.0, "源文件清理完成")
        else:
            self._update_cleanup_progress(100.0, "本次未启用源文件清理")
        if failed_items:
            self.status_var.set("部分完成")
            msg_title = "部分任务完成"
            msg_text = (
                f"任务处理完成！共 {len(failed_items)} 项失败：\n\n"
                "您可以查看日志区域了解详细信息，\n"
                "修改设置后可重新尝试失败的项目。"
            )
            self._play_notify_sound("error")
        elif cleanup_verify_issues:
            self.status_var.set("部分完成")
            msg_title = "转换完成（清理需关注）"
            msg_text = (
                "转换任务已完成，但清理核验发现部分目录仍有残留。\n\n"
                f"残留项：{len(cleanup_verify_issues)}\n"
                "请在日志中查看“核验：上级目录仍有 ...”对应条目。"
            )
            self._play_notify_sound("error")
        else:
            self.status_var.set("完成")
            msg_title = "全部转换完成"
            msg_text = "✓ 所有转换任务已成功完成！"
            self._play_notify_sound("success")
        
        self._update_progress(100.0, "全部任务完成")
        self._append_log("全部转换任务已完成", level="SUCCESS", task="全局")
        if failed_items:
            self._append_log(f"其中失败 {len(failed_items)} 项：", level="WARNING", task="全局")
            for item in failed_items:
                self._append_log(item, level="ERROR", task="全局")
        if cleanup_verify_issues:
            self._append_log(f"清理核验仍有残留 {len(cleanup_verify_issues)} 项：", level="WARNING", task="全局")
            for item in cleanup_verify_issues:
                self._append_log(item, level="WARNING", task="全局")
        self._save_config()
        messagebox.showinfo(msg_title, msg_text)

    def _on_error(self, message: str) -> None:
        message = sanitize_ffmpeg_error_text(message, message)
        self._set_busy(False)
        if self.delete_source_after_success:
            self._update_cleanup_progress(self.cleanup_progress_var.get(), "清理阶段已中断")
        if "取消" in message:
            self.status_var.set("已取消")
            self._append_log(f"任务取消：{message}", level="WARNING", task="全局")
            self._play_notify_sound("cancel")
            messagebox.showinfo("转换已取消", f"用户已取消转换任务\n\n{message}")
            return
        else:
            self.status_var.set("失败")
            self._append_log(f"错误：{message}", level="ERROR", task="全局")
            self._play_notify_sound("error")
            messagebox.showerror("转换失败", f"转换过程中出现错误：\n\n{message}\n\n请检查设置或日志获取更多信息。")

    def _save_config(self) -> None:
        delete_scope_mode = self._normalize_delete_scope_mode(self.delete_scope_var.get())
        include_related_files = delete_scope_mode != "playlist_only"
        cleanup_empty_dirs = delete_scope_mode == "with_related_and_dirs"
        self.config_model = AppConfig(
            last_output_dir=self.output_var.get().strip() or str(Path.cwd()),
            default_output_dir=self.default_output_dir,
            ffmpeg_path=self.ffmpeg_var.get().strip() or "ffmpeg",
            preset=self._preset_value(self.preset_var.get()),
            output_format=self.output_format_var.get().strip().lower() or "mp4",
            output_prefix=self.output_prefix_var.get(),
            output_suffix=self.output_suffix_var.get(),
            output_use_timestamp=self.output_timestamp_var.get(),
            conflict_strategy=self._conflict_value(self.conflict_var.get()),
            input_mode=(
                "local"
                if self.local_files
                else "url"
                if (urlparse(self.source_var.get().strip()).scheme in {"http", "https"})
                else "folder"
            ),
            folder_recursive_scan=self.folder_recursive_var.get(),
            folder_first_only_per_dir=self.folder_first_only_var.get(),
            preview_before_start=self.preview_before_start_var.get(),
            continue_on_error=self.continue_on_error_var.get(),
            smart_select_preference=self.smart_select_preference_var.get(),
            delete_to_recycle_bin=self.delete_to_recycle_var.get(),
            delete_scope_mode=delete_scope_mode,
            delete_include_related_files=include_related_files,
            delete_cleanup_empty_dirs=cleanup_empty_dirs,
            delete_preview_before_execute=self.delete_preview_before_execute_var.get(),
            enable_drag_drop=self.enable_drag_drop_var.get(),
            decrypt_auto_parse_key=self.decrypt_auto_parse_var.get(),
            manual_decrypt_key_hex=self.decrypt_key_var.get().strip(),
            manual_decrypt_iv_hex=self.decrypt_iv_var.get().strip(),
            transcode_mode=self.transcode_mode_var.get(),
            custom_video_resolution=self.custom_resolution_var.get().strip(),
            custom_video_bitrate=self.custom_video_bitrate_var.get().strip(),
            custom_video_fps=self.custom_fps_var.get().strip(),
            custom_audio_sample_rate=self.custom_audio_sample_rate_var.get().strip(),
            custom_audio_bitrate=self.custom_audio_bitrate_var.get().strip(),
            custom_templates_json=self._templates_to_json(),
            enable_resume=self.enable_resume_var.get(),
            enable_sound_notify=self.enable_sound_notify_var.get(),
            cleanup_preview_temp_on_exit=self.cleanup_preview_temp_on_exit_var.get(),
            max_workers=self.max_workers_var.get().strip() or "1",
            log_level_filter=self.log_level_filter_var.get().strip() or "全部",
            log_task_filter=self.log_task_filter_var.get().strip() or "全部任务",
            theme_mode=self._normalize_theme_mode(self.theme_mode_var.get()),
            progressbar_color_mode=self._normalize_progress_color_mode(self.progress_color_var.get()),
            window_geometry=self.master.winfo_geometry(),
        )
        save_config(self.config_model)
        self._refresh_delete_scope_status()

    def on_close(self) -> None:
        if self._log_render_after_id is not None:
            try:
                self.master.after_cancel(self._log_render_after_id)
            except Exception:
                pass
            self._log_render_after_id = None
        if self.cleanup_preview_temp_on_exit_var.get():
            removed = self._cleanup_preview_temp_files()
            if removed > 0:
                self._append_log(f"会话结束，已清理临时预览文件：{removed} 个", level="DEBUG", task="全局")
        self._save_config()
        self.master.destroy()
