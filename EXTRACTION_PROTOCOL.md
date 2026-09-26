# PIP LitDB Extraction Protocol

## 1. Purpose

This is the operating protocol for producing one complete, AI-ready extraction of one private PIP LitDB record.

It is written for a **record-owning extraction agent**. That agent is expected to use tools, inspect the publication visually and structurally, run the extraction software, diagnose problems, improve the software or an evidence-backed record override, rebuild the candidate, and repeat review until the staged extraction is ready for primary-agent finalization.

This is not a one-shot conversion task. Passing an automated validator is necessary but is not enough. The record owner must compare the extraction with the actual sources and exercise scientific and editorial judgment.

Detailed design decisions remain in `EXTRACTION_PLAN.md`. Schemas and executable behavior remain in the extraction code and schema files. If this protocol, the plan, and the current code materially disagree, stop and report the conflict rather than silently choosing one.

## 2. Agent model

### 2.1 One owner per record

Only one extraction agent may own and write to an active record at a time. The normal single-record workflow remains available. Jamie has authorized batches of up to three concurrent records using Astra High: concurrent owners must use the isolated workspaces and frozen shared code described in Section 2.5. Never run concurrent writing owners in the primary checkout.

The record owner may:

- inspect all source material for its assigned record;
- create new, uniquely named staged runs;
- edit shared extraction code when it finds a general defect in the single-record workflow; in isolated parallel mode, report the required fix to the primary agent under Section 2.5;
- add or edit tests for that defect;
- create or edit the assigned record's temporary private `extraction_overrides.yaml` when the correction is genuinely record-specific and fully supported by source evidence;
- run validators, tests, renderers, and the local extraction viewer;
- convene or request read-only reviewers after freezing a candidate;
- adjudicate review findings, make corrections, and rebuild the candidate.

The record owner must not:

- work on an unassigned record;
- promote its own candidate;
- mark its own work approved;
- update public record metadata during extraction or review;
- alter human-authored metadata;
- overwrite a prior staged run to conceal changes.

### 2.2 Review and iteration

The record owner uses the same loop as the primary agent:

1. extract;
2. validate;
3. compare against every source;
4. review the rendered result;
5. identify the underlying cause of each issue;
6. fix code or add an evidence-backed override;
7. add regression coverage when shared code changes;
8. create a fresh staged run;
9. repeat until no unresolved issue remains.

The record owner is the only writer during this loop.

The normal review mode is **one record owner performing five sequential self-reviews**, followed by a separate adjudication and primary-agent finalization. Freeze the candidate and its code before those reviews. Each pass covers its full role in Step 11, checks the sources and candidate, and records concrete evidence and findings. Label every owner-performed role as self-review. Fix accepted issues, rebuild a unique candidate, and repeat the affected reviews before handoff.

An **independent-review panel is a separate, explicitly requested mode**. When Jamie requests it, give each reviewer the same complete source set and frozen candidate. Review agents are read-only; the owner adjudicates and performs any edits between rounds. Follow the requested concurrency limit. Do not add independent reviewer agents to a normal extraction or an efficiency comparison without an explicit request for that mode.

### 2.3 Quality baseline

Jamie's current extraction setting, requested on 2026-09-08, is **GPT-6 Astra at High effort** (`gpt-6-astra`, `high`). Preserve it unless Jamie requests a different setting. GPT-5.6 Sol Extra High remains the historical comparison baseline. The normal workflow is the owner-led review mode in Section 2.2, including all five review scopes, source inspection, finding adjudication, required tests, exact clean reproduction, and primary-agent acceptance gates.

Reducing tool-output volume does not authorize reducing reasoning effort, source coverage, review scope, finding standards, or any acceptance gate. Keep the selected review mode fixed in a workflow comparison. If independent review is requested, compact output does not authorize replacing it with self-review. Do not claim that the two review modes have identical defect detection.

### 2.4 Efficiency measurement

Measure normal extractions from the existing owner and coordinator logs. Record the selected model and review mode, owner start, handoff, finalization, available token usage, and any approval wait. Separate setup and maintenance from extraction timing while retaining them in the total-work account. Do not add reviewer agents, repeat completed checks, or require extra owner reports or phase markers solely to collect a measurement. The normal source, review, test, and reproduction evidence is still required.

### 2.5 Isolated parallel owners

Use at most three owners, one per explicitly assigned record. The primary agent prepares a frozen batch with one to three record IDs:

```powershell
python scripts/coordinate_extraction.py prepare BATCH_ID RECORD_A RECORD_B RECORD_C
```

The tool creates ordinary private copies under `papers (private)/parallel/BATCH_ID/RECORD_ID/`, including the current uncommitted runtime, tests, metadata and that record's archived material. It does not use shared writable links. An active batch reserves the owner slots and prevents duplicate batch assignment. Each owner must set **every command's working directory to its assigned workspace**, use that workspace's scripts, and keep generated evidence in its own private diagnostics/staging directories. The owner marker blocks promotion and finalization inside these copies.

Shared code, schemas, tests, the protocol, archived sources, history and public metadata remain frozen for all owners. Owners may edit their assigned private override and create unique staged candidates. If a general code fix is needed, save the finding and proposed fix in private diagnostics and notify the primary agent. Do not replace a general code fix with an unsupported record override. The primary stops all owners before aborting the batch, applies and tests the shared fix, and prepares a new batch for the unfinished records. Preserve the prior workspaces as evidence. Owners then rebuild with the new code and repeat affected source audits/reviews; a changed fingerprint never inherits an old approval. Reuse unchanged source evidence only under Section 3.6.

Before handoff, stop writes to the reviewed and clean runs. The primary checks and imports each owner's exact bytes:

