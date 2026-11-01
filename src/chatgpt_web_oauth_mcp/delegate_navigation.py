"""Bounded Code Map navigation hints for explicitly scoped read-only delegates.

This module does not establish source truth: the delegated reviewer must still
read and cite the source lines. It prevents a broad linear scan of large files
when the caller already provided a concrete scope and relevant identifiers.
"""

from __future__ import annotations

from pathlib import Path
import re
from collections.abc import Sequence

from .code_map import python_symbol_spans


MAX_NAVIGATION_FILES = 6
MAX_NAVIGATION_FILE_BYTES = 800_000
MAX_NAVIGATION_SYMBOLS_PER_FILE = 12
MAX_NAVIGATION_HINTS = 36
_TOKENS = re.compile(r"[a-zA-Z][a-zA-Z_0-9]{3,}")


def has_concrete_python_scope(*, cwd: Path, paths: Sequence[str]) -> bool:
    """Require a real, contained source file before applying an auto-budget."""

    if not paths or len(paths) > MAX_NAVIGATION_FILES:
        return False
    root = cwd.resolve()
    for filename in paths:
        if not isinstance(filename, str):
            continue
        raw = Path(filename)
        if raw.is_absolute() or raw.suffix != ".py" or ".." in raw.parts:
            continue
        try:
            source = (root / raw).resolve()
            if source.is_relative_to(root) and source.is_file():
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def scoped_navigation_hints(
    *,
    cwd: Path,
    task_text: str,
    files_in_scope: Sequence[str],
    context_files: Sequence[str],
) -> list[str]:
    """Return bounded source symbol ranges without reading outside cwd."""

    if not files_in_scope and not context_files:
        return []
    root = cwd.resolve()
    words = {
        word.lower()
        for raw in _TOKENS.findall(task_text[:12_000])
        for word in raw.split("_")
        if len(word) >= 4
    }
    full_text = task_text[:12_000].lower()
    hints: list[str] = []
    seen: set[Path] = set()
    for filename in (*files_in_scope, *context_files):
        if len(seen) >= MAX_NAVIGATION_FILES or len(hints) >= MAX_NAVIGATION_HINTS:
            break
        if not isinstance(filename, str):
            continue
        raw = Path(filename)
        if raw.is_absolute() or raw.suffix != ".py" or ".." in raw.parts:
            continue
        try:
            source = (root / raw).resolve()
            if source in seen or not source.is_relative_to(root) or not source.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            if source.stat().st_size > MAX_NAVIGATION_FILE_BYTES:
                continue
            code = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        seen.add(source)
        try:
            spans = python_symbol_spans(path=source, text=code)
        except (RecursionError, ValueError):
            continue
        ranked: list[tuple[int, dict[str, object]]] = []
        for span in spans:
            name = str(span.get("name") or "").lower()
            if not name:
                continue
            pieces = {piece for piece in name.split("_") if len(piece) >= 4}
            score = 8 if name in full_text else len(pieces & words) * 2
            if score:
                ranked.append((score, span))
        ranked.sort(
            key=lambda pair: (-pair[0], int(pair[1]["start_line"]), str(pair[1]["qualname"]))
        )
        # No matching identifiers: give a tiny top-level map, not an
        # arbitrary list of hundreds of methods that encourages file sweeps.
        chosen = [span for _, span in ranked[:MAX_NAVIGATION_SYMBOLS_PER_FILE]]
        if not chosen:
            chosen = [
                span for span in spans
                if "." not in str(span.get("qualname") or "")
            ][:4]
        relative = source.relative_to(root).as_posix()
        for span in chosen[: max(0, MAX_NAVIGATION_HINTS - len(hints))]:
            hints.append(
                f"{relative}:{span['start_line']}-{span['end_line']} "
                f"{span['kind']} {span['qualname']}"
            )
    return hints
