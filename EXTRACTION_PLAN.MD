# PIP LitDB Extraction System Plan

## 1. Goal

Build an automated, per-record extraction system that turns the available PDF,
publisher HTML, and supplementary files into one clean, consistent, AI-ready
package.

The polished extraction should contain only useful publication content.
Diagnostic information—sources, confidence, OCR details, discrepancies, and
reviewer findings—will live separately in a sibling `extraction_diagnostic/`
directory.

The canonical deliverable is one content-only `record.json` per record. A
single shared `extraction_viewer.html` at the repository root renders any such
record for human review; it is a viewer of the canonical data, not a second
content format.

The system should maximize fidelity while acknowledging that occasional
mistakes are possible. Suspected problems will be reported without cluttering
or altering the extracted article.

Records `00075`, `00390`, `00559`, and `00614` are approved golden pilots in the
canonical JSON and shared-viewer format. Their superseded approved Markdown
packages are not canonical and may be removed with the rest of each approved
record's staging history. All future extraction runs use `record.json`; the
pipeline does not produce a second canonical content format.

## 2. Final Directory Contract

```text
papers (private)/00559/
├── pdf/                         # Main PDF source only; never modified
├── html/                        # Main HTML source only; never modified
├── supplementary/              # Direct publisher files and unpacked ZIP members
│   ├── <original-filename.ext>  # Original names, paths, and extensions are preserved
│   └── ...
├── extraction_old/              # Legacy comparison baseline; never a source
├── extraction/                  # Polished deliverable
│   ├── record.json               # Complete canonical content and asset registry
│   ├── figures/
│   │   └── main/
│   │       ├── figure_001.png
│   │       ├── scheme_001.png
│   │       └── ...
│   ├── tables/
│   │   └── main/
│   │       ├── table_001.png      # Only for an image/PDF-sourced table
│   │       └── table_001_cells/   # Only when graphical table cells need assets
│   │           └── cell_001.png
│   ├── equations/
│   │   └── main/
│   │       └── equation_001.png     # Only when a source image is useful
│   └── supplementary/
│       └── supplement_001/
│           ├── <original-filename.ext>
│           ├── figures/
│           └── tables/                  # Image/cell assets only
└── extraction_diagnostic/       # Technical material, out of the way
    ├── manifest.json
    ├── sources.json
    ├── blocks.jsonl
    ├── coverage.jsonl
    ├── reconciliation.jsonl
    ├── repairs.jsonl
    ├── source_anomalies.jsonl
    ├── warnings.jsonl
    ├── confidence.json
    ├── quality.json
    ├── overrides.yaml                 # Canonical approved override snapshot
    └── reviews/
```

The exact contents will vary by record. Empty asset categories do not require
placeholder files or directories.

The repository root also contains one shared `extraction_viewer.html`. It is
not copied into individual extraction directories. Open it and choose the
`papers (private)/` directory. The viewer shallow-scans only the selected
directory's immediate five-digit record directories, accepting a record only
when `<record-id>/extraction/record.json` exists. It must not recurse into
source, staging, legacy, or diagnostic directories during discovery.

The resulting library lists the available records in numeric ID order and
supports searching, opening a record, returning to the list, refreshing the
scan, and changing the selected private directory. The existing formatted and
raw JSON views render the chosen record's canonical content and linked local
assets. Returning from a record preserves the library context for convenient
review of multiple records.

Choosing `papers (private)/` is the sole loading workflow. The viewer does not
accept an individual `extraction/` folder, standalone `record.json`, or
URL/query parameter. It operates locally, offline, and read-only. The selected
directory handle and library state are retained only in memory for the current
browser session; the viewer does not persist private-directory access.

### Legacy Extraction Baselines

The pre-existing extraction directories have been renamed from
`extraction/` to `extraction_old/`. These legacy outputs are known to be
imperfect and are not authoritative publication sources. For now, they provide
useful comparison baselines for judging whether the new extraction system is
making progress and for identifying regressions or content that the new system
may have missed.

The pipeline must exclude `extraction_old/` from source discovery, source
fingerprints, reconciliation inputs, and new extraction output. A legacy
extraction may be consulted only as a separate comparison during review. It
must never be silently copied into or merged with a new extraction.