```powershell
python scripts/coordinate_extraction.py check BATCH_ID RECORD_ID
python scripts/coordinate_extraction.py import BATCH_ID RECORD_ID --run-id REVIEWED_RUN --repro-run-id CLEAN_RUN
```

Import rejects changed shared code, protected owner inputs, primary sources/metadata, an intervening private override change, stale manifests, unequal clean reproduction, and conflicting destination runs. Import only stages the candidate; the primary still performs every acceptance and finalization gate. Existing and owner-created private overrides are preserved and checked against the reviewed snapshots.

The primary finalizes one record at a time in the primary repository, using Section 10. The finalizer holds a repository-wide lock from planning metadata/queue updates through promotion and cleanup. A second coordinator fails promptly rather than overwriting another queue update. A crash leaves an inspectable lock; never remove it until the primary establishes that its holder has stopped.

After every assigned record passes live verification, release the batch slots with `python scripts/coordinate_extraction.py complete BATCH_ID`. If work must restart, first stop every owner and then use `python scripts/coordinate_extraction.py abort BATCH_ID --reason "Specific reason"`; this preserves all workspaces and evidence. The tools enforce filesystem/input boundaries, not OS-level isolation from other folders.

Keep Astra High and the complete five sequential self-reviews, separate adjudication, full tests, exact reproduction and primary acceptance **for each paper**. Measure preparation separately from extraction while retaining total work. Report observed overlap and complete batch duration; do not present summed concurrent owner durations as a measured sequential counterfactual or assume token savings. Count a record toward the requested batch target only after primary finalization and live verification pass.

## 3. Non-negotiable boundaries


### 3.1 Privacy and network access

- Treat everything under `papers (private)/` and `papers (private)/staging/` as private.
- Keep private source-derived content in the repository's private record or staging directories.
- Work offline by default.
- Do not upload source files, extracted text, figures, tables, supplements, or diagnostics to an external service.
- Do not follow or fetch remote links found in publication files merely to complete an extraction.
- Web research may be used only when specifically needed to understand a source anomaly, using authoritative evidence, and must never substitute for the archived publication sources.
- Do not download missing publication files unless Jamie separately authorizes that action. Follow `DOWNLOAD.md` when download work is authorized.

### 3.2 Source immutability

Never modify, rename, recompress, normalize, replace, or delete source material, including:

- `papers (private)/<record-id>/pdf/main.pdf`;
- `papers (private)/<record-id>/html/main.html` and its support files;
- files in `papers (private)/<record-id>/supplementary/`.

If a source is missing, corrupt, incomplete, or inaccessible, document the limitation. Do not manufacture a replacement.

### 3.3 Metadata protection

- Do not edit the public record YAML while extracting or reviewing.
- `jamies_human_only_notes` is human-authored. Never infer, interpret into new tags, add, remove, or change its contents without Jamie's explicit permission for that specific edit.
- Jamie has given standing authorization for the primary agent—not the record owner—to approve a candidate automatically after every gate in Sections 9 and 10 passes. The guarded finalizer then sets `pip_litdb_status` to `extracted_approved` (`Extracted - Approved`).

### 3.4 Existing output protection

- Never overwrite or delete a live `extraction/` during extraction development. An approved revision may replace it only through the guarded finalizer's `--replace` path, which archives the prior approved extraction and diagnostics under `extraction_history/`.
- Never overwrite a prior staged run. Use a new unique run ID for every rebuilt candidate.
- `extraction_old/` exists only as an imperfect historical comparison. It is not an authoritative source and must never be copied forward as extraction content.
- Do not delete old extractions or diagnostics unless Jamie explicitly requests it.
- Staging is temporary. After automatic finalization verifies the approved live copy and public status, remove that record's complete staging directory through the guarded finalization/cleanup path in Section 10.
- Preserve unrelated changes in the working tree.
- Do not commit, push, or open a pull request unless asked.

### 3.5 Prohibited shortcuts

- Do not use `clean_publisher_html.py`.
- Do not treat raw PDF text extraction, OCR output, or publisher HTML as automatically correct.
- Do not infer missing scientific content from general knowledge.
- Do not silently rewrite an author's wording, notation, table organization, or footnotes into a preferred house style.
- Do not call an extraction complete solely because a script finished or a schema validated.

### 3.6 Bounded evidence reads

Use `scripts/inspect_extraction.py` to navigate large HTML, JSON, and text files. It is read-only and bounds the entire response to 12,000 characters by default. It does not summarize, repair, or rewrite scientific content. JSON Pointer reads return the original JSON value, retaining number spelling, precision, escapes, and table structure.

For long contiguous reads, use `--max-output-chars 20000` on both the initial and
continuation commands, including batches of complete evidence views. Keep outlines,
targeted searches, and short checks at the default limit or a smaller suitable
limit. This changes how required content is paged; inspect all returned content
and follow every required continuation.

```powershell
python -X utf8 scripts/inspect_extraction.py outline "papers (private)/staging/NNNNN/RUN/extraction/record.json"
python -X utf8 scripts/inspect_extraction.py outline "papers (private)/staging/NNNNN/RUN/extraction/record.json" --pointer /sections
python -X utf8 scripts/inspect_extraction.py read "papers (private)/staging/NNNNN/RUN/extraction/record.json" --pointer /tables/0 --max-output-chars 20000
python -X utf8 scripts/inspect_extraction.py find "papers (private)/NNNNN/html/main.html" "exact source phrase"
```

