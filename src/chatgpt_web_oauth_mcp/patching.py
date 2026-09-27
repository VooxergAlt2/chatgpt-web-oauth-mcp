from __future__ import annotations

import difflib
import hashlib
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import stat

from .mutation_io import (
    atomic_write_bytes,
    durable_unlink,
    exclusive_mutation_lock,
    revision_bytes,
)
from .pathing import resolve_path


class PatchError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


@dataclass(frozen=True)
class DiffLine:
    kind: str
    text: str


@dataclass(frozen=True)
class AddFilePatch:
    path: str
    lines: list[str]


@dataclass(frozen=True)
class DeleteFilePatch:
    path: str


@dataclass(frozen=True)
class UpdateHunk:
    lines: list[DiffLine]
    patch_line: int


@dataclass(frozen=True)
class UpdateFilePatch:
    path: str
    move_to: str | None
    hunks: list[UpdateHunk]


@dataclass(frozen=True)
class PlannedChange:
    kind: str
    path: Path
    previous_path: Path | None
    old_text: str
    new_text: str
    hunks_applied: int


@dataclass(frozen=True)
class PathSnapshot:
    path: Path
    existed: bool
    raw: bytes | None
    mode: int | None


@dataclass(frozen=True)
class ResolvedOperation:
    operation: PatchOperation
    path: Path
    move_to: Path | None = None


PatchOperation = AddFilePatch | DeleteFilePatch | UpdateFilePatch


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _split_lines(text: str) -> list[str]:
    return text.splitlines()


def _join_lines(lines: list[str], *, trailing_newline: bool) -> str:
    if not lines:
        return ""
    suffix = "\n" if trailing_newline else ""
    return "\n".join(lines) + suffix


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:1024]:
        raise PatchError("not_text_file", f"Binary files are not supported: {path}", path=str(path))
    return raw.decode("utf-8", errors="replace")


def _snapshot_path(path: Path) -> PathSnapshot:
    if not path.exists():
        return PathSnapshot(path=path, existed=False, raw=None, mode=None)
    if not path.is_file():
        raise PatchError("not_a_file", f"Path is not a file: {path}", path=str(path))
    try:
        raw = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise PatchError(
            "file_read_failed",
            f"Failed to snapshot file before patching: {path}: {exc}",
            path=str(path),
        ) from None
    return PathSnapshot(path=path, existed=True, raw=raw, mode=mode)


def _snapshot_revision(snapshot: PathSnapshot) -> str | None:
    return revision_bytes(snapshot.raw) if snapshot.raw is not None else None


def _snapshot_matches(snapshot: PathSnapshot) -> bool:
    if not snapshot.existed:
        return not snapshot.path.exists()
    if not snapshot.path.exists() or not snapshot.path.is_file() or snapshot.raw is None:
        return False
    try:
        return (
            revision_bytes(snapshot.path.read_bytes()) == revision_bytes(snapshot.raw)
            and stat.S_IMODE(snapshot.path.stat().st_mode) == snapshot.mode
        )
    except OSError:
        return False


def _restore_snapshot(snapshot: PathSnapshot) -> None:
    if snapshot.existed:
        assert snapshot.raw is not None
        assert snapshot.mode is not None
        atomic_write_bytes(snapshot.path, snapshot.raw, mode=snapshot.mode)
        if not _snapshot_matches(snapshot):
            raise OSError(f"rollback verification failed for {snapshot.path}")
        return
    if snapshot.path.exists():
        durable_unlink(snapshot.path)
    if snapshot.path.exists():
        raise OSError(f"rollback removal verification failed for {snapshot.path}")


def _next_is_operation_header(line: str) -> bool:
    return (
        line.startswith("*** Add File: ")
        or line.startswith("*** Delete File: ")
        or line.startswith("*** Update File: ")
        or line == "*** End Patch"
    )


def _parse_add_file(lines: list[str], start: int) -> tuple[AddFilePatch, int]:
    path = lines[start][len("*** Add File: ") :]
    index = start + 1
    content: list[str] = []
    while index < len(lines) and not _next_is_operation_header(lines[index]):
        line = lines[index]
        if not line.startswith("+"):
            raise PatchError("invalid_patch", f"Add file lines must start with '+': {line}")
        content.append(line[1:])
        index += 1
    return AddFilePatch(path=path, lines=content), index


