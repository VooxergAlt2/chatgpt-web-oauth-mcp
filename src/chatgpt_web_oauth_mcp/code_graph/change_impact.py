from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from ..process_env import sanitized_child_env
from .snapshot import GitSnapshot, GitSnapshotError, resolve_git_snapshot


_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GIT_TIMEOUT_SECONDS = 30.0


class CodeGraphChangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PythonSymbolSpan:
    path: str
    name: str
    qualname: str
    full_name: str
    kind: str
    line: int
    end_line: int


@dataclass(frozen=True)
class FileChange:
    status: str
    old_path: str | None
    new_path: str | None


def _run_git(
    repository_root: Path,
    args: list[str],
    *,
    text: bool,
) -> str | bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            env=sanitized_child_env(),
            close_fds=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeGraphChangeError(
            f"git execution failed: {type(exc).__name__}: {exc}"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace")
        else:
            detail = stderr
        raise CodeGraphChangeError((detail or "git command failed").strip()[-8192:])
    return completed.stdout


def resolve_comparison_base(
    repository_root: Path,
    *,
    base_ref: str,
    head_ref: str,
    use_merge_base: bool,
) -> GitSnapshot:
    if not use_merge_base:
        return resolve_git_snapshot(repository_root, base_ref)
    for value in (base_ref, head_ref):
        normalized = value.strip()
        if not normalized or normalized.startswith("-") or "\x00" in normalized:
            raise GitSnapshotError(
                "comparison refs must be non-empty Git revisions and must not start with '-'."
            )
    merge_base = str(
        _run_git(
            repository_root,
            ["merge-base", base_ref, head_ref],
            text=True,
        )
    ).strip()
    if not _HEX_SHA_RE.fullmatch(merge_base):
        raise CodeGraphChangeError("git merge-base returned an invalid commit id.")
    return resolve_git_snapshot(repository_root, merge_base)


def _parse_name_status_z(payload: bytes) -> list[FileChange]:
    fields = payload.split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    result: list[FileChange] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("utf-8", errors="replace")
        index += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"}:
            if index + 1 >= len(fields):
                raise CodeGraphChangeError("Malformed NUL-delimited rename/copy diff.")
            old_path = fields[index].decode("utf-8", errors="replace")
            new_path = fields[index + 1].decode("utf-8", errors="replace")
            index += 2
        else:
            if index >= len(fields):
                raise CodeGraphChangeError("Malformed NUL-delimited name-status diff.")
            path = fields[index].decode("utf-8", errors="replace")
            index += 1
            old_path = None if code == "A" else path
            new_path = None if code == "D" else path
        result.append(FileChange(status=code, old_path=old_path, new_path=new_path))
    return result


def list_changed_files(base: GitSnapshot, head: GitSnapshot) -> list[FileChange]:
    if base.repository_id != head.repository_id:
        raise CodeGraphChangeError("base and head refs belong to different repositories.")
    payload = _run_git(
        head.repository_root,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base.tree_sha,
            head.tree_sha,
        ],
        text=False,
    )
    if not isinstance(payload, bytes):
        raise CodeGraphChangeError("git name-status unexpectedly returned text output.")
    return _parse_name_status_z(payload)


def _diff_ranges(
    repository_root: Path,
    *,
    base_tree: str,
    head_tree: str,
    paths: list[str],
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    output = str(
        _run_git(
            repository_root,
            [
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--unified=0",
                base_tree,
                head_tree,
                "--",
                *paths,
            ],
            text=True,
        )
    )
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    for line in output.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if old_count:
            old_ranges.append((old_start, old_count))
        if new_count:
            new_ranges.append((new_start, new_count))
    return old_ranges, new_ranges


def _read_blob(snapshot: GitSnapshot, path: str | None) -> str | None:
    if path is None:
        return None
    payload = _run_git(
        snapshot.repository_root,
        ["show", f"{snapshot.tree_sha}:{path}"],
        text=False,
    )
    if not isinstance(payload, bytes):
        raise CodeGraphChangeError("git show unexpectedly returned text output.")
    if b"\x00" in payload[:1024]:
        return None
    return payload.decode("utf-8", errors="replace")


class _PythonSpanVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: list[PythonSymbolSpan] = []
        self._scope: list[tuple[str, str]] = []

    @staticmethod
    def _start_line(node: ast.AST) -> int:
        values = [int(getattr(node, "lineno", 1) or 1)]
        for decorator in getattr(node, "decorator_list", []) or []:
            values.append(int(getattr(decorator, "lineno", values[0]) or values[0]))
        return min(values)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(("class", node.name))
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, async_function=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, async_function=True)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        async_function: bool,
    ) -> None:
        names = [name for _kind, name in self._scope]
        qualname = ".".join([*names, node.name])
        parent_kind = self._scope[-1][0] if self._scope else None
        kind = (
            "method"
            if parent_kind == "class"
            else ("async_function" if async_function else "function")
        )
        self.symbols.append(
            PythonSymbolSpan(
                path=self.path,
                name=node.name,
                qualname=qualname,
                full_name=f"{self.path}:<module>.{qualname}",
                kind=kind,
                line=self._start_line(node),
                end_line=int(getattr(node, "end_lineno", node.lineno) or node.lineno),
            )
        )
        self._scope.append(("function", node.name))
        self.generic_visit(node)
        self._scope.pop()


