import tkinter as tk
from pathlib import Path

from app.config import load_config
from app.ui import ConverterApp

try:
    from tkinterdnd2 import TkinterDnD  # type: ignore
except ImportError:  # pragma: no cover
    TkinterDnD = None


def _should_use_tkinter_dnd(enable_drag_drop: bool, dnd_module: object | None) -> bool:
    return enable_drag_drop and dnd_module is not None


def main() -> None:
    config = load_config(default_output_dir=str(Path.cwd()))
    use_dnd = _should_use_tkinter_dnd(config.enable_drag_drop, TkinterDnD)
    if use_dnd and TkinterDnD is not None:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    root.title("m3u8 转 mp4 转换工具")
    root.geometry("900x620")
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
