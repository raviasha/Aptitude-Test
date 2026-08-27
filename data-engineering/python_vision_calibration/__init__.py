"""Python-first, source-verified calibration helpers.

This package is deliberately separate from the legacy chapter builder and from
the V2 textbook pipeline.  Its outputs are calibration evidence only.
"""

from .models import RawBaselineRecord

__all__ = ["RawBaselineRecord"]
