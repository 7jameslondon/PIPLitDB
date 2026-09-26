"""API adapters. Dates are search scope; the engine applies no age exclusions."""

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from .config import DiscoveryError
from .records import Paper, normalize_doi


@dataclass
class Page:
    papers: list[Paper]
    next_cursor: str | None
    total: int
    request_url: str


def _count(value):
    if isinstance(value, bool) or str(value).isdigit() is False:
        raise DiscoveryError(f"API returned an invalid result count: {value!r}")
    return int(value)


def _date(item):
    for field in ("published", "published-online", "published-print", "issued"):
        parts = item.get(field, {}).get("date-parts", [])
        if parts and parts[0]:
            return "-".join(str(number).zfill(4 if index == 0 else 2) for index, number in enumerate(parts[0]))
    return ""


def _epmc_paper(item):
    source, identifier = str(item["source"]), str(item["id"])
    journal = item.get("journalInfo", {}).get("journal", {}).get("title", "")
    authors = [author.get("fullName") or author.get("collectiveName", "")
               for author in item.get("authorList", {}).get("author", [])]
    if not authors and item.get("authorString"):
        authors = [item["authorString"]]  # Preserve unparsed attribution, never invent an author split.
    doi = normalize_doi(item.get("doi"))
    return Paper("europe_pmc", f"{source}:{identifier}", item.get("title", ""), doi,
                 authors, journal, item.get("firstPublicationDate") or item.get("pubYear", ""),
                 item.get("abstractText", ""),
                 "https://europepmc.org/article/" + quote(source, safe="") + "/" + quote(identifier, safe=""),
                 "; ".join(item.get("pubTypeList", {}).get("pubType", [])))


def _crossref_paper(item):
    doi = normalize_doi(item.get("DOI"))
    if not doi:
        raise DiscoveryError("Crossref result has no valid DOI")
    authors = [author.get("name") or " ".join(filter(None, [author.get("given"), author.get("family")]))
               for author in item.get("author", [])]
    related = [relation.get("id", "") for group in item.get("relation", {}).values()
               for relation in group if relation.get("id-type") == "doi"]
    return Paper("crossref", doi, " ".join(item.get("title", [])), doi, authors,
                 "; ".join(item.get("container-title", [])), _date(item), item.get("abstract", ""),
                 "https://doi.org/" + quote(doi, safe="/"), item.get("type", ""), related)


def _biorxiv_paper(item):
    doi = normalize_doi(item.get("doi"))
    if not doi:
        raise DiscoveryError("bioRxiv result has no valid DOI")
    return Paper("biorxiv", doi, item.get("title", ""), doi,
                 [name.strip() for name in item.get("authors", "").split(";") if name.strip()],
                 "bioRxiv", item.get("date", ""), item.get("abstract", ""),
                 "https://doi.org/" + quote(doi, safe="/"), "preprint",
                 [item.get("published", "")], str(item.get("version", "")))


def fetch_page(client, spec, query, cursor=None) -> Page:
    """A malformed record fails its page instead of silently disappearing."""
    source = spec["source"]
    try:
        if source == "europe_pmc":
            expression = f"({query}) AND FIRST_PDATE:[{spec['since'] or '*'} TO {spec['until']}]"
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode({
                "query": expression, "format": "json", "resultType": "core",
                "pageSize": spec["page_size"], "cursorMark": cursor or "*",
            })
            payload = client.get(url)
            total = _count(payload["hitCount"])
            items = payload["resultList"]["result"]
            next_cursor = payload.get("nextCursorMark")
            parse = _epmc_paper
        elif source == "crossref":
            filters = [f"until-pub-date:{spec['until']}"]
            if spec["since"]:
                filters.insert(0, f"from-pub-date:{spec['since']}")
            params = {
                "query.bibliographic": query, "filter": ",".join(filters),
                "rows": spec["page_size"], "cursor": cursor or "*",
                "select": "DOI,title,author,container-title,published,published-online,published-print,issued,abstract,relation,type",
            }
            url = "https://api.crossref.org/works?" + urlencode(params)
            payload = client.get(url)
            if payload.get("status") != "ok":
                raise DiscoveryError("Crossref did not return status ok")
            message = payload["message"]
            total = _count(message["total-results"])
            items, next_cursor = message["items"], message.get("next-cursor")
            parse = _crossref_paper
        elif source == "biorxiv":
            offset = int(cursor or 0)
            url = f"https://api.biorxiv.org/details/biorxiv/{spec['since']}/{spec['until']}/{offset}/json"
            if spec.get("category"):
                url += "?" + urlencode({"category": spec["category"]})
            payload = client.get(url)
            message = payload["messages"][0]
            status = str(message.get("status", "")).casefold()
            items = payload["collection"]
            if status not in {"ok", "no posts found"}:
                raise DiscoveryError(f"bioRxiv API status: {status or 'missing'}")
            total = _count(message.get("total", 0) if status == "no posts found" else message["total"])
            # API page size may change; advance by actual returned postings.
            next_cursor = str(offset + len(items)) if offset + len(items) < total else None
            parse = _biorxiv_paper
        else:
            raise DiscoveryError(f"Unsupported source: {source}")
        if not isinstance(items, list):
            raise DiscoveryError("API items must be a list")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise DiscoveryError("API cursor must be a string")
        if not items:
            next_cursor = None
        return Page([parse(item) for item in items], next_cursor, total, url)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise DiscoveryError(f"{source} response error: {exc}") from exc
