"""wormlens -- Lossless episodic memory for Claude Code, OpenAI Codex CLI, and VS Code Copilot."""

try:
    from ._version import __version__
except ImportError:
    # No stamp present (fresh checkout, hooks not run yet, or stripped install).
    # Fall back to the release literal kept in sync with pyproject.toml.
    __version__ = "0.4.2"
