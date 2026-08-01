# PIP LitDB

**PIP LitDB** is a project to collect papers about PIPs (pyrrole–imidazole polyamides) and extract their contents. It has two parts. The first part is a version-controlled public database of PIP paper metadata and the tools to manage the database. The second part is a untracked private database that has a subset of copies of the actual PIP (PDFs and/or HTML copies), extracted text/figures of the paper, and the tools to manage extraction and the database.

## ARD

- Anyone can clone and use the public database without possessing any papers. The private papers can be moved or deleted without effecting the database.

- The only private untracked part of the project are the paper copies and extractions. The public database of metadata, the tools for the public database, the tools for the private database, and the extraction tools, are all tracked and public.

- One ID for each paper found. Sometimes a paper found online will have slightly diffrent metadata then an entry already in the database but is really just the same paper. For example a title might appear slightly diffrently due to how special charactuers are handled. Another example is when author names are abbrviated or spelled out. In cases like these there is really only one paper with the same content and so only one entry should be put into the database with just one ID. In other cases there is really two diffrent papers. For example one paper might have two diffrent versions from a publisher that really have diffrent conent. Another example is a preprint and a published article of that preprint. In these cases a seperate entry with a seperate ID should be put in the database and then they should be linked via their relasionship.

- The public database is organized as a file system of YMAL files so that tracking is handeled by git.

## Public Metadata Database

The database includes published research articles, preprints, and reviews about PIPs. The database accounts for relationships between articles such as errata, preprints, and versions.

The YAML files are the complete and only database. Each paper is stored in its own YAML file in `database/records`. The filename is the authoritative PIP LitDB ID: for example, `00001.yaml` has PIP LitDB ID `00001`. The ID is not repeated as a field inside the YAML record. Search and export tools derive the ID from the filename and include it in exported data when appropriate.

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
    ├── publication-stages.yaml
    ├── relationship-types.yaml
    └── record-statuses.yaml
```

Each relationship type in `database/vocabularies/relationship-types.yaml` must define its inverse relationship type. For example, the inverse of `is_preprint_of` is `has_preprint`, and the inverse of `corrects` is `is_corrected_by`. A symmetric relationship type may define itself as its inverse.

The database records the following fields for each paper:

- Document type (`document_type`): The kind of document. Allowed values are `research_article`, `review`, and `correction`.
- Publication stage (`publication_stage`): The publication stage of the document. Allowed values are `preprint` and `publication`.
- Title
- Authors: An ordered list of author names, ideally using each author's full name
- DOI
- URL: Ideally a DOI link; otherwise, a publisher link
- Related papers: A list containing the PIP LitDB ID and directed relationship type for each related paper. Every relationship must be stored in both related records using inverse relationship types. For example, if one record uses `is_preprint_of`, the other must use `has_preprint`.
- Publication year
- Journal: Ideally the standard full name, not an abbreviation. If it's a preprint, use the server name.
- PIP LitDB status: A text field used exclusivly by human end users
- PIP LitDB notes: An optional field used only when a note is essential or temporary

Document type and publication stage describe independent characteristics. For example, a preprint and its corresponding published article may both have `document_type: research_article`, while the preprint has `publication_stage: preprint` and the published article has `publication_stage: publication`. They remain separate records and are connected using the appropriate related-paper relationship.

Optional fields with no value, including status and notes, should be omitted rather than stored as empty strings.

An example paper record is:

```yaml
document_type: research_article
publication_stage: preprint
title: "Example paper title"
authors:
  - name: "Alex Jones"
  - name: "Morgan Jane Smith"
doi: "10.1234/example.123"
url: "https://doi.org/10.1234/example.123"
publication_year: 2024
journal: "BioRxiv"
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

### Validation

Automated database validation checks:

- Every record follows `paper.schema.json`.
- Every `document_type` and `publication_stage` value is defined in its corresponding vocabulary file.
- Every record filename matches the five-digit format `NNNNN.yaml`, begins at `00001`, and uniquely determines that record's PIP LitDB ID.
- Duplicate DOIs.
- Related-paper IDs and relationship types.
- Every related-paper entry has exactly one corresponding entry in the related record using the inverse relationship type defined in `relationship-types.yaml`.
- Values governed by the files in `database/vocabularies`.

