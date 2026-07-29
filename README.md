# PIP LitDB

**PIP LitDB** is a project to collect papers about PIPs (pyrrole–imidazole polyamides) and extract their contents. It has two parts. The first is a version-controlled public database that lists PIP papers. The second is a set of tools for processing authorized copies of the PIP papers.

## ARD

- Anyone can clone and use the public catalogue without possessing any papers. The private papers can be moved or deleted without damaging the catalogue.

- One ID for each paper found. Sometimes a paper can show up as having a slightly meta data but are really just the same paper. For example a title might appear slightly diffrently due to how special charactuers are handled. Another example is when author names are abbrviated or spelled out. In cases like these there is really only one paper with the same content and so only one entry should be put into the database with just one ID. In other cases there is really two diffrent papers. For example one paper might have two diffrent versions from a publisher that really have diffrent conent. Another example is a preprint and  a public

## Public Paper Database

The database includes published research articles, preprints, and reviews about PIPs. The database accounts for relationships between articles such as errata, preprint, versions, etc.

The database records the following fields for each paper:

- Title
- Authors: Ideally fullname author list
- DOI
- URL: Ideally a DOI link, otherwise a publisher link
- Related papers: A list containing the PIP LitDB ID and relationship type for each related paper, such as `Preprint`
- Publication year
- Journal: Ideally the standard full name not abbreviated. If its a preprint then the server name.
- PIP LitDB ID: A five-digit, zero-padded identifier beginning with `00001`
- PIP LitDB status: A text field that is blank by default
- PIP LitDB notes: Leave blank unless a note is essential or temporary

The database is stored in the `database` directory.

## Private Paper Copies and Extractions

Users with authorized access to papers may store PDF or HTML copies in the `papers (private)` directory. Markdown extractions and extracted figure images may also be stored there. Files should be organized as follows:

```text
papers (private)/
└── 00001/
    ├── pdf/
    ├── html/
    └── extraction/
        ├── text.md
        └── figures/
            └── figure 1.png
```

## Public Paper Extraction Tools

The `extraction tools` directory contains utilities that help authorized users download, inspect, and extract paper contents.

## Notes

- Never commit the contents of `papers (private)`. Keep this directory excluded through `.gitignore`.
