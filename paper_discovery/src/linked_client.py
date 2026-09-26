"""Public Linked Discoveries requests and PubMed citation parsing.

The website endpoint is experimental. No credentials or cookies are persisted.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from http.cookiejar import CookieJar
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
import xml.etree.ElementTree as ET

from .config import DiscoveryError
from .records import Paper, normalize_doi
from .store import now

BASE = "https://linkeddiscoveries.ncbi.nlm.nih.gov"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# The website accepts these quantities; requests above 200 are not supported.
# The response contains the seed in addition to the requested neighbours.
NEIGHBOR_QUANTITIES = (50, 100, 200)
MAX_NEIGHBORS = max(NEIGHBOR_QUANTITIES)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pmid_text(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise DiscoveryError("Expected a positive numeric PubMed identifier")
    return value


def validate_quantity(quantity):
    if type(quantity) is not int or quantity not in NEIGHBOR_QUANTITIES:
        raise DiscoveryError("Linked Discoveries quantity must be 50, 100 or 200 related papers, excluding the seed")
    return quantity


def displayed_articles(payload, pmid, quantity):
    """Validate and retain the seed plus up to quantity related papers.

    The website limits its display including the seed. Discovery instead keeps
    the final neighbour already present in the response, ordered by score.
    """
    pmid = pmid_text(pmid)
    validate_quantity(quantity)
    try:
        if pmid_text(payload["seedArticleId"]) != pmid:
            raise DiscoveryError("Linked Discoveries returned a different seed")
        articles = payload["articles"]
        if not isinstance(articles, list) or not articles:
            raise DiscoveryError("Linked Discoveries returned no seed article")
        seen = set()
        for item in articles:
            identifier = pmid_text(item["articleId"])
            if identifier in seen or not isinstance(item.get("title"), str) or not item["title"].strip():
                raise DiscoveryError("Duplicate identifier or missing title in Linked Discoveries response")
            seen.add(identifier)
            if identifier != pmid:
                score = item["score"]
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                    raise DiscoveryError("Invalid similarity score in Linked Discoveries response")
            if item.get("isSeed") and identifier != pmid:
                raise DiscoveryError("Inconsistent seed flag in Linked Discoveries response")
        if pmid not in seen:
            raise DiscoveryError("Linked Discoveries response is missing its seed")
        return sorted(articles, key=lambda a: (str(a["articleId"]) != pmid,
                                              -a.get("score", 0) if str(a["articleId"]) != pmid else 0))[:quantity + 1]
    except (KeyError, TypeError, AttributeError) as exc:
        raise DiscoveryError(f"Linked Discoveries response schema changed: {exc}") from exc


class WebsiteClient:
    def __init__(self, settings, *, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.cookies = CookieJar()
        self.opener = opener or build_opener(HTTPCookieProcessor(self.cookies)).open
        self.sleep, self.clock = sleep, clock
        self.last_request = None
        self.requests = 0
        self.token = None
        self.referer = None

    def request(self, request):
        for attempt in range(self.settings["retries"] + 1):
            if self.last_request is not None:
                interval = max(2.0, self.settings["min_interval_seconds"])
                self.sleep(max(0, interval - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.requests += 1
            delay = min(2 ** attempt, 30)
            try:
                with self.opener(request, timeout=self.settings["timeout_seconds"]) as response:
                    body = response.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    raise DiscoveryError("Website response exceeded 32 MiB")
                if not body.strip():
                    raise DiscoveryError("Empty website response; coverage is unknown")
                return body.decode("utf-8")
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                error = f"HTTP {exc.code} from {request.full_url}"
                retry_after = (exc.headers or {}).get("Retry-After", "")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                    if not math.isfinite(delay) or delay > 60:
                        raise DiscoveryError(error + "; retry later as requested by service") from exc
            except (URLError, TimeoutError, OSError, UnicodeError, DiscoveryError) as exc:
                error = f"Invalid or unavailable response from {request.full_url}: {exc}"
            if attempt < self.settings["retries"]:
                self.sleep(delay)
        raise DiscoveryError(f"{error}; failed after {self.settings['retries'] + 1} attempts")

    def establish_session(self, pmid):
        self.referer = f"{BASE}/{pmid}/"
        html = self.request(Request(self.referer, headers={"User-Agent": "PIP-LitDB-paper-discovery/1.0"}))
        root = re.search(r'<div[^>]*\bid="root"[^>]*>', html)
        if not root:
            raise DiscoveryError("Linked Discoveries page root changed")
        attrs = {key: unescape(value) for key, value in re.findall(r'([\w-]+)="([^"]*)"', root.group())}
        if attrs.get("data-pmid") != pmid or attrs.get("data-endpoint"):
            raise DiscoveryError("Linked Discoveries page endpoint changed")
        cookie_name = attrs.get("data-csrf-cookie-name")
        self.token = next((cookie.value for cookie in self.cookies if cookie.name == cookie_name), "")

    def neighborhood(self, pmid, quantity):
        pmid = pmid_text(pmid)
        validate_quantity(quantity)
        if self.token is None:
            self.establish_session(pmid)
        for session_attempt in range(2):
            headers = {"User-Agent": "PIP-LitDB-paper-discovery/1.0", "Accept": "application/json",
                       "Origin": BASE, "Referer": self.referer}
            params = urlencode({"neighbors": quantity})
            if self.token:
                headers.update({"X-CSRFToken": self.token, "Content-Type": "application/x-www-form-urlencoded"})
                request = Request(f"{BASE}/{pmid}/links/", data=params.encode(), headers=headers)
            else:
                request = Request(f"{BASE}/{pmid}/links/?{params}", headers=headers)
            try:
                payload = json.loads(self.request(request))
                displayed_articles(payload, pmid, quantity)
                return payload
            except HTTPError as exc:
                if exc.code == 403 and session_attempt == 0:
                    self.establish_session(pmid)
                else:
                    raise
            except json.JSONDecodeError as exc:
                raise DiscoveryError("Linked Discoveries returned invalid JSON") from exc


def pubmed_records(xml):
    """Normalize PubMed citations, retaining citation year and version links."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DiscoveryError(f"Invalid PubMed XML: {exc}") from exc
    if root.tag != "PubmedArticleSet" or root.find("ERROR") is not None:
        raise DiscoveryError("Unexpected PubMed XML response")

    def text(node):
        return "" if node is None else "".join(node.itertext()).strip()

    result = {}
    for record in root.findall("PubmedArticle"):
        citation = record.find("MedlineCitation")
        article = record.find("MedlineCitation/Article")
        if citation is None or article is None:
            raise DiscoveryError("PubMed citation is missing article data")
        pmid = pmid_text(citation.findtext("PMID", ""))
        if pmid in result:
            raise DiscoveryError("Duplicate PMID in PubMed XML")
        doi_values = {normalize_doi(text(node)) for node in record.findall("PubmedData/ArticleIdList/ArticleId[@IdType='doi']")}
        doi_values.update(normalize_doi(text(node)) for node in article.findall("ELocationID[@EIdType='doi']"))
        doi_values.discard("")
        if len(doi_values) > 1:
            raise DiscoveryError(f"Conflicting PubMed DOI values for {pmid}")
        year = article.findtext("Journal/JournalIssue/PubDate/Year", "")
        if not year:
            match = re.search(r"\b(?:18|19|20)\d{2}\b", article.findtext("Journal/JournalIssue/PubDate/MedlineDate", ""))
            year = match.group() if match else article.findtext("ArticleDate/Year", "")
        authors = []
        for author in article.findall("AuthorList/Author"):
            authors.append(text(author.find("CollectiveName")) or " ".join(filter(None, [author.findtext("LastName"), author.findtext("Initials") or author.findtext("ForeName")])) )
        abstract = " ".join((node.get("Label", "") + ": " if node.get("Label") else "") + text(node)
                            for node in article.findall("Abstract/AbstractText"))
        paper = Paper("pubmed", pmid, text(article.find("ArticleTitle")), next(iter(doi_values), ""), authors,
                      text(article.find("Journal/Title")), year, abstract,
                      f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                      "; ".join(text(node) for node in article.findall("PublicationTypeList/PublicationType")))
        result[pmid] = {"paper": paper.as_dict(), "citation_year": year,
                        "language": ";".join(text(node) for node in article.findall("Language")),
                        "metadata_source": "PubMed EFetch XML",
                        "version_links": [{"type": node.get("RefType", ""), "pmid": node.findtext("PMID", "")}
                                          for node in citation.findall("CommentsCorrectionsList/CommentsCorrections")]}
    return result
