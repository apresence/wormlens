"""Provider backends for wormlens -- auto-discovery registry."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from ._base import Provider

_PROVIDERS_DIR = Path(__file__).parent

PROVIDERS: dict[str, type[Provider]] = {}


_KNOWN_PROVIDERS = [
    "claude_code",
    "vscode_copilot",
    "wl_extract",
]


def _register_module(mod):
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if (
            isinstance(obj, type)
            and issubclass(obj, Provider)
            and obj is not Provider
            and getattr(obj, "provider_id", "")
        ):
            PROVIDERS[obj.provider_id] = obj


def _discover_providers():
    """Scan provider subdirectories and register Provider subclasses."""
    try:
        entries = sorted(_PROVIDERS_DIR.iterdir())
    except (NotADirectoryError, OSError):
        entries = None

    if entries is not None:
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name.startswith("_"):
                continue
            if not (entry / "__init__.py").is_file():
                continue

            module_name = f"{__package__}.{entry.name}"
            try:
                mod = importlib.import_module(module_name)
            except Exception as exc:
                print(
                    f"wormlens: warning: failed to import provider "
                    f"'{entry.name}': {exc}",
                    file=sys.stderr,
                )
                continue
            _register_module(mod)
    else:
        for name in _KNOWN_PROVIDERS:
            module_name = f"{__package__}.{name}"
            try:
                mod = importlib.import_module(module_name)
            except Exception:
                continue
            _register_module(mod)


_discover_providers()

ALL_PROVIDERS = list(PROVIDERS.values())

# Backwards compatibility
SOURCES = PROVIDERS
ALL_SOURCES = ALL_PROVIDERS


def detect_provider(path):
    """Auto-detect which provider backend handles a file."""
    for cls in ALL_PROVIDERS:
        if cls.detect(path):
            return cls
    return None

detect_source = detect_provider
