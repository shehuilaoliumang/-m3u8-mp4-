from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class M3U8IntegrityReport:
    source: str
    encrypted: bool
    encryption_method: str
    key_uri: str
    total_segments: int
    checked_segments: int
    missing_segments: list[str]
    remote_check_skipped: bool


@dataclass(frozen=True)
class M3U8FirstSegment:
    source: str
    segment_uri: str
    resolved_uri: str


@dataclass(frozen=True)
class M3U8Parsed:
    source: str
    lines: list[str]
    encrypted: bool
    encryption_method: str
    key_uri: str
    segments: list[str]


def _is_http_source(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _read_text(source: str) -> str:
    if _is_http_source(source):
        request = Request(source, headers={"User-Agent": "m3u8ToMp4/1.0"})
        with urlopen(request, timeout=8) as response:  # nosec B310
            return response.read().decode("utf-8", errors="replace")
    return Path(source).expanduser().resolve().read_text(encoding="utf-8", errors="replace")


def _resolve_uri(base_source: str, uri: str) -> str:
    if _is_http_source(uri):
        return uri
    if _is_http_source(base_source):
        return urljoin(base_source, uri)
    return str((Path(base_source).expanduser().resolve().parent / uri).resolve())


def parse_m3u8(source: str) -> M3U8Parsed:
    text = _read_text(source)
    segments: list[str] = []
    encrypted = False
    encryption_method = ""
    key_uri = ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY"):
            encrypted = True
            attrs = line.split(":", 1)[1] if ":" in line else ""
            for chunk in attrs.split(","):
                if "=" not in chunk:
                    continue
                k, v = chunk.split("=", 1)
                key = k.strip().upper()
                val = v.strip().strip('"')
                if key == "METHOD":
                    encryption_method = val
                elif key == "URI":
                    key_uri = _resolve_uri(source, val)
            continue
        if line.startswith("#"):
            continue
        segments.append(line)

    return M3U8Parsed(
        source=source,
        lines=text.splitlines(),
        encrypted=encrypted,
        encryption_method=encryption_method,
        key_uri=key_uri,
        segments=segments,
    )


def check_integrity(
    source: str,
    skip_checked_prefix: int = 0,
) -> M3U8IntegrityReport:
    parsed = parse_m3u8(source)
    missing: list[str] = []
    checked = 0
    remote_skipped = False

    is_http = _is_http_source(source)
    if is_http:
        # 避免大量网络探测导致校验过慢，仅做结构检查。
        remote_skipped = True
    else:
        for idx, segment_uri in enumerate(parsed.segments):
            if idx < max(0, skip_checked_prefix):
                checked += 1
                continue
            segment_path = Path(_resolve_uri(source, segment_uri))
            if not segment_path.exists():
                missing.append(str(segment_path))
            checked += 1

    return M3U8IntegrityReport(
        source=source,
        encrypted=parsed.encrypted,
        encryption_method=parsed.encryption_method,
        key_uri=parsed.key_uri,
        total_segments=len(parsed.segments),
        checked_segments=checked,
        missing_segments=missing,
        remote_check_skipped=remote_skipped,
    )


def get_first_segment(source: str) -> M3U8FirstSegment | None:
    parsed = parse_m3u8(source)
    if not parsed.segments:
        return None
    first = parsed.segments[0]
    return M3U8FirstSegment(
        source=source,
        segment_uri=first,
        resolved_uri=_resolve_uri(source, first),
    )

