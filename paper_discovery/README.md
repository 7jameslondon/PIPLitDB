# Paper discovery

Find papers that could be added to PIP LitDB, keep a review queue, and repeat
searches when needed. **New means new to the database.** Publication dates limit
search scope; a paper's age never makes a returned candidate ineligible.

The tool reads `database/records/` and `database/removed-dois.yaml`. It writes its
own staging/state files and never edits public records or human-only notes.

## Quick start

Run these commands from the repository root using Python 3.12 or later. PyYAML
is the only third-party dependency of this feature and is already in the project
requirements. If needed, install it with the same interpreter:

```powershell
python -m pip install PyYAML==6.0.3
python -m paper_discovery plan
python -m paper_discovery run
python -m paper_discovery list
```

`plan` shows the sources, queries, topic rules and effective date ranges without
writing files or contacting a service. `run` saves a report directory and prints
its path. Open that directory's `report.md` to read candidates and coverage.
Progress goes to stderr; the final run summary is JSON on stdout.

The default run searches **Europe PMC and Crossref over the last five years**.
Each run repeats those windows to catch later-indexed records. Exact DOI matches
already in the catalogue or the removed list are excluded. Previously discovered
candidates are updated without resetting decisions.

## Sources and search scope

| Source | Behavior |
| --- | --- |
| Europe PMC | Keyword searches of indexed literature, including preprints; requests core citation metadata and abstracts. |
| Crossref | Bibliographic keyword searches of DOI metadata. These can be broad; local topic rules select candidates. |
| bioRxiv | Direct date-range feed covering all categories by default, filtered locally. Opt-in because a multi-year scan can require many requests. |
| Linked Discoveries | Separate `linked` command: use catalogue papers mapped to PubMed as similarity seeds, then export staged screening leads. |

Select one source or repeat `--source` to choose several. Explicit selection
also enables a source marked disabled in the configuration:

```powershell
python -m paper_discovery run --source europe_pmc
python -m paper_discovery run --source biorxiv
python -m paper_discovery run --source europe_pmc --source crossref --since 2024-01-01
python -m paper_discovery plan --source biorxiv --since 2023-01-01 --until 2025-12-31
```

Edit `config/default.yaml`, or pass `--config PATH`, to set each source's
`lookback_years`, `max_pages`, queries, and page size. `lookback_years: null`
removes the lower date bound for keyword sources; bioRxiv uses 2013-01-01 as its
earliest feed boundary. `--since` overrides the configured lower bound, and
`--until` defaults to today. These are source metadata dates and may differ from
the year used in a paper's formal citation.

All configured terms in a topic rule must occur in the title/abstract; any
matching rule is sufficient. Terms match word prefixes after punctuation and
case normalization. The `polyamid` stem is restricted to `polyamide` and
`polyamides`, avoiding similarly named polyamidines and polyamidoamines.
Rules cover pyrrole/imidazole, Py-Im/PyIm, PI, DNA-binding, hairpin and
minor-groove polyamides, lexitropsins, synthetic genome readers, SynGR and SynTEF.

Optional `unless_title` terms disable an individual rule when a term occurs in
the title. The broad DNA-binding, hairpin and minor-groove rules use this to
reduce PNA and PAMAM/dendrimer matches. More specific rules can still identify
papers comparing these materials with pyrrole-imidazole polyamides. A generic
pyrrole/imidazole or heterocycle mention alone is insufficient. These are search
aids, not relevance judgments; both false matches and misses remain possible.
Adjust source queries and local rules together. The observation log retains
keyword results that did not match, and linked runs retain them in `comparison.json`.

Crossref uses separate singular/plural polyamide and lexitropsin queries plus
synthetic genome reader, SynGR/DNA and SynTEF/DNA queries. All returned records
then pass through the same local topic rules.