def _parse_hunk(lines: list[str], start: int) -> tuple[UpdateHunk, int]:
    patch_line = start + 1
    index = start
    if lines[index].startswith("@@"):
        index += 1

    diff_lines: list[DiffLine] = []
    while index < len(lines):
        line = lines[index]
        if _next_is_operation_header(line) or line.startswith("@@"):
            break
        if line == "*** End of File":
            index += 1
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise PatchError("invalid_patch", f"Unexpected patch line: {line}")
        diff_lines.append(DiffLine(kind=line[0], text=line[1:]))
        index += 1

    if not diff_lines:
        raise PatchError("invalid_patch", "Update hunks must contain at least one diff line.")

    has_additions = any(line.kind == "+" for line in diff_lines)
    has_removals = any(line.kind == "-" for line in diff_lines)
    has_context = any(line.kind == " " for line in diff_lines)

    if not has_additions and not has_removals:
        raise PatchError(
            "empty_hunk",
            (
                f"Hunk at patch line {patch_line} contains only context lines; "
                "did you mean to add '-' or '+' markers?"
            ),
            patch_line=patch_line,
        )

    if has_additions and not has_removals and not has_context:
        raise PatchError(
            "unanchored_hunk",
            (
                f"Hunk at patch line {patch_line} contains only '+' lines and cannot be "
                "anchored uniquely; add surrounding context or '-' lines."
            ),
            patch_line=patch_line,
        )

    return UpdateHunk(lines=diff_lines, patch_line=patch_line), index


def _parse_update_file(lines: list[str], start: int) -> tuple[UpdateFilePatch, int]:
    path = lines[start][len("*** Update File: ") :]
    index = start + 1
    move_to: str | None = None
    if index < len(lines) and lines[index].startswith("*** Move to: "):
        move_to = lines[index][len("*** Move to: ") :]
        index += 1

    hunks: list[UpdateHunk] = []
    while index < len(lines) and not _next_is_operation_header(lines[index]):
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)

    if not hunks and move_to is None:
        raise PatchError("invalid_patch", f"Update file patch has no changes: {path}")
    return UpdateFilePatch(path=path, move_to=move_to, hunks=hunks), index


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchError("invalid_patch", "Patch must start with '*** Begin Patch'.")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "*** End Patch":
            return operations
        if line.startswith("*** Add File: "):
            operation, index = _parse_add_file(lines, index)
            operations.append(operation)
            continue
        if line.startswith("*** Delete File: "):
            operations.append(DeleteFilePatch(path=line[len("*** Delete File: ") :]))
            index += 1
            continue
        if line.startswith("*** Update File: "):
            operation, index = _parse_update_file(lines, index)
            operations.append(operation)
            continue
        raise PatchError("invalid_patch", f"Unexpected patch header: {line}")

    raise PatchError("invalid_patch", "Patch must end with '*** End Patch'.")


def _find_sequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return -1
    max_start = len(lines) - len(needle) + 1
    for index in range(max(start, 0), max_start + 1):
        if lines[index : index + len(needle)] == needle:
            return index
    return -1


def _find_sequence_matches(lines: list[str], needle: list[str]) -> list[int]:
    if not needle:
        return []
    max_start = len(lines) - len(needle) + 1
    if max_start < 0:
        return []
    matches: list[int] = []
    for index in range(0, max_start + 1):
        if lines[index : index + len(needle)] == needle:
            matches.append(index)
    return matches


