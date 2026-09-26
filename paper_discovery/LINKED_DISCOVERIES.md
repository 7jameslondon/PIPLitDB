# Linked Discoveries workflow

The `linked` command is the maintained entry point for the staged
[NLM Linked Discoveries](https://linkeddiscoveries.ncbi.nlm.nih.gov/) workflow.
It lives in `src/` and does not import Python scripts from dated staging folders.
It uses existing catalogue papers as PubMed seeds and finds related papers that
may be missing from the catalogue. Publication age does not exclude a result.

## Run and inspect

From the repository root:

```powershell
python -m paper_discovery linked --run-dir paper_discovery/staging/linked/run-001 --quantity 200
```

`--quantity` accepts 50, 100 or 200; the default is the maximum, **200 related
papers, excluding the seed**. Discovery retains the seed separately, so these
settings allow up to 51, 101 or 201 records respectively. Ordering remains
seed-first and then descending similarity score. Different seeds can return
many of the same papers, so the final distinct count can be much lower than
seeds multiplied by quantity.

The [NLM user guide](https://linkeddiscoveries.ncbi.nlm.nih.gov/userguide/)
describes a website display capped at 200 nodes including the seed. The public
endpoint returns 200 neighbours plus the seed for a request of 200. Discovery
keeps that final returned neighbour instead of reproducing the display's
truncation. Live requests for 201 and 500 neighbours both returned HTTP 400 on
2026-09-25; increasing the local number beyond 200 cannot request more supported
results from this endpoint. See [VALIDATION.md](VALIDATION.md) for the checks.

The run snapshots the public citation fields of every current catalogue record.
It attempts to map each record to PubMed using exact DOI identity, then a unique
normalized title only when the source supplies no DOI. A conflicting DOI is
recorded for inspection, not accepted through title similarity. Unmapped records
remain visible, with `lookup_failed` distinguished from
`no_verified_pubmed_match`. A mapped record can have more than one seed PMID.

Europe PMC supplies citation metadata; PubMed EFetch fills missing citations.
The final comparison excludes catalogue DOIs, removed DOIs, already queued
candidates and configured attached-material matches from new screening leads.
Topic rules are the same configurable rules used by keyword discovery.
Missing DOIs, possible versions and title collisions remain review concerns.

Open `report.md` for coverage and leads, `candidates.csv` for a spreadsheet-ready
export, or `candidate-dois.txt` for one deduplicated DOI per line. These exports
do not assign a human relevance decision. Candidates without DOIs appear in the
report and CSV, but cannot appear in the DOI-only file.

## Steps and cached runs

`--step` defaults to `all`. The individual steps are:

| Step | Action | Required saved input |
| --- | --- | --- |
| `resolve` | Snapshot catalogue citation fields and build the seed manifest. | None; an existing snapshot is reused. |
| `collect` | Fetch or reuse each seed's related-paper response. | `seeds.json` |
| `enrich` | Resolve citation metadata, compare and export reports. | `neighborhoods.json` |
| `report` | Compare again with the current catalogue, queue and rules, then export. No network requests. | Saved neighbourhoods and citation metadata |

```powershell
python -m paper_discovery linked --run-dir paper_discovery/staging/linked/run-001 --step report
```

Reusing a directory retains its original catalogue snapshot and successful
responses. A normal repeat of `all` retries failed seed mapping and missing
collection/metadata work; successfully cached data is reused. The manifest's
quantity governs collection. Choose a new run directory when changing the
quantity or asking for an independent search of the current catalogue:

```powershell
python -m paper_discovery linked --run-dir paper_discovery/staging/linked/run-002 --reference-run paper_discovery/staging/linked/run-001 --quantity 200
```

Existing saved neighbourhoods remain readable. Repeating `collect` or `all`
rebuilds their membership from saved raw responses and recovers the formerly
discarded final neighbour without fetching that response again. Run `enrich`
after `collect` if the added record needs citation metadata. `report` alone uses
the saved membership and does not expand it. Existing manifests with quantity
50 or 100 retain that request size; use a new directory with quantity 200 to
request the maximum.

The optional reference must be a different run containing `comparison.json`.
Its citation metadata can be reused; its seed responses are not copied into the
new run. `all-screening-leads.json` contains all current matches absent from the
catalogue and queue. The report, CSV, DOI list and
`additional-screening-leads.json` contain the subset whose PMID and DOI were
absent from **all resolved results** in the reference comparison. This is a
comparison baseline, not a history of human decisions or a filter by paper age.

`--work-dir PATH` selects the staging root and existing keyword queue to read.
The run directory must resolve below that work directory's `staging/`.
`--repository-root PATH` selects a different catalogue; `--config PATH` supplies
matching and HTTP settings. There is no cache TTL, explicit refresh mode or
automatic migration into the main queue in this cleanup.

## Evidence and status

| Artifact | Purpose |
| --- | --- |
| `catalogue-snapshot.json` | Fixed seed-selection input containing public citation fields only. |
| `config.json` | Configuration used for the latest invocation. |
| `seed-resolution.json`, `unresolved-seeds.csv` | Mapping methods, disagreements, failures and unresolved records. |
| `seeds.json`, `seed-metadata.json` | Unique seed PMIDs, quantity and citation metadata. |
| `raw/<pmid>.json` | One validated original response per successful seed. |
| `neighborhoods.json`, `progress.json` | Ordered per-seed membership and coverage; new collections mark `quantity_excludes_seed: true`. |
| `seed-attempts.jsonl` | Append-only collection attempts, including repeats and failures. |
| `mapping-responses/`, `metadata-batches/` | Cached identifier/citation responses. |
| `papers.json`, `metadata-errors.json` | Normalized citations and latest metadata errors. |
| `comparison.json`, `comparison-summary.json` | Every resolved result and its disposition, plus coverage and fingerprints. |
| `all-screening-leads.json`, `additional-screening-leads.json` | All current screening matches and the subset beyond the reference. |
| `report.md`, `candidates.csv`, `candidate-dois.txt` | Unreviewed candidate exports. |
| `state/writer.lock` | Run-local process lock; this is not the main queue. |

The command returns 0 for complete processing, 2 for incomplete coverage or an
error, and 130 when interrupted. Mapping failures, failed/unavailable seeds,
unattempted seeds and unresolved result metadata keep the final report partial.
A completed lookup with no verified PubMed match remains visible but is not a
network failure. A valid response containing only its seed is recorded separately.
"Complete" means processing completed for the selected, mappable seeds; it does
not mean that every catalogue paper was mappable or every relevant paper found.

The website integration relies on public page/JSON shapes rather than a
versioned API contract. It uses an anonymous in-memory session, validates seed
identity and response shape, spaces website requests by at least two seconds,
retries transient failures within limits, and stops collection after three
consecutive non-404 failures. Missing/empty/malformed responses are failures,
not evidence that no related papers exist. Saved progress survives an interrupt.

Reports can be regenerated without touching historical `review-notes.json` or
manually curated priority/DOI files. Public catalogue records, human-only notes
and main-queue decisions are read-only throughout this workflow. The normal
`list`, `review` and `history` commands continue to cover the keyword queue only.

## Removing legacy duplicate response copies

```powershell
python -m paper_discovery cleanup
python -m paper_discovery cleanup --apply
```

Cleanup only considers `responses/<name>.json` with a sibling
`raw/<name>.json` under the selected work directory's staging tree. It compares
size and SHA-256, skips symbolic links, junctions and hard-linked pairs, then
rechecks both files while holding the run lock before deleting the redundant
response copy. A changed pair is skipped. There is no general deduplication or
recursive deletion; the raw response, mappings, metadata, reports and manual
review artifacts remain. Each applied run writes `staging/cleanup/<id>.json`
with hashes, retained paths, deletions, skips and reclaimed bytes.
