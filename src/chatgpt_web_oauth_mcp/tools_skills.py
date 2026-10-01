from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .delegate_guidance import (
    CODE_GRAPH_USE_GUIDE,
    CODE_GRAPH_USE_URI,
    DELEGATE_USE_GUIDE,
    DELEGATE_USE_URI,
    FILE_USE_GUIDE,
    FILE_USE_URI,
    GIT_USE_GUIDE,
    GIT_USE_URI,
    PROCESS_USE_GUIDE,
    PROCESS_USE_URI,
    RUNTIME_USE_GUIDE,
    RUNTIME_USE_URI,
    SKILL_INDEX_URI,
    code_graph_use_payload,
    delegate_use_payload,
    file_use_payload,
    git_use_payload,
    process_use_payload,
    runtime_use_payload,
    skill_index_json,
)
from .tool_context import READ_ONLY_TOOL


def register_skill_tools(mcp: Any) -> dict[str, object]:
    """Register progressive-disclosure guidance as tools and MCP resources."""

    @mcp.resource(
        SKILL_INDEX_URI,
        name="skill-index",
        title="Skill Index",
        description=(
            "Discover task-specific operating guides exposed by this MCP server and "
            "which guide should be loaded before each tool family is used."
        ),
        mime_type="application/json",
    )
    def skill_index_resource() -> str:
        return skill_index_json()

    @mcp.resource(
        DELEGATE_USE_URI,
        name="delegate-use",
        title="Delegate Use Guide",
        description=(
            "Operating guide for bounded CLI-agent delegation, harness selection, "
            "monitoring, cancellation, and independent verification."
        ),
        mime_type="text/markdown",
    )
    def delegate_use_resource() -> str:
        return DELEGATE_USE_GUIDE

    @mcp.resource(
        FILE_USE_URI,
        name="file-use",
        title="File Use Guide",
        description=(
            "Safe operating guide for file discovery, search, reading, code maps, "
            "and mutation tools."
        ),
        mime_type="text/markdown",
    )
    def file_use_resource() -> str:
        return FILE_USE_GUIDE

    @mcp.resource(
        CODE_GRAPH_USE_URI,
        name="code-graph-use",
        title="Code Graph Use Guide",
        description=(
            "Operating guide for immutable Joern CPG preparation and semantic "
            "callers, callees, impact, and path queries."
        ),
        mime_type="text/markdown",
    )
    def code_graph_use_resource() -> str:
        return CODE_GRAPH_USE_GUIDE

    @mcp.resource(
        PROCESS_USE_URI,
        name="process-use",
        title="Process Use Guide",
        description=(
            "Lifecycle guide for synchronous commands, durable background jobs, "
            "and persistent interactive tmux sessions."
        ),
        mime_type="text/markdown",
    )
    def process_use_resource() -> str:
        return PROCESS_USE_GUIDE

    @mcp.resource(
        RUNTIME_USE_URI,
        name="runtime-use",
        title="Runtime Use Guide",
        description=(
            "Lifecycle guide for stable Codex runtime acquisition/reuse, concurrency, "
            "idle-TTL GC, and detached-binding LRU eviction."
        ),
        mime_type="text/markdown",
    )
    def runtime_use_resource() -> str:
        return RUNTIME_USE_GUIDE

    @mcp.resource(
        GIT_USE_URI,
        name="git-use",
        title="Git Use Guide",
        description=(
            "Safe operating guide for repository inspection, commits, history, "
            "and Git worktrees."
        ),
        mime_type="text/markdown",
    )
    def git_use_resource() -> str:
        return GIT_USE_GUIDE

    guide_loaders = {
        "delegate-use": delegate_use_payload,
        "file-use": file_use_payload,
        "code-graph-use": code_graph_use_payload,
        "process-use": process_use_payload,
        "runtime-use": runtime_use_payload,
        "git-use": git_use_payload,
    }

    @mcp.tool(
        name="get_guide",
        title="Get Operating Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load one progressive-disclosure operating guide by name. Available guides: "
            "delegate-use, file-use, code-graph-use, process-use, runtime-use, git-use. "
            "The skill index remains available as the skill://chatgpt-web-oauth-mcp/index resource."
        ),
    )
    def get_guide(
        name: Annotated[
            Literal[
                "delegate-use",
                "file-use",
                "code-graph-use",
                "process-use",
                "runtime-use",
                "git-use",
            ],
            Field(description="Operating guide name."),
        ],
    ) -> dict[str, object]:
        return guide_loaders[name]()

    @mcp.tool(
        name="get_code_graph_use",
        title="Get Code Graph Use Guide",
        annotations=READ_ONLY_TOOL,
        description=(
            "Load the immutable Joern Code Graph operating guide. Call before the first "
            "code_graph_* workflow or when semantic call-graph evidence is needed."
        ),
    )
    def get_code_graph_use() -> dict[str, object]:
        return code_graph_use_payload()

    return {
        "get_guide": get_guide,
        "get_code_graph_use": get_code_graph_use,
    }
