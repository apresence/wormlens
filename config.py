"""Wormlens runtime configuration.

Two independent knobs over session discovery:

  * ``use_defaults`` -- whether to scan each provider's built-in default
    location (e.g. ``~/.claude/projects`` for Claude Code). Turn it off when
    the defaults don't apply.
  * ``extra_globs`` -- explicit glob patterns pointing at additional session
    files. **These are always globs, never structural roots** -- what the
    pattern matches is exactly what gets scanned. No magic, no "is this a
    directory or a pattern" guessing.

Defaults are provider-structured (CC knows its ``projects/*/​*.jsonl`` layout);
extras are pure globs you write yourself. So to add a backup CC tree you spell
out the layout: ``"/backup/.claude/projects/*/*.jsonl"``. A flat folder of
session files is just ``"/dump/*.jsonl"``; recurse with ``"/arch/**/*.jsonl"``.

Configuration is merged from these sources, lowest precedence first:

  1. built-in defaults             (each provider's home/env-derived root)
  2. a config file                 (TOML or JSON)
  3. environment variables         (WORMLENS_EXTRA_GLOBS, WORMLENS_NO_DEFAULTS)
  4. CLI flags                     (--extra-glob, --no-default-dirs, --config)

Config file search order (first existing file wins). If $WORMLENS_CONFIG is
set it takes precedence and is used verbatim:

    $WORMLENS_CONFIG
    ./.wormlens.toml                 ./.wormlens.json
    $XDG_CONFIG_HOME/wormlens/config.{toml,json}   (default ~/.config/wormlens)
    ~/.claude/.wormlens/config.{toml,json}

Schema (TOML shown; JSON uses the same keys):

    # Disable EVERY provider's built-in default roots. Per-source toggles win.
    use_defaults = true

    # Globs handed to every provider (matched .jsonl files are scanned).
    extra_globs = ["/dump/*.jsonl"]

    [sources.cc]                     # claude code  (aliases: claude_code, claude-code)
    extra_globs = ["/backup/.claude/projects/*/*.jsonl"]
    use_defaults = true

    [sources.codex]                  # openai codex (alias: openai-codex)
    extra_globs = ["/mnt/host/.codex/sessions/**/rollout-*.jsonl"]

    [sources.vscode]                 # vs code copilot (alias: vscode-copilot)
    use_defaults = false

Glob strings support ``~`` and ``$VAR`` expansion and ``**`` recursion.
"""

from __future__ import annotations

import glob as _glob
import json
import os
import sys
from pathlib import Path

try:  # TOML is stdlib only on 3.11+; JSON is the universal fallback.
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on <3.11
    _tomllib = None


# Only .jsonl session files are picked up from a glob (cc/codex/vscode all use
# JSONL). claude.ai / wl-extract are file-only providers fed via CLI args.
_SESSION_SUFFIX = ".jsonl"


# Maps friendly config keys onto canonical provider ids.
_SOURCE_ALIASES = {
    "claude_code": "cc",
    "claude-code": "cc",
    "claude": "cc",
    "openai-codex": "codex",
    "codex-cli": "codex",
    "vscode-copilot": "vscode",
    "copilot": "vscode",
    "claude-ai": "claude_ai",
    "claude_web": "claude_ai",
}