After a record's replacement extraction has been approved and the legacy copy
is no longer useful for comparison, its `extraction_old/` directory may
eventually be deleted. Deletion will be a separate, explicitly authorized
cleanup operation performed only after final verification; the extraction
pipeline must never delete legacy directories automatically.

## 3. `record.json` Contract

`record.json` is the single canonical content extraction for the record. It
contains the complete main publication and all extracted supplementary content
in a deterministic, versioned structure. The top-level record identity appears
near the beginning of the file, followed by front matter, article sections,
figures, tables, supporting information, supplements, references, and the asset
registry.

The file is content-only. It must not contain source paths, checksums,
confidence scores, OCR details, repair records, source geometry, review
findings, or other provenance. Those belong only in the sibling
`extraction_diagnostic/` directory.

Textual blocks store both faithful plain text and deterministic safe HTML so
machines can consume simple text while the shared viewer can preserve semantic
formatting. This duplication is part of the one canonical JSON representation;
it is not a parallel article format.

The canonical content includes:

1. Exact title, authors, journal, year, citation, DOI, and PIP LitDB record ID.
2. Abstract and other front matter.
3. Article sections in reading order.
4. Acknowledgments, disclosures, data availability, and similar sections.
5. References.
6. Complete structured tables, including titles, cells, spans, footnotes, and
   graphical-cell references.
7. Consolidated figure and scheme captions with asset references.
8. Full supplementary content, with one clearly identified entry per source,
   including extracted prose, headings, lists, footnotes, equations, tables,
   references, captions, and linked assets in its own reading order.

### Formatting Rules

- Keep record identity and title at the beginning of the canonical JSON; do not
  place pipeline or diagnostic metadata there.
- Remove page numbers, running headers, footers, watermarks, and publisher
  navigation.
- Do not insert artificial page-break markers.
- Join soft line wraps while preserving real paragraphs and lists.
- Remove captions from the narrative and collect them in their designated
  section.
- Keep in-text mentions such as “Figure 2” as plain text.
- Register external assets with stable IDs and portable relative paths rather
  than embedding binary data in JSON.
- Preserve URLs appearing in the publication as readable text.
- Keep extraction warnings and confidence scores out of article content.
- Never summarize, paraphrase, or stylistically rewrite the authors' language.
- Include all extracted textual and structured publication content in
  `record.json`. A reader or machine must not need to open a second text file to
  obtain supplement text, table values, equation bodies, captions, footnotes,
  or references.
- Keep separate files only when the content is inherently binary or large, or
  is a useful fidelity-preserving asset, such as an image, original supplement,
  dataset, workbook, video, source rendering, or equation image. Register every
  such file in `record.json` and reference it from the relevant content item.
- Resolve every asset path from the directory containing `record.json`, denoted
  by `asset_path_base: "extraction_root"`. Paths must be local, portable,
  relative POSIX paths and must not escape the extraction directory.
- Store tables inline in `record.json`. New-format extractions do not create
  per-table JSON or CSV files.

Historical approved extractions may still contain `record.md`. The validator
retains legacy Markdown support so those records remain reviewable, but
`record.md` is not produced by the new pipeline and is not an alternative
canonical format for new extractions.

For a new-format run, `extraction_diagnostic/manifest.json` declares
`record.json` as `application/vnd.pip-litdb.record+json` with schema version
`1.1`. The declaration selects the canonical JSON contract without putting
diagnostic data into the content file.

## 4. Source-Discovery Stage

The script first inventories everything belonging to the record:

- Public YAML metadata.
- `pdf/main.pdf` when present.
- `html/main.html`, including recognized legacy main-HTML locations.
- Every file under the record's `supplementary/` directory, regardless of
  extension.
- File type, size, checksum, and basic parseability.

The record's `supplementary/` directory is the authoritative location for
directly downloaded supplementary files and safely unpacked publisher-ZIP
members. Original filenames, archive-relative paths, extensions, and file
bytes are preserved. A publisher-delivered outer ZIP is a temporary acquisition
container and should not remain in the authoritative source directory after
successful extraction. Discovery cannot assume supplements are PDFs; the
collection includes Word documents, spreadsheets, images, presentations,
videos, molecular data, unpacked archive members, and other formats.

