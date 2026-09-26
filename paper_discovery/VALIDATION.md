# Discovery cleanup validation

Validated on 2026-09-25 using the existing Python environment and PyYAML.

## Automated regression suite

```powershell
python -B -m unittest discover -s tests -p test_paper_discovery.py -q
```

**74 tests passed**: the existing 52 discovery tests plus 22 linked/matching/
cleanup tests. The added tests cover:

- Lexitropsin and Py-Im/PyIm vocabulary, old papers, PNA/PAMAM false matches,
  unrelated heterocycles and relevant comparisons mentioning PNA.
- Response identity, display ordering/capping, seed-only responses, malformed
  responses, anonymous-session refresh and bounded retry delays.
- Exact-DOI seed mapping, restricted title fallback, conflicting identities,
  lookup failures, and retrying a failed mapping through the whole workflow.
- Cached collection, single raw-response storage, failure stopping, interrupted
  progress and invalid saved neighbourhood membership.
- Europe PMC/PubMed metadata fallback, missing-metadata partial status,
  catalogue/removed-DOI/queue exclusions and reference comparisons.
- Report exports, unchanged catalogue and queue decisions, and keeping
  human-only metadata out of seed snapshots and search artifacts.
- Exact duplicate cleanup, hash rechecks, path boundaries and a read-only dry run.

## Live single-seed check

An isolated one-record catalogue used DOI `10.1021/ja0744899`, mapped to PMID
`17880081`, with quantity 50. The initial sandboxed attempt was blocked by local
network permissions and reported partial coverage. Retrying with network access
completed at 2026-09-25 14:20:59 UTC:

- One seed mapped and successfully collected.
- 50 distinct returned PMIDs, including the seed.
- Citation metadata resolved for all 50; no failed seeds or missing metadata.
- One raw response stored, with no duplicate `responses/` copy.

Evidence is under the ignored
`staging/validation/linked-smoke-20260925T141958Z/` folder. The fixture produced
37 topic matches, but it contained only one catalogue paper. **Those are test
outputs, not a count of new candidates missing from the real catalogue.** This
check validates the current request/response contract; it is not another full
catalogue search and does not establish future website compatibility.

A subsequent complete replay reused the saved seed response and all 50
citations with both network clients set to fail on any request. It completed
with zero network requests and did not create the main SQLite queue.

## Workspace cleanup

At the start of this cleanup, `staging/` and `state/` contained only their
placeholders. The earlier trial evidence and SQLite state were not available in
this workspace, so the prior full-catalogue trial could not be replayed here.

Both the cleanup dry run and `cleanup --apply` found zero duplicate pairs and
reclaimed **0 bytes**. The applied audit is
`staging/cleanup/20260925T142815Z_3e2c1e52.json`. Deletion behavior was verified
with temporary duplicate/mismatched/changed-file fixtures in the test suite.
No historical candidate lists or review notes were removed.

## Maximum Linked Discoveries quantity

On 2026-09-25 the endpoint rejected requests for 201 and 500 neighbours with
HTTP 400 (`Invalid form data`). Its largest supported request remains 200.
Successful requests return the seed plus up to the requested number of related
papers. The discovery selector now keeps all 200 neighbours plus the seed,
rather than truncating the complete list to 200 records.

The updated regression suite has **76 passing tests**. Added checks cover the
maximum default, rejection of unsupported or incorrectly typed quantities,
retaining the final neighbour at quantities 50/100/200, rebuilding membership
from cached responses and reading both legacy and expanded saved membership.

Replaying the 31 real responses from the targeted PNAS capture test required
no network calls. The expanded selection increased distinct retained PMIDs
from **3,360 to 3,374**, recovering 14 additional records. Replaying the live
50-neighbour response for seed PMID 27830652 also retained the target paper,
PMID 20176964, which occupies position 51 including the seed.

Probe responses and replay results are recorded under the ignored
`staging/validation/linked-limits-20260925T155507Z/` directory. The earlier
single-seed smoke-test counts above describe the selector before this change;
historical evidence and candidate reports were preserved.
