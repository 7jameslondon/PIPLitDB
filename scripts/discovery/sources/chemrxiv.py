"""ChemRxiv enrichment policy.

ChemRxiv works are discovered and normalized through Crossref and OpenAlex.  The
provider advertises an OpenAPI-compatible interface, but its public documentation
does not currently expose a stable DOI-details contract suitable for unattended
use.  Keeping this explicit no-op enricher prevents direct access from being
mistaken for an independent discovery source.
"""

from __future__ import annotations

from ..models import Candidate


class ChemRxivEnricher:
    name = "chemrxiv"

    @staticmethod
    def applicable(candidate: Candidate) -> bool:
        return bool(candidate.doi and "chemrxiv" in candidate.doi.casefold())

    @staticmethod
    def enrich(candidate: Candidate) -> Candidate:
        if ChemRxivEnricher.applicable(candidate):
            candidate.warnings.append(
                "ChemRxiv metadata is aggregator-derived; direct DOI enrichment is disabled until a stable contract is fixture-tested"
            )
        return candidate