Source discovery must exclude `extraction/`, `extraction_old/`,
`extraction_diagnostic/`, and all staging, cache, backup, and temporary
directories. Generated or legacy material must never be classified as a
publication source.

Source files remain untouched after acquisition and any required publisher-ZIP
unpacking are complete. The extraction pipeline must not unpack or otherwise
modify the authoritative source directory.

### Privacy and Staging Boundary

All temporary source-derived material must stay under the fixed private staging
root:

```text
papers (private)/staging/<record-id>/<run-id>/
```

This includes extraction candidates, page renderings, OCR intermediates, caches,
diagnostic logs containing publication content, and temporary review material.
Before writing, the script must resolve and verify every path so that symlinks,
reparse points, relative paths, or path traversal cannot escape
`papers (private)/staging/`. Source discovery must always exclude the staging
root.

Staging is temporary rather than permanent extraction history. After the exact
candidate passes all protocol gates, the primary agent uses Jamie's standing
policy to finalize it automatically, verify the live extraction and diagnostics,
set the public status to `extracted_approved`, and run guarded cleanup for that
record. The cleanup must independently
verify the live approval, manifest, source fingerprint, validation findings,
approved status, and equality of the approved staged and live extraction
before deleting `papers (private)/staging/<record-id>/`. It must preserve the
staging root and every other record's staging directory. A failed promotion or
status update must leave staging untouched.

Extraction operates offline by default. It must not follow or fetch remote URLs
found in HTML, PDFs, or document relationships. Public tests use synthetic
fixtures rather than text or images copied from private papers. Private source
or derived content must not be sent to an unrelated third-party service without
explicit user approval. The planned Codex review panel may inspect the private
files within the current authorized workspace.

## 5. Extraction Stage

The pipeline produces independent candidates before reconciling them.

### Publisher HTML

Use semantic HTML for:

- Section hierarchy.
- Paragraph and list boundaries.
- References and citation links.
- Semantic tables.
- Superscripts and subscripts.
- MathML or equation metadata.
- Original-resolution figure assets.
- Supplement links and labels.

The extraction pipeline must not use `scripts/clean_publisher_html.py` or any
other destructive pre-cleaning step. It reads the preserved source HTML
directly and performs non-destructive parsing in memory or on a private staging
copy. Elements that are excluded from `record.json` remain available for source
comparison and must be accounted for by the coverage ledger.

### Native PDF

Use the PDF for:

- Confirming that the HTML contains the complete visible publication.
- Visible scientific notation.
- Footnotes.
- Reading order.
- Page-spanning text.
- Figure, table, and equation boundaries.
- Material absent from the HTML.
- Page-level visual verification.

### Rendered PDF and OCR

Render every PDF page and compare its visible document content with the native
text and layout extraction. Readable-looking prose is not sufficient evidence
that the text layer preserved scientific content correctly. Pay particular
attention to equations, textual tables, captions, multi-column regions, Greek
letters, minus signs, multiplication signs, primes, superscripts, subscripts,
units, and numerical values.

Activate page- or region-level OCR when:

- A page is image-only.
- The text layer is absent or corrupt.
- Important regions, such as equations or labels, cannot be recovered
  reliably.
- Native extraction produces clearly implausible reading order.
- Visual comparison suggests a missing, substituted, or flattened scientific
  symbol, value, index, sign, or exponent even when the surrounding prose looks
  plausible.

OCR should operate at page or region level rather than replacing good native
text throughout the document. Native extraction, OCR, HTML, and the rendered
page should be compared when resolving a science-sensitive region. Any
meaning-bearing symbol or value that remains uncertain is recorded as
`needs_review` and blocks promotion.

OCR is limited to document-text regions and scanned textual tables or
equations. It must not be run on the pixels inside scientific figures, schemes,
or extracted figure assets, and it must not be run on video frames. Native
captions, alt text, accessibility descriptions, and slide text may still be
preserved as text.

### Public Record Metadata

The YAML record supplies the expected title, authors, DOI, year, and journal.
It helps with identity checking, but visible publication content remains
authoritative when formatting differs.

## 6. PDF/HTML Reconciliation

Publisher HTML is the default authoritative content source when it passes
identity and full-text completeness checks. The PDF is primarily an independent
completeness and error check, as well as a recovery source when HTML content is
missing or clearly defective.

