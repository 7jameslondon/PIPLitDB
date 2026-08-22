"""Private-paper extraction pipeline.

The package contains reusable, offline-only extraction components. Command-line
entry points live in :mod:`scripts.extract_record` and
:mod:`scripts.validate_extraction`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - imported only by static type checkers
    from .pipeline import ExtractionError, ExtractionResult, extract_record


def __getattr__(name: str) -> Any:
    """Load the rendering pipeline only when its public API is requested.

    Validation and promotion intentionally do not need PDF rendering
    dependencies, so importing their submodules must stay lightweight.
    """

    if name in __all__:
        from .pipeline import ExtractionError, ExtractionResult, extract_record

        values = {
            "ExtractionError": ExtractionError,
            "ExtractionResult": ExtractionResult,
            "extract_record": extract_record,
        }
        return values[name]
    raise AttributeError(name)

__all__ = ["ExtractionError", "ExtractionResult", "extract_record"]
