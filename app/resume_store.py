from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class ResumeRecord:
    status: str
    output_file: str
    updated_at: str
    note: str = ""


class ResumeStore:
    """任务级断点记录：用于批量转换时跳过已完成项。"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.file_path = output_dir / ".m3u8_to_mp4_resume.json"
        self._data = self._load()

    @staticmethod
    def build_task_id(source: str, output_name: str | None, output_subdir: str | None) -> str:
        payload = f"{source}|{output_name or ''}|{output_subdir or ''}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, task_id: str) -> ResumeRecord | None:
        raw = self._data.get(task_id)
        if not isinstance(raw, dict):
            return None
        status = str(raw.get("status", ""))
        output_file = str(raw.get("output_file", ""))
        updated_at = str(raw.get("updated_at", ""))
        note = str(raw.get("note", ""))
        if not status:
            return None
        return ResumeRecord(status=status, output_file=output_file, updated_at=updated_at, note=note)

    def mark(self, task_id: str, status: str, output_file: str = "", note: str = "") -> None:
        previous = self._data.get(task_id, {}) if isinstance(self._data.get(task_id), dict) else {}
        self._data[task_id] = {
            "status": status,
            "output_file": output_file,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "note": note,
            "segments_checked_prefix": str(previous.get("segments_checked_prefix", "0")),
        }
        self._save()

    def get_segments_checked_prefix(self, task_id: str) -> int:
        raw = self._data.get(task_id)
        if not isinstance(raw, dict):
            return 0
        value = raw.get("segments_checked_prefix", "0")
        try:
            parsed = int(str(value))
        except ValueError:
            return 0
        return max(0, parsed)

    def mark_segments_checked_prefix(self, task_id: str, checked_prefix: int) -> None:
        raw = self._data.get(task_id)
        if not isinstance(raw, dict):
            raw = {
                "status": "pending",
                "output_file": "",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "note": "",
            }
            self._data[task_id] = raw
        raw["segments_checked_prefix"] = str(max(0, checked_prefix))
        raw["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.file_path.exists():
            return {}
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        try:
            self.file_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return

