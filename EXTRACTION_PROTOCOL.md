# PIP LitDB Extraction Protocol

## 1. Purpose

This is the operating protocol for producing one complete, AI-ready extraction of one private PIP LitDB record.

It is written for a **record-owning extraction agent**. That agent is expected to use tools, inspect the publication visually and structurally, run the extraction software, diagnose problems, improve the software or an evidence-backed record override, rebuild the candidate, and repeat review until the staged extraction is ready for Jamie to assess.

This is not a one-shot conversion task. Passing an automated validator is necessary but is not enough. The record owner must compare the extraction with the actual sources and exercise scientific and editorial judgment.

Detailed design decisions remain in `EXTRACTION_PLAN.md`. Schemas and executable behavior remain in the extraction code and schema files. If this protocol, the plan, and the current code materially disagree, stop and report the conflict rather than silently choosing one.

## 2. Agent model

### 2.1 One record owner at a time

Only one extraction agent may own and write to an active record at a time. Do not assign several records to several writing agents concurrently.

The record owner may:

- inspect all source material for its assigned record;
- create new, uniquely named staged runs;
- edit shared extraction code when it finds a general defect;
- add or edit tests for that defect;
- edit the assigned record's private `extraction_overrides.yaml` when the correction is genuinely record-specific and fully supported by source evidence;
- run validators, tests, renderers, and the local extraction viewer;
- convene or request read-only reviewers after freezing a candidate;
- adjudicate review findings, make corrections, and rebuild the candidate.

The record owner must not:

- work on an unassigned record;
- promote its own candidate;
- mark its own work approved;
- update public record metadata before explicit human approval;
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

Independent review is performed only after a candidate and its code are frozen. Review agents are read-only: they inspect and report findings but do not edit files. They may be run concurrently because they cannot collide with the record owner. If Jamie requests strict single-subagent execution, run the reviewer roles sequentially instead. The record owner then adjudicates the findings and performs any edits before a new review round.

This gives one agent full ownership of the extraction while preserving independent review.

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
- After Jamie explicitly approves a promoted extraction, the primary agent—not the record owner acting autonomously—may separately set `pip_litdb_status` to `extracted_approved` (`Extracted - Approved`).

### 3.4 Existing output protection

- Never overwrite or delete a live `extraction/` during extraction development.
- Never overwrite a prior staged run. Use a new unique run ID for every rebuilt candidate.
- `extraction_old/` exists only as an imperfect historical comparison. It is not an authoritative source and must never be copied forward as extraction content.
- Do not delete old extractions or diagnostics unless Jamie explicitly requests it.
- Staging is temporary. After Jamie approves a record and the approved live copy and public status are verified, remove that record's complete staging directory with the guarded cleanup command in Section 10.
- Preserve unrelated changes in the working tree.
- Do not commit, push, or open a pull request unless asked.

### 3.5 Prohibited shortcuts

- Do not use `clean_publisher_html.py`.
- Do not treat raw PDF text extraction, OCR output, or publisher HTML as automatically correct.
- Do not infer missing scientific content from general knowledge.
- Do not silently rewrite an author's wording, notation, table organization, or footnotes into a preferred house style.
- Do not call an extraction complete solely because a script finished or a schema validated.

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

The user selects `papers (private)/`. The viewer prefers a live extraction. If no live extraction exists, it may show the latest completed staged candidate, which must be clearly labeled **Staged / unapproved**.

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

The private record-specific override file is:

```text
papers (private)/<record-id>/extraction_overrides.yaml
```

Supported override areas currently include:

- `text_repairs`;
- `pdf_crops`;
- `supplement_exclusions`;
- `front_matter`;
- `supporting_information_additions`;
- `source_anomalies`;
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

Run:

```powershell
python scripts/extract_record.py <record-id> --inventory-only
```