def _python_symbols(path: str, text: str | None) -> tuple[list[PythonSymbolSpan], str | None]:
    if text is None:
        return [], None
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [], f"{path}:{exc.lineno or 0}: {exc.msg}"
    visitor = _PythonSpanVisitor(path)
    visitor.visit(tree)
    return visitor.symbols, None


def _intersects(span: PythonSymbolSpan, ranges: list[tuple[int, int]]) -> bool:
    for start, count in ranges:
        end = start + count - 1
        if span.line <= end and span.end_line >= start:
            return True
    return False


def _range_has_symbol(
    start: int,
    count: int,
    symbols: list[PythonSymbolSpan],
) -> bool:
    end = start + count - 1
    return any(symbol.line <= end and symbol.end_line >= start for symbol in symbols)


def is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    parts = normalized.split("/")
    return (
        "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def collect_python_change_set(
    base: GitSnapshot,
    head: GitSnapshot,
) -> dict[str, object]:
    changes = list_changed_files(base, head)
    changed_files = sorted(
        {
            path
            for change in changes
            for path in (change.old_path, change.new_path)
            if path is not None
        }
    )
    python_changes = [
        change
        for change in changes
        if (change.old_path or change.new_path or "").endswith(".py")
    ]
    unsupported_files = sorted(
        {
            path
            for change in changes
            for path in (change.new_path or change.old_path,)
            if path is not None and not path.endswith(".py")
        }
    )

    changed_symbols: list[dict[str, object]] = []
    module_scope_changes: list[dict[str, object]] = []
    errors: list[str] = []

    for change in python_changes:
        diff_paths = [
            path
            for path in (change.old_path, change.new_path)
            if path is not None
        ]
        old_ranges, new_ranges = _diff_ranges(
            head.repository_root,
            base_tree=base.tree_sha,
            head_tree=head.tree_sha,
            paths=sorted(set(diff_paths)),
        )
        old_text = _read_blob(base, change.old_path)
        new_text = _read_blob(head, change.new_path)
        old_symbols, old_error = _python_symbols(change.old_path or "", old_text)
        new_symbols, new_error = _python_symbols(change.new_path or "", new_text)
        if old_error:
            errors.append(old_error)
        if new_error:
            errors.append(new_error)

        old_by_qualname = {symbol.qualname: symbol for symbol in old_symbols}
        new_by_qualname = {symbol.qualname: symbol for symbol in new_symbols}
        changed_qualnames = {
            symbol.qualname for symbol in old_symbols if _intersects(symbol, old_ranges)
        }
        changed_qualnames.update(
            symbol.qualname for symbol in new_symbols if _intersects(symbol, new_ranges)
        )

        for qualname in sorted(changed_qualnames):
            old_symbol = old_by_qualname.get(qualname)
            new_symbol = new_by_qualname.get(qualname)
            if old_symbol is None:
                status = "added"
            elif new_symbol is None:
                status = "deleted"
            else:
                status = "modified"
            symbol = new_symbol or old_symbol
            if symbol is None:
                continue
            changed_symbols.append(
                {
                    "status": status,
                    "name": symbol.name,
                    "qualname": symbol.qualname,
                    "kind": symbol.kind,
                    "file": symbol.path,
                    "line": symbol.line,
                    "end_line": symbol.end_line,
                    "full_name": symbol.full_name,
                    "base_full_name": old_symbol.full_name if old_symbol else None,
                    "head_full_name": new_symbol.full_name if new_symbol else None,
                    "test_symbol": is_test_path(symbol.path),
                }
            )

        old_module_ranges = [
            {"start": start, "count": count}
            for start, count in old_ranges
            if not _range_has_symbol(start, count, old_symbols)
        ]
        new_module_ranges = [
            {"start": start, "count": count}
            for start, count in new_ranges
            if not _range_has_symbol(start, count, new_symbols)
        ]
        path_change = (
            change.status in {"R", "C"}
            and change.old_path is not None
            and change.new_path is not None
            and change.old_path != change.new_path
        )
        if old_module_ranges or new_module_ranges or path_change:
            module_scope_changes.append(
                {
                    "status": change.status,
                    "old_path": change.old_path,
                    "new_path": change.new_path,
                    "path_change": path_change,
                    "old_ranges": old_module_ranges,
                    "new_ranges": new_module_ranges,
                }
            )

    changed_symbols.sort(
        key=lambda item: (
            str(item["file"]),
            int(item["line"]),
            str(item["full_name"]),
        )
    )
    changed_tests = sorted(path for path in changed_files if is_test_path(path))
    return {
        "changed_files": changed_files,
        "changed_test_files": changed_tests,
        "unsupported_changed_files": unsupported_files,
        "changed_symbols": changed_symbols,
        "module_scope_changes": module_scope_changes,
        "analysis_errors": errors,
    }