By default, `filters.exclude_attached_material` also omits records explicitly
typed as datasets/components and Crossref posted-content titled as separately
deposited data or supplementary figures, tables and movies. This prevents a
paper's many supplementary DOIs from filling the publication queue. Exclusion
reasons remain in `observations.jsonl`. Set this option to `false` and rerun to
consider those results too. This title-based screening is a configurable heuristic;
the tool does not record it as a human dismissal. `list` applies the filters in
the supplied `--config` (or the default config) to existing candidates as well.

## Review decisions

Use the candidate ID printed by `list`, a unique prefix with at least six hex
digits, or a DOI. These are discovery IDs, not five-digit PIP LitDB record IDs.

```powershell
python -m paper_discovery review CANDIDATE_ID --decision keep --note "Relevant; verify citation before entry"
python -m paper_discovery review CANDIDATE_ID --decision dismiss --note "Outside the collection's scope"
python -m paper_discovery review CANDIDATE_ID --decision pending --note "Reconsider"
python -m paper_discovery list --decision keep
python -m paper_discovery list --decision dismiss
python -m paper_discovery list --decision all --json
```

- `pending`: needs review; default for a newly discovered candidate.
- `keep`: selected for later metadata entry; remains in the open queue.
- `dismiss`: omitted from the open queue and future candidate reports. Evidence
  and decisions remain available, and the candidate can be reopened.

`keep` does not add a YAML record. Add selected papers through the existing
metadata workflow. After a DOI is added to the public database, `list` and later
run reports omit it from the open queue automatically. `--decision all` also
shows candidates that have since been added or removed.
It also shows previously staged candidates now excluded by the active filters.

Different DOIs remain separate even with identical titles. Matching titles and
source-reported links to existing works are flagged for version/identity review.
Missing DOIs remain candidates using source identifiers; they need verification
before metadata entry. A later DOI on the same stable source identifier preserves
its candidate ID and decision. Automatic merging across sources requires a shared
DOI; ambiguous missing-DOI candidates can still need manual duplicate review.

## Repeating and completing searches

`python -m paper_discovery run` can be run whenever desired; this feature does not
install a scheduler. `history` shows previous coverage and failures:

```powershell
python -m paper_discovery history --limit 10
```

Every source/query has a page budget. Reaching it produces a **partial** report
and exit code 2. API errors also produce incomplete coverage, preserve results
already found, and return exit code 2. A successful empty search returns 0.
Do not interpret an incomplete run as evidence that no papers are missing.

For Europe PMC and Crossref, increase the budget and rerun if necessary:

```powershell
python -m paper_discovery run --source crossref --max-pages 200
```

Their queries restart rather than reusing potentially expired API cursors.
Candidates and decisions still deduplicate across these reruns.

Direct bioRxiv scans save a numeric checkpoint after each completed page. Repeat
the same command to continue. Its date interval stays pinned until completion,
even when today's date changes. Once finished, the next run starts a fresh
window. Changes to search rules or scope start a new scan. Use `--restart` to
explicitly restart one. Wider page budgets can be used when continuing a scan.

Requests are serial and throttled. The client retries transient failures and
rate limits with backoff. Optional `http.contact_email` identifies requests to
services; no account or API key is required by these adapters. Empty/malformed
API responses are failures, not empty search results.

If a run reports `Empty API response` with `HTTP 200` and `received 0 bytes`,
the service returned no JSON; coverage remains unknown and completed bioRxiv
pages keep their checkpoint. Repeat the same command to resume when the service
is responding. An empty HTTP body does not establish that no papers were found.

## Linked Discoveries

Use all catalogue papers that can be mapped to PubMed as seeds. This workflow
has no publication-date cutoff. Choose a run directory below the work
directory's `staging/` folder:

```powershell
python -m paper_discovery linked --run-dir paper_discovery/staging/linked/run-001 --quantity 200
```

The default is the largest supported request: **200 related papers plus the
seed**, up to 201 records per seed. The seed no longer consumes a related-paper
slot. Requests for 201 or 500 neighbours were rejected by the service in the
2026-09-25 limit check.

