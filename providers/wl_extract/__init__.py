"""Wormlens-extract provider backend.

Reads prior wormlens extract files (.wl, .md) as input. Enables
cross-session recall chains where the agent feeds previously
extracted sessions back into wl for further processing.
"""

from .parser import WlExtractProvider

__all__ = ["WlExtractProvider"]
