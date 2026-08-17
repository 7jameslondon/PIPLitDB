# Download Missing Paper Materials

For the assigned five-digit paper ID:

1. Read `database/records/<ID>.yaml` and inspect `papers (private)/<ID>/` to identify missing source files. Check only the main PDF, full-text HTML, and all supplementary material; do not create or modify extractions.
2. Check the main PDF, full-text HTML, and every supplement independently. Failure to obtain one material does not prevent downloading other materials that are publicly available.
3. Apply these source restrictions separately to each material:
   - Main PDF: Download from the publisher or an official institutional or subject repository.
   - Full-text HTML: Download only from the publisher's article page or publisher-controlled infrastructure. Do not substitute HTML from PMC, an institutional repository, an archive, or another third party.
   - Supplementary material: Download only from the publisher's article page or the exact publisher-managed asset, CDN, or supplementary platform exposed by that page. A publisher-managed Figshare record is permitted when it is directly associated with the article. Do not substitute supplementary files from PMC, an institutional repository, an author repository, or another archive.
   These restrictions apply even when a repository copy appears identical to the publisher's version. Do not use unofficial mirrors, guessed URLs, access-control workarounds, or overwrite valid existing files.
4. Inspect the publisher article page, rendered DOM, and embedded supporting-information widgets for all listed supplements. If a publisher wrapper fails, use the exact public asset URL exposed by that page or widget only when it is publisher-controlled or part of a publisher-managed supplementary platform.
5. Before declaring publisher material paywalled, blocked, or unavailable, follow the institutional-access workflow below. A failed unauthenticated web request, command-line request, or publisher-wrapper request is not sufficient evidence that the material is unavailable when the user's connected browser may have institutional access.
6. If a material still cannot be obtained from a source permitted for that material, leave it missing, record the blocker, and continue checking the remaining materials. Do not use a repository copy as a fallback for full-text HTML or supplementary material.
7. Follow the repository layout: `pdf/main.pdf`, `html/main.html`, and `pdf/supplementary.pdf`. Follow an established local naming pattern if the paper has multiple supplements.
8. Confirm every downloaded file is nonempty, has the expected format and MIME type, parses successfully, and contains identifying information matching the paper. Ensure HTML is actual full text rather than a landing, abstract-only, error, or challenge page and conforms to the full-text HTML archival format below. Record both the publisher article page and the final official asset URL.
9. Report the outcome for each material, including files added, material already present, validation performed, the publisher article page, the final official asset URL, and confirmation that the source satisfies the material-specific restrictions above. Do not commit files under `papers (private)`.

## Institutional-access workflow

1. Prefer the user's connected external browser profile for publisher access because it may retain an institutional login, library proxy configuration, or access extension. Reuse an already open publisher tab when practical. Otherwise, navigate directly to the publication DOI from the YAML record and allow the browser's existing access configuration to handle any redirect. Do not begin by constructing or guessing institutional-login or proxy URLs.
2. Inspect the rendered publisher page before attempting unauthenticated command-line downloads. Strong evidence of access includes a publisher `Subscribed`, institutional-access, or equivalent marker; article sections beyond the abstract; and a publisher PDF control. Do not infer full-text access from the title, abstract, or the presence of a PDF-shaped link alone.
3. If the publisher requires sign-in, leave the DOI or publisher page open for the user to complete the institutional login in that same browser profile. Never request, inspect, store, or enter the user's password, one-time code, or other login credentials. After the user confirms that login is complete, recheck the DOI in the same browser session before trying another source.
4. When authenticated access is available, obtain each material independently:
   - Full-text HTML: Capture content only from the authenticated publisher article page or publisher-controlled infrastructure. Prefer the publisher's raw HTML response when it can be captured in the authenticated session; otherwise capture the complete rendered article container after the full text has loaded. Treat that capture as source material and normalize it into the archival format below. Do not save an unprocessed publisher page or complete rendered site DOM as the final `html/main.html`.
   - Main PDF: Use the exact publisher PDF URL exposed by the authenticated rendered page and retrieve it within the same authenticated browser context when necessary. Validate the downloaded bytes as a real PDF. Do not overwrite an existing valid `pdf/main.pdf`; use publisher access to fill a missing or invalid main PDF only.
   - Supplementary material: Continue to use only the exact publisher-managed links or widgets exposed by the article page. Authentication may be used when required, but the same supplementary-material source restrictions still apply.