- HTML supplies the default wording, document structure, links, semantic
  tables, machine-readable equations, and original image files.
- The PDF verifies visible content, reading order, typography, footnotes,
  figure boundaries, and whether the HTML omitted or corrupted anything.
- PDF content replaces or supplements HTML content only when there is clear
  evidence of an omission or large error, such as a missing section, truncated
  passage, absent equation body, broken table, missing figure or caption, or
  seriously corrupted scientific notation.
- OCR is a fallback source when neither HTML nor the native PDF text layer can
  recover the content reliably.
- Public YAML helps validate identity and normalized bibliographic fields.

The system aligns corresponding headings, paragraphs, captions, tables,
equations, and references. Differences involving spelling or layout may be
resolved automatically. Differences involving numbers, signs, exponents,
chemical formulas, DNA sequences, conclusions, equations, or table cells are
potentially meaningful and require stronger evidence or review.

Every departure from otherwise valid HTML must be recorded in
`extraction_diagnostic/` with the affected content identifier and supporting
PDF evidence. When a major PDF/HTML conflict is ambiguous rather than a clear
HTML omission or error, the extraction remains based on HTML, is marked
`needs_review`, and cannot be promoted until the conflict is resolved. The
script must never silently combine materially different manuscript versions.

## 7. Scientific Formatting Rules

All text is UTF-8 and normalized conservatively.

The extraction should preserve:

- Greek letters such as α, β, γ, and μ.
- Superscripts and subscripts.
- Mathematical minus signs and multiplication signs.
- Prime symbols in nucleotide sequences.
- Scientific notation such as `4.5 × 10⁹ M⁻¹`.
- Chemical formulas and stereochemical notation.
- Special characters in author names.
- Citation markers and equation numbers.

Readable Unicode is preferred in ordinary prose. Display equations use LaTeX
when recoverable, with a source image retained when the equation cannot be
represented confidently.

Chemical structures and reaction schemes are figures or schemes, not
speculative text conversions.

Every inferred glyph replacement, dehyphenation, OCR correction, or notation
repair is recorded only in `extraction_diagnostic/`.

## 8. Figure and Scheme Handling

For each expected figure or scheme:

1. Identify it independently from PDF captions and HTML structure.
2. Prefer the complete original HTML image when available.
3. Verify that the image matches the PDF figure.
4. Otherwise crop it from a high-resolution PDF rendering.
5. Retain complete multi-panel figures by default.
6. Exclude logos, publisher icons, decorative graphics, and equation fragments.
7. Assign stable filenames based on the printed label.
8. Place the caption in the relevant consolidated main-article or supplement
   figure entry in `record.json`.

Include an unlabeled image only when it is clearly scientific content.
Ambiguous images are reported for review instead of automatically being called
figures.

Do not OCR figure or scheme images. Preserve their pixels faithfully and use
only associated source text—such as captions, native labels, alt text, or
accessibility descriptions—as textual content.

## 9. Table Handling

Every table is represented inline as an object in the canonical
`record.json` `tables` array. It preserves rows, columns, spans, headers,
footnotes, units, safe cell HTML, machine records, and references to any
graphical cell assets. New-format extractions do not generate companion
per-table JSON or CSV files.

The current record schema `1.1` validates that each embedded table is complete
and internally consistent; it never rewrites, normalizes, rectangularizes, or
otherwise imposes a common scientific layout on the authors' table. Preserve
multi-part and intentionally irregular tables, row and column spans, scientific
notation, and the authors' footnote wording, order, and scope as extracted. A
schema-valid table can therefore retain any faithful publisher-specific
arrangement supported by the container.

Table `source_image`, structure-asset, and machine-record asset paths follow the
top-level `asset_path_base: "extraction_root"` contract. They resolve from the
directory containing `record.json` and must also correspond to files registered
in its asset registry. The schema version describes the JSON container
contract, not the table's visual format or scientific content. A future
incompatible change to the JSON shape requires a schema-version increase;
correcting or faithfully reproducing an author's table does not.

Retain a full-table source image only when the accepted table source is an
image or a PDF rendering. Do not create a full-table image for an HTML-native
table. Create a table-specific cell-image subfolder only when one or more cells
contain graphical content that cannot be represented faithfully in JSON alone;
do not create an empty cell-image directory for ordinary textual cells.