- Outlines and search excerpts are navigation aids, not evidence of complete inspection. Read all required content and inspect every source page and asset as required below. Continue with `--start <next_start>` until the needed selection is fully read; never treat an unread remainder as reviewed. For outlines, the offset counts immediate children; for reads and searches, it counts decoded Unicode characters within the selected view.
- Every response includes the source SHA-256. Use `--expect-sha256 <hash>` on continuation reads. A mismatch requires refreshing the view and reassessing affected coverage. `stat <path>` checks a file's identity without printing its content. Avoid reprinting a read only when the same agent has already inspected the same path, hash, selector, and range and still has the exact evidence available. This saves repeated output, not required comparison with a changed candidate. A new selection still requires inspection even if the file hash matches.
- Reads, outlines, and searches also return a `view_token`. On an exact repeat, pass `--if-view-token <token>` only if you personally inspected that view and still retain its exact evidence. A match returns `view_unchanged: true` without content; use the retained response's pagination. Changed bytes, paths, pointers, pages, reader code, or view settings produce a normal fresh view. Never borrow another reviewer's token or use a token after losing the underlying evidence. A token is not a record of review or proof that a diagnostic is current; required checks still depend on current inputs, code, and settings.
- Avoid raw whole-file dumps, arbitrary `[:N]` slices, and line-based searches that print an entire minified HTML line. Use bounded literal searches to find positions, then exact reads with sufficient surrounding context. For ordinary code searches, prefer scoped `rg` output. A search miss never proves source absence; encodings, markup, escapes, and visual content still require appropriate inspection.
- The helper decodes UTF-8 strictly by default; specify `--encoding` when archived evidence establishes another encoding. It preserves decoded line endings. Character positions are not byte offsets. JSON reads preserve quoted strings and escapes; use the raw source and viewer for interpretation, rather than mistaking a JSON escape for authored notation.
- Run tests through `scripts/run_tests.py` with `--log-dir` set to the assigned private diagnostic/staging area (Step 9). It saves complete stdout/stderr and structured outcomes in a new directory for each invocation, then returns counts, bounded failure excerpts, and exact file locators. Keep other source-derived working reports in that private area too. Inspect every failure fully with bounded reads; a short preview is not a complete diagnosis. Existing page renders or inventories may be reused only when their source fingerprints and generating tool/settings still match; every reviewer must perform its own required inspection and judgment.
- `scripts/run_tests.py` and `scripts/inventory_extraction.py` allocate bounded, race-safe, non-overwriting child directories under the assigned private output parent. A persistent permission failure is reported immediately. Use the ordinary approved outside-sandbox retry when the runtime blocks directory creation; do not weaken output privacy or redirect diagnostics into source or public trees.
- These reads do not replace PDF/image inspection, source reconciliation, the viewer, validation, any review role, or the clean reproducibility run. They do not mark coverage complete or approve a candidate. Keep the complete original files available throughout.

For several independent reads, use `batch` to share one response budget. Save a
JSON request array in the assigned private diagnostic area, for example
`batch-reads.json`:

```json
[
  {"command": "read", "path": "papers (private)/staging/NNNNN/RUN/extraction/record.json", "pointer": "/tables/0"},
  {"command": "read", "path": "papers (private)/staging/NNNNN/RUN/extraction_diagnostic/coverage.jsonl"}
]
```

```powershell
python -X utf8 scripts/inspect_extraction.py batch "papers (private)/diagnostics/NNNNN/batch-reads.json" --max-output-chars 20000
python -X utf8 scripts/inspect_extraction.py batch "papers (private)/diagnostics/NNNNN/batch-reads.json" --cursor "<next_cursor>" --max-output-chars 20000
```

The request file accepts 1–100 `stat`, `read`, `outline`, or `find` requests, using
the corresponding options as JSON fields (`encoding`, `expect_sha256`, `pointer`,
`start`, `limit`, `query`, `context`). Paths are relative to the working directory,
as in single-file commands. Set `--max-output-chars` on the batch command; per-item
budgets and `if_view_token` are not accepted. The selected limit covers the entire
serialized batch, including metadata, escapes, and the cursor. The helper still
defaults to 12,000 characters when no limit is supplied; use the explicit
20,000-character setting above for long reads.

Inspect every returned result and resume with its exact `next_cursor` until it is
null. Results retain request indices, original content, source hashes, and exact
ranges. A null cursor completes only the requested views; outlines, searches, and
explicit starting offsets still have their usual coverage limits. The cursor
pins the request file, reader implementation, and every input, including files
whose results have not yet been returned. A change requires a new batch and
reassessment of affected coverage. Full evidence remains in the original files;
the reader creates no files and is not a filesystem lock.

Emit one batch response per tool result. Do not concatenate several full-size
helper responses into an oversized combined result. Save other large command
outputs in the assigned private diagnostics area and read them through the helper.
For a 20,000-character response through `functions.exec`, explicitly allow at
least 10,000 output tokens in both `exec_command.max_output_tokens` and the outer
`functions.exec` output budget. Emit the helper stdout once, for example with
`text(result.output)`, without wrapping the full command result or adding another
large output. This is a starting allowance for wrapper headroom, not a conversion
between characters and tokens; token-dense content may need a smaller batch. If
transport reports truncation, reduce the batch budget and retry from the last
fully inspected cursor. Never count a truncated response or an unread continuation
as completed inspection.

For directory listings, use `scripts/inventory_extraction.py files <path> --save-dir <private-diagnostics>`.
It saves all file paths, sizes, SHA-256 hashes, and directory entries, including
hidden entries, while returning a bounded preview. Keep the save directory outside
the scanned tree. Scope listings to permitted sources, the frozen candidate, or
your own diagnostics; do not inventory other reviewers' reports.

For subsequent checks, add `--previous <snapshot.json> --expect-previous-sha256 <hash>`
using the returned snapshot locator and hash. The tool rehashes file contents,
reports added/removed/modified paths, saves complete changes, and reuses the
snapshot when unchanged. `--contains <literal>` filters only the preview; counts,
snapshots, and change detection still cover the complete scope. Inspect saved
`/entries` or `/changes` with the paged reader, continuing until every needed entry
is inspected. Use idle/frozen inputs; this is not a filesystem lock or a substitute
for the source audit. Links, unreadable entries, mismatched baselines, and detected
changes during hashing fail rather than producing a partial successful inventory.