def _canon_source(key: str) -> str:
    return _SOURCE_ALIASES.get(key, key)


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _as_glob_list(value) -> list[str]:
    """Coerce a config/env value into a list of expanded glob strings."""
    if value is None:
        return []
    if isinstance(value, str):
        # Allow os.pathsep- or comma-separated strings (env-var friendly).
        parts = [s for chunk in value.split(os.pathsep) for s in chunk.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    out = []
    for part in parts:
        part = str(part).strip()
        if part:
            out.append(_expand(part))
    return out


def _config_search_paths() -> list[Path]:
    paths: list[Path] = []
    cwd = Path.cwd()
    paths.append(cwd / ".wormlens.toml")
    paths.append(cwd / ".wormlens.json")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    paths.append(base / "wormlens" / "config.toml")
    paths.append(base / "wormlens" / "config.json")
    paths.append(Path.home() / ".claude" / ".wormlens" / "config.toml")
    paths.append(Path.home() / ".claude" / ".wormlens" / "config.json")
    return paths


def _load_file(path: Path) -> dict:
    """Parse a single config file. Raises ValueError with a clear message."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:  # treat everything else as TOML
        if _tomllib is None:
            raise ValueError(
                f"{path}: TOML config needs Python 3.11+ (tomllib); "
                f"use a .json config on this interpreter ({sys.version_info.major}."
                f"{sys.version_info.minor})"
            )
        data = _tomllib.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a table/object")
    return data


class WormlensConfig:
    """Resolved discovery configuration, queried per provider id."""

    def __init__(
        self,
        *,
        global_use_defaults: bool = True,
        source_use_defaults: dict | None = None,
        generic_globs: list[str] | None = None,
        source_globs: dict | None = None,
        loaded_path: Path | None = None,
        error: str | None = None,
    ):
        self.global_use_defaults = global_use_defaults
        self.source_use_defaults = source_use_defaults or {}
        self.generic_globs = generic_globs or []
        self.source_globs = source_globs or {}
        self.loaded_path = loaded_path
        self.error = error

    def use_defaults(self, provider_id: str) -> bool:
        """Whether the provider's built-in default roots should be scanned."""
        if provider_id in self.source_use_defaults:
            return self.source_use_defaults[provider_id]
        return self.global_use_defaults

    def globs(self, provider_id: str) -> list[str]:
        """Glob patterns for a provider (generic + source-specific), de-duped."""
        merged = list(self.generic_globs) + list(self.source_globs.get(provider_id, []))
        seen: set[str] = set()
        out: list[str] = []
        for g in merged:
            if g not in seen:
                seen.add(g)
                out.append(g)
        return out

    def glob_matches(self, provider_id: str) -> list[tuple[str, list[Path]]]:
        """For each provider glob, return (pattern, matched session files).

        A pattern is expanded with recursive ``**`` support; only existing
        ``.jsonl`` files are kept. Empty match lists are preserved so callers
        (e.g. ``--doctor``) can flag patterns that matched nothing.
        """
        out: list[tuple[str, list[Path]]] = []
        for pat in self.globs(provider_id):
            files = sorted(
                Path(m)
                for m in _glob.glob(pat, recursive=True)
                if Path(m).is_file() and Path(m).suffix == _SESSION_SUFFIX
            )
            out.append((pat, files))
        return out

    def extra_files(self, provider_id: str) -> list[Path]:
        """All session files matched by the provider's globs, de-duped."""
        seen: set[str] = set()
        out: list[Path] = []
        for _pat, files in self.glob_matches(provider_id):
            for f in files:
                key = str(f)
                if key not in seen:
                    seen.add(key)
                    out.append(f)
        return out

    def resolve_roots(self, provider_id: str, default_roots: list[Path]) -> list[Path]:
        """The provider's built-in default roots, or none if disabled. De-duped."""
        if not self.use_defaults(provider_id):
            return []
        seen: set[str] = set()
        out: list[Path] = []
        for r in default_roots:
            key = str(r)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    @classmethod
    def load(
        cls,
        *,
        config_path: str | os.PathLike | None = None,
        cli_extra_globs: list[str] | None = None,
        cli_no_defaults: bool = False,
    ) -> "WormlensConfig":
        """Build config from file + environment + CLI overrides."""
        global_use_defaults = True
        source_use_defaults: dict = {}
        generic_globs: list[str] = []
        source_globs: dict = {}
        loaded_path: Path | None = None
        error: str | None = None

        # -- 1. config file -------------------------------------------------
        explicit = config_path or os.environ.get("WORMLENS_CONFIG")
        if explicit:
            candidate = Path(explicit)
            search = [candidate] if candidate.exists() else []
            if not candidate.exists():
                error = f"config file not found: {candidate}"
        else:
            search = [p for p in _config_search_paths() if p.is_file()]
            search = search[:1]  # first existing wins

        for path in search:
            try:
                data = _load_file(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                break
            loaded_path = path
            if "use_defaults" in data:
                global_use_defaults = bool(data["use_defaults"])
            generic_globs.extend(_as_glob_list(data.get("extra_globs")))
            sources = data.get("sources") or {}
            if isinstance(sources, dict):
                for raw_key, scfg in sources.items():
                    if not isinstance(scfg, dict):
                        continue
                    pid = _canon_source(raw_key)
                    if "use_defaults" in scfg:
                        source_use_defaults[pid] = bool(scfg["use_defaults"])
                    globs = _as_glob_list(scfg.get("extra_globs"))
                    if globs:
                        source_globs.setdefault(pid, []).extend(globs)

        # -- 2. environment -------------------------------------------------
        generic_globs.extend(_as_glob_list(os.environ.get("WORMLENS_EXTRA_GLOBS")))
        env_no_def = os.environ.get("WORMLENS_NO_DEFAULTS")
        if env_no_def is not None and env_no_def.strip().lower() in ("1", "true", "yes", "on"):
            global_use_defaults = False

        # -- 3. CLI ---------------------------------------------------------
        if cli_extra_globs:
            for g in cli_extra_globs:
                generic_globs.extend(_as_glob_list(g))
        if cli_no_defaults:
            global_use_defaults = False

        return cls(
            global_use_defaults=global_use_defaults,
            source_use_defaults=source_use_defaults,
            generic_globs=generic_globs,
            source_globs=source_globs,
            loaded_path=loaded_path,
            error=error,
        )


_CONFIG: WormlensConfig | None = None


def get_config() -> WormlensConfig:
    """Return the process-wide config, loading defaults lazily on first use."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = WormlensConfig.load()
    return _CONFIG


def configure(
    *,
    config_path: str | os.PathLike | None = None,
    extra_globs: list[str] | None = None,
    no_defaults: bool = False,
) -> WormlensConfig:
    """(Re)load config applying CLI overrides. Call once from main()."""
    global _CONFIG
    _CONFIG = WormlensConfig.load(
        config_path=config_path,
        cli_extra_globs=extra_globs,
        cli_no_defaults=no_defaults,
    )
    return _CONFIG


def reset_config() -> None:
    """Drop the cached config (test seam / re-read after env changes)."""
    global _CONFIG
    _CONFIG = None