Record the inventory in diagnostics. Check file hashes, types, sizes, page counts, HTML assets, PDF embedded assets, supplementary members, and package contents.

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
python -X utf8 -m unittest discover -s tests
```

Investigate failures. Do not modify unrelated behavior simply to make a test green.

### Step 10: Rebuild from scratch

After any code, schema, or override change, create another unique staged run. Do not manually carry files from the prior candidate unless the extractor itself reproducibly does so.

Re-run validation and the source audit. Continue Steps 8–10 until the record owner finds no unresolved issue.

### Step 11: Freeze the candidate and run the review panel

Once the record owner believes the candidate is complete:

- stop editing the candidate and extraction code;
- identify the exact run ID, source fingerprints, override fingerprint, pipeline fingerprint, and test result;
- give each reviewer the same frozen candidate and source set;
- instruct reviewers to be read-only and to report concrete evidence, not edit files.

Use five independent roles:

1. **Text and reading order** — omissions, duplication, paragraph boundaries, headings, lists, references, navigation noise, and source order.
2. **Scientific notation, equations, and tables** — symbols, Greek letters, subscripts, superscripts, units, equations, values, spans, headers, and footnotes.
3. **Figures, schemes, and supplements** — asset identity and quality, missing panels, caption pairing, supplementary coverage, presentations, embedded files, and media.
4. **AI-readiness and consistency** — schema consistency, stable identifiers, path resolution, duplication, machine usability, and viewer clarity.
5. **Adversarial completeness** — search specifically for omissions, unsupported corrections, hidden source conflicts, false confidence, and content that passed automated checks accidentally.

If a role is run by the record owner rather than an independent reviewer, label it as self-review. Do not represent it as independent.

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
- a clean rerun from identical inputs produces equivalent canonical content;
- no Critical or Scientific finding remains unresolved;
- every accepted structural limitation is explicit and evidence-backed;
- all required reviewer roles and adjudication are complete.

### Step 14: Hand off the staged candidate

Report to the primary agent and Jamie:

- record ID and exact title;
- staged run ID and path;
- sources used and any missing source material;
- major repairs or extractor changes;
- tests and validation performed;
- review rounds and their disposition;
- remaining source limitations;
- exact findings that promotion would need to accept, if any;
- confirmation that the candidate is still unapproved and unpromoted.

Do not describe the result as perfect. State what was checked and whether any known limitation remains.

## 9. Acceptance gates

A candidate is ready to be shown for approval only when:

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

Only Jamie can approve the extraction.

## 10. Promotion after explicit approval

Promotion is a separate, explicitly authorized action. The primary agent should promote only the exact reviewed run:

```powershell
python scripts/promote_extraction.py <record-id> --run-id <reviewed-run-id> --accept-finding <finding-code>
```

Repeat `--accept-finding` only for the exact current findings Jamie has knowingly accepted. Do not use broad or stale acceptance codes.

The promotion tool must verify current source and override fingerprints, manifest and pipeline hashes, and the absence of a live-output collision before atomically copying the reviewed candidate.

After successful promotion:

1. verify the live `extraction/record.json` and assets;
2. verify the live `extraction_diagnostic/` snapshot;
3. open the live record in the viewer;
4. only with Jamie's explicit approval, set public `pip_litdb_status` to `extracted_approved`;
5. do not change `jamies_human_only_notes`;
6. remove the record's staging history with:

   ```powershell
   python scripts/cleanup_approved_staging.py <record-id> --check-only
   python scripts/cleanup_approved_staging.py <record-id>
   ```

7. verify that only `papers (private)/staging/<record-id>/` was removed and that the live extraction and diagnostics remain intact;
8. report exactly what was promoted, what metadata changed, and which staged runs were removed.

The cleanup command is deliberately separate from promotion. It refuses to
delete anything unless the public status is `extracted_approved`, the live
approval and diagnostics are internally consistent, current archived sources
still match the approved fingerprint, validation reproduces the accepted
findings, and the approved staged extraction matches the live extraction. It
never removes another record's staging directory.

## 11. Standard assignment for a record-owning agent

The primary agent should give an extraction agent an assignment equivalent to:

> You own extraction of record `<record-id>` for this task. Read `EXTRACTION_PROTOCOL.md` completely and follow it. Inspect every archived source, create only uniquely named staged runs, and use the full extract–validate–source-audit–fix–test–rebuild loop. You may fix shared extraction code with regression tests and may edit this record's private override only when supported by exact source evidence. Preserve unrelated work and never modify source files, public metadata, `jamies_human_only_notes`, live `extraction/`, or prior staged runs. You are the sole writing extraction agent. Freeze a candidate before arranging read-only review. Do not promote or mark the record approved. Hand back the exact staged run, evidence, test results, review adjudication, and all remaining limitations.

## 12. Definition of done for the record owner

The record owner's work is done when one exact, reproducible staged candidate has passed the protocol, its independent findings have been adjudicated, no known fixable issue remains, and it has been handed to Jamie for approval without promotion or metadata changes.