## 4. Source authority and reconciliation

### 4.1 Default source hierarchy

Use publisher HTML as the default authority for article text, headings, reading order, references, links, and structured tables.

Use the PDF to:

- check completeness;
- detect HTML omissions or large publisher errors;
- verify equations, symbols, subscripts, superscripts, Greek letters, units, and other scientific notation;
- verify figure and table placement, numbering, captions, and panel completeness;
- recover content that HTML does not contain;
- crop figures or tables only when a suitable original asset is unavailable.

Use supplementary files as primary sources for their own content. A supplement is not less authoritative merely because it is separate from the article.

### 4.2 Exceptions

HTML may be overridden only when direct evidence shows an omission or material error. Prefer a stronger archived source for the same publication, normally the PDF or the original supplementary file.

Every nontrivial reconciliation decision must be traceable in `extraction_diagnostic/`, not inserted as distracting provenance in the canonical reader-facing content.

If sources genuinely conflict and the correct reading cannot be established, preserve the uncertainty in diagnostics and report it as unresolved. Never choose silently.

### 4.3 Faithfulness

Normalize structure, not scientific meaning.

Allowed normalization includes consistent field placement, paragraph boundaries, link representation, and stable machine-readable containers. It does not include changing what the authors reported, reinterpreting footnote scope, recalculating values, rewriting equations, or imposing a new table layout.

## 5. Output contract

### 5.1 Canonical extraction

The approved canonical extraction is:

```text
papers (private)/<record-id>/extraction/
  record.json
  <linked binary or unusually large assets>
```

`record.json` is the single machine-readable representation of the complete record. It contains the article and supplementary content, including metadata, body sections, equations, figures, tables, supporting information, links, and asset references.

Binary assets remain separate when embedding them in JSON would be impractical. Every asset reference must resolve relative to the extraction directory, use a stable path, and point to an existing file.

Do not create `record.md`, duplicate prose files, CSV table exports, or per-table JSON files as canonical output.

### 5.2 Diagnostics

Provenance, confidence, source locators, reconciliation notes, coverage records, validation output, review findings, and override evidence belong in the sibling directory:

```text
papers (private)/<record-id>/extraction_diagnostic/
```

For a staged candidate they live under that candidate's staged diagnostic directory. Diagnostics are preserved during promotion but are kept out of the normal reading experience.

### 5.3 Viewer

Use `extraction_viewer.html` for human review. The viewer is a presentation of `record.json`, not a second source of truth.

The user selects `papers (private)/`. The viewer prefers a live extraction. If no live extraction exists, it may show the latest completed staged candidate, which must be clearly labeled **Staged / pending finalization**.

Inspect both the raw JSON and the rendered viewer. A good rendering cannot excuse incorrect JSON, and valid JSON cannot excuse an unreadable or misleading rendering.

## 6. Content requirements

### 6.1 Record completeness

The extraction must represent the entire archived publication, not only the main prose. Include, when present:

- title and bibliographic metadata;
- authors and affiliations;
- abstract and keywords;
- all body sections and subsections in source order;
- lists, quotations, footnotes, endnotes, acknowledgments, disclosures, funding, data availability, and author contributions;
- display equations and scientifically meaningful inline notation;
- every figure, scheme, table, graphical abstract, and caption;
- references;
- supporting-information descriptions;
- the contents and assets of supplementary PDFs, presentations, spreadsheets, documents, data files, images, audio, and video to the extent supported by their formats;
- plain-text forms of external and internal links, with local asset links where appropriate.

Missing content is never filled by guessing. A publisher omission such as an unavailable graphical abstract must be documented as a source limitation in metadata/diagnostics and must not be treated as an extractor failure after it is confirmed.

### 6.2 Text and reading order

- Remove repeated headers, footers, page numbers, navigation chrome, advertising, and other non-content noise.
- Reconstruct paragraphs across visual line and page breaks without joining distinct paragraphs.
- Preserve meaningful lists, headings, quotations, and section order.
- Keep figure and table captions in their structured figure/table records rather than interweaving duplicate captions through body prose.
- Convert links to plain readable text while retaining usable targets where the schema supports them.
- Preserve author wording, spelling, and scientifically meaningful punctuation.
- Avoid silent hyphenation errors, duplicated page-boundary text, lost characters, and OCR artifacts.

### 6.3 Scientific notation and equations

- Preserve Unicode Greek letters when reliably known.
- Preserve superscripts, subscripts, charges, isotopes, units, mathematical operators, and chemical notation with the closest faithful schema-supported representation.
- Keep display equations as stable equation blocks in source order.
- Compare HTML and PDF for every equation and suspicious symbol sequence.
- Never replace a symbol based only on what would be scientifically plausible.
- OCR may be used on document text regions when necessary, but not on scientific figure, scheme, chart, or video pixels as a substitute for the visual asset.

### 6.4 Figures, schemes, and graphical material

- Prefer the highest-quality original publisher HTML asset after verifying it against the PDF.
- When no suitable original asset exists, render or crop from the PDF at sufficient resolution.
- Preserve the complete figure, including every panel, label, legend element, and scale bar.
- Automated validation warns when meaningful pixels touch a PDF crop boundary. Visually adjudicate every such warning: widen a clipped crop, or document acceptance when the authored artwork intentionally reaches the edge.
- Do not crop a multi-panel figure into inferred components unless the archived source itself provides those components and the schema calls for them.
- Do not OCR figure, scheme, chart, or video pixels. Preserve the visual and its authored caption; include authored machine-readable associated text when available.
- Record figure number, label, caption, source order, and local asset path consistently.
- A missing publisher asset must be explicitly diagnosed rather than replaced with a fabricated or unrelated image.