5. Authentication through the user's authorized institutional subscription is permitted; bypassing a paywall, challenge, or access control is not. If the connected browser is unavailable, the institutional session has expired, or access is still denied after the user signs in, record that specific blocker and continue with the other materials.

## Full-text HTML archival format

1. Save `html/main.html` as a self-contained, article-focused body fragment. The literal file must begin with `<body>` and end with `</body>` and must not contain a doctype, `<html>`, or `<head>`. Retain the title, authors, abstract, main article sections, equations, tables, figure captions, acknowledgments, references, and supporting-information description when present. Exclude publisher navigation, account controls, advertisements, tracking elements, modal dialogs, surveys, metrics, and unrelated recommended content when those elements can be separated from the article.
2. Remove executable, interactive, and styling content. The final file must not contain `<script>`, `<style>`, stylesheet `<link>` elements, `<iframe>`, `<noscript>`, `<template>`, `<object>`, `<embed>`, `<canvas>`, forms, or interactive form controls. Remove inline `style` attributes, CSS `class` attributes, event-handler attributes such as `onclick`, JavaScript URLs, responsive `srcset`/`sizes` attributes, and publisher `data-*` attributes. Normal citation, DOI, and supplementary-document hyperlinks may remain.
3. Make retained images self-contained:
   - Retain scientific images such as the graphical abstract, figures, schemes, charts, image-based tables, and image-based equations. Exclude publisher logos, UI icons, advertisements, tracking pixels, and survey graphics.
   - For every scientific figure, inspect the rendered DOM for publisher controls or links labeled `View Large`, `Full Size`, `Original`, `Open Image`, `Zoom`, or an equivalent. Also inspect image anchors, `srcset` candidates, galleries, and figure wrappers. Prefer the highest-resolution publisher-managed asset explicitly exposed by the page.
   - A large-figure URL may be a publisher wrapper rather than the image itself. When so, inspect that wrapper's rendered DOM and observed page assets to identify the exact publisher-managed image URL. Do not guess an original-image URL from a filename pattern.
   - Download only the exact publisher-managed image assets exposed by the article or large-figure wrapper. Avoid bundling ads, trackers, or unrelated third-party page images.
   - Embed each retained image directly in its `<img src>` as a base64 data URL of the form `data:<MIME-type>;base64,<data>`. The final file must contain no remote image `src`, `srcset`, lazy-load, or CSS-image dependency.
   - Remove duplicate desktop/mobile/responsive copies of the same figure after styling is removed. Preserve one copy with its useful alt text and caption. A publisher large-figure hyperlink may remain for provenance, but the displayed image must not depend on it.
   - If a publisher exposes no larger asset or the exact large asset cannot be obtained, embed the best permitted publisher image available and report that limitation rather than guessing another URL.
4. Validate the normalized HTML after writing it:
   - Confirm the literal body-only structure and successful HTML parsing.
   - Confirm the title, DOI, abstract, major body sections, and references are present.
   - Confirm the prohibited tags and attributes above occur zero times.
   - Confirm every retained `<img>` uses a data URL, every base64 payload decodes to nonempty image bytes, and no remote image source remains.
   - Confirm every publisher figure is represented once, all exposed large-figure assets were considered, and the decoded dimensions of embedded large figures match the downloaded assets.
   - Report the final HTML byte size, total embedded-image count, number of large figures embedded, any omitted images or unavailable large assets, and the HTML checksum.