def _fuzzy_hunk_candidates(
    lines: list[str], needle: list[str], *, k: int = 3
) -> list[dict[str, object]]:
    """Return the top ``k`` line windows in ``lines`` that most resemble
    ``needle``. Each result carries a 1-based ``line``, similarity ratio and
    a short ``snippet`` so failure payloads can guide the caller.
    """
    window_size = max(len(needle), 1)
    needle_blob = "\n".join(needle)
    if not lines or not needle_blob:
        return []
    scored: list[tuple[float, int, str]] = []
    for i in range(0, max(len(lines) - window_size + 1, 1)):
        window = "\n".join(lines[i : i + window_size])
        ratio = difflib.SequenceMatcher(None, window, needle_blob, autojunk=False).ratio()
        if ratio <= 0.0:
            continue
        scored.append((ratio, i + 1, window))
    scored.sort(key=lambda item: item[0], reverse=True)
    suggestions: list[dict[str, object]] = []
    for ratio, line_no, snippet in scored[:k]:
        preview = snippet if len(snippet) <= 400 else snippet[:400] + "\u2026"
        suggestions.append(
            {
                "line": line_no,
                "similarity": round(ratio, 3),
                "snippet": preview,
            }
        )
    return suggestions


def _exact_hunk_candidates(
    lines: list[str], needle: list[str], matches: list[int], *, k: int = 3
) -> list[dict[str, object]]:
    suggestions: list[dict[str, object]] = []
    window_size = max(len(needle), 1)
    for index in matches[:k]:
        snippet = "\n".join(lines[index : index + window_size])
        preview = snippet if len(snippet) <= 400 else snippet[:400] + "\u2026"
        suggestions.append({"line": index + 1, "snippet": preview})
    return suggestions


def _apply_hunk(
    lines: list[str],
    hunk: UpdateHunk,
    cursor: int,
    *,
    path: Path,
    hunk_index: int,
) -> tuple[list[str], int]:
    old_lines = [line.text for line in hunk.lines if line.kind != "+"]
    new_lines = [line.text for line in hunk.lines if line.kind != "-"]
    search_start = max(cursor - len(old_lines), 0)
    matches = _find_sequence_matches(lines, old_lines)
    if not matches:
        raise PatchError(
            "patch_context_not_found",
            (
                f"Could not match update hunk #{hunk_index + 1} in {path}. "
                "See `candidates` for the closest line windows; the hunk's expected "
                "context is in `expected`."
            ),
            path=str(path),
            hunk_index=hunk_index,
            patch_line=hunk.patch_line,
            expected=old_lines,
            search_started_at_line=search_start + 1,
            candidates=_fuzzy_hunk_candidates(lines, old_lines, k=3),
        )
    if len(matches) != 1:
        raise PatchError(
            "ambiguous_context_match",
            (
                f"Update hunk #{hunk_index + 1} in {path} matched {len(matches)} locations. "
                "Patch context must match exactly one location; add more surrounding context."
            ),
            path=str(path),
            hunk_index=hunk_index,
            patch_line=hunk.patch_line,
            expected=old_lines,
            match_count=len(matches),
            expected_match_count=1,
            matching_lines=[match + 1 for match in matches],
            candidates=_exact_hunk_candidates(lines, old_lines, matches, k=3),
        )
    match_index = matches[0]
    updated = lines[:match_index] + new_lines + lines[match_index + len(old_lines) :]
    return updated, match_index + len(new_lines)


def _plan_update(path: Path, move_to: Path | None, hunks: list[UpdateHunk]) -> PlannedChange:
    if not path.exists():
        raise PatchError("file_not_found", f"File not found: {path}", path=str(path))
    if not path.is_file():
        raise PatchError("not_a_file", f"Path is not a file: {path}", path=str(path))

    original = _read_text(path)
    lines = _split_lines(original)
    cursor = 0
    for hunk_index, hunk in enumerate(hunks):
        lines, cursor = _apply_hunk(lines, hunk, cursor, path=path, hunk_index=hunk_index)

    target = move_to or path
    if move_to and move_to.exists() and move_to != path:
        raise PatchError("target_exists", f"Move target already exists: {move_to}", path=str(move_to))

    return PlannedChange(
        kind="move" if move_to and move_to != path else "update",
        path=target,
        previous_path=path if move_to and move_to != path else None,
        old_text=original,
        new_text=_join_lines(lines, trailing_newline=original.endswith("\n")),
        hunks_applied=len(hunks),
    )


def _plan_add(path: Path, lines: list[str]) -> PlannedChange:
    if path.exists():
        raise PatchError("path_exists", f"Path already exists: {path}", path=str(path))
    return PlannedChange(
        kind="add",
        path=path,
        previous_path=None,
        old_text="",
        new_text=_join_lines(lines, trailing_newline=bool(lines)),
        hunks_applied=1,
    )