### 6.5 Tables

- Represent table structure inline in `record.json` according to the versioned table schema.
- Reproduce author-defined title, caption, headers, rows, spans, values, units, and footnotes faithfully.
- Do not reinterpret or re-scope a footnote merely to make it seem clearer.
- Do not create CSV files.
- Do not create separate per-table JSON files.
- For an HTML-sourced table, do not add a redundant table image.
- For a PDF- or image-sourced table, retain the table image along with the structured JSON representation.
- Create a table asset subfolder only when graphical cell contents or other required table assets exist.
- Asset paths must resolve relative to the extraction directory.

### 6.6 Supplementary material

- Inventory every file recursively under `supplementary/` before extracting.
- Expect standalone publisher-delivered ZIP supplements to have been safely unpacked during source acquisition. Treat their unpacked members, original archive-relative paths, and decompressed bytes as the authoritative sources; the outer ZIP should not remain.
- Do not unpack or modify supplementary archives during extraction. If an outer publisher ZIP remains, report it as a source-acquisition issue rather than silently treating it as the canonical supplement.
- Copy authoritative supplementary files byte-for-byte when they must remain available as linked assets; preserve their source-relative paths, filenames, and extensions.
- Extract useful textual and structured content into `record.json` so the record can be understood without opening each supplement.
- Preserve large spreadsheets, datasets, media, and other binaries as linked local assets rather than forcing a lossy transcription.
- Do not create CSV substitutes for spreadsheets.
- Do not exclude a file merely because its format is difficult. Unsupported or inaccessible content is a reported limitation and an acceptance issue.

### 6.7 Presentations

For PowerPoint supplementary material:

- parse OOXML non-destructively;
- use native figure captions to identify qualifying figure slides;
- render each qualifying slide with installed Microsoft PowerPoint as a lossless PNG with a 5,000-pixel long edge;
- render every qualifying slide in a multi-slide presentation;
- do not crop a slide into inferred component figures;
- do not use a low-resolution package thumbnail as the normal fallback;
- treat failure to render a required slide as a structural finding that blocks promotion;
- preserve native slide text as machine-readable `figure_text` and retain coordinates in diagnostics;
- do not flatten native slide text into duplicate prose in the viewer;
- preserve original media and reachable embedded workbooks;
- copy embedded XLSX files byte-for-byte and associate them with the relevant table or chart;
- do not OCR figure images or videos.

## 7. Overrides and code fixes

The canonical override for an approved extraction is its immutable, manifest-bound diagnostic snapshot:

```text
papers (private)/<record-id>/extraction_diagnostic/overrides.yaml
```

Do not edit that approved snapshot in place. When an approved record needs a new revision, copy it to the temporary working path:

```text
papers (private)/<record-id>/extraction_overrides.yaml
```

Edit and review only the temporary working copy. While present, it takes precedence for new builds. Guarded post-approval cleanup verifies that it exactly matches the newly approved diagnostic snapshot and then removes it. If the two files differ, cleanup must stop rather than discard unapproved work. A rebuild with no working copy automatically uses the approved diagnostic snapshot.

Supported override areas currently include:

- `text_repairs`;
- `rich_text_overrides` for exact, source-pinned formatting corrections that leave the paired plain-text claim unchanged;
- `pdf_crops`;
- `supplement_exclusions`;
- `front_matter`;
- `reference_entries`;
- `supporting_information_additions`;
- `supplement_block_additions`;
- `supplement_figure_overrides` for hash-pinned semantic classification of an
  already preserved, otherwise uncaptioned supplement visual;
- `source_anomalies`;
- `standalone_image_figures`;
- `docx_figure_crops`;
- `supplement_table_overrides`;
- `table_overrides`;
- `pdf_text`;
- `pdf_ocr`;
- `expected_counts`.

Use an override only for a fact or anomaly specific to that archived record. Every repair or addition must include an exact source path or locator, the reason for the change, and sufficient evidence to verify it.

Use this decision rule:

1. If the same defect could affect another publication of this type, fix the shared extractor and add a regression test.
2. If the source itself has a unique omission, malformed structure, exceptional crop, or record-specific ambiguity, use an evidence-backed override.
3. If the correct result cannot be established from archived evidence, do neither; report the unresolved limitation.

Do not use an override to hide a general parser defect, bypass validation, or insert plausible but unsupported content.

## 8. End-to-end workflow

### Step 1: Accept one explicit assignment

Confirm the exact record ID. Read this file completely, then consult the relevant portions of:

- `EXTRACTION_PLAN.md`;
- `README.md`;
- `DOWNLOAD.md` only if source acquisition was separately authorized;
- current schemas, extractor code, validator code, promotion code, and relevant tests.

Check the working tree and preserve unrelated changes. Establish that no other writing agent owns the record.

For a parallel assignment, confirm the exact isolated workspace and its `.extraction-owner.json` marker. Use that workspace as the working directory for every command; the primary checkout is not the owner's working directory.

### Step 2: Preflight identity and sources

Verify that the private directory matches the assigned public record. Check title, DOI or other stable identifier, filenames, and publication identity.

Confirm the available inputs:

- HTML and support files;
- main PDF;
- every supplementary file;
- any existing private override;
- any `extraction_old/` comparison material, while remembering that it is not a source.

Report an identity mismatch immediately.

### Step 3: Create a machine inventory

The optional runner in `scripts/REVIEW_RUNNER.md` can bundle this inventory with
source preparation, candidate creation, tests and mechanical handoff evidence.
Its hashed source inventory is equivalent for this step. All source inspection,
scientific judgment, five sequential self-reviews, adjudication and primary
acceptance requirements below still apply.

