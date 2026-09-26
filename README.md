# PIP LitDB

**PIP LitDB** is a project to collect scholarly works about PIPs (pyrrole–imidazole polyamides) and extract their contents. It has two parts. The first part is a version-controlled public database of PIP literature metadata and the tools to manage the database. The second part is an untracked private database that has a subset of copies of the source documents (PDFs and/or HTML copies), extracted text/figures, and the tools to manage extraction and the database.

## ARD

- Anyone can clone and use the public database without possessing any source documents. The private copies remain untracked, but their publicly committed `pip_litdb_file_status` values must be updated when the project's holdings change.

- The only private untracked part of the project are the paper copies and extractions. The public database of metadata, the tools for the public database, the tools for the private database, and the extraction tools, are all tracked and public.

- One ID for each distinct work found. Sometimes a work found online will have slightly different metadata than an entry already in the database but is really the same work. For example, a title might appear differently because of how special characters are handled, or author names may be abbreviated in one source and spelled out in another. In cases like these, only one entry and one ID should be put into the database. In other cases there are genuinely different works or versions, such as a preprint and its published article. These should receive separate IDs and be linked through their relationship.

- The public database is organized as a file system of YMAL files so that tracking is handeled by git.

## Public Metadata Database

The database includes published research articles, preprints, reviews, corrections, and books about PIPs. Only English-language articles should be included. Cover picture articles—that is, articles that only describe a cover picture or provide similar cover-related commentary—should not be included. The database accounts for relationships between works such as errata, preprints, and versions.

Articles from problematic journals should not be included. The current list of problematic journals is:

- *Medicinal Chemistry* (OMICS Publishing Group; ISSN 2161-0444)

The following publication series are excluded from the collection's scope:

- *Proceedings for Annual Meeting of The Japanese Pharmacological Society* (Japanese Pharmacological Society; online ISSN 2435-4953): conference abstracts and presentation summaries. [Publisher reference](https://www.jstage.jst.go.jp/browse/jpssuppl/_pubinfo/-char/en).

The YAML files are the complete and only database. Each work is stored in its own YAML file in `database/records`. The filename is the authoritative PIP LitDB ID: for example, `00001.yaml` has PIP LitDB ID `00001`. The ID is not repeated as a field inside the YAML record. Search and export tools derive the ID from the filename and include it in exported data when appropriate.

The database is organized as follows:

```text
database/
├── records/
│   ├── 00001.yaml
│   ├── 00002.yaml
│   └── 00003.yaml
├── schema/
│   └── paper.schema.json
└── vocabularies/
    ├── document-types.yaml
    ├── file-statuses.yaml
    ├── jamies-human-only-note-tags.yaml
    ├── language-statuses.yaml
    ├── publication-stages.yaml
    ├── publisher-access-statuses.yaml
    ├── relationship-types.yaml
    └── record-statuses.yaml
```

Each relationship type in `database/vocabularies/relationship-types.yaml` must define its inverse relationship type. For example, the inverse of `is_preprint_of` is `has_preprint`, and the inverse of `corrects` is `is_corrected_by`. A symmetric relationship type may define itself as its inverse.

The database records the following fields for each work:

- Document type (`document_type`): The kind of document. Allowed values are `research_article`, `review`, `correction`, and `book`.
- Publication stage (`publication_stage`): The publication stage of the document. Allowed values are `preprint` and `publication`.
- Language status (`language_status`): The result of checking the publication's primary language. Allowed values are `english`, `non_english`, `uncertain`, and `unchecked`.
- Human user-reported publisher access (`human_user_reported_publisher_access`): A required current human report about access to the publisher's main full text, regardless of whether that full text is provided as HTML or PDF. Allowed values are `access`, `no_access`, and `unknown`. Automated checks must not infer this value.
- PIP LitDB file status (`pip_litdb_file_status`): Publicly committed statuses for the project's private holdings of the main PDF, supplementary material, and full-text HTML. All three status fields are required.
- Title
- Authors: One ordered list of author objects. `name` preserves the name as published in the work. Optional `canonical_name` records the project's normalized form of the same author's name and should be omitted when it is identical to `name`. ORCID identifiers are not stored. A correction verified from the source to have no credited byline uses `authors: []` and must include an explanatory `pip_litdb_notes` value. This means the correction is uncredited, not that its authors are unchecked; do not substitute the authors of the corrected paper or invent an author. All other document types require at least one author, and the `authors` field is always required.
- DOI (`doi`): The required bare DOI without a `https://doi.org/` prefix. User-facing links are generated from this value.
- Related papers: A list containing the PIP LitDB ID and directed relationship type for each related paper. Every relationship must be stored in both related records using inverse relationship types. For example, if one record uses `is_preprint_of`, the other must use `has_preprint`.
- Publication year: The year used in the work's formal citation. For an issue-assigned publication, use the issue year even when the article was published online in an earlier year. Crossref's `published-print` year and PubMed's citation year are preferred authoritative sources when available. Do not substitute Crossref's generic `published` or `issued` year, or PubMed's `Epub` year, when those fields represent an earlier online-first publication. For a work without an issue assignment, use the year shown in the authoritative recommended citation. Publication years must be 1800 or later and no more than two years after the current calendar year.
- Journal or publication venue (`journal`): For an article, use the standard full journal name rather than an abbreviation. For a book, use its series name when available, otherwise its publisher or imprint. For a preprint, use the server name.
- PIP LitDB status: A text field used exclusivly by human end users
- PIP LitDB notes: An optional field used only when a note is essential or temporary
- Jamie's human-only notes (`jamies_human_only_notes`): An optional collection of human-authored tags. Its only subfield is currently `tags`.

### Jamie's human-only notes policy

`jamies_human_only_notes` is for human use only. AI and automation must not infer, add, change, or remove its tags. Each specific edit requires explicit human permission. Tools may preserve, validate, display, and user-filter these tags without interpreting them.

This field is part of the public metadata database and remains publicly visible. “Human-only” restricts how the field is authored and interpreted; it does not make the field private.

When `jamies_human_only_notes` is present, its required `tags` subfield is a list containing one or more values controlled by `database/vocabularies/jamies-human-only-note-tags.yaml`. The currently allowed tags are:

- `read_later`
- `interesting_for_cooperativity`
- `important_to_me`
- `interesting_monomer`

Blank is the default. Represent blank by omitting `jamies_human_only_notes` entirely, rather than storing an empty object or empty `tags` list. When tags are present, for example:

```yaml
jamies_human_only_notes:
  tags:
    - read_later
    - interesting_for_cooperativity
```

File status values are defined in `database/vocabularies/file-statuses.yaml`:

- `unchecked`: The project's holdings for this material have not been assessed.
- `present`: All expected material of this kind is stored and has passed basic validation.
- `needed`: The material is known to exist, is not stored, and still needs to be acquired.
- `partial`: Some, but not all, expected material of this kind is stored.
- `error`: A stored copy exists but is corrupt, incorrect, incomplete, or otherwise fails validation.
- `uncertain`: The material was investigated, but its existence, availability, or completeness could not be determined.
- `not_available`: The material is known or expected to exist but cannot be obtained from an allowed source.
- `not_applicable`: The publication is confirmed not to provide this material or format.

Document type and publication stage describe independent characteristics. For example, a preprint and its corresponding published article may both have `document_type: research_article`, while the preprint has `publication_stage: preprint` and the published article has `publication_stage: publication`. They remain separate records and are connected using the appropriate related-paper relationship.

Optional fields with no value, including status and notes, should be omitted rather than stored as empty strings.

An example record is:

```yaml
document_type: research_article
publication_stage: preprint
title: "Example paper title"
authors:
  - name: "A. Jones"
    canonical_name: "Alex Jones"
  - name: "Morgan Jane Smith"
doi: "10.1234/example.123"
publication_year: 2024
journal: "BioRxiv"
language_status: unchecked
human_user_reported_publisher_access: unknown
pip_litdb_file_status:
  main_pdf: unchecked
  supplementary_material: unchecked
  full_text_html: unchecked
related_papers:
  - pip_litdb_id: "00002"
    relationship_type: is_preprint_of
```

Because `00001.yaml` contains an `is_preprint_of` relationship to `00002`, `00002.yaml` must contain the corresponding inverse relationship:

```yaml
related_papers:
  - pip_litdb_id: "00001"
    relationship_type: has_preprint
```

Both entries must be added, changed, or removed together.

### Record removal procedure

To remove an article, delete its YAML record, delete its corresponding
`papers (private)/NNNNN` directory, and add its DOI to
`database/removed-dois.yaml` in the same change. Remove any `related_papers` entries
in other records that reference the deleted record, but do not rename or renumber
those records. PIP LitDB IDs are permanent: deleting a record does not shift later
IDs, and the deleted ID must never be reused. The removed DOI prevents the article
from being added again under a different ID.

### Validation

Automated database validation checks:

- Every record follows `paper.schema.json`.
- Every `document_type`, `publication_stage`, `language_status`, `human_user_reported_publisher_access`, `pip_litdb_file_status`, and `jamies_human_only_notes.tags` value is defined in its corresponding vocabulary file.
- Every record filename matches the five-digit format `NNNNN.yaml`, begins at `00001`, and uniquely determines that record's PIP LitDB ID.
- Duplicate DOIs.
- Related-paper IDs and relationship types.
- Every related-paper entry has exactly one corresponding entry in the related record using the inverse relationship type defined in `relationship-types.yaml`.
- Values governed by the files in `database/vocabularies`.

It also rejects duplicate YAML/JSON keys, YAML aliases, symlinked database paths, malformed
vocabulary definitions, blank or padded single-line values, duplicate authors within a record,
credentialed, non-public, or unsupported URLs in public notes, local/private filesystem references
in public notes, self-relationships, missing relationship targets, reuse of a record ID found in
base-branch history, and relationship vocabulary inverses that are not themselves reciprocal.
Exact normalized title/year collisions are reported as reviewer warnings because they can represent
either accidental duplicates or legitimate separate versions.

Run the complete validation locally with Python 3.12 or later:

```powershell
python -m pip install -r requirements.txt
python -X utf8 scripts/run_tests.py
python scripts/validate_metadata.py
```

The test runner prints concise counts and failure excerpts, saving complete logs
and structured results in a unique directory under
`papers (private)/diagnostics/test-runs/`. Use `--log-dir` to select the assigned
private diagnostic area during extraction. Pass focused unittest arguments after
`--`; inspect every failure in the saved report, including issues omitted from
the preview. Failures, crashes, and empty test selections return a nonzero status.

The runner imports the selected test modules before executing tests. If discovery
or an import fails, it reports `discovery_failed` with zero tests run and saves all
errors. Fix the reported problem before retrying; install `requirements.txt` with
the same Python executable shown in the report when dependencies are missing.
ReportLab, used to generate PDF test fixtures, is included in the requirements.
When that package is absent from the selected Python, the runner can reuse the
existing Windows Codex bundle in the test worker. It appends that package directory
after the interpreter's existing paths, preserving installed-package precedence.
No packages are installed automatically. Reports record the executable and any
added package paths.

To get the same add/remove/modify/rename summary produced for a pull request, include a base Git
revision:

```powershell
python scripts/validate_metadata.py --base origin/main --head HEAD
```

That default uses merge-base semantics to summarize a pull-request branch. For an exact transition,
such as a pushed branch's before and after commits, add `--comparison direct`.

The `Validate metadata` GitHub Actions workflow runs for every pull request targeting `main`.
GitHub checks out the proposed merge result, then the workflow validates the complete database and
runs the validator's unit tests. The `Validate metadata` job should be a strict required check for
`main`, so a pull request must be updated and checked again whenever the base branch changes. The
same workflow validates metadata changes after they reach `main` and can also be run manually.

The CODEOWNERS policy requests repository-owner review when validation workflows, schemas,
controlled vocabularies, or validator code change. Because pull request authors cannot approve their
own changes, Code Owner approval should only be made mandatory after another trusted reviewer is
available.

The job validates the entire resulting database, not only changed files, so removing a
referenced record or changing only one side of a relationship fails the check. It also adds a job
summary with compact ID ranges, field-level modifications, errors, and non-blocking human-review
warnings.

Search and export tools read the YAML files directly.

## Public Metadata User Interface

The `UI` directory contains a static HTML, CSS, and JavaScript interface for browsing the public metadata database. The published interface is available at:

`https://7jameslondon.github.io/PIPLitDB/`

The interface does not maintain a separate database or generated metadata export. When the page is loaded, it identifies the current commit on the repository's default branch, discovers the YAML files in `database/records`, and reads the records and vocabulary files directly from that commit. PIP LitDB IDs are derived from the record filenames in the same way as the other database tools.

After a record is added, changed, or deleted and the change is pushed to the default branch, refreshing the interface loads the updated database. No separate database synchronization step is required.

The interface is organized as follows:

```text
UI/
├── index.html
├── app.js
├── config.js
├── styles.css
└── README.md
```

The site is deployed to GitHub Pages by `.github/workflows/deploy-pages.yml`. The deployment contains only the static files in `UI`; the metadata continues to be read from the canonical YAML records in the repository.

To preview the interface locally, start a static web server from the repository root:

```powershell
python -m http.server 8000
```

Then open `http://localhost:8000/UI/`. The project must be served from the repository root so the interface can discover `database/records` and load the vocabulary files. Opening `UI/index.html` directly with a `file://` URL will not work because web browsers cannot enumerate arbitrary local files.

The interface displays only information from the public metadata database. It does not read, publish, or link to the contents of `papers (private)`.

## Private Paper Copies and Extractions

Users with authorized access may store source documents and generated
extractions under `papers (private)`. Main articles, supplementary sources, and
generated outputs are kept separate:

```text
papers (private)/
|-- staging/                     # Temporary private extraction work
`-- 00001/
    |-- pdf/
    |   `-- main.pdf
    |-- html/
    |   `-- main.html
    |-- supplementary/           # Publisher files and unpacked ZIP members
    |   |-- original-name.docx
    |   `-- archive-directory/
    |       `-- original-member.ext
    |-- extraction_old/          # Temporary legacy comparison baseline
    |-- extraction/              # record.json plus linked binary/large assets
    `-- extraction_diagnostic/   # Technical reports and review material
```

Store every supplementary source file in the record's `supplementary/`
directory, regardless of format. Preserve each directly downloaded file's
original filename, extension, and bytes.

When the publisher supplies a standalone `.zip` supplement, safely unpack it
into `supplementary/`, preserving every member's archive-relative path,
filename, extension, and decompressed bytes. Do not flatten or rename members,
and do not retain the outer ZIP after complete extraction has been verified.
Do not recursively unpack nested ZIP members. This exception does not apply to
ZIP-based document formats such as `.docx`, `.xlsx`, or `.pptx`, which remain
intact.

Do not place supplementary files in `pdf/` or `html/`. After acquisition and
any required ZIP unpacking are complete, the source directories are inputs and
must not be modified by the extraction pipeline.

The existing `extraction_old/` directories contain imperfect legacy outputs.
They may be used only for comparison and are not authoritative publication
sources. They will eventually be deleted through a separate, explicitly
authorized cleanup after their replacements have been approved.

The detailed structure and behavior of new `extraction/` and
`extraction_diagnostic/` outputs are summarized below. Empty or
not-yet-generated directories do not need to be created as placeholders.

Each new extraction has one canonical, content-only `record.json`; this is the
sole content format produced for all future extraction runs. Article and
supplement text, captions, references, and structured tables are stored in that
file. Images, videos, original supplements, workbooks, datasets, and other
binary or large assets remain separate files referenced by paths relative to
the record's `extraction/` directory. New-format extractions do not create
per-table JSON or CSV files. Provenance, confidence, OCR details, and review
material remain out of the polished record in the sibling
`extraction_diagnostic/` directory.

When a PowerPoint slide is identified by its native caption as a scientific
figure, the extractor uses an installed Microsoft PowerPoint application to
render the complete slide as a lossless PNG with a 5,000-pixel long edge. That
whole-slide image is the canonical visual so chemical structures, labels,
legends, and panels retain their positional relationships. Native slide text
remains machine-readable in `record.json`, but the viewer does not present
figure-label fragments as linear prose; their coordinates are retained in
`extraction_diagnostic/`. The original PPTX and embedded media remain unchanged
and downloadable. Low-resolution package thumbnails are never accepted as
figure substitutes, and a failed required slide render blocks approval.

For a human-friendly view, open the shared repository-root
`extraction_viewer.html` and choose the `papers (private)/` directory. The
viewer scans only its immediate five-digit record directories and lists those
with a live `extraction/record.json`. Search the list, select a record, return
to the list to choose another, or use **Refresh records** and **Change private
folder** as needed. The viewer formats the canonical JSON and linked local
assets; it is not copied into each record.

This is the viewer's only loading workflow: it does not accept an individual
`extraction/` folder, standalone `record.json`, or URL/query parameter. It is
read-only and offline, and access to the chosen private directory lasts only
for the current browser session. Historical `record.md` files remain supported
only by extraction validation for older packages, not by the viewer.

Install the repository dependencies and create a staged candidate one record
at a time:

```powershell
python -m pip install -r requirements.txt
python scripts/extract_record.py NNNNN --run-id pilot-001
python scripts/validate_extraction.py "papers (private)/staging/NNNNN/pilot-001" --expected-title "Exact title"
```

For bounded, read-only inspection of large extraction JSON and archived HTML,
use `python -X utf8 scripts/inspect_extraction.py --help`. The helper provides
JSON outlines, exact value reads, literal searches, source hashes, and explicit
pagination. Follow Section 3.6 of `EXTRACTION_PROTOCOL.md` to avoid oversized or
repeated output while retaining complete source inspection and all review gates.
The current extraction setting is GPT-6 Astra at High effort. GPT-5.6 Sol
Extra High remains the historical workflow-comparison baseline.

For the authorized parallel mode, use
`python scripts/coordinate_extraction.py prepare BATCH_ID RECORD_A RECORD_B RECORD_C`
with one to three five-digit record IDs. Each owner uses its own returned private
workspace as its command working directory. Shared code and protected inputs
stay frozen; general fixes go through the primary agent after all owners stop.
The coordinator's `check` and `import` commands verify and copy the exact reviewed
and clean runs into primary staging. They do not approve a candidate. The primary
retains all review and acceptance gates and finalizes records serially with a
repository-wide lock. See `EXTRACTION_PROTOCOL.md` Section 2.5 for the full
assignment, handoff, restart and completion procedure.

For several inspection requests, use `inspect_extraction.py batch <requests.json>`.
For long reads, add `--max-output-chars 20000` on both the initial command and each
`--cursor <next_cursor>` continuation. The selected budget includes all metadata
and escaping; the helper still defaults to 12,000 characters when unspecified.
Keep targeted searches and short checks small. Continue until the cursor is null;
every requested view remains available without summarizing its content. The cursor
pins the request file, reader, and all input files. Section 3.6 specifies output
headroom, one response per tool result, and how to retry after truncation.

Use `scripts/inventory_extraction.py sources NNNNN --save-dir <private-diagnostics>`
for saved source inventories, or its `files <path>` command for directory listings.
The helper returns bounded previews and compares pinned snapshots to report all
additions, removals, and content changes. Full listings remain available on disk.
The inspection helper's `--if-view-token` can omit an exact repeat of a view that
the same agent already inspected and still retains. Follow Section 3.6 for scope,
pagination, and reviewer independence; neither feature replaces source inspection
or establishes that old diagnostics remain valid for changed inputs or code.

For scanned pages, the pipeline uses pinned local OCR dependencies and records
the engine, model hashes, page regions, confidence, and reviewed repairs only
in `extraction_diagnostic/`. Figure and scheme pixels are excluded from OCR.
Candidates remain staged while the record-owning agent completes validation,
source comparison, five-role review, adjudication, and a clean reproducibility
run. Jamie's standing policy then authorizes the primary agent to finalize and
approve a candidate automatically when every protocol gate passes:

```powershell
python scripts/finalize_extraction.py NNNNN --run-id final-reviewed-run --repro-run-id clean-repro-run
```

The finalizer records standing-policy provenance, promotes the exact reviewed
run, sets `pip_litdb_status: extracted_approved`, updates the extraction queue,
independently verifies the live result, and removes only that record's staging
history. New or unresolved findings stop finalization; routine user approval is
not requested. Approved revisions use `--replace`, which archives the prior
live extraction and diagnostics under the private record's
`extraction_history/`. The lower-level cleanup command remains available for
guarded recovery and accepts both legacy explicit-user approvals and the new
standing-policy approvals.

After finalization, combine the coordinator's saved-evidence, live-file and
browser checks in one read-only invocation:

```powershell
python scripts/check_extraction_completion.py NNNNN --test-report "papers (private)/diagnostics/<full-suite-run>/report.json" --log-dir "papers (private)/diagnostics/<assigned-area>"
```

Use the full default discovery report from `scripts/run_tests.py`. The checker
reuses guarded live validation, checks the review inventory and recorded clean
rebuild, verifies the test log hash and queue entry, and checks the actual viewer
including byte-matched local downloads. It returns a compact summary and saves
complete reports, browser output, and separate image/caption screenshots in a
new private directory. Windows uses installed Edge with a dedicated download
folder and private temporary browser files. Node and Playwright use installed
or bundled runtimes; explicit paths are available through `--node` and
`--playwright-module`.

`machine_checks_passed` still requires primary-agent inspection of the saved
screenshots and the existing source/review judgments. Confirm the supplied test
report covers the final code. The checker does not promote, change metadata,
remove staging, or re-extract a paper, so it can also verify a saved approved
record. Current before/after hashes establish that its own run was read-only;
the finalizer's original metadata-change and cleanup evidence remains required.

When both a publisher copy and a PubMed Central copy of the same manuscript are available, retain
the publisher copy as `main.pdf`; retain the PubMed Central copy only when no publisher copy is
available.

## Public Paper Extraction Tools

The `extraction tools` directory contains utilities that help authorized users download, inspect, and extract paper contents.

## Notes

- Never commit the contents of `papers (private)`. Keep this directory excluded through `.gitignore`.
- Never include private files or private filesystem paths in the public YAML records.
