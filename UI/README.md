# PIP LitDB UI

This directory contains a static HTML, CSS, and JavaScript interface for the
public metadata database. It reads the canonical YAML files at runtime and does
not generate or maintain a second database.

## Local preview

Start a static server from the repository root so that the records directory is
available to the UI. For example:

```powershell
python -m http.server 8000
```

Then open `http://localhost:8000/UI/`. The local mode reads the web server's
directory listing for `database/records`, so adding or deleting a YAML record is
reflected on refresh. Opening `index.html` directly with a `file://` URL will not
work because browsers cannot enumerate local files that way.

## GitHub Pages

On a standard `owner.github.io/repository/UI/` project URL, the UI detects the
owner and repository automatically. It requests the current commit tree from
GitHub and loads the original YAML records at that exact commit. Set the GitHub
values in `config.js` only if the site uses a custom domain or a different URL
layout.

The YAML parser is loaded from jsDelivr, so the UI currently requires an internet
connection even in local preview mode.