Run:

```powershell
python -X utf8 scripts/inventory_extraction.py sources NNNNN --save-dir "papers (private)/diagnostics/NNNNN/inventories"
```

This preserves the same allowlisted source details as `extract_record.py --inventory-only`
in a complete private snapshot, with a concise console response. Compare subsequent
inventories using the snapshot/hash options in Section 3.6. Check file hashes,
types, sizes, page counts, HTML assets, PDF embedded assets, supplementary members,
and package contents. Use `inventory_extraction.py files` for HTML support-file or
other directory listings. Filesystem/source inventories do not enumerate embedded
HTML images or native package internals; those still require format-specific inspection.

Here, package contents means structural inspection of retained native packages
such as `.docx`, `.xlsx`, and `.pptx`, or intentional nested archives. It does
not authorize retaining or unpacking a publisher-delivered outer ZIP during
extraction.

### Step 4: Perform an exhaustive source assessment

Before trusting the baseline extraction:

- render and inspect every page of the main PDF;
- inspect the publisher HTML structure and all locally archived HTML assets;
- open or structurally inspect every supplementary file;
- enumerate expected figures, schemes, tables, equations, and supporting files independently from the extractor;
- note unusual notation, complex tables, multi-panel figures, source conflicts, and known publisher omissions;
- identify image-only PDF pages and regions that require document-text OCR;
- inspect presentations, embedded workbooks, media, and datasets using format-appropriate tools.

Do not sample only the first pages or obvious assets.

### Step 5: Build a baseline staged candidate

Choose a unique run ID that identifies the record and iteration, for example `agent-<date>-r01`.

Run:

```powershell
python scripts/extract_record.py <record-id> --run-id <unique-run-id>
```

When a private override is needed:

```powershell
python scripts/extract_record.py <record-id> --run-id <unique-run-id> --override "papers (private)/<record-id>/extraction_overrides.yaml"
```

For a revision of an approved record, first copy
`extraction_diagnostic/overrides.yaml` to this temporary working path. Do not
edit the diagnostic snapshot directly. After approval, the working copy is
removed automatically.

Never reuse the run ID of an earlier candidate.

### Step 6: Validate

Run the validator with the exact expected title:

```powershell
python scripts/validate_extraction.py "papers (private)/staging/<record-id>/<unique-run-id>" --expected-title "<exact title>"
```

When independently established expected counts are available, add the applicable flags:

```text
--main-assets
--tables
--supplement-tables
--supplement-figures
--equations
--presentation-embedded-files
```

Do not accept counts generated only from the candidate itself as independent coverage evidence.

Read every validation finding. A known or expected finding is not harmless until it has been checked against the source and explained. Critical, scientific, or unresolved structural findings cannot be silently accepted.

### Step 7: Conduct the record owner's source audit

Compare the candidate with the sources from beginning to end:

- title, authors, affiliations, abstract, and metadata;
- every section and paragraph in order;
- equations and scientific notation;
- figures, schemes, captions, panels, and asset quality;
- tables, cell structure, values, and footnotes;
- references and links;
- every supplementary item and its extracted or linked content;
- all local asset paths and file identities.

Use both direct JSON inspection and the local viewer. Follow links. Render images at readable scale. Open spreadsheets and media with suitable local tools. Compare representative extracted text programmatically, but do not rely on string comparison alone.

### Step 8: Triage every issue by cause

For each issue, decide whether it is:

- a general extractor defect;
- a record-specific source anomaly;
- a viewer-only presentation defect;
- an unsupported-format limitation;
- a genuine source conflict;
- an old-comparison discrepancy with no source basis.

Fix the underlying cause. Do not patch generated `record.json` by hand.

For shared code fixes, add focused regression tests. For record-specific corrections, add evidence to the private override and diagnostics. For source limitations, describe the missing material accurately without fabricating it.

### Step 9: Test the software

Run focused tests while iterating, then run the full suite before freezing the candidate:

```powershell
python -X utf8 scripts/run_tests.py --log-dir "papers (private)/staging/NNNNN/RUN/extraction_diagnostic/test-logs"
```

The default runs the full `unittest discover -s tests -v` suite. For focused work,
append unittest arguments after `--`, for example `-- discover -s tests -p test_record_json.py`.
Use a working diagnostic directory before the first staged run exists. The runner
creates a unique child directory every time and retains `unittest.log` and
`report.json`; never overwrite earlier test evidence or dump a complete log into
the conversation. Do not use unittest's `--buffer`/`-b`, which discards output from
successful tests.

The runner checks imports for the selected suite before executing any tests. A
`discovery_failed` result means zero tests ran; inspect all saved import errors and
fix the environment or code before retrying. For missing packages, use the reported
Python executable to install `requirements.txt`, which includes ReportLab for PDF
test fixtures. An existing Windows Codex ReportLab bundle is used as a fallback
when that package is missing, with installed packages retaining precedence. The
report records the interpreter and added package paths; no automatic installation
occurs. Failed discovery does not satisfy the test gate.

The short response includes outcome counts, the full log's SHA-256, up to three
failure/error excerpts, and the number of additional issues. Read every issue in
`report.json` using `scripts/inspect_extraction.py` (outline `/issues`, then read
each `/issues/N`, continuing every page). Inspect the full log for additional
context and investigate unexpected skips. Subtest and fixture failures are outcome
events, so their counts need not add up to the number of test methods run.
A missing result, an interrupted run, a crash, or an empty test selection returns
a failing exit status; none satisfies the full-suite gate. Keep the report and log
paths with the frozen candidate's test evidence.

Investigate failures. Do not modify unrelated behavior simply to make a test green.
This changes test reporting only; all tests and scientific acceptance gates still apply.

