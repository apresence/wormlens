"""Wormlens runtime configuration.

Lets users point wormlens at additional session directories (beyond each
provider's built-in defaults) and, when the defaults don't apply, switch
them off entirely.

Configuration is merged from these sources, lowest precedence first:

  1. built-in defaults             (each provider's home/env-derived root)
  2. a config file                 (TOML or JSON)
  3. environment variables         (WORMLENS_EXTRA_DIRS, WORMLENS_NO_DEFAULTS)
  4. CLI flags                     (--extra-dir, --no-default-dirs, --config)

Config file search order (first existing file wins). If $WORMLENS_CONFIG is
set it takes precedence and is used verbatim:

    $WORMLENS_CONFIG
    ./.wormlens.toml                 ./.wormlens.json
    $XDG_CONFIG_HOME/wormlens/config.{toml,json}   (default ~/.config/wormlens)
    ~/.claude/.wormlens/config.{toml,json}

Schema (TOML shown; JSON uses the same keys):

    # Disable EVERY provider's built-in default roots. Per-source toggles win.
    use_defaults = true

    # Extra dirs handed to every provider (each interprets them its own way).
    extra_dirs = ["/extra/projects"]

    [sources.cc]                     # claude code  (aliases: claude_code, claude-code)
    extra_dirs = ["/mnt/host/.claude/projects"]
    use_defaults = true

    [sources.codex]                  # openai codex (alias: openai-codex)
    extra_dirs = []

    [sources.vscode]                 # vs code copilot (alias: vscode-copilot)
    use_defaults = false

Dir strings support ``~`` and ``$VAR`` expansion.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:  # TOML is stdlib only on 3.11+; JSON is the universal fallback.
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on <3.11
    _tomllib = None


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


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def _as_dir_list(value) -> list[Path]:
    """Coerce a config/env value into a list of expanded Paths."""
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
        generic_extra: list[Path] | None = None,
        source_extra: dict | None = None,
        loaded_path: Path | None = None,
        error: str | None = None,
    ):
        self.global_use_defaults = global_use_defaults
        self.source_use_defaults = source_use_defaults or {}
        self.generic_extra = generic_extra or []
        self.source_extra = source_extra or {}
        self.loaded_path = loaded_path
        self.error = error

    def use_defaults(self, provider_id: str) -> bool:
        """Whether the provider's built-in default roots should be scanned."""
        if provider_id in self.source_use_defaults:
            return self.source_use_defaults[provider_id]
        return self.global_use_defaults

    def extra_dirs(self, provider_id: str) -> list[Path]:
        """Extra discovery dirs for a provider (generic + source-specific)."""
        merged = list(self.generic_extra) + list(self.source_extra.get(provider_id, []))
        seen: set[str] = set()
        out: list[Path] = []
        for d in merged:
            key = str(d)
            if key not in seen:
                seen.add(key)
                out.append(d)
        return out

    def resolve_roots(self, provider_id: str, default_roots: list[Path]) -> list[Path]:
        """Combine default roots (if enabled) with extra dirs, de-duped, order-preserving."""
        roots: list[Path] = []
        if self.use_defaults(provider_id):
            roots.extend(default_roots)
        roots.extend(self.extra_dirs(provider_id))
        seen: set[str] = set()
        out: list[Path] = []
        for r in roots:
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
        cli_extra_dirs: list[str] | None = None,
        cli_no_defaults: bool = False,
    ) -> "WormlensConfig":
        """Build config from file + environment + CLI overrides."""
        global_use_defaults = True
        source_use_defaults: dict = {}
        generic_extra: list[Path] = []
        source_extra: dict = {}
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
            generic_extra.extend(_as_dir_list(data.get("extra_dirs")))
            sources = data.get("sources") or {}
            if isinstance(sources, dict):
                for raw_key, scfg in sources.items():
                    if not isinstance(scfg, dict):
                        continue
                    pid = _canon_source(raw_key)
                    if "use_defaults" in scfg:
                        source_use_defaults[pid] = bool(scfg["use_defaults"])
                    dirs = _as_dir_list(scfg.get("extra_dirs"))
                    if dirs:
                        source_extra.setdefault(pid, []).extend(dirs)

        # -- 2. environment -------------------------------------------------
        generic_extra.extend(_as_dir_list(os.environ.get("WORMLENS_EXTRA_DIRS")))
        env_no_def = os.environ.get("WORMLENS_NO_DEFAULTS")
        if env_no_def is not None and env_no_def.strip().lower() in ("1", "true", "yes", "on"):
            global_use_defaults = False

        # -- 3. CLI ---------------------------------------------------------
        if cli_extra_dirs:
            for d in cli_extra_dirs:
                generic_extra.extend(_as_dir_list(d))
        if cli_no_defaults:
            global_use_defaults = False

        return cls(
            global_use_defaults=global_use_defaults,
            source_use_defaults=source_use_defaults,
            generic_extra=generic_extra,
            source_extra=source_extra,
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
    extra_dirs: list[str] | None = None,
    no_defaults: bool = False,
) -> WormlensConfig:
    """(Re)load config applying CLI overrides. Call once from main()."""
    global _CONFIG
    _CONFIG = WormlensConfig.load(
        config_path=config_path,
        cli_extra_dirs=extra_dirs,
        cli_no_defaults=no_defaults,
    )
    return _CONFIG


def reset_config() -> None:
    """Drop the cached config (test seam / re-read after env changes)."""
    global _CONFIG
    _CONFIG = None