Open that directory's `report.md`, `candidates.csv` or `candidate-dois.txt`.
These are unreviewed screening leads. The command compares results with the
current catalogue, removed DOIs and existing discovery queue; it does not import
results into that queue or change decisions.

Reusing a run directory reuses its catalogue snapshot and response cache.
Choose a new directory for a later search. Optional `--reference-run PATH`
reuses citation metadata and exports leads absent from a previous comparison.
`--step resolve|collect|enrich|report` runs individual stages; `report` works
from saved evidence without network requests. See
[LINKED_DISCOVERIES.md](LINKED_DISCOVERIES.md) for coverage, caching and files.

## Cleaning duplicate trial files

```powershell
python -m paper_discovery cleanup
python -m paper_discovery cleanup --apply
```

The first command only reports a plan. `--apply` removes a legacy
`responses/<name>.json` only when the sibling `raw/<name>.json` has identical
bytes, verified by size and SHA-256 before deletion. It stays within `staging/`,
skips redirected paths and keeps the raw evidence, metadata and review files.
Each applied cleanup saves an audit under `staging/cleanup/`. New linked runs
already save one raw response per seed and do not create this redundant copy.

## Files and implementation

```text
paper_discovery/
|-- __main__.py                # python -m paper_discovery
|-- src/                      # CLI, adapters, matching, engine, SQLite state
|   |-- linked_client.py      # Website requests, response validation, PubMed XML
|   |-- linked_discoveries.py # Seed mapping, collection, metadata, comparison
|   |-- linked_reports.py    # Unreviewed linked candidate exports
|   `-- cleanup.py           # Verified redundant response removal
|-- config/default.yaml       # Source scopes, queries, matching rules, HTTP settings
|-- tests/                    # Offline regression tests
|-- staging/runs/<run-id>/
|   |-- config.json           # Settings used by this run
|   |-- run.json              # Coverage, counts, errors, catalogue/config fingerprints
|   |-- observations.jsonl    # Normalized source results and every disposition
|   |-- candidates.json       # Open queue snapshot plus IDs first seen this run
|   `-- report.md             # Readable coverage and candidate report
|-- state/discovery.sqlite3   # Candidates, source evidence, decisions, history, checkpoints
|-- state/writer.lock         # OS lock, released automatically when a writer exits
|-- staging/linked/<name>/    # User-selected linked run directories and evidence
|-- staging/cleanup/          # Applied cleanup audit reports
|-- DESIGN.md                 # Architecture and boundaries
|-- LINKED_DISCOVERIES.md     # Linked command usage and artifacts
|-- VALIDATION.md             # Checks and their limits
`-- README.md
```

Code/configuration/documentation are version-controlled. Generated staging and
state files are Git-ignored. Back up `state/` if keeping the review history is
important. Reports are snapshots; `list` reflects current decisions and the
current catalogue. Counts in `run.json` describe observations, which can include
the same DOI from multiple queries; `new_candidates` counts distinct candidates.

Use `--work-dir PATH` on any command to isolate a separate queue or test run.
Use the same work directory for its subsequent review, list and history commands.
`--repository-root PATH` selects a different read-only public catalogue.
Only one keyword run/review writer may use a work directory at a time. Linked
commands use a separate lock within each run directory. Hard interruptions
may leave a run marked `running` until the next run records it as interrupted;
successfully committed candidate data and bioRxiv checkpoints survive.

## Tests

```powershell
python -m unittest discover -s tests -p test_paper_discovery.py -v
```

The small bridge in the repository's `tests/` directory includes this feature's
tests in normal repository discovery and CI. Tests use temporary catalogues and
fake API responses; they do not need network access or private paper files.

See [VALIDATION.md](VALIDATION.md) for the cleanup checks, regression suite and
single-seed live Linked Discoveries check.