def _plan_delete(path: Path) -> PlannedChange:
    if not path.exists():
        raise PatchError("path_not_found", f"Path not found: {path}", path=str(path))
    if path.is_dir():
        raise PatchError("not_a_file", f"Delete file patch only supports files: {path}", path=str(path))
    return PlannedChange(
        kind="delete",
        path=path,
        previous_path=None,
        old_text=_read_text(path),
        new_text="",
        hunks_applied=1,
    )


def _serialize_change(change: PlannedChange) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": change.kind,
        "path": str(change.path),
    }
    if change.previous_path is not None:
        payload["previous_path"] = str(change.previous_path)
    return payload


def _render_diff(change: PlannedChange) -> str:
    old_path = str(change.previous_path or change.path)
    new_path = str(change.path)
    return "".join(
        difflib.unified_diff(
            change.old_text.splitlines(keepends=True),
            change.new_text.splitlines(keepends=True),
            fromfile=old_path,
            tofile=new_path,
        )
    )


def _resolve_operations(
    operations: list[PatchOperation],
    *,
    workspace_root: Path,
) -> tuple[list[ResolvedOperation], list[Path]]:
    resolved: list[ResolvedOperation] = []
    touched_paths: list[Path] = []
    seen: dict[Path, int] = {}
    for index, operation in enumerate(operations):
        path = resolve_path(operation.path, workspace_root)
        move_to = (
            resolve_path(operation.move_to, workspace_root)
            if isinstance(operation, UpdateFilePatch) and operation.move_to
            else None
        )
        operation_paths = [path]
        if move_to is not None and move_to != path:
            operation_paths.append(move_to)
        for candidate in operation_paths:
            if candidate in seen:
                raise PatchError(
                    "conflicting_patch_paths",
                    (
                        f"Patch path is touched by more than one operation: {candidate}. "
                        "Combine changes to one path into a single patch operation."
                    ),
                    path=str(candidate),
                    first_operation_index=seen[candidate],
                    operation_index=index,
                )
            seen[candidate] = index
            touched_paths.append(candidate)
        resolved.append(ResolvedOperation(operation=operation, path=path, move_to=move_to))
    return resolved, sorted(touched_paths, key=lambda item: str(item.resolve(strict=False)))


def _plan_resolved_operation(item: ResolvedOperation) -> PlannedChange:
    operation = item.operation
    if isinstance(operation, AddFilePatch):
        return _plan_add(item.path, operation.lines)
    if isinstance(operation, DeleteFilePatch):
        return _plan_delete(item.path)
    return _plan_update(item.path, item.move_to, operation.hunks)