HTML tables provide the first structured candidate. The PDF rendering verifies
cell alignment and visible values. When the HTML-native table is accepted, this
verification does not cause the PDF rendering to be retained as a table image.
Native PowerPoint tables and chart-data caches use the structured
`source_kind: "presentation"` representation introduced in record schema
`1.1`; like HTML-native tables, they do not require a redundant source image.
When the accepted source is an image or PDF, retain its full-table source image
as a linked external asset. Record any representation limitation only in
`extraction_diagnostic/`.

## 10. Equation Handling

Inline equations remain in the prose.

Publisher containers that explicitly identify a display equation are emitted
in DOM order with the prose before and after them kept as separate blocks.
Consecutive display equations are all retained, and nested wrappers produce
one outer equation block rather than duplicate math.

Display equations receive:

- A stable equation identifier.
- Readable LaTeX in `record.json` where recoverable.
- The printed equation number.
- A source image when useful.
- A reference from `record.json` to any equation asset.

A missing equation body, such as an extraction containing only `(1)`, is a
blocking error.

## 11. Supplement Handling

Preserve a byte-identical copy of every authoritative supplementary source file
in the extraction package, retain its original filename, extension, and
source-relative path, and assign it a stable ID. Authoritative sources include
direct publisher files and members safely unpacked from a publisher-delivered
outer ZIP. The outer ZIP is an acquisition container, is not an authoritative
source, and is not copied into the extraction package. The authoritative files
remain in the record-level `supplementary/` source directory and are never
modified by the extraction pipeline. All extracted supplementary text and
structure go directly into the corresponding section of `record.json`;
supplements do not receive separate `content.md` files.

- PDF, Word, or text supplements are extracted in full into `record.json`.
- Figures and tables within them use the same rules as the main article.
- PowerPoint supplements are parsed non-destructively. Native slide text,
  speaker notes, captions, accessibility descriptions, and object coordinates
  are extracted without OCR. All paragraphs in one caption text box form one
  consolidated caption.
- A slide identified by a native figure caption is itself a composed scientific
  figure. Render the complete slide with Microsoft PowerPoint's native
  compositor as a lossless PNG with a 5,000-pixel long edge, and use that PNG as
  the figure's authoritative human-facing visual. Render each qualifying slide
  independently in a multi-slide presentation. Do not crop the slide into
  components and do not use the low-resolution package thumbnail as a fallback.
  A missing or failed complete-slide render is a structural finding that blocks
  promotion.
- Native text from a rendered figure slide remains in `record.json` as
  machine-readable `figure_text`; its slide number and coordinates remain in
  `extraction_diagnostic/`. The viewer does not display those fragments as
  linear prose because doing so destroys their positional meaning. The complete
  slide PNG supplies the visual context.
- Embedded figure images, videos, and other media are preserved as separate
  downloadable assets in their original available formats and linked to the
  complete-slide figure when used by only that slide. The original PPTX remains
  byte-identical. For slides containing animation or video, the static render is
  the overview while the original media remain separately accessible.
- Follow internal PowerPoint package relationships to embedded scientific data.
  In particular, copy each reachable embedded workbook byte-for-byte in its
  original format, register it as a supplementary-data asset, and link it to
  the chart or table that uses it. Do not create CSV derivatives. Unreachable
  embedded files and unavailable external relationships remain explicit review
  diagnostics rather than silent omissions.
- Spreadsheets retain the original workbook as an external asset, while their
  readable cell content and sheet structure appear inline in `record.json`.
- Images, videos, and scientific datasets retain their original form and
  receive a descriptive inventory entry and local asset reference in
  `record.json`. Figure images and video frames are not OCRed.
- Standalone publisher-delivered ZIP supplements are safely unpacked during
  source acquisition, not by the extraction pipeline. Nested ZIP members and
  other intentional archive files remain intact and are inventoried safely
  before any supported content is read.
- Unsupported files are still preserved and linked; they are never silently
  omitted.

## 12. `extraction_diagnostic/`

This sibling directory supports diagnosis and improvement but is not part of
normal reading. It contains:

- Source inventory and checksums.
- A bidirectional content-coverage ledger.
- Pipeline and dependency versions.
- OCR pages and methods.
- Confidence by category.
- PDF/HTML differences.
- Automatic repairs.
- Excluded material and reasons.
- Suspected omissions or ambiguities.
- Automated validation results.
- Agent-review findings.
- Reproducible evidence-backed overrides.
- Final status: `draft`, `needs_review`, `reviewed`, or `approved`.

`record.json` does not link to this directory and contains none of its
diagnostic fields.

### Content-Coverage Ledger

`coverage.jsonl` acts as a hidden completeness checklist. Every meaningful
item independently found in the PDF, HTML, or supplementary material must be
assigned one of these statuses:

- `included`: represented in the new extraction and linked to its stable output
  identifier and RFC 6901 JSON Pointer in `record.json`.
- `duplicate`: another source representation of content already included.
- `intentionally_excluded`: publication content that should not appear in the
  extraction, such as a repeating page header, with the reason recorded.
- `unresolved`: not yet accounted for and requiring review.

The check also works in the opposite direction: every paragraph, section,
equation, table, figure, and supplementary item in the new extraction must map
back from its `record.json` JSON Pointer to source evidence. The pointer is kept
in `coverage.jsonl`, not in the polished JSON. This helps detect both missing
source content and unsupported material introduced into the output.

An extraction cannot be approved while any coverage item remains unresolved.

## 13. Script Design

The tracked implementation is expected to consist of:

```text
extraction_viewer.html
scripts/
├── extract_record.py
├── validate_extraction.py
├── review_extraction.py
└── extraction/
    ├── discovery.py
    ├── html_extractor.py
    ├── pdf_extractor.py
    ├── pdf_text_extractor.py
    ├── pdf_ocr_text.py
    ├── pdf_article_extractor.py
    ├── ocr.py
    ├── reconciliation.py
    ├── figures.py
    ├── tables.py
    ├── table_schema.py          # Legacy table-sidecar compatibility
    ├── record_json.py
    ├── record_schema.py
    ├── rich_text.py
    ├── schemas/
    │   ├── record-1.0.schema.json
    │   ├── record-1.1.schema.json
    │   └── table-1.0.schema.json # Legacy table-sidecar compatibility
    ├── equations.py
    ├── supplements.py
    ├── pptx_supplement.py
    ├── render_powerpoint_slides.ps1
    ├── xml_supplement.py
    ├── renderer.py
    ├── reporting.py
    ├── validation.py
    └── models.py
```

The command-line interface should support:

- One record at a time.
- Read-only inventory or dry-run mode.
- The fixed private staging root `papers (private)/staging/`, with isolated
  record and run subdirectories.
- Extraction without review.
- Validation of an existing candidate.
- Review-panel execution.
- Guarded standing-policy finalization of a reviewed, reproducible candidate.
- Later, batch processing.

The system should be modular so PDF, OCR, table, or equation engines can be
replaced without changing the output contract.

Deterministic parsing and validation should handle as much as possible. AI may
help classify or reconcile ambiguous content, but it must not invent missing
text or silently rewrite source content.

## 14. Safe-Write Behavior

Generate extraction candidates under `papers (private)/staging/` first. The
script must:

- Never modify source PDFs, HTML, or supplements.
- Never overwrite an existing extraction by default; approved revisions use a
  guarded replacement path that archives the prior live version.
- Validate all links and files before promotion.
- Refuse promotion when the staged manifest's extraction-code fingerprint no
  longer matches the currently checked-out pipeline.
- Use atomic directory promotion.
- Require the explicit `--replace` finalization mode for existing extractions.
- Preserve the manifest-bound approved override in `extraction_diagnostic/overrides.yaml` across reruns; use any record-root `extraction_overrides.yaml` only as a temporary working copy and remove it after guarded approval.
- Alter only the controlled public `pip_litdb_status` field during finalization;
  never alter `jamies_human_only_notes`.
- Remove a record's entire staging directory only through guarded
  post-approval cleanup after its approved live copy and public status have
  been verified.

Every candidate remains staged until it has been reviewed and reproduced. The
primary agent then automatically approves, promotes, verifies, updates status
and queue, and removes it through the guarded standing-policy finalizer.

## 15. Automated Validation

Each run checks:

- Correct record identity.
- A new-format manifest declares `record.json`, its media type, and schema
  version, and the file validates against the declared supported schema. New
  runs use `1.1`; approved `1.0` records remain valid.
- `record.json` is valid UTF-8, contains only the canonical content fields, and
  contains no provenance, confidence, source, repair, or review fields.
- Safe-HTML fragments obey the restricted content contract.
- Complete source inventory.
- Every meaningful source item is included, duplicated, intentionally excluded,
  or explicitly unresolved in `coverage.jsonl`.
- Every output content block and asset maps back to source evidence through a
  valid RFC 6901 JSON Pointer, and every coverage pointer resolves in
  `record.json`.
- No unresolved coverage items remain before approval.
- Record identity and the exact title appear at the beginning of the canonical
  object.
- Valid heading hierarchy.
- Absence of recurring page furniture.
- No broken or escaping relative links.
- No missing or orphaned assets.
- Figure, table, equation, and supplement counts.
- Reachable presentation-embedded workbook/data-file counts.
- No duplicate IDs.
- No unexplained empty files.
- No obvious mojibake or replacement characters.
- Numbers, units, Greek letters, signs, and exponents across sources.
- No unresolved OCR uncertainty affecting a scientific symbol, value, label,
  equation, or table cell.
- OCR jobs never target scientific figure or scheme pixels, extracted figure
  assets, or video frames.
- Table cell alignment and values.
- Every inline table validates as part of its declared record schema; no new-format
  table has a separate JSON or CSV derivative. Malformed JSON and schema
  violations block acceptance without changing the table to make it conform.
- A full-table image exists for each image/PDF-sourced table and does not exist
  for an HTML-native table; cell-image directories exist only when referenced
  graphical cells require them.
- `record.json` and legacy `record.md` never coexist in one extraction.
- Equation presence.
- Deterministic output from identical inputs.
- No private paths or content written into public metadata.

Mutation tests should deliberately delete paragraphs, alter exponents, shift
table cells, clip figures, duplicate headers, and break links to verify that
the validators detect them.

For historical packages with no new-format manifest declaration, validation
may fall back to the legacy `record.md` contract. This is compatibility for
already-produced records only; the current extraction pipeline always writes
`record.json`.

## 16. Review Modes

The normal extraction and efficiency-comparison baseline is one record owner
performing five sequential self-reviews, followed by separate adjudication
and primary-agent finalization. Use GPT-6 Astra High unless Jamie
requests a different setting. Freeze the candidate and code before review.
All source, test, reproduction, and acceptance requirements still apply.

Jamie has authorized batches of up to three concurrent records with one Astra High owner per paper in isolated private workspace copies. Shared runtime and protected inputs remain frozen, guarded imports preserve the exact reviewed runs, and the primary finalizes records serially under a repository-wide lock. Shared fixes require stopping all owners and rebuilding/reviewing affected candidates against a new common snapshot. This changes paper scheduling, not the review mode or acceptance gates. See `EXTRACTION_PROTOCOL.md` Section 2.5.

The five required roles are:

1. Text and reading-order reviewer.
2. Scientific notation, equations, and table reviewer.
3. Figure, scheme, and supplement reviewer.
4. AI-readiness and consistency reviewer.
5. Adversarial reviewer looking for hidden omissions or unsupported
   corrections.

Label owner-performed roles as self-review. Keep each role's evidence and
findings distinct and complete; a separate adjudication pass resolves them.

Early pilots used an independent-review panel. That remains a separate mode
when Jamie explicitly requests it: read-only reviewers independently inspect
the complete sources and frozen candidate before the owner adjudicates.
Do not add that panel to the normal baseline solely to measure efficiency,
and do not present self-review as independent review. Keep the selected
mode fixed when comparing workflows; equivalent defect detection is not
assumed. `EXTRACTION_PROTOCOL.md` Sections 2 and 8 define the current process.

Findings use these levels:

- **Critical:** Wrong publication, fabricated material, or a missing source
  document.
- **Scientific:** Altered meaning, value, equation, table cell, sequence, or an
  incomplete figure.
- **Localized:** OCR or formatting defect without confirmed scientific impact.
- **Cosmetic:** Harmless presentation inconsistency.

All findings remain under `extraction_diagnostic/reviews/`.

## 17. Pilot Record `00559`

