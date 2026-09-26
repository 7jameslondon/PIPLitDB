"""Standalone, staged Linked Discoveries workflow behind the discovery CLI.

Existing run directories retain their snapshot and cache. This module does not
write the normal candidate queue and has no dependency on dated trial scripts.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from .config import DiscoveryError, read_yaml, fingerprint
from .http import JsonClient
from .linked_client import (BASE, EUTILS, MAX_NEIGHBORS, WebsiteClient, displayed_articles, pmid_text,
                            pubmed_records, read_json, save_json, validate_quantity)
from .records import Catalogue, Paper, attachment_reason, match_rules, normalize_doi, normalized_text
from .sources import _epmc_paper
from .store import Store, now, writer_lock


def epmc_record(raw):
    if raw.get("source") != "MED":
        raise DiscoveryError("Expected a PubMed-indexed Europe PMC record")
    return {"paper": _epmc_paper(raw).as_dict(), "citation_year": raw.get("pubYear", ""),
            "language": raw.get("language", ""), "metadata_source": "Europe PMC core"}


def cached_records(directory):
    """Read normalized metadata or legacy trial data; never execute trial code."""
    if directory is None:
        return {}
    directory = Path(directory)
    records = {}
    for name in ("seed-metadata.json", "papers.json", "pubmed-metadata.json", "pubmed-fallback.json", "seed-metadata-pubmed.json"):
        path = directory / name
        if path.exists():
            for pmid, record in read_json(path).items():
                records[pmid_text(pmid)] = dict(record, paper=Paper(**record["paper"]).as_dict())
    # Legacy all-catalogue/expanded runs used raw EPMC dictionaries here.
    for name in ("metadata.json", "seed-metadata-epmc.json"):
        path = directory / name
        if path.exists():
            for pmid, raw in read_json(path).items():
                records.setdefault(pmid_text(pmid), epmc_record(raw))
    return records


def query_epmc(client, run, expression, size=1000, folder="mapping-responses"):
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode({
        "query": expression, "format": "json", "resultType": "core", "pageSize": size})
    key = hashlib.sha256(url.encode()).hexdigest()
    path = run / folder / f"epmc-{key}.json"
    saved = read_json(path) if path.exists() else None
    if saved and saved.get("url") != url:
        raise DiscoveryError("Metadata cache request identity mismatch")
    payload = saved["response"] if saved else client.get(url)
    try:
        items = payload["resultList"]["result"]
        if not isinstance(items, list) or isinstance(payload["hitCount"], bool) or int(payload["hitCount"]) != len(items):
            raise DiscoveryError("Incomplete Europe PMC metadata response")
        result = {}
        for item in items:
            pmid = pmid_text(item["id"])
            if pmid in result:
                raise DiscoveryError("Duplicate PubMed record in metadata response")
            result[pmid] = epmc_record(item)
    except (KeyError, TypeError, ValueError) as exc:
        raise DiscoveryError(f"Invalid Europe PMC metadata response: {exc}") from exc
    if saved is None:
        save_json(path, {"at": now(), "url": url, "response": payload})
    return result, url


def fetch_pubmed(client, run, identifiers):
    identifiers = [pmid_text(pmid) for pmid in identifiers]
    url = EUTILS + "/efetch.fcgi?" + urlencode({"db": "pubmed", "id": ",".join(identifiers),
                                               "retmode": "xml", "tool": "PIP-LitDB-paper-discovery"})
    path = run / "metadata-batches" / ("pubmed-" + hashlib.sha256(url.encode()).hexdigest() + ".xml")
    xml = path.read_text(encoding="utf-8") if path.exists() else client.request(
        Request(url, headers={"User-Agent": "PIP-LitDB-paper-discovery/1.0", "Accept": "application/xml"}))
    records = pubmed_records(xml)
    if not set(records).issubset(set(identifiers)):
        raise DiscoveryError("PubMed supplied unexpected identifiers")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(".xml.tmp")
        temporary.write_text(xml, encoding="utf-8")
        temporary.replace(path)
    return records, url


def resolve_seeds(root, work, run, config, *, reference=None, client=None, website=None, quantity=MAX_NEIGHBORS, progress=print):
    validate_quantity(quantity)
    client = client or JsonClient(config["http"])
    website = website or WebsiteClient(config["http"])
    snapshot_path = run / "catalogue-snapshot.json"
    if snapshot_path.exists():
        snapshot = read_json(snapshot_path)
    else:
        catalogue = Catalogue.load(root)
        snapshot = {"created_at": now(), "catalogue_fingerprint": catalogue.fingerprint, "records": []}
        for path in sorted((root / "database/records").glob("[0-9][0-9][0-9][0-9][0-9].yaml")):
            record = read_yaml(path)
            # Never carry human-only fields into search requests or artifacts.
            snapshot["records"].append({"record_id": path.stem, "doi": normalize_doi(record["doi"]),
                "title": record["title"], "year": record.get("publication_year"), "journal": record.get("journal"),
                "document_type": record.get("document_type"), "publication_stage": record.get("publication_stage")})
        save_json(snapshot_path, snapshot)
    by_doi = {r["doi"]: r for r in snapshot["records"]}
    by_title = defaultdict(list)
    for record in snapshot["records"]:
        by_title[normalized_text(record["title"])].append(record)
    mappings, metadata = defaultdict(dict), {}
    failures, disagreements = [], []

    def accept(pmid, item, evidence):
        pmid = pmid_text(pmid)
        paper = Paper(**item["paper"])
        record, method = by_doi.get(paper.doi), "exact_doi"
        if record is None:
            matches = by_title.get(normalized_text(paper.title), [])
            if len(matches) == 1:
                if not paper.doi:
                    record, method = matches[0], "exact_normalized_title_no_source_doi"
                else:
                    disagreements.append({"record_id": matches[0]["record_id"], "pmid": pmid,
                        "catalogue_doi": matches[0]["doi"], "source_doi": paper.doi, "evidence": evidence})
        if record is not None:
            mappings[record["doi"]][pmid] = {"pmid": pmid, "method": method, "evidence": evidence,
                "abstract_available": bool(paper.abstract), "publication_type": paper.publication_type}
            metadata[pmid] = item

    for directory in (reference, run):
        for pmid, item in cached_records(directory).items():
            accept(pmid, item, str(directory))
    for path in sorted((work / "staging/runs").glob("*/observations.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                paper = read_observation_paper(line)
                if paper and paper.source == "europe_pmc" and paper.source_id.startswith("MED:"):
                    accept(paper.source_id[4:], {"paper": paper.as_dict(), "metadata_source": "Saved Europe PMC observation",
                                                "citation_year": "", "language": ""}, str(path))

    def quoted(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    for field, batch_size in (("doi", 20), ("title", 10)):
        missing = [r for r in snapshot["records"] if r["doi"] not in mappings]
        for offset in range(0, len(missing), batch_size):
            batch = missing[offset:offset + batch_size]
            expression = "SRC:MED AND (" + " OR ".join(field.upper() + ":" + quoted(r[field]) for r in batch) + ")"
            try:
                items, url = query_epmc(client, run, expression)
                for pmid, item in items.items():
                    accept(pmid, item, url)
            except (DiscoveryError, OSError, ValueError) as exc:
                failures.append({"phase": field, "records": [r["record_id"] for r in batch], "error": str(exc)})
            progress(f"PubMed seed mapping: {len(mappings)}/{len(snapshot['records'])} records")
    missing = [r for r in snapshot["records"] if r["doi"] not in mappings]
    for offset in range(0, len(missing), 25):
        batch = missing[offset:offset + 25]
        url = EUTILS + "/esearch.fcgi?" + urlencode({"db": "pubmed", "term": " OR ".join(quoted(r["doi"]) + "[aid]" for r in batch),
                                                    "retmode": "json", "retmax": 1000, "tool": "PIP-LitDB-paper-discovery"})
        path = run / "mapping-responses" / ("pubmed-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
        try:
            payload = read_json(path) if path.exists() else client.get(url)
            result = payload["esearchresult"]
            ids = [pmid_text(pmid) for pmid in result["idlist"]]
            if len(set(ids)) != len(ids) or int(result["count"]) != len(ids):
                raise DiscoveryError("Incomplete PubMed identifier lookup")
            save_json(path, payload)
            for start in range(0, len(ids), 100):
                items, metadata_url = fetch_pubmed(website, run, ids[start:start + 100])
                if set(items) != set(ids[start:start + 100]):
                    raise DiscoveryError("Incomplete PubMed seed metadata")
                for pmid, item in items.items():
                    accept(pmid, item, metadata_url)
        except (DiscoveryError, OSError, ValueError, KeyError, TypeError) as exc:
            failures.append({"phase": "pubmed_doi", "records": [r["record_id"] for r in batch], "error": str(exc)})
    seeds, resolved = {}, []
    failed_records = {r for failure in failures for r in failure["records"]}
    for record in snapshot["records"]:
        matches = list(mappings.get(record["doi"], {}).values())
        status = "mapped" if matches else "lookup_failed" if record["record_id"] in failed_records else "no_verified_pubmed_match"
        resolved.append(dict(record, matches=matches, status=status))
        for match in matches:
            seed = seeds.setdefault(match["pmid"], {"pmid": match["pmid"], "title": record["title"],
                "reason": "existing catalogue paper", "catalogue_records": [], "mapping_evidence": []})
            seed["catalogue_records"].append(record["record_id"])
            seed["mapping_evidence"].append(match)
    manifest = {"quantity": quantity, "catalogue_snapshot_at": snapshot["created_at"],
                "catalogue_fingerprint": snapshot["catalogue_fingerprint"], "seeds": list(seeds.values())}
    resolution = {"snapshot_at": snapshot["created_at"], "checked_at": now(), "catalogue_records": len(resolved),
                  "mapped_records": len(mappings), "unique_seed_pmids": len(seeds), "records": resolved,
                  "identity_disagreements": disagreements, "failures": failures}
    save_json(run / "seed-metadata.json", metadata)
    save_json(run / "seed-resolution.json", resolution)
    save_json(run / "seeds.json", manifest)
    return resolution


def read_observation_paper(line):
    value = json.loads(line).get("paper")
    return Paper(**value) if value else None


def collect(run, config, *, website=None, progress=print):
    manifest = read_json(run / "seeds.json")
    quantity = validate_quantity(manifest["quantity"])
    seeds = manifest["seeds"]
    ids = [pmid_text(seed["pmid"]) for seed in seeds]
    if len(set(ids)) != len(ids):
        raise DiscoveryError("Duplicate seeds in manifest")
    website = website or WebsiteClient(config["http"])
    successful, failures, counts = [], [], Counter()
    started, consecutive_errors = now(), 0

    def checkpoint(interrupted=False):
        attempted = len(successful) + len(failures)
        status = "interrupted" if interrupted else "complete" if attempted == len(seeds) and not failures else "partial"
        data = {"started_at": started, "updated_at": now(), "status": status,
                "requested_seeds": len(seeds), "attempted_seeds": attempted, "quantity": quantity,
                "quantity_excludes_seed": True,
                "seeds": successful, "failures": failures, "counts": dict(counts)}
        save_json(run / "neighborhoods.json", data)
        save_json(run / "progress.json", {k: v for k, v in data.items() if k not in {"seeds", "failures"}} |
                  {"successful_seeds": len(successful), "unique_pmids": len({p for s in successful for p in s["pmids"]})})
        return data

    try:
        for index, seed in enumerate(seeds, 1):
            pmid = pmid_text(seed["pmid"])
            path = run / "raw" / f"{pmid}.json"
            entry = dict(seed, pmid=pmid, page_url=f"{BASE}/{pmid}/", quantity=quantity)
            try:
                cached = path.exists()
                payload = read_json(path) if cached else website.neighborhood(pmid, quantity)
                articles = displayed_articles(payload, pmid, quantity)
                if not cached:
                    save_json(path, payload)  # Exactly one stored copy of each response.
                entry.update(status="complete", pmids=[str(a["articleId"]) for a in articles],
                    response_count=len(payload["articles"]), neighbor_count=len(articles) - 1, used_cached_response=cached,
                    retrieved_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    response_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                successful.append(entry)
                counts["cached" if cached else "fetched"] += 1
                consecutive_errors = 0
            except HTTPError as exc:
                entry.update(status="unavailable" if exc.code == 404 else "failed", http_status=exc.code, error=f"HTTP {exc.code}")
                failures.append(entry)
                consecutive_errors = 0 if exc.code == 404 else consecutive_errors + 1
            except (DiscoveryError, OSError, ValueError, KeyError, TypeError) as exc:
                entry.update(status="failed", error=str(exc))
                failures.append(entry)
                consecutive_errors += 1
            with (run / "seed-attempts.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            checkpoint()
            progress(f"Linked Discoveries seed {index}/{len(seeds)}: {entry['status']}")
            if consecutive_errors >= 3:
                progress("Stopped after three consecutive errors; completed responses are retained.")
                break
    except KeyboardInterrupt:
        checkpoint(interrupted=True)
        raise
    return checkpoint()


def read_neighborhoods(run):
    data = read_json(run / "neighborhoods.json")
    try:
        attempts = data["seeds"] + data["failures"]
        identifiers = [pmid_text(seed["pmid"]) for seed in attempts]
        if (type(data["attempted_seeds"]) is not int or type(data["requested_seeds"]) is not int or
                len(attempts) != data["attempted_seeds"] or data["requested_seeds"] < len(attempts) or
                len(set(identifiers)) != len(identifiers)):
            raise DiscoveryError("Inconsistent saved seed coverage")
        for seed in data["seeds"]:
            pmids = [pmid_text(pmid) for pmid in seed["pmids"]]
            quantity = validate_quantity(seed.get("quantity", data.get("quantity", MAX_NEIGHBORS)))
            # Legacy saved selections may contain only quantity total records.
            if not 1 <= len(pmids) <= quantity + 1 or len(set(pmids)) != len(pmids) or pmids[0] != str(seed["pmid"]):
                raise DiscoveryError("Invalid saved neighbourhood membership")
        if any(f["status"] not in {"failed", "unavailable"} for f in data["failures"]):
            raise DiscoveryError("Invalid saved seed failure status")
    except (KeyError, TypeError, AttributeError) as exc:
        raise DiscoveryError(f"Invalid saved neighbourhood data: {exc}") from exc
    return data


def enrich(root, work, run, config, *, reference=None, client=None, website=None, progress=print):
    neighborhoods = read_neighborhoods(run)
    memberships = defaultdict(list)
    for seed in neighborhoods["seeds"]:
        for rank, pmid in enumerate(seed["pmids"], 1):
            memberships[pmid_text(pmid)].append({"seed_pmid": seed["pmid"], "rank": rank,
                                                "catalogue_records": seed.get("catalogue_records", [])})
    metadata = cached_records(reference)
    metadata.update(cached_records(run))
    metadata = {pmid: item for pmid, item in metadata.items() if pmid in memberships}
    client, website = client or JsonClient(config["http"]), website or WebsiteClient(config["http"])
    errors = []
    missing = sorted(set(memberships) - set(metadata), key=int)
    try:
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            expression = "SRC:MED AND (" + " OR ".join("EXT_ID:" + pmid for pmid in batch) + ")"
            try:
                items, url = query_epmc(client, run, expression, 100, "metadata-batches")
                if not set(items).issubset(set(batch)):
                    raise DiscoveryError("Europe PMC returned unexpected PMIDs")
                metadata.update(items)
            except (DiscoveryError, OSError, ValueError) as exc:
                errors.append({"phase": "europe_pmc", "pmids": batch, "error": str(exc)})
            progress(f"Linked Discoveries citations: {len(metadata)}/{len(memberships)}")
        missing = sorted(set(memberships) - set(metadata), key=int)
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            try:
                items, url = fetch_pubmed(website, run, batch)
                metadata.update(items)
                if set(batch) - set(items):
                    errors.append({"phase": "pubmed", "pmids": sorted(set(batch) - set(items)), "error": "Missing PubMed metadata"})
            except (DiscoveryError, OSError, ValueError) as exc:
                errors.append({"phase": "pubmed", "pmids": batch, "error": str(exc)})
    finally:
        save_json(run / "papers.json", metadata)
        save_json(run / "metadata-errors.json", errors)
    return compare(root, work, run, config, reference=reference)


def compare(root, work, run, config, *, reference=None):
    neighborhoods = read_neighborhoods(run)
    metadata = cached_records(run)
    memberships = defaultdict(list)
    for seed in neighborhoods["seeds"]:
        for rank, pmid in enumerate(seed["pmids"], 1):
            memberships[pmid_text(pmid)].append({"seed_pmid": seed["pmid"], "rank": rank,
                                                "catalogue_records": seed.get("catalogue_records", [])})
    previous = read_json(reference / "comparison.json")["records"] if reference else []
    previous_pmids = {r["pmid"] for r in previous}
    previous_dois = {r["paper"]["doi"] for r in previous if r["paper"].get("doi")}
    catalogue = Catalogue.load(root)
    queue = []
    if (work / "state/discovery.sqlite3").exists():
        store = Store(work, readonly=True)
        try:
            queue = store.candidates(catalogue, decision="all")
        finally:
            store.close()
    queue_dois = {r["paper"]["doi"]: r["id"] for r in queue if r["paper"]["doi"]}
    queue_pmids = {s["source_id"][4:]: r["id"] for r in queue for s in r["sources"]
                   if s["source"] == "europe_pmc" and s["source_id"].startswith("MED:")}
    records, candidates, counts = [], [], Counter()
    for pmid in sorted(set(memberships) & set(metadata), key=int):
        item = metadata[pmid]
        paper = Paper(**item["paper"])
        disposition = catalogue.disposition(paper)
        queue_id = queue_dois.get(paper.doi) or queue_pmids.get(pmid)
        reason = attachment_reason(paper) if config["filters"]["exclude_attached_material"] else ""
        if disposition == "candidate":
            disposition = "previously_staged" if queue_id else "attached_material" if reason else "not_in_catalogue_or_queue"
        rules = match_rules(paper, config["match_rules"])
        record = {"pmid": pmid, "paper": paper.as_dict(), "citation_year": item.get("citation_year", ""),
            "language": item.get("language", ""), "metadata_source": item.get("metadata_source", "Saved citation metadata"),
            "matched_rules": rules, "additional_signals": [], "disposition": disposition, "screening_reason": reason,
            "catalogue_id": catalogue.dois.get(paper.doi), "queue_id": queue_id, "warnings": catalogue.warnings(paper),
            "seeds": memberships[pmid], "in_previous_trial": pmid in previous_pmids or bool(paper.doi and paper.doi in previous_dois)}
        records.append(record)
        counts[disposition] += 1
        if disposition == "not_in_catalogue_or_queue" and rules:
            candidates.append(record)
    candidates.sort(key=lambda r: (-len(r["seeds"]), min(s["rank"] for s in r["seeds"]), int(r["pmid"])))
    additional = [r for r in candidates if not r["in_previous_trial"]]
    missing = sorted(set(memberships) - set(metadata), key=int)
    mapping = read_json(run / "seed-resolution.json") if (run / "seed-resolution.json").exists() else {}
    partial = missing or neighborhoods["failures"] or neighborhoods["attempted_seeds"] < neighborhoods["requested_seeds"] or mapping.get("failures")
    summary = {"updated_at": now(), "status": "partial" if partial else "complete",
        "requested_seeds": neighborhoods["requested_seeds"], "attempted_seeds": neighborhoods["attempted_seeds"],
        "successful_seeds": len(neighborhoods["seeds"]), "seed_only_responses": sum(len(s["pmids"]) == 1 for s in neighborhoods["seeds"]),
        "unavailable_seeds": sum(f["status"] == "unavailable" for f in neighborhoods["failures"]),
        "failed_seeds": sum(f["status"] == "failed" for f in neighborhoods["failures"]),
        "unique_pmids": len(memberships), "metadata_resolved": len(records), "dispositions": dict(counts),
        "all_screening_leads": len(candidates), "additional_screening_leads": len(additional),
        "missing_metadata": missing, "metadata_errors": read_json(run / "metadata-errors.json") if (run / "metadata-errors.json").exists() else [],
        "catalogue_fingerprint": catalogue.fingerprint, "catalogue_records_at_comparison": len(catalogue.dois),
        "config_fingerprint": fingerprint(config),
        "reference_run": str(reference) if reference else None, "report_directory": str(run)}
    save_json(run / "all-screening-leads.json", candidates)
    save_json(run / "additional-screening-leads.json", additional)
    save_json(run / "comparison-summary.json", summary)
    save_json(run / "comparison.json", dict(summary, records=records))
    return summary


def run_linked(config, root, work, run, *, step="all", reference=None, quantity=MAX_NEIGHBORS, progress=print):
    run = run.resolve()
    staging = (work / "staging").resolve()
    if not run.is_relative_to(staging) or run == staging:
        raise DiscoveryError("Linked Discoveries run directory must be below the work directory's staging folder")
    if reference:
        reference = reference.resolve()
        if reference == run or not (reference / "comparison.json").is_file():
            raise DiscoveryError("Reference run must be a different completed run with comparison.json")
    if step not in {"all", "resolve", "collect", "enrich", "report"}:
        raise DiscoveryError("Unknown Linked Discoveries step")
    validate_quantity(quantity)
    run.mkdir(parents=True, exist_ok=True)
    # A run-local lock protects caches; the main review queue remains read-only.
    with writer_lock(run):
        save_json(run / "config.json", config)
        resolution_path = run / "seed-resolution.json"
        mapping_failed = resolution_path.exists() and bool(read_json(resolution_path).get("failures"))
        if step == "resolve" or (step == "all" and (not (run / "seeds.json").exists() or mapping_failed)):
            summary = resolve_seeds(root, work, run, config, reference=reference, quantity=quantity, progress=progress)
            if step == "resolve":
                return {"status": "partial" if summary["failures"] else "complete", "mapped_records": summary["mapped_records"],
                        "catalogue_records": summary["catalogue_records"], "report_directory": str(run)}
        if step in {"all", "collect"}:
            summary = collect(run, config, progress=progress)
            if step == "collect":
                return {k: v for k, v in summary.items() if k not in {"seeds", "failures"}} | {"report_directory": str(run)}
        if step in {"all", "enrich"}:
            summary = enrich(root, work, run, config, reference=reference, progress=progress)
        else:
            summary = compare(root, work, run, config, reference=reference)
        from .linked_reports import build_report
        build_report(run)
        return summary