def _current_revision(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        return revision_bytes(path.read_bytes())
    except OSError:
        return None


def _apply_change_transactional(
    change: PlannedChange,
    *,
    snapshots: dict[Path, PathSnapshot],
    attempted_paths: list[Path],
) -> None:
    if change.kind == "delete":
        attempted_paths.append(change.path)
        durable_unlink(change.path)
        if change.path.exists():
            raise OSError(f"delete verification failed for {change.path}")
        return

    raw = change.new_text.encode("utf-8")
    source_path = change.previous_path or change.path
    source_snapshot = snapshots[source_path]
    mode = source_snapshot.mode if source_snapshot.mode is not None else 0o600
    attempted_paths.append(change.path)
    atomic_write_bytes(change.path, raw, mode=mode)
    if _current_revision(change.path) != revision_bytes(raw):
        raise OSError(f"post-write revision verification failed for {change.path}")

    if change.kind == "move" and change.previous_path is not None and change.previous_path != change.path:
        attempted_paths.append(change.previous_path)
        durable_unlink(change.previous_path)
        if change.previous_path.exists():
            raise OSError(f"move source removal verification failed for {change.previous_path}")


def _rollback_attempted_paths(
    attempted_paths: list[Path],
    *,
    snapshots: dict[Path, PathSnapshot],
) -> list[str]:
    rollback_errors: list[str] = []
    restored: set[Path] = set()
    for path in reversed(attempted_paths):
        if path in restored:
            continue
        restored.add(path)
        try:
            _restore_snapshot(snapshots[path])
        except OSError as exc:
            rollback_errors.append(f"{path}: {exc}")
    return rollback_errors


def _diff_line_counts(diff_text: str) -> tuple[int, int]:
    lines_added = 0
    lines_removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            lines_added += 1
            continue
        if line.startswith("-"):
            lines_removed += 1
    return lines_added, lines_removed


def _change_warnings(change: PlannedChange, *, lines_added: int, lines_removed: int) -> list[str]:
    warnings: list[str] = []
    if change.kind in {"update", "move"} and lines_added > 0 and lines_removed == 0:
        warnings.append(
            "update inserted lines without removing any existing lines; verify this was intended"
        )
    return warnings


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize_change(change: PlannedChange, *, diff_text: str) -> dict[str, object]:
    lines_added, lines_removed = _diff_line_counts(diff_text)
    warnings = _change_warnings(change, lines_added=lines_added, lines_removed=lines_removed)
    payload: dict[str, object] = {
        "kind": change.kind,
        "path": str(change.path),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "bytes_before": len(change.old_text.encode("utf-8")),
        "bytes_after": len(change.new_text.encode("utf-8")),
        "hunks_applied": change.hunks_applied,
        "sha256_after": _sha256_text(change.new_text),
        "warnings": warnings,
    }
    if change.previous_path is not None:
        payload["previous_path"] = str(change.previous_path)
    return payload


def apply_patch(
    *,
    patch: str,
    workspace_root: Path,
    dry_run: bool = False,
    validate_only: bool = False,
    return_diff: bool = False,
) -> dict[str, object]:
    try:
        operations = parse_patch(patch)
        resolved_operations, touched_paths = _resolve_operations(
            operations,
            workspace_root=workspace_root,
        )
        with ExitStack() as locks:
            for path in touched_paths:
                locks.enter_context(exclusive_mutation_lock(path))

            snapshots = {path: _snapshot_path(path) for path in touched_paths}
            planned_changes = [
                _plan_resolved_operation(operation)
                for operation in resolved_operations
            ]
            rendered_diffs = [_render_diff(change) for change in planned_changes]
            file_summaries = [
                _summarize_change(change, diff_text=diff_text)
                for change, diff_text in zip(planned_changes, rendered_diffs, strict=True)
            ]
            warnings = list(
                dict.fromkeys(
                    warning
                    for file_summary in file_summaries
                    for warning in file_summary.get("warnings", [])
                )
            )

            should_apply = not dry_run and not validate_only
            if should_apply:
                for path, snapshot in snapshots.items():
                    if not _snapshot_matches(snapshot):
                        return _error(
                            "revision_conflict",
                            f"File changed while patch was planned: {path}. No patch changes were applied.",
                            path=str(path),
                            expected_revision=_snapshot_revision(snapshot),
                            actual_revision=_current_revision(path),
                        )

                attempted_paths: list[Path] = []
                try:
                    for change in planned_changes:
                        _apply_change_transactional(
                            change,
                            snapshots=snapshots,
                            attempted_paths=attempted_paths,
                        )
                except OSError as exc:
                    rollback_errors = _rollback_attempted_paths(
                        attempted_paths,
                        snapshots=snapshots,
                    )
                    return _error(
                        "write_failed",
                        (
                            f"Transactional patch commit failed: {exc}. "
                            + (
                                "Rollback also failed for: " + "; ".join(rollback_errors)
                                if rollback_errors
                                else "All attempted path mutations were rolled back."
                            )
                        ),
                        rolled_back=not rollback_errors,
                        rollback_errors=rollback_errors,
                    )

            payload: dict[str, object] = {
                "success": True,
                "changes": [_serialize_change(change) for change in planned_changes],
                "files": file_summaries,
                "warnings": warnings,
                "applied": should_apply,
                "validated": dry_run or validate_only,
            }
            if return_diff:
                payload["diff"] = "".join(rendered_diffs)
            return payload
    except PatchError as exc:
        return _error(exc.code, str(exc), **exc.extra)
    except OSError as exc:
        return _error("patch_io_error", f"Patch filesystem operation failed: {exc}")