The first pilot followed this process:

1. Freeze the output contract in this plan.
2. Inventory the PDF, HTML, and supplement.
3. Render and assess every PDF page.
4. Extract HTML and PDF candidates independently.
5. Reconcile the body, notation, figures, schemes, tables, equations, and
   references.
6. Extract the supplementary PDF.
7. Generate staged `extraction/` and `extraction_diagnostic/` directories.
8. Run deterministic validation.
9. Run the five-reviewer panel.
10. Consolidate findings.
11. Present the extraction and important uncertainties to the user.
12. Turn recurring defects into general pipeline rules.
13. Use record-specific overrides only when a general solution is
    inappropriate.
14. Rerun and repeat until every finalization gate passes, then have the primary
    agent approve automatically under the standing policy.
15. Preserve the approved output as the private golden regression case.

## 18. Additional Pilots

After `00559`, select records one at a time to add new document archetypes.
Strong candidates include:

- `00390`: PDF-only article with recoverable legacy font encoding and no OCR.
- `00614`: Full-page scanned article using reviewed, local region OCR with
  figure and scheme pixels excluded from recognition.
- `00432`: PDF/HTML reconciliation with many complex tables.
- `00548`: PDF-only book chapter.
- `00564`: Short article with a separate experimental supplement.
- A record with spreadsheet or Word supplements.
- A record with video or unusual scientific datasets.
- A mixed native-text/OCR PDF.
- A record with multi-page or vector figures.

Each newly discovered failure should first produce a general extraction or
validation rule. Once a new archetype stops revealing critical failure
classes, it becomes another regression case.

## 19. Corpus-Wide Rollout

Only after the pilot set is stable:

1. Build a read-only classification matrix for all private records.
2. Group records by source and complexity.
3. Run extraction in controlled batches.
4. Skip unchanged, already approved records using source hashes.
5. Stage replacements for existing legacy extractions.
6. Run automated validation on every record.
7. Run targeted agent review, with deeper review for warnings and unusual
   records.
8. Produce a per-record review summary.
9. Inspect every record for consistency, missing assets, and anomalies.
10. Automatically finalize only packages that pass every standing-policy gate;
    leave blocked packages staged and report their unresolved issue.

Existing extractions must not be mass-replaced until the new pipeline has
demonstrated better results across the representative pilot set.

## 20. New-Format Acceptance Criteria

A record produced under the canonical JSON contract is complete only when:

- All source and supplementary files are represented.
- Every meaningful source item is accounted for in both directions, with no
  unresolved coverage entries.
- The title and article structure are correct.
- No page furniture remains.
- No body paragraphs are missing, duplicated, or reordered.
- Scientific notation is preserved.
- No scientific symbol, value, label, equation, or table cell remains
  unresolved after OCR and visual comparison.
- Every displayed equation is present.
- Every expected figure, scheme, and table has exactly one correct identity.
- Figure crops contain all panels and no unrelated body text.
- PDF-cropped figures, schemes, and graphical abstracts receive an advisory warning when meaningful pixels touch an image boundary; each warning is visually reviewed for clipping versus intentional edge-to-edge artwork.
- Tables retain values, headers, footnotes, and units.
- `record.json` validates against its declared supported schema; every table is embedded
  in it, no table has a separate JSON or CSV derivative, and table or cell
  images exist only under the source-dependent rules in Section 9.
- Every diagnostic coverage target is a valid RFC 6901 JSON Pointer into
  `record.json`.
- All local links resolve.
- Every PowerPoint slide identified as a scientific figure has a complete
  high-resolution PNG render; package thumbnails are not accepted as figure
  substitutes.
- The supplement is usable and linked.
- No unresolved critical or scientific findings remain.
- A clean rerun produces the same content.
- The review panel completes and disagreements are resolved.
- The primary agent records standing-policy approval after every preceding gate
  passes; routine candidate approval is not requested from the user.

Archived legacy `record.md` packages remain governed by their original
acceptance evidence and the compatibility validator. They are historical
comparison material only: all live approved golden pilots now use
`record.json`, and every future extraction must use the JSON contract.

This plan keeps the polished extraction simple while retaining enough
technical evidence in `extraction_diagnostic/` to find and correct the inevitable
occasional mistake.
