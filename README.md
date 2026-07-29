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
    ├── relationship-types.yaml
    └── record-statuses.yaml
```

Each relationship type in `database/vocabularies/relationship-types.yaml` must define its inverse relationship type. For example, the inverse of `is_preprint_of` is `has_preprint`, and the inverse of `corrects` is `is_corrected_by`. A symmetric relationship type may define itself as its inverse.

The database records the following fields for each paper:

- Paper type (`paper_type`): The kind of paper, such as a research article, review, preprint, or correction
- Title
- Authors: An ordered list of author names, ideally using each author's full name
- DOI
- URL: Ideally a DOI link; otherwise, a publisher link
- Related papers: A list containing the PIP LitDB ID and directed relationship type for each related paper. Every relationship must be stored in both related records using inverse relationship types. For example, if one record uses `is_preprint_of`, the other must use `has_preprint`.
- Publication year
- Journal: Ideally the standard full name, not an abbreviation. If it's a preprint, use the server name.
- PIP LitDB status: A text field used exclusivly by human end users
- PIP LitDB notes: An optional field used only when a note is essential or temporary

Optional fields with no value, including status and notes, should be omitted rather than stored as empty strings.

An example paper record is:

```yaml
paper_type: preprint
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

Database validation should check:

- Every record follows `paper.schema.json`.
- Every record filename matches the five-digit format `NNNNN.yaml`, begins at `00001`, and uniquely determines that record's PIP LitDB ID.
- Duplicate DOIs.
- Related-paper IDs and relationship types.
- Every related-paper entry has exactly one corresponding entry in the related record using the inverse relationship type defined in `relationship-types.yaml`.
- Values governed by the files in `database/vocabularies`.

Search and export tools read the YAML files directly.

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