### Step 10: Rebuild from scratch

After any code, schema, or override change, create another unique staged run. Do not manually carry files from the prior candidate unless the extractor itself reproducibly does so.

Re-run validation and the source audit. Continue Steps 8–10 until the record owner finds no unresolved issue.

### Step 11: Freeze the candidate and complete the five review roles

Once the record owner believes the candidate is complete:

- stop editing the candidate and extraction code;
- identify the exact run ID, source fingerprints, override fingerprint, pipeline fingerprint, and test result;
- perform each role below as a separate sequential self-review of the same frozen candidate and complete source set;
- record concrete evidence and findings in a separate report for each role, then adjudicate them in Step 12.

All five roles are required:

1. **Text and reading order** — omissions, duplication, paragraph boundaries, headings, lists, references, navigation noise, and source order.
2. **Scientific notation, equations, and tables** — symbols, Greek letters, subscripts, superscripts, units, equations, values, spans, headers, and footnotes.
3. **Figures, schemes, and supplements** — asset identity and quality, missing panels, caption pairing, supplementary coverage, presentations, embedded files, and media.
4. **AI-readiness and consistency** — schema consistency, stable identifiers, path resolution, duplication, machine usability, and viewer clarity.
5. **Adversarial completeness** — search specifically for omissions, unsupported corrections, hidden source conflicts, false confidence, and content that passed automated checks accidentally.

Name the stored role reports so the finalizer can identify them: each filename must contain, respectively, `text` and `reading`; `scientific`, `notation`, or `equation`; `figure`, `scheme`, or `supplement`; `ai` and `readiness` (or `machine_readiness`); and `adversarial` or `completeness`. Name the separate adjudication file with `adjudication` or `adjudicate` in its filename.

Label owner-performed roles as self-review; never represent them as independent. Keep the reports concise and reference saved source, viewer, validation, and test evidence instead of copying complete inventories or logs into every report.

Only for an explicitly requested independent-review mode, assign these same five roles to read-only reviewers with identical access to the complete sources and frozen candidate. Do not substitute the owner's conclusions for their source inspection.

### Step 12: Record and adjudicate findings

Each finding should contain:

- a stable finding ID;
- reviewer role;
- severity;
- exact candidate path, field, block, or asset;
- exact source path and locator;
- concise description;
- evidence;
- recommended disposition.

Use these human-review severities:

- **Critical** — wrong record, missing major source, corrupt output, privacy leak, or unusable canonical extraction;
- **Scientific** — meaning-changing textual, numerical, symbolic, equation, table, figure, or caption error;
- **Localized** — bounded omission, ordering, association, or structural defect that does not change the rest of the record;
- **Cosmetic** — presentation defect with no content or machine-readiness impact.

Store review reports under the staged candidate's `extraction_diagnostic/reviews/` area when practical. Keep a separate adjudication record stating, for every finding: accepted, rejected with evidence, accepted source limitation, or fixed in a named later run.

The record owner adjudicates; it does not automatically obey a reviewer. Conflicting findings are resolved against archived evidence.

If any accepted issue requires a change, unfreeze the work, fix it, test it, build a new unique run, and repeat the affected reviews. A changed candidate is not the same reviewed candidate.

### Step 13: Final reproducibility audit

Before handoff, confirm all of the following:

- the staged manifest matches the staged extraction one-to-one;
- every path in `record.json` resolves and stays within the candidate extraction directory;
- every expected article and supplementary component is represented exactly once unless duplication is intentional and documented;
- figure, table, equation, and supplement counts agree with independent source inspection;
- original linked supplements retain their filenames, extensions, and bytes;
- no source or private material escaped the private/staging scope;
- source, override, schema, pipeline, and candidate fingerprints are current;
- the full test suite passes;
- a distinct clean rerun from identical inputs produces byte-for-byte identical canonical extraction files and matching manifest content apart from run identity;
- no Critical or Scientific finding remains unresolved;
- every accepted structural limitation is explicit and evidence-backed;
- all required reviewer roles and adjudication are complete.

### Step 14: Hand off the staged candidate for automatic finalization

Report to the primary agent:

- record ID and exact title;
- staged run ID and path;
- sources used and any missing source material;
- major repairs or extractor changes;
- tests and validation performed;
- review rounds and their disposition;
- remaining source limitations;
- exact validation findings and their adjudication, if any;
- confirmation that the candidate is still staged and unpromoted.

Do not describe the result as perfect. State what was checked and whether any known limitation remains.

## 9. Acceptance gates

A candidate is ready for standing-policy finalization only when:

- publication identity is verified;
- every archived source was inventoried and inspected;
- required content and asset coverage is complete;
- all links and local paths resolve;
- tables and scientific notation have been source-checked;
- required presentation slides and other visual assets rendered successfully;
- validation findings were individually adjudicated;
- focused and full tests pass;
- a clean reproducibility run succeeds;
- the five review roles are complete;
- there are no unresolved Critical or Scientific findings;
- known source limitations are clear and truthful;
- the exact candidate under review has not changed since review.

The record owner cannot approve its own extraction. Jamie's standing policy authorizes the primary agent to approve automatically only after all gates pass. If a gate fails or a new finding remains, the primary agent must stop, leave the candidate staged, and report the blocker instead of asking for routine approval.

## 10. Automatic standing-policy finalization

Finalization is a separate primary-agent action. It must use the exact frozen reviewed run plus a distinct clean run that reproduces the canonical extraction exactly:

```powershell
python scripts/finalize_extraction.py <record-id> --run-id <reviewed-run-id> --repro-run-id <clean-reproducibility-run-id>
```

