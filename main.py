import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from app.config import load_config
from app.ui import ConverterApp

try:
    from tkinterdnd2 import TkinterDnD  # type: ignore
except ImportError:  # pragma: no cover
    TkinterDnD = None


def _should_use_tkinter_dnd(enable_drag_drop: bool, dnd_module: object | None) -> bool:
    return enable_drag_drop and dnd_module is not None


def _mount_scrollable_app(root: tk.Tk) -> ConverterApp:
    container = tk.Frame(root)
    container.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(container, highlightthickness=0)
    vbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)

    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    app = ConverterApp(root, auto_pack=False)
    app_window = canvas.create_window((0, 0), window=app, anchor="nw")

    resize_after_id: str | None = None
    last_canvas_width = 0
    scrollbar_visible = False

    def apply_host_theme() -> None:
        mode = app.theme_mode_var.get() if hasattr(app, "theme_mode_var") else "light"
        host_bg = "#1f1f1f" if mode == "dark" else "#f7f7f7"
        root.configure(bg=host_bg)
        container.configure(bg=host_bg)
        canvas.configure(bg=host_bg, highlightbackground=host_bg)

    def update_scroll_region() -> None:
        nonlocal scrollbar_visible, last_canvas_width
        current_width = canvas.winfo_width()
        if current_width > 1 and current_width != last_canvas_width:
            canvas.itemconfigure(app_window, width=current_width)
            last_canvas_width = current_width

        canvas.configure(scrollregion=canvas.bbox("all"))
        need_scroll = app.winfo_reqheight() > canvas.winfo_height() + 4
        if need_scroll == scrollbar_visible:
            return
        scrollbar_visible = need_scroll
        if need_scroll:
            vbar.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            vbar.pack_forget()

    def schedule_update_scroll_region() -> None:
        nonlocal resize_after_id
        if resize_after_id is not None:
            return
        # 合并连续 Configure 事件，降低窗口拖拽缩放时抖动。
        resize_after_id = root.after(24, flush_scroll_region_update)

    def flush_scroll_region_update() -> None:
        nonlocal resize_after_id
        resize_after_id = None
        update_scroll_region()

    def on_canvas_configure(event: tk.Event) -> None:
        _ = event
        schedule_update_scroll_region()

    def on_app_configure(_event: tk.Event) -> None:
        schedule_update_scroll_region()

    canvas.bind("<Configure>", on_canvas_configure)
    app.bind("<Configure>", on_app_configure)

    def on_mousewheel(event: tk.Event) -> None:
        if app.winfo_reqheight() <= canvas.winfo_height():
            return
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta != 0:
            canvas.yview_scroll(delta, "units")

    for widget in (canvas, app):
        widget.bind("<MouseWheel>", on_mousewheel)
        widget.bind("<Button-4>", lambda _event: canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda _event: canvas.yview_scroll(1, "units"))

    app.bind("<<ThemeChanged>>", lambda _event: apply_host_theme())
    apply_host_theme()

    root.after(80, update_scroll_region)
    return app


def main() -> None:
    config = load_config(default_output_dir=str(Path.cwd()))
    use_dnd = _should_use_tkinter_dnd(config.enable_drag_drop, TkinterDnD)
    if use_dnd and TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    root.title("m3u8 转 mp4 转换工具")
    root.geometry("900x620")
    app = _mount_scrollable_app(root)
    if os.environ.get("M3U8_OPEN_HELP_ON_START") == "1":
        root.after(1000, app.open_help_window)
    root.mainloop()


if __name__ == "__main__":
    main()
