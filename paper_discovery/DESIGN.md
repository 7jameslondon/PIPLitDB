# Discovery architecture

Discovery treats "new" as absent from the current catalogue and local discovery
state. Publication dates constrain keyword search cost and coverage, not the
eligibility of a returned paper. All public records and human-authored metadata
remain read-only.

## Keyword workflow

`cli.py` loads validated configuration and invokes `engine.py`. `sources.py`
normalizes Europe PMC, Crossref and direct bioRxiv records into `Paper` objects.
`http.py` handles JSON requests, throttling and retries. `records.py` provides
DOI normalization, topic matching, attached-material screening and catalogue
comparison. `store.py` persists candidates, source evidence, human review
decisions, runs and bioRxiv checkpoints in the local SQLite queue.

Raw source observations and their dispositions are retained separately from
the candidate queue. This allows inspecting missed matches and preserves the
reason a returned item was excluded. Exact shared DOIs support automatic
identity merging; matching titles or version links produce review warnings.

Europe PMC/Crossref searches repeat their configured windows. Direct bioRxiv
scans can resume numeric checkpoints with a pinned date range. Coverage errors
and exhausted page budgets remain explicit partial results. Discovery does not
write new catalogue YAML files or install a scheduler.

## Linked workflow

`linked_client.py` implements the public website interaction and strict response
validation, plus PubMed XML normalization. `linked_discoveries.py` handles the
sequence of catalogue snapshot, seed identity resolution, neighbourhood
collection, citation enrichment and comparison. `linked_reports.py` produces
unreviewed exports. These modules are maintained source code; none imports
executable code from an ignored or dated staging directory.

Linked runs share `Paper`, topic rules, catalogue exclusions and HTTP settings
with keyword discovery. They read the existing queue to suppress already-staged
papers, but their own evidence and exports remain in a user-selected run folder.
This preserves the existing boundary between trial results and persistent human
decisions. A run-local lock protects its files; it does not create the main
SQLite database. See [LINKED_DISCOVERIES.md](LINKED_DISCOVERIES.md) for commands,
reference comparisons, cache behavior and coverage limitations.

## Matching and retention

Rules combine title/abstract terms with optional per-rule title exclusions.
Specific polyamide vocabulary can match a paper even when broader rules are
disabled by a PNA/PAMAM title signal. The `polyamid` stem only expands to
`polyamide(s)`; unrelated names sharing the prefix cannot satisfy that term.
Rules are configurable screening aids and never infer human-only tags.

New linked collection stores one raw JSON response per seed. `cleanup.py`
handles the older redundant `responses/` copies only when a byte-identical
`raw/` copy remains. It validates paths, hashes and file sizes, preserves evidence
and human review artifacts, and writes an audit for every applied cleanup.

This cleanup does not redesign fresh-versus-resume behavior or migrate Linked
Discoveries results into the persistent review queue. It also does not establish
a relevance classifier: all matching leads still require review.
