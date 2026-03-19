from __future__ import annotations

import threading
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover
    send2trash = None

from app.config import AppConfig, load_config, save_config
from app.converter import (
    CancelledError,
    ConvertFailedError,
    DeployFailedError,
    FFmpegNotFoundError,
    InvalidInputError,
    auto_detect_ffmpeg_path,
    convert_m3u8_to_mp4,
    deploy_ffmpeg,
)


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master = master
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
        self.conflict_var = tk.StringVar(value=self._conflict_label(self.config_model.conflict_strategy))
        self.folder_recursive_var = tk.BooleanVar(value=self.config_model.folder_recursive_scan)
        self.folder_first_only_var = tk.BooleanVar(value=self.config_model.folder_first_only_per_dir)
        self.preview_before_start_var = tk.BooleanVar(value=self.config_model.preview_before_start)
        self.continue_on_error_var = tk.BooleanVar(value=self.config_model.continue_on_error)
        self.smart_select_preference_var = tk.StringVar(value=self.config_model.smart_select_preference)
        self.delete_to_recycle_var = tk.BooleanVar(value=self.config_model.delete_to_recycle_bin)
        self.enable_drag_drop_var = tk.BooleanVar(value=self.config_model.enable_drag_drop)
        self.status_var = tk.StringVar(value="就绪")
        self.dependency_status_var = tk.StringVar(value="依赖状态：检测中...")
        self.drag_runtime_status_var = tk.StringVar(value="拖放状态：检测中...")
        self.progress_text_var = tk.StringVar(value="等待开始")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.ffmpeg_hint_var = tk.StringVar(value="FFmpeg：自动检测中...")

        self.cancel_event: threading.Event | None = None
        self.delete_source_after_success = False
        self.drag_drop_runtime_enabled = False
        self._working = False

        self._build_widgets()
        self._bind_live_validation()
        self._setup_drag_drop()
        self._update_folder_option_state(False)
        self.auto_detect_ffmpeg(show_message=False)
        self._refresh_drag_runtime_status()
        self._refresh_dependency_status()
        self._refresh_action_state()
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def _build_widgets(self) -> None:
        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, weight=0)
        self.columnconfigure(3, weight=0)

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
            command=lambda: (self._save_config(), self._refresh_dependency_status()),
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
        settings_menu.add_checkbutton(
            label="启用拖放（需重启生效）",
            variable=self.enable_drag_drop_var,
            command=self._on_toggle_drag_drop,
        )
        settings_menu.add_separator()
        settings_menu.add_command(label="设置默认输出目录", command=self.set_default_output_dir)
        settings_menu.add_command(label="重置默认输出目录", command=self.reset_default_output_dir)
        settings_btn.configure(menu=settings_menu)
        settings_btn.grid(row=0, column=3, sticky="e", pady=(0, 8))
        self.settings_btn = settings_btn

        self.help_btn = ttk.Button(self, text="帮助", command=self.open_help_window)
        self.help_btn.grid(row=0, column=2, sticky="e", padx=(0, 8), pady=(0, 8))

        ttk.Label(self, textvariable=self.ffmpeg_hint_var).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Label(self, text="输入来源（自动识别）：").grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(self, text="支持文件 / 文件夹 / URL").grid(row=1, column=1, sticky="w", pady=(0, 8))

        self.recursive_check = ttk.Checkbutton(
            self,
            text="递归扫描子目录",
            variable=self.folder_recursive_var,
        )
        self.recursive_check.grid(row=1, column=2, sticky="w", pady=(0, 8))

        self.first_only_check = ttk.Checkbutton(
            self,
            text="每目录仅首个",
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

        self.preview_before_check = ttk.Checkbutton(
            self,
            text="开始前弹窗预览",
            variable=self.preview_before_start_var,
        )
        self.preview_before_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.continue_on_error_check = ttk.Checkbutton(
            self,
            text="单项失败继续后续",
            variable=self.continue_on_error_var,
        )
        self.continue_on_error_check.grid(row=6, column=2, columnspan=2, sticky="w", pady=(0, 8))

        self.preview_btn = ttk.Button(self, text="预览任务", command=self.preview_tasks)
        self.preview_btn.grid(row=7, column=0, sticky="ew", pady=(0, 8))

        self.convert_btn = ttk.Button(self, text="开始转换", command=self.start_convert)
        self.convert_btn.grid(row=7, column=1, sticky="ew", pady=(0, 8))

        self.cancel_btn = ttk.Button(self, text="取消转换", command=self.cancel_convert, state=tk.DISABLED)
        self.cancel_btn.grid(row=7, column=2, sticky="ew", padx=(8, 8), pady=(0, 8))

        self.export_log_btn = ttk.Button(self, text="导出日志", command=self.export_log)
        self.export_log_btn.grid(row=7, column=3, sticky="ew", pady=(0, 8))

        ttk.Label(self, text="任务预览：").grid(row=8, column=0, sticky="w")
        self.preview_box = scrolledtext.ScrolledText(self, height=7, state=tk.DISABLED)
        self.preview_box.grid(row=9, column=0, columnspan=4, sticky="nsew", pady=(4, 8))

        ttk.Progressbar(
            self,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
        ).grid(row=10, column=0, columnspan=4, sticky="ew", pady=(0, 4))

        ttk.Label(self, textvariable=self.progress_text_var).grid(
            row=11,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(0, 8),
        )

        ttk.Label(self, text="日志：").grid(row=12, column=0, sticky="w")
        self.log_box = scrolledtext.ScrolledText(self, height=12, state=tk.DISABLED)
        self.log_box.grid(row=13, column=0, columnspan=4, sticky="nsew", pady=(4, 8))
        self.rowconfigure(13, weight=1)

        ttk.Label(self, textvariable=self.status_var).grid(row=14, column=0, columnspan=2, sticky="w")
        ttk.Label(self, textvariable=self.dependency_status_var).grid(row=14, column=2, columnspan=2, sticky="e")
        ttk.Label(self, textvariable=self.drag_runtime_status_var).grid(row=15, column=0, columnspan=4, sticky="w")

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

    def _delete_source_file(self, file_path: str) -> None:
        source_path = Path(file_path)
        if not source_path.exists():
            return

        if self.delete_to_recycle_var.get() and send2trash is not None:
            send2trash(str(source_path))
            return

        source_path.unlink()

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
        content = self.log_box.get("1.0", tk.END).strip()
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
        self._append_log(f"日志已导出：{file_path}")

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
        self._append_log(f"部署失败：{message}")
        messagebox.showerror(
            "FFmpeg 部署失败",
            f"✗ FFmpeg 部署过程出现错误：\n\n{message}\n\n"
            "请尝试以下方式：\n"
            "1. 手动选择 ffmpeg.exe 文件\n"
            "2. 确保系统已安装 winget\n"
            "3. 查看日志了解更多详情"
        )

    def auto_detect_ffmpeg(self, show_message: bool = True) -> None:
        detected = auto_detect_ffmpeg_path()
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

    def _append_log(self, text: str) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

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

        readme_text = readme_path.read_text(encoding="utf-8")
        sections = self._parse_readme_sections(readme_text)

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
        self.help_all_sections = sections

        tree.bind("<<TreeviewSelect>>", self._on_help_tree_select)
        self._rebuild_help_tree(self.help_all_sections)

    def _set_busy(self, busy: bool) -> None:
        self._working = busy
        self.cancel_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.settings_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.preview_before_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.continue_on_error_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        editable_state = tk.DISABLED if busy else tk.NORMAL
        self.source_entry.configure(state=editable_state)
        self.output_entry.configure(state=editable_state)
        self.output_name_entry.configure(state=editable_state)
        self.source_btn.configure(state=editable_state)
        self.output_browse_btn.configure(state=editable_state)
        self.export_log_btn.configure(state=editable_state)
        self._refresh_action_state()

    def _update_progress(self, percent: float, message: str) -> None:
        value = max(0.0, min(100.0, percent))
        self.progress_var.set(value)
        self.progress_text_var.set(f"{int(value)}% - {message}")

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
        lines = [f"总任务数：{len(sources)}", ""]
        for index, (source, suggested_name, rel_subdir) in enumerate(sources, start=1):
            current_output_name = base_output_name or suggested_name
            if base_output_name and len(sources) > 1:
                current_output_name = f"{base_output_name}_{index}"

            if not current_output_name:
                parsed = urlparse(source)
                current_output_name = Path(parsed.path).stem or f"task_{index}"

            target_output_dir = Path(output_dir)
            if rel_subdir is not None:
                target_output_dir = target_output_dir / rel_subdir

            target_file = target_output_dir / f"{current_output_name}.mp4"
            lines.append(f"[{index}] 输入：{source}")
            lines.append(f"    输出：{target_file}")
        return lines

    def preview_tasks(self) -> None:
        try:
            sources = self._collect_sources()
        except InvalidInputError as exc:
            messagebox.showwarning("输入无效", str(exc))
            return

        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("缺少目录", "请先选择输出目录。")
            return

        base_output_name = self.output_name_var.get().strip() or None
        lines = self._build_preview_lines(sources, output_dir, base_output_name)
        self._set_preview_text("\n".join(lines) + "\n")
        self._append_log("已刷新任务预览")

    def start_convert(self) -> None:
        if self._working:
            return

        try:
            sources = self._collect_sources()
        except InvalidInputError as exc:
            messagebox.showwarning("输入无效", str(exc))
            return

        output_dir = self.output_var.get().strip()
        if not output_dir:
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

            delete_choice = messagebox.askyesnocancel(
                "源文件处理",
                f"检测到 {local_source_count} 个本地源文件\n\n"
                f"删除方式：{delete_mode}\n\n"
                "转换成功后是否删除源 m3u8 文件？",
            )
            if delete_choice is None:
                self._append_log("用户取消了转换任务")
                return
            self.delete_source_after_success = delete_choice
            self._append_log(
                "本次任务源文件处理：转换成功后删除"
                if delete_choice
                else "本次任务源文件处理：保留源文件"
            )

        if self.preview_before_start_var.get():
            preview_text = "\n".join(preview_lines[:20])
            if len(preview_lines) > 20:
                preview_text += "\n...（预览已截断，可在界面任务预览区查看完整内容）"
            proceed = messagebox.askyesno(
                "开始转换确认",
                f"确认以下转换任务？\n\n{preview_text}\n\n"
                "确定：开始转换 | 取消：返回修改"
            )
            if not proceed:
                self._append_log("用户取消了转换任务")
                return

        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.status_var.set("转换中...")
        self.progress_var.set(0.0)
        self.progress_text_var.set("0% - 开始转换")
        self._append_log(f"任务开始，共 {len(sources)} 个输入源")

        thread = threading.Thread(
            target=self._convert_worker,
            args=(sources, output_dir, self.ffmpeg_var.get().strip() or "ffmpeg"),
            daemon=True,
        )
        thread.start()

    def _convert_worker(self, sources: list[tuple[str, str | None, Path | None]], output_dir: str, ffmpeg_bin: str) -> None:
        total = len(sources)
        preset = self._preset_value(self.preset_var.get())
        conflict_strategy = self._conflict_value(self.conflict_var.get())
        base_output_name = self.output_name_var.get().strip() or None
        continue_on_error = self.continue_on_error_var.get()
        failed_items: list[str] = []

        try:
            for index, (source, suggested_name, rel_subdir) in enumerate(sources, start=1):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise CancelledError("用户取消了任务。")

                current_output_name = base_output_name or suggested_name
                if base_output_name and total > 1:
                    current_output_name = f"{base_output_name}_{index}"

                target_output_dir = Path(output_dir)
                if rel_subdir is not None:
                    target_output_dir = target_output_dir / rel_subdir
                    target_output_dir.mkdir(parents=True, exist_ok=True)

                self.master.after(0, self._append_log, f"[{index}/{total}] 开始处理：{source}")
                if rel_subdir is not None:
                    self.master.after(0, self._append_log, f"[{index}/{total}] 输出子目录：{target_output_dir}")

                def progress_callback(percent: float, message: str, idx: int = index) -> None:
                    overall = ((idx - 1) + percent / 100.0) / total * 100
                    self.master.after(0, self._update_progress, overall, f"[{idx}/{total}] {message}")

                try:
                    result = convert_m3u8_to_mp4(
                        input_file=source,
                        output_dir=str(target_output_dir),
                        ffmpeg_bin=ffmpeg_bin,
                        progress_callback=progress_callback,
                        output_name=current_output_name,
                        preset=preset,
                        conflict_strategy=conflict_strategy,
                        cancel_event=self.cancel_event,
                    )
                except (FFmpegNotFoundError, InvalidInputError, ConvertFailedError) as exc:
                    if continue_on_error:
                        failed_items.append(f"[{index}/{total}] {source} -> {exc}")
                        self.master.after(0, self._append_log, f"[{index}/{total}] 失败，继续：{exc}")
                        continue
                    raise

                if result.skipped:
                    self.master.after(0, self._append_log, f"[{index}/{total}] 跳过同名文件：{result.output_file}")
                else:
                    self.master.after(0, self._append_log, f"[{index}/{total}] 完成：{result.output_file}")

                if (
                    self.delete_source_after_success
                    and not result.skipped
                    and self._is_local_file_source(source)
                ):
                    try:
                        self._delete_source_file(source)
                        mode = "回收站" if self.delete_to_recycle_var.get() and send2trash is not None else "永久删除"
                        self.master.after(0, self._append_log, f"[{index}/{total}] 已处理源文件（{mode}）：{source}")
                    except OSError as exc:
                        self.master.after(0, self._append_log, f"[{index}/{total}] 删除源文件失败：{source} ({exc})")

            self.master.after(0, self._on_success_batch, failed_items)
        except (FFmpegNotFoundError, InvalidInputError, ConvertFailedError, CancelledError) as exc:
            self.master.after(0, self._on_error, str(exc))
        except Exception as exc:  # pragma: no cover
            self.master.after(0, self._on_error, f"未预期错误：{exc}")

    def _on_success_batch(self, failed_items: list[str]) -> None:
        self._set_busy(False)
        if failed_items:
            self.status_var.set("部分完成")
            status_icon = "⚠️"
            msg_title = "部分任务完成"
            msg_text = (
                f"任务处理完成！共 {len(failed_items)} 项失败：\n\n"
                "您可以查看日志区域了解详细信息，\n"
                "修改设置后可重新尝试失败的项目。"
            )
        else:
            self.status_var.set("完成")
            status_icon = "✓"
            msg_title = "全部转换完成"
            msg_text = "✓ 所有转换任务已成功完成！"
        
        self._update_progress(100.0, "全部任务完成")
        self._append_log("全部转换任务已完成")
        if failed_items:
            self._append_log(f"其中失败 {len(failed_items)} 项：")
            for item in failed_items:
                self._append_log(item)
        self._save_config()
        messagebox.showinfo(msg_title, msg_text)

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        if "取消" in message:
            self.status_var.set("已取消")
            self._append_log(f"任务取消：{message}")
            messagebox.showinfo("转换已取消", f"用户已取消转换任务\n\n{message}")
            return
        else:
            self.status_var.set("失败")
            self._append_log(f"错误：{message}")
            messagebox.showerror("转换失败", f"转换过程中出现错误：\n\n{message}\n\n请检查设置或日志获取更多信息。")

    def _save_config(self) -> None:
        self.config_model = AppConfig(
            last_output_dir=self.output_var.get().strip() or str(Path.cwd()),
            default_output_dir=self.default_output_dir,
            ffmpeg_path=self.ffmpeg_var.get().strip() or "ffmpeg",
            preset=self._preset_value(self.preset_var.get()),
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
            enable_drag_drop=self.enable_drag_drop_var.get(),
            window_geometry=self.master.winfo_geometry(),
        )
        save_config(self.config_model)

    def on_close(self) -> None:
        self._save_config()
        self.master.destroy()