The standing policy does not permit arbitrary finding acceptance. The only automatically acceptable validation finding is `diagnostic_warning_automated_pdf_html_alignment_not_implemented`, and only when the source alignment was manually reconciled and that exact code is documented in the candidate's review adjudication. Any other current finding blocks finalization until it is fixed or Jamie gives specific instructions for that exceptional case.

The finalizer verifies current source and override fingerprints, manifest and pipeline hashes, all five review-role files, adjudication, exact canonical equivalence with the clean reproducibility run, and live-output safety before promotion. It records `approved_by: primary_agent_under_standing_policy`, the approval policy and version, and the finalization evidence in `approval.json`; it must never claim a fresh user approval.

After successful promotion:

1. verify the live `extraction/record.json` and assets;
2. verify the live `extraction_diagnostic/` snapshot;
3. open the live record in the viewer;
4. confirm that the finalizer set public `pip_litdb_status` to `extracted_approved` and changed only that controlled metadata field;
5. confirm that `jamies_human_only_notes` was not changed;
6. confirm that the queue entry says `Extraction completed and approved.`;
7. confirm that `papers (private)/staging/<record-id>/` and any matching temporary `extraction_overrides.yaml` were removed, while the live extraction and canonical diagnostic override remain intact;
8. report exactly what was finalized, what metadata changed, and which staged runs were removed. Routine user approval is no longer requested.

Use the repeatable coordinator completion check for the mechanical parts of this
list, with the saved full-suite report that covers the final code:

```powershell
python scripts/check_extraction_completion.py <record-id> --test-report "papers (private)/diagnostics/<full-suite-run>/report.json" --log-dir "papers (private)/diagnostics/<assigned-area>"
```

The command reuses guarded live validation with deletion disabled, indexes the
five review roles and adjudication against the approval's evidence, checks the
recorded clean-rebuild identity and test-log bytes, verifies the queue, and runs
the actual viewer with only the canonical extraction files mounted read-only.
The browser checks decoded images, raw JSON, schema/asset errors, the lightbox,
return to the library, and every local download against its canonical bytes.
It uses installed Edge on Windows with a dedicated download directory and
explicit download acceptance. Temporary browser profiles and artifacts also stay
in that run's private directory, avoiding system-temp cleanup retries. Complete
reports, full browser output, and separate media/caption screenshots remain in
a new private output directory;
the console returns a compact result and exact locators.

`machine_checks_passed` is not completion of visual or scientific review. Inspect
the saved screenshots at readable scale, retain every source-inspection and
review/adjudication obligation, and confirm test evidence is current. The check
verifies existing approval and rebuild evidence; it does not perform a new clean
rebuild, approve a record, or replace the primary agent's judgment. Its before/
after hashes cover changes during this read-only check; retain the finalizer's
original controlled-metadata and staging-cleanup evidence as well. Reuse the
saved check results only while their inputs and generating code remain current.
For a failed check, inspect the complete report/log at the returned path before
retrying the affected step.

For an approved revision, build and review new staged and reproducibility runs from the current approved metadata, then add `--replace`. The finalizer requires an existing valid approval, keeps the public status approved, archives the prior live `extraction/` and `extraction_diagnostic/` under the record's private `extraction_history/`, and records the replaced run and archive path in the new approval.

`scripts/promote_extraction.py` remains only as a legacy/manual path for a case where Jamie gives explicit record-specific approval and finding instructions. It is not the normal future workflow.

The finalizer runs the guarded cleanup check before deletion. The cleanup code
accepts both legacy `approved_by: user` records and correctly formed
standing-policy approvals. It refuses to delete anything unless the public
status is `extracted_approved`, the live approval and diagnostics are internally
consistent, current archived sources still match the approved fingerprint,
validation reproduces the accepted findings, and the approved staged extraction
matches the live extraction. It never removes another record's staging directory.

## 11. Standard assignment for a record-owning agent

The primary agent should give an extraction agent an assignment equivalent to:

> You own extraction of record `<record-id>` for this task. Read `EXTRACTION_PROTOCOL.md` completely and follow it. Use GPT-6 Astra High and the normal owner-led review mode. Inspect every archived source, create only uniquely named staged runs, and use the full extract–validate–source-audit–fix–test–rebuild loop. You may fix shared extraction code with regression tests and may edit this record's private override only when supported by exact source evidence. Preserve unrelated work and never modify source files, public metadata, `jamies_human_only_notes`, live `extraction/`, or prior staged runs. You are the sole writing extraction agent. Freeze the candidate and code, perform all five sequential self-review roles, label them accurately, and record a separate adjudication. Do not spawn reviewer agents for this mode. Do not promote or mark the record approved. Hand back the exact reviewed run, clean reproducibility run, source and viewer evidence, test results, five review reports, adjudication, and all remaining limitations for primary-agent finalization.

Use the bounded evidence-read workflow in Section 3.6. Reuse unchanged evidence and keep tool output and the final handoff concise while preserving complete saved evidence. The primary agent collects measurement data from existing logs under Section 2.4.

For an isolated parallel assignment, add the exact workspace path and batch ID. State explicitly that the owner may write only its assigned private override and private diagnostics/staging, must use the copied scripts from that workspace, and must report shared code fixes to the primary under Section 2.5. All review and evidence requirements in the standard assignment still apply.

If Jamie explicitly requests an independent-review panel, state that mode and its concurrency limit in the assignment and apply Sections 2.2 and 8 Step 11 accordingly. Preserve the selected model/effort setting and all acceptance gates in either mode.

## 12. Definition of done for the record owner

The record owner's work is done when one exact, reproducible staged candidate has passed the protocol, all five review roles and their findings have been adjudicated under the selected review mode, no known fixable issue remains, and the reviewed candidate and clean reproducibility run have been handed to the primary agent without promotion or metadata changes. The primary agent then finalizes it under the standing policy or reports a blocking gate.
