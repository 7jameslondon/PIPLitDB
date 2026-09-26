"""Citation identities and read-only comparison with the public catalogue."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .config import DiscoveryError, read_yaml


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() in {"p", "title", "h1", "h2", "h3", "h4", "div", "jats:p"}:
            self.parts.append(" ")


def plain(value) -> str:
    parser = _Text()
    parser.feed(str(value or ""))
    return " ".join(unescape("".join(parser.parts)).split())


def normalized_text(value) -> str:
    # Paper fields have already been converted to text. Parsing them as HTML
    # again would consume literal angle brackets and could hide following text.
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def normalize_doi(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.I)
    if text.lower().startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        text = unquote(urlsplit(text).path[1:])
    text = text.casefold()
    return text if re.fullmatch(r"10\.\d{4,9}/\S+", text) else ""


@dataclass
class Paper:
    source: str
    source_id: str
    title: str
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    publication_date: str = ""
    abstract: str = ""
    url: str = ""
    publication_type: str = ""
    related_dois: list[str] = field(default_factory=list)
    version: str = ""

    def __post_init__(self):
        self.doi = normalize_doi(self.doi)
        for name in ("title", "journal", "abstract", "publication_type"):
            setattr(self, name, plain(getattr(self, name)))
        self.authors = [plain(author) for author in self.authors if plain(author)]
        self.related_dois = sorted({normalize_doi(doi) for doi in self.related_dois} - {"", self.doi})
        if not self.source_id or not self.title:
            raise DiscoveryError(f"{self.source} returned a paper without an identifier or title")

    @property
    def identity(self):
        return f"doi:{self.doi}" if self.doi else f"{self.source}:{self.source_id}"

    @property
    def candidate_id(self):
        return "c_" + hashlib.sha256(self.identity.encode()).hexdigest()[:24]

    def as_dict(self):
        return asdict(self)


def match_rules(paper: Paper, rules: list[dict]) -> list[str]:
    text = normalized_text(paper.title + " " + paper.abstract)
    # These names share a prefix with polyamide but denote different chemistry.
    # Mask tokens, not entire papers: a PIP/PNA comparison can still be relevant.
    text = re.sub(r"\bpolyamidoamines?\b|\bpolyamidines?\b|\bnon polyamides?\b", " ", text)
    text = re.sub(r"\bpoly\s+amido\s*amines?\b", " ", text)
    title = normalized_text(paper.title)

    def contains(value, term):
        tokens = normalized_text(term).split()
        # Preserve the documented prefix semantics for custom terms, with a
        # chemically specific expansion of the default polyamid stem.
        pattern = r"\s+".join("polyamides?\\b" if t == "polyamid" else re.escape(t) for t in tokens)
        return bool(re.search(r"\b" + pattern, value))

    return [rule["name"] for rule in rules
            if all(contains(text, term) for term in rule["all"])
            and not any(contains(title, term) for term in rule.get("unless_title", []))]


def attachment_reason(paper: Paper) -> str:
    kind = paper.publication_type.casefold()
    if kind in {"component", "dataset"}:
        return f"Source identifies a {kind}, rather than a standalone publication."
    if paper.source == "crossref" and kind == "posted-content":
        title = normalized_text(paper.title)
        if re.match(r"^(?:data from\b|(?:supplementary (?:figures?|tables?|data|materials?)|"
                    r"(?:figures?|tables?|movies?) s?\d+|supporting information)\b.*\bfrom\b)", title):
            return "Crossref title identifies separately deposited data or supplementary material."
    return ""


@dataclass
class Catalogue:
    dois: dict[str, str]
    titles: dict[str, list[str]]
    removed: set[str]
    fingerprint: str

    @classmethod
    def load(cls, root: Path):
        paths = sorted((root / "database" / "records").glob("[0-9][0-9][0-9][0-9][0-9].yaml"))
        if not paths:
            raise DiscoveryError(f"No catalogue records under {root / 'database/records'}")
        dois, titles = {}, {}
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode() + path.read_bytes())
            record = read_yaml(path)
            if not isinstance(record, dict):
                raise DiscoveryError(f"Invalid catalogue record {path.name}")
            doi = normalize_doi(record.get("doi"))
            title = normalized_text(record.get("title"))
            if not doi or not title:
                raise DiscoveryError(f"Missing valid DOI or title in {path.name}")
            if doi in dois:
                raise DiscoveryError(f"Duplicate catalogue DOI: {doi}")
            dois[doi] = path.stem
            titles.setdefault(title, []).append(path.stem)
        removed_path = root / "database" / "removed-dois.yaml"
        digest.update(removed_path.read_bytes())
        removed = read_yaml(removed_path)
        if not isinstance(removed, list) or any(not isinstance(doi, str) or not normalize_doi(doi) for doi in removed):
            raise DiscoveryError("removed-dois.yaml must be a list of valid DOIs")
        return cls(dois, titles, {normalize_doi(doi) for doi in removed}, digest.hexdigest())

    def disposition(self, paper: Paper) -> str:
        if paper.doi in self.dois:
            return "already_in_database"
        if paper.doi in self.removed:
            return "removed_doi"
        return "candidate"

    def warnings(self, paper: Paper) -> list[str]:
        warnings = []
        if not paper.doi:
            warnings.append("No valid DOI supplied; metadata entry needs DOI verification.")
        same_title = self.titles.get(normalized_text(paper.title), [])
        if same_title and paper.doi not in self.dois:
            warnings.append("Same normalized title as record(s) " + ", ".join(same_title) + "; review versions/identity.")
        for doi in paper.related_dois:
            if doi in self.dois:
                warnings.append(f"Source links a related work already in record {self.dois[doi]} ({doi}).")
        return warnings
