"""Task-owned read-only MCP surface for the isolated ACP native harness.

The gateway is constructed by the worker, never by a model tool call. Its
capability contains only one workspace, an explicit scope and seven fixed tools.
It accepts stateless MCP HTTP on an already connected private stream; it does
not listen on the host network and cannot forward arbitrary RPC methods.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import signal
import stat
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .code_map import _js_imports, _js_symbols, _python_imports, _python_symbols
from .delegate_diagnostics import DelegateDiagnosticsAccumulator
from .supervision import CHECKPOINT_TOOL, PHASES, SupervisorChannel, SupervisionError

SERVER_NAME = "opss_readonly"
SERVER_URL = "http://127.0.0.1:1/mcp"
TOKEN_HEADER = "X-Opss-Task-Token"
TOOL_NAMES = ("read_text", "search", "code_map_symbols", "code_map_references",
              "code_map_imports", "git_status", "git_diff")
REQUEST_LIMIT = 32768
RESPONSE_LIMIT = 65536
TOTAL_RESPONSE_LIMIT = 8 * 1024 * 1024
FILE_LIMIT = 524288
SCAN_LIMIT = 8 * 1024 * 1024
FILE_COUNT_LIMIT = 4096
HTTP_TIMEOUT = 10
_BLOCKED_PARTS = frozenset({
    ".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".config",
    ".local", ".cache", "node_modules", "__pycache__", ".venv", "venv",
})
_BLOCKED_NAMES = frozenset({
    ".netrc", ".npmrc", ".pypirc", "credentials.json", "credential.json",
    "secrets.json", "secret.json", "token.json", "tokens.json", "oauth_creds.json",
    "antigravity-oauth-token", "oauth-token", "id_rsa", "id_ed25519",
    "id_ecdsa", "id_dsa",
})
_LANGUAGES = {"python": {".py", ".pyi"}, "javascript": {".js", ".jsx", ".mjs", ".cjs"},
              "typescript": {".ts", ".tsx"}}


class GatewayError(ValueError):
    """Fixed diagnostics, with no filesystem paths or credential contents."""


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _integer(value, default, lower, upper):
    if value is None:
        return default
    if type(value) is not int or not lower <= value <= upper:
        raise GatewayError("acp_gateway_argument_invalid")
    return value


def _safe_name(relative: str) -> bool:
    for part in PurePosixPath(relative).parts:
        lowered = part.lower()
        if (lowered in _BLOCKED_PARTS or lowered in _BLOCKED_NAMES
                or lowered == ".env" or lowered.startswith(".env.")
                or lowered.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))
                or lowered.endswith("-oauth-token")):
            return False
    return True


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}}


_PATH = {"type": "string", "description": "Workspace-relative path within this task's explicit scope."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
_OFFSET = {"type": "integer", "minimum": 0, "maximum": 100000}
_LANGUAGE = {"type": "string", "enum": list(_LANGUAGES)}


class AcpReadonlyGateway:
    """A revocable capability for exactly one worker/owner and scoped workspace."""

    def __init__(self, *, workspace: Path, owner: str, task_id: str,
                 files_in_scope: Sequence[str], workspace_alias: Path | None = None,
                 baseline_root: Path | None = None, max_calls: int = 64,
                 out_of_scope: Sequence[str] = (), supervisor: SupervisorChannel | None = None,
                 diagnostics: DelegateDiagnosticsAccumulator | None = None):
        if (not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{64}", owner)
                or not isinstance(task_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task_id)):
            raise GatewayError("acp_gateway_identity_invalid")
        if (isinstance(files_in_scope, (str, bytes)) or not files_in_scope
                or len(files_in_scope) > 256):
            raise GatewayError("acp_gateway_scope_required")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir() or self.workspace == Path("/"):
            raise GatewayError("acp_gateway_workspace_invalid")
        self.workspace_alias = Path(workspace_alias or self.workspace)
        if not self.workspace_alias.is_absolute():
            raise GatewayError("acp_gateway_workspace_invalid")
        self.owner, self.task_id = owner, task_id
        self.max_calls = _integer(max_calls, 64, 1, 128)
        self.calls = 0
        self.tool_calls = {name: 0 for name in TOOL_NAMES}
        self.supervisor = supervisor
        if supervisor is not None:
            self.tool_calls[CHECKPOINT_TOOL] = 0
        self.rejected = 0
        self.response_bytes = 0
        self.requests = 0
        self.revoked = False
        self.diagnostics = diagnostics if diagnostics is not None else DelegateDiagnosticsAccumulator()
        self._token = secrets.token_hex(32)
        self._root_identity = self._directory_identity(self.workspace)
        self.baseline_root = Path(baseline_root).resolve(strict=True) if baseline_root else None
        self._baseline_identity = (self._directory_identity(self.baseline_root)
                                   if self.baseline_root else None)
        self.scope = tuple(dict.fromkeys(self._path(p) for p in files_in_scope))
        if isinstance(out_of_scope, (str, bytes)) or len(out_of_scope) > 256:
            raise GatewayError("acp_gateway_scope_invalid")
        self.excluded = tuple(dict.fromkeys(self._path(p) for p in out_of_scope))
        if any(not _safe_name(p) for p in self.scope):
            raise GatewayError("acp_gateway_scope_invalid")
        self._lock = asyncio.Lock()
        self._initialized = False

    @staticmethod
    def _directory_identity(path):
        metadata = path.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise GatewayError("acp_gateway_workspace_invalid")
        return metadata.st_dev, metadata.st_ino

    def _path(self, value):
        if (not isinstance(value, str) or not value or len(value) > 1024
                or any(ord(c) < 32 for c in value) or "\\" in value):
            raise GatewayError("acp_gateway_path_invalid")
        path = PurePosixPath(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.workspace_alias)
            except ValueError:
                raise GatewayError("acp_gateway_path_outside_scope") from None
        if ".." in path.parts:
            raise GatewayError("acp_gateway_path_outside_scope")
        result = str(path)
        if result.startswith("-"):
            raise GatewayError("acp_gateway_path_invalid")
        return result

    def _allowed(self, relative):
        if not _safe_name(relative) or self._excluded(relative):
            return False
        return any(scope == "." or relative == scope or relative.startswith(scope + "/")
                   or (any(c in scope for c in "*?[") and fnmatch.fnmatchcase(relative, scope))
                   for scope in self.scope)

    def _excluded(self, relative):
        return any(scope == "." or relative == scope or relative.startswith(scope + "/")
                   or (any(c in scope for c in "*?[") and fnmatch.fnmatchcase(relative, scope))
                   for scope in self.excluded)

    def _interesting(self, relative):
        if not _safe_name(relative) or self._excluded(relative):
            return False
        if relative == "." or self._allowed(relative):
            return True
        for scope in self.scope:
            prefix = re.split(r"[*?\[]", scope, maxsplit=1)[0].rstrip("/")
            if not prefix or prefix.startswith(relative + "/") or relative.startswith(prefix + "/"):
                return True
        return False

    @contextlib.contextmanager
    def _root_fd(self, baseline=False):
        root = self.baseline_root if baseline else self.workspace
        expected = self._baseline_identity if baseline else self._root_identity
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise GatewayError("acp_gateway_workspace_changed") from None
        try:
            metadata = os.fstat(fd)
            if (metadata.st_dev, metadata.st_ino) != expected:
                raise GatewayError("acp_gateway_workspace_changed")
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _open(self, relative, *, baseline=False, directory=False):
        if not _safe_name(relative):
            raise GatewayError("acp_gateway_path_denied")
        with self._root_fd(baseline) as root_fd:
            opened = []
            current = root_fd
            try:
                parts = PurePosixPath(relative).parts
                for index, part in enumerate(parts):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    if index < len(parts) - 1 or directory:
                        flags |= os.O_DIRECTORY
                    current = os.open(part, flags, dir_fd=current)
                    opened.append(current)
                yield current
            except OSError:
                raise GatewayError("acp_gateway_file_unavailable") from None
            finally:
                for fd in reversed(opened):
                    os.close(fd)

    def _read(self, relative, *, baseline=False):
        if not self._allowed(relative):
            raise GatewayError("acp_gateway_path_outside_scope")
        with self._open(relative, baseline=baseline) as fd:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise GatewayError("acp_gateway_file_type_denied")
            if metadata.st_size > FILE_LIMIT:
                raise GatewayError("acp_gateway_file_too_large")
            chunks = []
            size = 0
            while chunk := os.read(fd, min(65536, FILE_LIMIT + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > FILE_LIMIT:
                    raise GatewayError("acp_gateway_file_too_large")
        data = b"".join(chunks)
        if b"\0" in data:
            raise GatewayError("acp_gateway_binary_file")
        try:
            return data.decode("utf-8")
        except UnicodeError:
            raise GatewayError("acp_gateway_encoding_unsupported") from None

    def _paths(self, selected=".", *, baseline=False):
        """Bounded, symlink-free fd walk; reads only explicitly allowed files."""
        if not self._interesting(selected):
            raise GatewayError("acp_gateway_path_outside_scope")
        with self._open(selected, baseline=baseline) as fd:
            metadata = os.fstat(fd)
            if stat.S_ISREG(metadata.st_mode):
                return [selected] if self._allowed(selected) else []
            if not stat.S_ISDIR(metadata.st_mode):
                raise GatewayError("acp_gateway_file_type_denied")
            result = []
            visited = 0
            def walk(directory_fd, prefix, depth):
                nonlocal visited
                if depth > 32:
                    raise GatewayError("acp_gateway_scan_limit")
                with os.scandir(directory_fd) as entries:
                    names = []
                    for entry in entries:
                        visited += 1
                        if visited > 20000:
                            raise GatewayError("acp_gateway_scan_limit")
                        names.append(entry.name)
                for name in sorted(names):
                    relative = name if prefix == "." else prefix + "/" + name
                    if not self._interesting(relative):
                        continue
                    try:
                        entry_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISDIR(entry_metadata.st_mode):
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                            dir_fd=directory_fd)
                            try:
                                walk(child, relative, depth + 1)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(entry_metadata.st_mode) and self._allowed(relative):
                            result.append(relative)
                            if len(result) > FILE_COUNT_LIMIT:
                                raise GatewayError("acp_gateway_scan_limit")
                    except OSError:
                        continue
            walk(fd, selected, 0)
            return result

    def mcp_server(self):
        """ACP schema dict, constructed only by the trusted worker."""
        return {"type": "http", "name": SERVER_NAME, "url": SERVER_URL,
                "headers": [{"name": TOKEN_HEADER, "value": self._token}]}

    def native_config(self):
        return {"name": SERVER_NAME, "http": {"url": SERVER_URL,
                                             "headers": {TOKEN_HEADER: self._token}},
                **({"supervision_enabled": True} if self.supervisor is not None else {})}

    def revoke(self):
        self.revoked = True

    def summary(self):
        return {"enabled": True, "read_only": True, "calls": self.calls,
                "tool_calls": dict(self.tool_calls),
                "rejected": self.rejected, "requests": self.requests,
                "response_bytes": self.response_bytes,
                "revoked": self.revoked,
                "evidence_source": "workspace_baseline" if self.baseline_root else "git",
                "diagnostics": self.diagnostics.summary()}

    def tools(self):
        evidence = ("Compares the private workspace with its immutable task baseline; this is workspace evidence."
                    if self.baseline_root else
                    "Compares scoped raw working files to Git HEAD; index state is not reported separately.")
        tools = [
            _tool("read_text", "Read bounded UTF-8 text with 1-based line anchors from this task's scope.",
                  {"path": _PATH, "start_line": {"type": "integer", "minimum": 1},
                   "line_limit": _LIMIT}, ("path",)),
            _tool("search", "Bounded literal text search over scoped regular files; never executes regex or commands.",
                  {"query": {"type": "string", "minLength": 1, "maxLength": 256},
                   "path": _PATH, "limit": _LIMIT, "offset": _OFFSET,
                   "ignore_case": {"type": "boolean"}}, ("query",)),
            _tool("code_map_symbols", "Definitions from the existing Opss lightweight CodeMap parsers.",
                  {"path": _PATH, "language": _LANGUAGE, "limit": _LIMIT, "offset": _OFFSET}),
            _tool("code_map_references", "Bounded textual identifier references with file and line anchors.",
                  {"symbol": {"type": "string", "minLength": 1, "maxLength": 128},
                   "path": _PATH, "limit": _LIMIT, "offset": _OFFSET}, ("symbol",)),
            _tool("code_map_imports", "Imports from the existing Opss lightweight CodeMap parsers.",
                  {"path": _PATH, "language": _LANGUAGE, "limit": _LIMIT, "offset": _OFFSET}),
            _tool("git_status", evidence, {"path": _PATH, "limit": _LIMIT}),
            _tool("git_diff", evidence + " Returns a bounded unified diff.",
                  {"path": _PATH}),
        ]
        if self.supervisor is not None:
            checkpoint = _tool(
                CHECKPOINT_TOOL,
                "Cooperative supervisor checkpoint. Publish a concise report with source anchors, not private reasoning. "
                "Call when the task requests review, you need a decision, or a tool reports supervisor_checkpoint_requested. "
                "Do not run other tools in parallel. This call waits for the owner and returns a decision and free-text "
                "correction in the SAME turn. A decision never changes file permissions or accepts code.",
                {"phase": {"type": "string", "enum": list(PHASES)},
                 "summary": {"type": "string", "minLength": 1, "maxLength": 8192},
                 "evidence": {"type": "array", "maxItems": 24,
                              "items": {"type": "string", "maxLength": 1024}}},
                ("phase", "summary"),
            )
            checkpoint["annotations"].update(readOnlyHint=False, idempotentHint=False)
            tools.append(checkpoint)
        return tools

    def _validate_arguments(self, name, arguments):
        tool = next((tool for tool in self.tools() if tool["name"] == name), None)
        if tool is None:
            raise GatewayError("acp_gateway_tool_not_allowed")
        schema = tool["inputSchema"]
        if (not isinstance(arguments, dict) or set(arguments) - set(schema["properties"])
                or not set(schema["required"]).issubset(arguments)):
            raise GatewayError("acp_gateway_argument_invalid")
        path = self._path(arguments.get("path", "."))
        if not self._interesting(path):
            raise GatewayError("acp_gateway_path_outside_scope")
        return path

    def _read_tool(self, path, arguments):
        text = self._read(path)
        start = _integer(arguments.get("start_line"), 1, 1, 1000000)
        limit = _integer(arguments.get("line_limit"), 100, 1, 200)
        lines = text.splitlines()
        selected = lines[start - 1:start - 1 + limit]
        content = "\n".join(f"{start + index}: {line}" for index, line in enumerate(selected))
        truncated = start - 1 + len(selected) < len(lines)
        return {"success": True, "path": path, "content": content,
                "start_line": start, "end_line": start + len(selected) - 1,
                "truncated": truncated, "next_line": start + len(selected) if truncated else None}

    def _scan_tool(self, name, path, arguments):
        limit = _integer(arguments.get("limit"), 100, 1, 200)
        offset = _integer(arguments.get("offset"), 0, 0, 100000)
        language = arguments.get("language", "python")
        if language not in _LANGUAGES:
            raise GatewayError("acp_gateway_argument_invalid")
        query = arguments.get("query") if name == "search" else arguments.get("symbol")
        if name in {"search", "code_map_references"}:
            maximum = 256 if name == "search" else 128
            if not isinstance(query, str) or not 1 <= len(query) <= maximum:
                raise GatewayError("acp_gateway_argument_invalid")
        ignore_case = arguments.get("ignore_case", False)
        if type(ignore_case) is not bool:
            raise GatewayError("acp_gateway_argument_invalid")
        reference = re.compile(r"(?<!\w)" + re.escape(query) + r"(?!\w)") if name == "code_map_references" else None
        rows = []
        total = 0
        scanned = 0
        skipped = 0
        seen = 0
        complete = True
        for relative in self._paths(path):
            if name in {"code_map_symbols", "code_map_imports"} and Path(relative).suffix not in _LANGUAGES[language]:
                continue
            try:
                text = self._read(relative)
            except GatewayError:
                skipped += 1
                continue
            total += len(text.encode())
            if total > SCAN_LIMIT:
                complete = False
                break
            scanned += 1
            if name in {"search", "code_map_references"}:
                matches = []
                needle = query.casefold() if ignore_case else query
                for line, value in enumerate(text.splitlines(), 1):
                    found = (needle in (value.casefold() if ignore_case else value)
                             if name == "search" else bool(reference.search(value)))
                    if found:
                        matches.append({"file": relative, "line": line, "text": value[:2048]})
                        if len(matches) > offset + limit:
                            break
            elif name == "code_map_symbols":
                matches, error = (_python_symbols(Path(relative), text) if language == "python"
                                  else (_js_symbols(Path(relative), text), None))
                if error:
                    skipped += 1
            else:
                imports, error = (_python_imports(text, path=Path(relative)) if language == "python"
                                  else (_js_imports(text), None))
                matches = [{"file": relative, "imports": imports}]
                if error:
                    skipped += 1
            for row in matches:
                if seen >= offset:
                    rows.append(row)
                seen += 1
                if len(rows) > limit:
                    complete = False
                    break
            if len(rows) > limit:
                break
        return {"success": True, "results": rows[:limit], "scanned_files": scanned,
                "skipped_files": skipped, "truncated": not complete,
                "next_offset": offset + limit if len(rows) > limit else None}

    async def _git(self, *arguments):
        """Fixed local Git only. No shell, provider, config-driven executable or stdin."""
        command = ["/usr/bin/git", "--no-pager", "--literal-pathspecs",
                   "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                   "-c", "diff.external=", "-c", "core.pager=cat", *arguments]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.workspace, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent",
                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                     "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                     "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"})
            async with asyncio.timeout(3):
                chunks = []
                size = 0
                while chunk := await process.stdout.read(min(8192, RESPONSE_LIMIT + 1 - size)):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > RESPONSE_LIMIT:
                        raise GatewayError("acp_gateway_output_limit")
                await process.wait()
            if process.returncode:
                raise GatewayError("acp_gateway_git_unavailable")
            return b"".join(chunks)
        except (OSError, TimeoutError):
            raise GatewayError("acp_gateway_git_unavailable") from None
        finally:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def _git_tool(self, name, path, arguments):
        if self.baseline_root:
            return self._baseline_tool(name, path, arguments)
        root = await self._git("rev-parse", "--show-toplevel")
        if Path(os.fsdecode(root).strip()).resolve() != self.workspace:
            raise GatewayError("acp_gateway_git_root_mismatch")
        # Metadata/blob reads do not invoke clean/smudge filters, external diff,
        # textconv or repository hooks. Never use status/diff on host source.
        raw = await self._git("ls-tree", "-r", "-z", "--full-tree", "HEAD")
        head_files = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            relative = raw_path.decode("utf-8")
            if not self._allowed(relative) or not (path == "." or relative == path or relative.startswith(path + "/")):
                continue
            if kind != "blob" or mode not in {"100644", "100755"} or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", oid):
                raise GatewayError("acp_gateway_git_file_unsupported")
            head_files[relative] = (mode, oid)
        try:
            current_files = set(self._paths(path))
        except GatewayError as exc:
            if str(exc) != "acp_gateway_file_unavailable":
                raise
            current_files = set()
        changes = []
        chunks = []
        total = 0
        for relative in sorted(set(head_files) | current_files):
            after = self._read(relative) if relative in current_files else ""
            data = after.encode()
            total += len(data)
            if total > SCAN_LIMIT:
                raise GatewayError("acp_gateway_scan_limit")
            entry = head_files.get(relative)
            same = False
            if entry is not None and relative in current_files:
                digest = hashlib.sha1() if len(entry[1]) == 40 else hashlib.sha256()
                digest.update(b"blob " + str(len(data)).encode() + b"\0" + data)
                with self._open(relative) as fd:
                    executable = bool(os.fstat(fd).st_mode & 0o111)
                same = digest.hexdigest() == entry[1] and executable == (entry[0] == "100755")
            if same:
                continue
            changes.append({"path": relative, "status": "??" if entry is None else
                            (" D" if relative not in current_files else " M")})
            if name == "git_diff":
                before = (await self._git("cat-file", "blob", entry[1])).decode("utf-8") if entry else ""
                if "\0" in before:
                    raise GatewayError("acp_gateway_binary_file")
                total += len(before.encode())
                if total > SCAN_LIMIT:
                    raise GatewayError("acp_gateway_scan_limit")
                chunks.extend(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                  fromfile="a/" + relative, tofile="b/" + relative, n=3))
                if sum(len(chunk.encode()) for chunk in chunks) > RESPONSE_LIMIT:
                    raise GatewayError("acp_gateway_output_limit")
        if name == "git_status":
            limit = _integer(arguments.get("limit"), 100, 1, 200)
            return {"success": True, "source": "git", "changes": changes[:limit],
                    "comparison": "raw_worktree_to_head", "truncated": len(changes) > limit}
        return {"success": True, "source": "git", "diff": "".join(chunks),
                "comparison": "raw_worktree_to_head",
                "truncated": False}

    def _baseline_tool(self, name, path, arguments):
        def available(baseline):
            try:
                return set(self._paths(path, baseline=baseline))
            except GatewayError as exc:
                if str(exc) == "acp_gateway_file_unavailable":
                    return set()
                raise
        before_paths = available(True)
        after_paths = available(False)
        changes = []
        chunks = []
        total = 0
        for relative in sorted(before_paths | after_paths):
            before = self._read(relative, baseline=True) if relative in before_paths else ""
            after = self._read(relative) if relative in after_paths else ""
            total += len(before.encode()) + len(after.encode())
            if total > SCAN_LIMIT:
                raise GatewayError("acp_gateway_scan_limit")
            if before == after and (relative in before_paths) == (relative in after_paths):
                continue
            state = " M" if relative in before_paths and relative in after_paths else (" D" if relative in before_paths else "??")
            changes.append({"path": relative, "status": state})
            if name == "git_diff":
                chunks.extend(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                                  fromfile="a/" + relative, tofile="b/" + relative, n=3))
                if sum(len(chunk.encode()) for chunk in chunks) > RESPONSE_LIMIT:
                    raise GatewayError("acp_gateway_output_limit")
        if name == "git_status":
            limit = _integer(arguments.get("limit"), 100, 1, 200)
            return {"success": True, "source": "workspace_baseline", "changes": changes[:limit],
                    "truncated": len(changes) > limit}
        return {"success": True, "source": "workspace_baseline", "diff": "".join(chunks),
                "truncated": False}

    async def _call(self, name, arguments):
        try:
            if self.revoked:
                raise GatewayError("acp_gateway_revoked")
            if self.calls >= self.max_calls:
                raise GatewayError("acp_gateway_call_limit")
            self.calls += 1
            path = self._validate_arguments(name, arguments)
            if self.supervisor is not None and name == CHECKPOINT_TOOL:
                result = await self.supervisor.checkpoint(**arguments)
            elif self.supervisor is not None and self.supervisor.requested():
                raise GatewayError("supervisor_checkpoint_requested")
            elif name == "read_text":
                result = self._read_tool(path, arguments)
            elif name.startswith("code_map_") or name == "search":
                result = self._scan_tool(name, path, arguments)
            else:
                result = await self._git_tool(name, path, arguments)
            if len(_json(result)) > RESPONSE_LIMIT - 2048:
                raise GatewayError("acp_gateway_output_limit")
            # Count completed bounded reads only; rejected attempts cannot prove
            # that a live model actually received custom MCP evidence.
            self.tool_calls[name] += 1
            self.diagnostics.record_success(name, arguments, result)
            return result
        except (GatewayError, ValueError, TypeError, RecursionError) as exc:
            code = str(exc) if isinstance(exc, (GatewayError, SupervisionError)) else "acp_gateway_argument_invalid"
            self.diagnostics.record_error(name, arguments, code)
            raise

    async def dispatch(self, request):
        """JSON-RPC dispatcher used only after the stream's task token check."""
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or isinstance(request.get("id"), (dict, list, bool))
                or set(request) - {"jsonrpc", "id", "method", "params"}):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        identifier = request.get("id")
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "Invalid params"}}
        if self.revoked:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32000, "message": "acp_gateway_revoked"}}
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            protocol = params.get("protocolVersion")
            if protocol not in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}:
                protocol = "2025-03-26"
            self._initialized = True
            result = {"protocolVersion": protocol, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": SERVER_NAME, "version": "1.0"},
                      "instructions": "Task-scoped filesystem reads and optional cooperative supervisor checkpoint. No jobs, delegates, shell, file writes or credentials."}
        elif method == "ping":
            result = {}
        elif method == "tools/list" and self._initialized:
            result = {"tools": self.tools()}
        elif method == "tools/call" and self._initialized:
            try:
                async with self._lock:
                    data = await self._call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": _json(data).decode()}],
                          "isError": False}
            except (GatewayError, ValueError, TypeError, RecursionError) as exc:
                self.rejected += 1
                code = str(exc) if isinstance(exc, (GatewayError, SupervisionError)) else "acp_gateway_argument_invalid"
                result = {"content": [{"type": "text", "text": _json({"success": False, "error": {"code": code}}).decode()}],
                          "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not available"}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result} if "id" in request else None

    async def serve_http(self, reader, writer):
        """One strict, bounded stateless Streamable HTTP request; always closes."""
        status, result = 400, b""
        try:
            async with asyncio.timeout(HTTP_TIMEOUT) as request_timeout:
                raw_headers = await reader.readuntil(b"\r\n\r\n")
                if len(raw_headers) > 8192:
                    raise GatewayError("acp_gateway_http_invalid")
                lines = raw_headers.decode("ascii").split("\r\n")
                request_line = lines[0].split(" ")
                if len(request_line) != 3 or request_line[2] != "HTTP/1.1":
                    raise GatewayError("acp_gateway_http_invalid")
                method, target, _ = request_line
                headers = {}
                for line in lines[1:-2]:
                    key, separator, value = line.partition(":")
                    key = key.lower()
                    if (not separator or not re.fullmatch(r"[a-z0-9-]+", key)
                            or key in headers or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                        raise GatewayError("acp_gateway_http_invalid")
                    headers[key] = value.strip()
                token = headers.get(TOKEN_HEADER.lower(), "")
                if not secrets.compare_digest(token, self._token) or self.revoked:
                    status = 401
                elif self.requests >= self.max_calls * 4 + 16:
                    self.revoke()
                    status = 401
                elif target != "/mcp":
                    status = 404
                elif method != "POST":
                    status = 405
                elif ("transfer-encoding" in headers or
                      headers.get("content-type", "").split(";", 1)[0] != "application/json"):
                    status = 400
                else:
                    self.requests += 1
                    length_text = headers.get("content-length", "")
                    if not re.fullmatch(r"[0-9]{1,6}", length_text):
                        raise GatewayError("acp_gateway_http_invalid")
                    length = int(length_text)
                    if not 1 <= length <= REQUEST_LIMIT:
                        status = 413
                    else:
                        data = await reader.readexactly(length)
                        request = json.loads(data)
                        # Extend only an authenticated, fully received checkpoint
                        # call. Header/body reads and ordinary tools stay bounded.
                        if (self.supervisor is not None and isinstance(request, dict)
                                and request.get("method") == "tools/call"
                                and isinstance(request.get("params"), dict)
                                and request["params"].get("name") == CHECKPOINT_TOOL):
                            request_timeout.reschedule(self.supervisor.deadline + 2)
                        response = await self.dispatch(request)
                        status = 202 if response is None else 200
                        result = _json(response) if response is not None else b""
                        if len(result) > RESPONSE_LIMIT:
                            result = _json({"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32000, "message": "acp_gateway_output_limit"}})
        except (OSError, TimeoutError, UnicodeError, ValueError, RecursionError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            status, result = 400, b""
        finally:
            if self.response_bytes + len(result) > TOTAL_RESPONSE_LIMIT:
                self.revoke()
                status, result = 413, b""
            self.response_bytes += len(result)
            reasons = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
                       404: "Not Found", 405: "Method Not Allowed", 413: "Content Too Large"}
            with contextlib.suppress(OSError, ConnectionError):
                writer.write((f"HTTP/1.1 {status} {reasons[status]}\r\n"
                              f"Content-Length: {len(result)}\r\nContent-Type: application/json\r\n"
                              "Connection: close\r\n\r\n").encode() + result)
                await writer.drain()
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
