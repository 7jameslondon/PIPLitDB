"""Shared semantic caption recognition for supplementary sources."""

from __future__ import annotations

import re


CAPTION_PATTERN = re.compile(
    r"^(?:(?:Supplemental|Supplementary|Supporting)\s+)?"
    # Some native PDFs split the first word after ``Fi`` while retaining one
    # visible word (``Fi gure S13``). Accept only that exact intra-word break
    # in an otherwise complete numbered caption.
    # Accept compact publisher labels such as ``Fig.S1`` as well as the more
    # usual ``Fig. S1``.  The required numbered token and punctuation below
    # keep ordinary prose beginning with "fig" from matching.
    r"(?P<kind>Fi(?:\s+)?gures?|Figs?\.?|Schemes?|Tables?)\s*"
    r"(?P<number>[A-Za-z]*\s*\d+(?:[A-Za-z]|[.-]\d+)*)"
    r"(?:\s*[.:-]\s*|(?:(?<=S\d)|(?<=S\d\d))\s+(?=(?-i:[A-Z])))",
    re.IGNORECASE,
)


def normalize_caption_number(value: str) -> str:
    """Remove only spacing between a caption's letter prefix and digits."""

    return re.sub(r"(?<=[A-Za-z])\s+(?=\d)", "", value.strip())


def normalize_caption_kind(value: str) -> str:
    """Normalize the one reviewed native-PDF intra-word caption break."""

    return re.sub(r"\s+", "", value).casefold()


def is_figure_caption(value: str) -> bool:
    match = CAPTION_PATTERN.match(value)
    return bool(match and normalize_caption_kind(match.group("kind")).startswith("fig"))


__all__ = [
    "CAPTION_PATTERN",
    "is_figure_caption",
    "normalize_caption_kind",
    "normalize_caption_number",
]
