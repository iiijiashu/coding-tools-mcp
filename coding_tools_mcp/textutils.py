from __future__ import annotations

import locale
import os
import codecs
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_LINES = 2000


def output_page_bytes(data: bytes, start: int, limit: int, *, final: bool) -> tuple[bytes, str]:
    """Keep byte cursors while returning whole characters, including tiny pages."""
    encoding = "utf-8"
    # Retention may cut the first character. Inspect the remaining UTF-8
    # bytes so one lost prefix does not corrupt every subsequent page.
    leading = 0
    while leading < min(3, len(data)) and 0x80 <= data[leading] <= 0xBF:
        leading += 1
    try:
        codecs.getincrementaldecoder(encoding)(errors="strict").decode(data[leading:], final=False)
    except UnicodeDecodeError:
        if os.name == "nt":
            encoding = locale.getpreferredencoding(False)
        try:
            codecs.getincrementaldecoder(encoding)(errors="strict").decode(data, final=False)
        except (UnicodeDecodeError, LookupError):
            return data[start:start + limit], "unknown"
    end = min(len(data), start + limit)
    while True:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        decoder.decode(data[start:end], final=False)
        pending = len(decoder.getstate()[0])
        if not pending:
            return data[start:end], encoding
        complete_end = end - pending
        if complete_end > start:
            return data[start:complete_end], encoding
        if end == len(data):
            return (data[start:end] if final else b""), encoding
        end += 1


def decode_output_bytes(data: bytes) -> str:
    """Decode command output without corrupting UTF-8 or Windows OEM text."""

    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    if os.name == "nt":
        getencoding = getattr(locale, "getencoding", None)
        encoding = (getencoding() if getencoding is not None else locale.getpreferredencoding(False)) or "utf-8"
        if encoding.lower().replace("-", "") not in {"utf8", "utf8sig"}:
            try:
                return data.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                pass
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class TextTruncation:
    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def metadata(self, *, prefix: str = "") -> dict[str, Any]:
        key = f"{prefix}_" if prefix else ""
        return {
            f"{key}truncated_by": self.truncated_by,
            f"{key}total_lines": self.total_lines,
            f"{key}total_bytes": self.total_bytes,
            f"{key}output_lines": self.output_lines,
            f"{key}output_bytes": self.output_bytes,
            f"{key}last_line_partial": self.last_line_partial,
            f"{key}first_line_exceeds_limit": self.first_line_exceeds_limit,
        }


def truncate_text_head(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = 50 * 1024) -> TextTruncation:
    if max_lines <= 0:
        max_lines = 1
    if max_bytes <= 0:
        max_bytes = 1
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TextTruncation(text, False, None, total_lines, total_bytes, total_lines, total_bytes, False, False, max_lines, max_bytes)

    first_line_bytes = len(lines[0].encode("utf-8")) if lines else 0
    if first_line_bytes > max_bytes:
        prefix = truncate_string_to_bytes_from_start(lines[0], max_bytes)
        return TextTruncation(
            prefix,
            True,
            "bytes",
            total_lines,
            total_bytes,
            1 if prefix else 0,
            len(prefix.encode("utf-8")),
            False,
            True,
            max_lines,
            max_bytes,
        )

    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines):
        if len(output) >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output.append(line)
        output_bytes += line_bytes
    content = "\n".join(output)
    return TextTruncation(
        content,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output),
        len(content.encode("utf-8")),
        False,
        False,
        max_lines,
        max_bytes,
    )


def truncate_text_tail(text: str, *, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = 50 * 1024) -> TextTruncation:
    if max_lines <= 0:
        max_lines = 1
    if max_bytes <= 0:
        max_bytes = 1
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TextTruncation(text, False, None, total_lines, total_bytes, total_lines, total_bytes, False, False, max_lines, max_bytes)

    candidate_lines = lines[:-1] if lines and lines[-1] == "" else lines
    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for reverse_index, line in enumerate(reversed(candidate_lines)):
        if len(output) >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if reverse_index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output:
                partial = truncate_string_to_bytes_from_end(line, max_bytes)
                output.append(partial)
                last_line_partial = True
            break
        output.append(line)
        output_bytes += line_bytes
    output.reverse()
    content = "\n".join(output)
    return TextTruncation(
        content,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output),
        len(content.encode("utf-8")),
        last_line_partial,
        False,
        max_lines,
        max_bytes,
    )


def truncate_string_to_bytes_from_start(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    end = max(0, min(max_bytes, len(data)))
    while end > 0 and end < len(data) and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[:end].decode("utf-8", errors="replace")


def truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    start = len(data) - max_bytes
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:].decode("utf-8", errors="replace")