It also rejects duplicate YAML/JSON keys, YAML aliases, symlinked database paths, malformed
vocabulary definitions, blank or padded single-line values, duplicate authors within a record,
DOI/DOI-URL mismatches, embedded URL credentials, non-public or unsupported URL targets,
local/private filesystem references in public notes, self-relationships, missing relationship targets,
reuse of a record ID found in base-branch history, and relationship vocabulary inverses that are not
themselves reciprocal.
Exact normalized title/year and URL collisions are reported as reviewer warnings because they can
represent either accidental duplicates or legitimate separate versions.

Run the complete validation locally with Python 3.12 or later:

```powershell
python -m pip install -r requirements-validation.txt
python -m unittest discover -s tests -v
node --test tests/test_pr_status_decision.cjs
python scripts/validate_metadata.py
```

To get the same add/remove/modify/rename summary produced for a pull request, include a base Git
revision:

```powershell
python scripts/validate_metadata.py --base origin/main --head HEAD
```

That default uses merge-base semantics to summarize a pull-request branch. For an exact transition,
such as a pushed branch's before and after commits, add `--comparison direct`.

Two isolated GitHub Actions workflows cover pull requests. The `Validate metadata` workflow's
trusted job uses the workflow, dependencies, validator, schema, and controlled vocabularies from the
base branch and treats the proposed pull-request checkout's records as data only. It resolves
GitHub's pull-request merge ref with bounded retries, verifies that its parents are the event's exact
base and head commits, and records the resulting tree identity before and after validation. A
regenerated synthetic merge commit remains valid when its parents and tree are unchanged; changed
content fails validation instead of publishing a stale result. The trusted job publishes the
`Validate metadata records (main)` status on the stable pull-request head commit so GitHub's
synthetic merge commit can be regenerated without losing the status. The status name is scoped to
the protected base branch, and both pending and final writes verify the pull request's current state,
base ref, base commit, and head commit. Opening, reopening, updating, or editing a pull request that
targets `main` triggers validation. Relevant pushes to `main` immediately return every current open
pull request to pending and revalidate its merge content with the new base and trusted rules. This
also covers stacked pull requests whose head already contains the new `main` commit and therefore
does not change when `main` advances. The pure status-decision logic is behaviorally tested; API calls
remain in trusted workflow code.

This status should be a strict required check: branch rules must require the topic branch to be up to
date with `main` before merging. A second pass, still using trusted executable code, verifies that
proposed schema and vocabulary files remain present, parseable, symlink-safe, and internally
consistent. It also requires the resolver, status-decision helper, validator, workflow, dependency
manifest, tests, and `.github/CODEOWNERS` policy to remain regular files. The CODEOWNERS policy
assigns those enforcement paths, the schema, and the controlled vocabularies to `@7jameslondon`;
branch protection for `main` should require review from Code Owners so changes to the enforcement
mechanism or its rules cannot approve themselves. The separate `Test metadata validator` workflow
runs `Test proposed metadata validator` in the ordinary, read-only pull-request context to exercise
validator and rule changes before they merge without creating the protected status name.

The trusted job validates the entire resulting database, not only changed files, so removing a
referenced record or changing only one side of a relationship fails the check. It also adds a job
summary with compact ID ranges, field-level modifications, errors, and non-blocking human-review
warnings. The same trusted validation runs after metadata changes reach `main` and can be run
manually.

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

Users with authorized access to papers may store PDF or HTML copies of the main manuscript and supplementary files in the `papers (private)` directory. Markdown extractions and extracted figure images may also be stored there. Files should be organized as follows:

```text
papers (private)/
└── 00001/
    ├── pdf/
        ├── main.pdf
        └── supplementary.pdf
    ├── html/
        └── main.html
    └── extraction/
        ├── main.md
        ├── supplementary.md
        └── figures/
            └── figure 1.png
```

## Public Paper Extraction Tools

The `extraction tools` directory contains utilities that help authorized users download, inspect, and extract paper contents.

## Notes

- Never commit the contents of `papers (private)`. Keep this directory excluded through `.gitignore`.
- Never include private files or private filesystem paths in the public YAML records.
