"""Data models for wormlens extraction."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    """A single message in a chat session."""
    role: str
    text: str
    timestamp: str = ""
    session_id: str = ""
    source_file: str = ""
    msg_type: str = "msg"
    source_line: int = 0
    display_turn: int = 0  # stamped pre-slice so --rev / -n preserve original turn labels
    metadata: dict = field(default_factory=dict)


@dataclass
class ChatSession:
    """A complete chat session containing messages."""
    session_id: str
    title: str = "Untitled"
    start_ts: str = ""
    end_ts: str = ""
    source_file: str = ""
    source_type: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    checkpoints: list[dict] = field(default_factory=list)


@dataclass
class FilterOpts:
    """Controls which message types make it into the output."""
    thinking: bool = False
    tools: bool = False
    hooks: bool = False
    bash: bool = False
    code_edits: bool = False
    refs: bool = False
    teammates: bool = False
    system_msgs: bool = False
    compact_markers: bool = False
    strip_tags: bool = True
    parse_commands: bool = True
    skip_empty: bool = True

    def included_types(self) -> list[str]:
        """Return list of included content type labels for display."""
        types = ["user", "assistant"]
        if self.teammates:
            types.append("team")
        if self.thinking:
            types.append("thinking")
        if self.tools:
            types.append("tools")
        if self.hooks:
            types.append("hooks")
        if self.bash:
            types.append("bash")
        if self.code_edits:
            types.append("code_edits")
        if self.refs:
            types.append("refs")
        if self.system_msgs:
            types.append("system_msgs")
        return types

    def should_include(self, msg: ChatMessage) -> bool:
        """Return True if this message passes the filter."""
        t = msg.msg_type
        if t == "msg":
            return True
        if t == "thinking":
            return self.thinking
        if t in ("tool_use", "tool_result"):
            return self.tools
        if t == "hook":
            return self.hooks
        if t == "bash":
            return self.bash
        if t == "code_edit":
            return self.code_edits
        if t == "ref":
            return self.refs
        if t == "team_msg":
            return self.teammates
        if t == "system_inject":
            return self.system_msgs
        if t == "compact":
            return self.compact_markers
        return True
