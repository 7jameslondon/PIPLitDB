The optional `review_extraction.py` runner automates routine extraction preparation and mechanical evidence. It does not perform source review, write review judgments, approve, promote, alter public metadata, or touch human-only notes. Use Astra High and the complete owner-led workflow in `EXTRACTION_PROTOCOL.md`.

From the assigned repository/workspace:

```powershell
python -X utf8 scripts/review_extraction.py prepare NNNNN --run-id review-r01
```

This saves a full hashed source inventory, renders every archived PDF page at 150 DPI, saves uncorrected native PDF text, builds the staged candidate, and writes a lossless compact JSON reading view. Read the returned source index and inspect every source with the appropriate tools. Original HTML, packages, media and supplementary files still need direct inspection. No expected scientific count is inferred from the candidate. The machine inventory satisfies Step 3's inventory requirement; it does not satisfy Step 4's inspection requirement.

Use `inspect_extraction.py` for bounded reads of the saved JSON, including every page of long results. Raw sources, canonical JSON and all detailed diagnostics remain available. Correct source-supported defects as usual, then use a new run ID. `--reuse-sources PATH_TO_RECEIPT` reuses preparation only after checking every source, generator, rendering setting and saved artifact hash. Reusing prepared files does not mean another agent has inspected them. Inspect higher-resolution originals or render focused regions where 150 DPI is insufficient. `--page-dpi` changes page evidence resolution; `--crop-dpi` (default 300) controls canonical PDF assets.

After the complete source audit and repairs:

```powershell
python -X utf8 scripts/review_extraction.py freeze NNNNN --run-id review-r01 --browser-channel msedge
```

This reruns validation, runs the complete test suite, checks the existing viewer against the exact staged output through its read-only adapter, saves screenshots and download checks, and records code/source/candidate/evidence hashes. Read every finding and inspect screenshots. Tests, browser checks and source judgments retain their normal requirements. The returned freeze receipt is evidence for the five separate sequential self-review reports and separate adjudication, which the owner must actually perform and write under the candidate's `extraction_diagnostic/reviews/` directory. Do not hand-edit generated canonical files or machine receipts. Any source, override, code or candidate change requires rebuilding and repeating affected checks/reviews.

After the five reviews and adjudication:

```powershell
python -X utf8 scripts/review_extraction.py handoff NNNNN --run-id review-r01 --freeze PATH_TO_FREEZE_JSON --repro-run-id review-clean-r01
```

This checks evidence currency, required review files, the standing-policy finding allowlist and a new clean extraction's exact canonical equivalence. It saves a compact handoff receipt. Machine success still requires primary source, scientific and visual acceptance, followed by the existing separate finalizer and live completion checker. Receipts never claim that an automated check establishes scientific correctness. Failed or interrupted commands retain their diagnostics and do not issue a success receipt; inspect and fix the cause before another attempt.
