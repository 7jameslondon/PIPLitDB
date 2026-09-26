"""Readable, unreviewed candidate exports for standalone linked searches."""
import csv
from urllib.parse import quote

from .linked_client import read_json


def md(value):
    value = str(value or "")
    for character in "\\`*_{}[]<>|":
        value = value.replace(character, "\\" + character)
    return value.replace("\n", " ")


def build_report(run):
    summary = read_json(run / "comparison-summary.json")
    candidates = read_json(run / "additional-screening-leads.json")
    mapping = read_json(run / "seed-resolution.json") if (run / "seed-resolution.json").exists() else {}
    unresolved = [r for r in mapping.get("records", []) if r["status"] != "mapped"]
    with (run / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ["pmid", "doi", "title", "citation_year", "journal", "language", "seed_count", "matched_rules", "warnings"])
        writer.writeheader()
        for record in candidates:
            writer.writerow({"pmid": record["pmid"], "doi": record["paper"]["doi"], "title": record["paper"]["title"],
                "citation_year": record["citation_year"], "journal": record["paper"]["journal"], "language": record["language"],
                "seed_count": len(record["seeds"]), "matched_rules": "; ".join(record["matched_rules"]),
                "warnings": "; ".join(record["warnings"])})
    dois = list(dict.fromkeys(r["paper"]["doi"] for r in candidates if r["paper"]["doi"]))
    (run / "candidate-dois.txt").write_text("\n".join(dois) + ("\n" if dois else ""), encoding="utf-8")
    with (run / "unresolved-seeds.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ["record_id", "doi", "title", "status"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(unresolved)
    lines = ["# Linked Discoveries search", "", f"Status: **{summary['status']}**. Compared at {summary['updated_at']}.", "",
        f"Catalogue snapshot: {mapping.get('catalogue_records', 'not recorded')} records; {summary['requested_seeds']} PubMed seeds; {len(unresolved)} unresolved mappings.",
        f"Seed attempts: {summary['attempted_seeds']}; valid responses: {summary['successful_seeds']}; seed-only responses: {summary['seed_only_responses']}; unavailable: {summary['unavailable_seeds']}; failed: {summary['failed_seeds']}.",
        f"Distinct returned PubMed records: {summary['unique_pmids']}; citation metadata resolved: {summary['metadata_resolved']}; missing metadata: {len(summary['missing_metadata'])}.", "",
        f"There are {summary['all_screening_leads']} unreviewed screening leads absent from the current catalogue, removed-DOI list and existing discovery queue.",
        f"This export contains {len(candidates)} leads ({len(dois)} distinct DOIs)" + (" beyond the explicitly supplied reference run." if summary["reference_run"] else "; no earlier run was used as a baseline."), "",
        "A topic match is a search signal, not an inclusion decision. Check scope, language, versions and citation details before adding a paper. Publication age is not an exclusion. Website neighbourhoods are capped and are not exhaustive literature searches.", "",
        "## Candidates", "", "| Year | Paper | DOI / PubMed | Matching rules |", "| --- | --- | --- | --- |"]
    for record in candidates:
        paper = record["paper"]
        identifier = f"[{md(paper['doi'])}](https://doi.org/{quote(paper['doi'], safe='/')})" if paper["doi"] else f"[PMID {record['pmid']}](https://pubmed.ncbi.nlm.nih.gov/{record['pmid']}/)"
        lines.append(f"| {md(record['citation_year'])} | {md(paper['title'])} | {identifier} | {md('; '.join(record['matched_rules']))} |")
    lines += ["", "## Evidence and limitations", "",
        "Seed identities and mapping failures are in `seed-resolution.json`; unresolved mappings are exported to `unresolved-seeds.csv`. A failed lookup is distinct from a completed lookup without a verified match.",
        "`neighborhoods.json` retains seed coverage and failures. `raw/` retains one response per seed. Citation responses and any fallback failures are in `metadata-batches/` and `metadata-errors.json`. `comparison.json` retains every resolved result, including results that did not match a topic rule.",
        "`candidates.csv` and `candidate-dois.txt` are unreviewed exports. Existing `review-notes.json`, priority lists and other historical review artifacts are not overwritten.",
        "This command does not add catalogue records or write decisions to the main review queue. Reusing this run directory reuses its snapshot and response cache; it is not a fresh periodic search. Use a new directory for a separate run.",
        "Linked Discoveries uses an experimental public website endpoint rather than a documented stable API. The command validates response identities and shapes, throttles requests, and reports partial coverage.", ""]
    (run / "report.md").write_text("\n".join(lines), encoding="utf-8")
