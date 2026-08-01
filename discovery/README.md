# Automated PIP paper discovery

The discovery tool searches OpenAlex, PubMed, Crossref, and arXiv for papers in
an inclusive publication/posting date window. Cross-source results are
reconciled with field-level provenance, scored using the tracked rules in
`queries.yaml`, compared with the canonical YAML database, and reported for
human review. bioRxiv records found by an aggregator are enriched through the
direct DOI-details API. ChemRxiv discovery currently uses Crossref and OpenAlex;
direct enrichment stays disabled until its public API exposes a stable,
fixture-tested DOI-details contract.

The default window ends on the current UTC date and begins six calendar months
earlier. For example, a run ending on August 31 begins on February 28 (or 29 in
a leap year). Created, updated, and indexed dates never make an older paper
eligible.

## Local use

Install Python 3.12 dependencies:

```powershell
python -m pip install --requirement requirements-discovery.txt
```

Set `OPENALEX_API_KEY` and `DISCOVERY_CONTACT_EMAIL`. `NCBI_API_KEY` is optional.
Then run a read-only search:

```powershell
python scripts/discover_papers.py --dry-run
python scripts/discover_papers.py --from 2026-01-01 --until 2026-06-30 --dry-run
```

Responses are cached below `.cache/discovery/`; reports go to
`artifacts/discovery/`. Use `--offline` to prohibit network access and require
cached responses. A repeated `--source` option is diagnostic and always
read-only. `--allow-partial` produces a visibly partial report and also disables
record writes.

To generate records after a complete all-source run:

```powershell
python scripts/discover_papers.py --from 2026-01-01 --until 2026-06-30 --write-records --base origin/main
```

Generation allocates IDs above every record filename in reachable base history,
writes into a temporary complete database first, and runs the existing validator
again against an ephemeral base/head commit. Any failure removes the files it
created.

## Query and exclusion review

Edit `queries.yaml` to add generic phrases or supporting concepts. Do not add a
hidden benchmark title, DOI, or exact author combination. Low-scoring acronym
matches remain report-only.

When a proposed work is irrelevant, add exactly one stable DOI, source ID, or
URL to `exclusions.yaml`, together with a concise reason and ISO decision date,
then remove that proposed record. Exclusions are exact only; gaps among IDs in
an unmerged PR are valid.

## Historical backtest

The committed benchmark is deliberately marked `not_recorded` until exact dates
and source eligibility are manually reviewed. Bootstrap a review candidate
without overwriting it implicitly:

```powershell
python scripts/build_discovery_benchmark.py --as-of 2026-07-31 --output artifacts/discovery/known-papers-candidate.yaml
```

After review and recording, run the fixed-window benchmark:

```powershell
python scripts/backtest_discovery.py --from 2018-01-01 --as-of 2026-07-31 --window-months 6 --step-months 6
```

The report explicitly describes retrospective recovery from current indexes. A
fresh cache run receives a new immutable namespace; reviewed compact replay data
can be committed without raw responses or full abstracts.

## Scheduled operation and recovery

`discover-papers.yml` runs Sundays at 06:17 in `America/Chicago`. Manual runs
default to dry-run. Discovery has read-only repository permissions and source
credentials; publication receives the checksummed bounded handoff but no source
credentials. Publication is gated on a complete four-source run with no source
filter or partial mode.

Configure these repository secrets before rollout:

- `OPENALEX_API_KEY`
- `DISCOVERY_CONTACT_EMAIL`
- optional `NCBI_API_KEY`
- `DISCOVERY_PUBLISH_TOKEN`, preferably a narrowly scoped GitHub App token (or a
  machine-user token whose identity is not `@7jameslondon`) able to create PRs,
  labels, and review requests. A maintainer-owned token would author the PR as
  the requested reviewer and cannot satisfy the notification design.

Repository Actions settings must allow workflow-created pull requests. Use a
controlled fixture candidate to verify that creating the PR triggers the
trusted `Validate metadata records` status on the current merge commit. Review
is requested from `@7jameslondon` only after that status succeeds. Confirm that
the maintainer's GitHub notification settings deliver review requests by email.

Batch branch names and commit trailers contain only hashes and validated dates.
An interrupted run inventories same-repository automation branches and marked
PRs before searching. It resumes orphan branches and open batches; the durable
PR timeline, rather than mutable current-reviewer state, prevents duplicate
review notifications.

GitHub can disable scheduled workflows after repository inactivity. Re-enable
the workflow from the Actions page and consider an external weekly dead-man
alert if a missing run must page an operator.
