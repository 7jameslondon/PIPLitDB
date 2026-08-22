"""Shared semantic caption recognition for supplementary sources."""

from __future__ import annotations

import re


CAPTION_PATTERN = re.compile(
    r"^(?:(?:Supplementary|Supporting)\s+)?"
    r"(?P<kind>Figures?|Figs?\.?|Schemes?|Tables?)\s+"
    r"(?P<number>[A-Za-z]*\d+(?:[A-Za-z]|[.-]\d+)*)\s*[.:-]\s*",
    re.IGNORECASE,
)


def is_figure_caption(value: str) -> bool:
    match = CAPTION_PATTERN.match(value)
    return bool(match and match.group("kind").casefold().startswith("fig"))


__all__ = ["CAPTION_PATTERN", "is_figure_caption"]
