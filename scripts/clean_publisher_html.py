"""Create a script-free, style-free article body with embedded images."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from lxml import etree, html


REMOVED_TAGS = (
    "script",
    "style",
    "link",
    "iframe",
    "template",
    "object",
    "embed",
    "canvas",
    "video",
    "audio",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "nav",
    "search",
)

# Some publisher article bodies keep scientific fallback images inside
# ``noscript`` wrappers even after those images are rendered in the live page.
# Preserve the fallback content while still removing the prohibited wrapper.
UNWRAPPED_TAGS = ("noscript", "source")


def canonical_url(url: str) -> str:
    """Drop fragments while retaining signed-query parameters."""
    return urldefrag(url)[0]


def path_key(url: str) -> str:
    """Return a query-independent URL key for fallback matching."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def load_assets(manifest_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_url: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    duplicate_paths: set[str] = set()
    frontiers_originals: dict[str, dict] = {}

    for asset in manifest.get("assets", []):
        if asset.get("kind") != "image":
            continue
        url = canonical_url(asset["url"])
        by_url[url] = asset
        key = path_key(url)
        if key in by_path:
            duplicate_paths.add(key)
        else:
            by_path[key] = asset

        # Frontiers renders article figures through its own ``/api/ipx/``
        # image proxy while retaining the publisher-hosted original URL in
        # the archival DOM.  Associate that observed original URL with the
        # largest captured proxy rendition.  Both URLs are supplied by the
        # publisher page; this does not infer or synthesize an asset URL.
        frontiers_match = re.fullmatch(
            r"https://www\.frontiersin\.org/api/ipx/[^/]+/"
            r"(https://www\.frontiersin\.org/files/Articles/.+)",
            url,
        )
        if frontiers_match is not None:
            original = canonical_url(frontiers_match.group(1))
            prior = frontiers_originals.get(original)
            score = (
                int(asset.get("width") or 0) * int(asset.get("height") or 0),
                int(asset.get("bytes") or 0),
            )
            prior_score = (
                int(prior.get("width") or 0) * int(prior.get("height") or 0),
                int(prior.get("bytes") or 0),
            ) if prior is not None else (-1, -1)
            if score > prior_score:
                frontiers_originals[original] = asset

    for key in duplicate_paths:
        by_path.pop(key, None)
    for original, asset in frontiers_originals.items():
        by_url[original] = asset
        by_path[path_key(original)] = asset
    return by_url, by_path


def find_asset(
    source: str,
    base_url: str,
    by_url: dict[str, dict],
    by_path: dict[str, dict],
) -> dict | None:
    absolute = canonical_url(urljoin(base_url, source))
    return by_url.get(absolute) or by_path.get(path_key(absolute))


def remove_element(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is None:
        return
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)


def serialize_body_fragment(root: etree._Element) -> str:
    """Serialize one archival body without nesting a complete HTML document."""
    root_tag = etree.QName(root).localname.lower()

    if root_tag == "html":
        bodies = root.xpath("./body")
        if len(bodies) != 1:
            raise ValueError(
                "A complete HTML document must contain exactly one body element."
            )
        root = bodies[0]
        root_tag = "body"

    if root_tag == "body":
        parts = [root.text or ""]
        parts.extend(
            html.tostring(child, encoding="unicode", method="html")
            for child in root
        )
        serialized = "".join(parts)
    else:
        serialized = html.tostring(root, encoding="unicode", method="html")

    output = f"<body>\n{serialized}\n</body>"
    if re.search(r"<!doctype\b", output, flags=re.IGNORECASE):
        raise ValueError("Archival HTML must not contain a doctype declaration.")
    for tag in ("html", "head"):
        if re.search(rf"</?{tag}\b", output, flags=re.IGNORECASE):
            raise ValueError(f"Archival HTML must not contain a nested <{tag}> element.")
    if len(re.findall(r"<body(?:\s[^>]*)?>", output, flags=re.IGNORECASE)) != 1:
        raise ValueError("Archival HTML must contain exactly one opening body tag.")
    if len(re.findall(r"</body>", output, flags=re.IGNORECASE)) != 1:
        raise ValueError("Archival HTML must contain exactly one closing body tag.")
    for tag in REMOVED_TAGS:
        if re.search(rf"<{tag}\b", output, flags=re.IGNORECASE):
            raise ValueError(f"Archival HTML must not contain a <{tag}> element.")
    return output


def clean_html(
    source_path: Path,
    manifest_path: Path,
    output_path: Path,
    base_url: str,
    large_figure_manifest_path: Path | None = None,
) -> dict[str, int | str]:
    source = source_path.read_text(encoding="utf-8")
    # HTML parsers treat ``noscript`` contents as raw text when scripting is
    # enabled. Remove the wrapper in the source string first so fallback
    # scientific images become normal elements that can be embedded below.
    source = re.sub(r"</?noscript\b[^>]*>", "", source, flags=re.IGNORECASE)
    parser = html.HTMLParser(encoding="utf-8", remove_comments=True)
    root = html.fromstring(source, parser=parser)

    # ScienceDirect and Cell Press expose the complete article in a semantic
    # article element. Select it before stripping the surrounding global
    # header, navigation, recommendations, and account controls.
    sciencedirect_articles = root.xpath(
        './/article[.//*[@id="abstracts"] and .//*[@id="body"]]'
    )
    if sciencedirect_articles:
        root = copy.deepcopy(
            max(sciencedirect_articles, key=lambda element: len(" ".join(element.itertext())))
        )

    # J-STAGE's downloadable HTML is a complete site shell.  Its article
    # metadata and full scientific content live in two publisher-observed
    # containers, while the surrounding body is navigation, account, sharing,
    # and recommendation UI.  Join only those two article containers before
    # the normal cleanup pass so the archival fragment remains article-only.
    jstage_titles = root.xpath(
        './/*[contains(concat(" ", normalize-space(@class), " "), " global-article-title ")]'
    )
    jstage_contents = root.xpath('.//*[@id="article-overiew-abstract-wrap"]')
    if jstage_titles and jstage_contents:
        overview = jstage_titles[0].xpath(
            'ancestor::div[contains(concat(" ", normalize-space(@class), " "), " col-md-18 ")][1]'
        )
        if overview:
            article_root = html.Element("div")
            article_root.append(copy.deepcopy(overview[0]))
            article_root.append(copy.deepcopy(jstage_contents[0]))
            root = article_root

    # De Gruyter Brill's legacy HTML route contains a publisher-rendered,
    # text-layer article viewer plus separate same-issue recommendations and
    # site chrome. Preserve the article viewer and its exact DOI block only.
    degruyter_views = root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " pdfView ")'
        ' and .//*[@id="articleAbstractView"]]'
    )
    if degruyter_views:
        article_root = html.Element("div")
        doi_blocks = root.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), " doi-isbn ")][1]'
        )
        if doi_blocks:
            article_root.append(copy.deepcopy(doi_blocks[0]))
        article_root.append(copy.deepcopy(degruyter_views[0]))
        root = article_root

    # Oxford Academic's complete article content, including its title,
    # authors, abstract, body, figures, references, and license, is contained
    # in this observed wrapper. Exclude the surrounding navigation and cookie
    # controls before the generic cleanup pass.
    oup_articles = root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " content-inner-wrap ")'
        ' and .//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]]'
    )
    if oup_articles:
        root = copy.deepcopy(oup_articles[0])

    # ACS and older Silverchair/Oxford templates place the article record in
    # a stable ContentColumn container even when no semantic article wrapper
    # is present. It contains the title, body, figures, and references while
    # excluding the journal shell around it.
    content_columns = root.xpath(
        './/*[@id="ContentColumn" and .//h1 and '
        '(.//*[contains(translate(normalize-space(string(.)), '
        '"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "references")] '
        'or .//*[@id="references"])]'
    )
    if content_columns:
        root = copy.deepcopy(
            max(content_columns, key=lambda element: len(" ".join(element.itertext())))
        )

    # Wiley's semantic article encloses the full scientific record while the
    # body also contains journal navigation, cookie controls, and AI widgets.
    wiley_articles = root.xpath(
        './/article[.//*[contains(concat(" ", normalize-space(@class), " "), " article__body ")]]'
    )
    if wiley_articles:
        root = copy.deepcopy(
            max(wiley_articles, key=lambda element: len(" ".join(element.itertext())))
        )

    # Frontiers exposes an article-focused main container that excludes the
    # global journal shell and consent manager while retaining all inline
    # sections, figures, acknowledgments, and references.
    frontiers_mains = root.xpath(
        './/main[contains(concat(" ", normalize-space(@class), " "), " ArticleDetailsV4__main ")]'
    )
    if frontiers_mains:
        root = copy.deepcopy(frontiers_mains[0])
    by_url, by_path = load_assets(manifest_path)
    large_figure_assets: dict[int, dict] = {}
    if large_figure_manifest_path is not None:
        large_figure_manifest = json.loads(
            large_figure_manifest_path.read_text(encoding="utf-8")
        )
        large_figure_assets = {
            int(asset["figure"]): asset
            for asset in large_figure_manifest.get("assets", [])
        }

    removed_elements = 0

    # Remove clearly separable publisher recommendations and browser-extension
    # overlays before stripping their identifying attributes. These are not
    # article content and can otherwise leave unrelated titles, thumbnails, or
    # repeated "Download PDF" labels inside the archival fragment.
    noise_xpaths = (
        './/*[@data-testid="SmallButtonDetails"]',
        # LibKey Nomad injects empty per-citation wrappers after DOI links in
        # the publisher DOM. They are browser-extension UI, not article text.
        './/*[starts-with(@id, "libkey-nomad-")]',
        # ScienceDirect's current article shell includes a duplicate outline,
        # figure-thumbnail list, and table index in a div-based navigation
        # block. The inline article body below already retains each item.
        './/*[@role="navigation" and @aria-label="Table of contents"]',
        # Frontiers duplicates article figures in a separate outline rail.
        # Keep the numbered inline ArticleFigure blocks and discard this
        # navigation-only thumbnail list.
        './/*[contains(concat(" ", normalize-space(@class), " "), " FiguresOutlineWrapper ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " ArticleMetrics ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " AnnouncementCard ")]',
        # ACS repeats each numbered figure in a reveal/modal container after
        # the inline article figure.  The modal carries the same caption and
        # controls and would otherwise duplicate the scientific content.
        './/*[@content-id and starts-with(@id, "fig")]',
        # AACR's Silverchair article markup likewise repeats each inline
        # figure inside a hidden ``fig-modal`` reveal container.
        './/*[contains(concat(" ", normalize-space(@class), " "), " fig-modal ")]',
        # MDPI duplicates every inline figure in a popup gallery and in a
        # hidden display-object section.  Preserve the inline scientific
        # figure blocks and discard these repeated viewer copies.
        './/*[@id="article-popup"]',
        './/section[@id="Figures"]',
        # MDPI keeps a second full-resolution copy of every inline figure in
        # a hidden popup container immediately after the article figure.
        './/*[contains(concat(" ", normalize-space(@class), " "), " html-fig_show ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " c-article-recommendations ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " app-explore-related-subjects ")]',
        './/*[@id="figure-carousel-section"]',
        './/*[@id="Toolbar"]//img',
        './/*[@id="issueInfo-OUP_IssueInfo_Article"]',
        './/table[@aria-hidden="true"]',
        # ScienceDirect appends a cited-by preview containing unrelated
        # downstream article titles after the article references.
        './/*[@id="section-cited-by"]',
        # Oxford's comment form and toolbars are publisher interaction UI,
        # not part of the scientific article record.
        './/*[@id="usercomments"]',
        './/*[@id="divCommentModal"]',
        './/*[@id="Toolbar"]',
        # Older Wiley pages retain cloned article/section navigation panels
        # inside the otherwise correct article container.
        './/*[@id="article_Pop"]',
        './/*[@id="sections_Pop"]',
        './/*[@id="recommended-articles"]',
        './/*[@id="cited-by"]',
        './/*[@id="metrics"]',
        './/nav[.//a[@href="#cited-by"] or .//a[@href="#metrics"]]',
        './/nav[@aria-label="Article navigation and tools"]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " article-row-right ")]',
        # ACS appends non-article metrics, citation, alert, recommendation, and
        # embedded Figshare viewer widgets to the same main container as the
        # scientific article. Preserve the article's own Supporting
        # Information section/link, but discard these publisher UI rails.
        './/*[contains(concat(" ", normalize-space(@class), " "), " artmet-wrapper ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " article-article-cited-by ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " vt-widget-alerts ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " vt-related-content ")]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " widget-ArticleDataSupplements ")]',
        # J-STAGE appends asynchronous UI panels after the complete inline
        # article. They duplicate figures/tables or remain empty placeholders
        # when no supplementary material, citations, or result list exists.
        './/*[@id="figures-tables-wrap"]',
        './/*[@id="supplimentary-materials-wrap"]',
        './/*[@id="resultsanddiscussion-wrap"]',
        './/*[@id="citedby-wrap"]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " ref-tooltip ")]',
        './/article[contains(@class, "frontend-filesViewer")]',
        './/img[contains(translate(@alt, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "orcid")]',
        './/img[contains(translate(@alt, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "crossmark")]',
        './/img[contains(@src, "unknown-user.png")]',
        './/img[translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz") = "plos"]',
        './/img[translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz") = "elsevier"]',
        './/a[contains(concat(" ", normalize-space(@class), " "), " citation--logo ")]//img',
        './/a[contains(concat(" ", normalize-space(@class), " "), " pdf-download ")]//img',
        './/img[starts-with(translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "cover image")]',
        './/img[translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz") = "issue cover"]',
        './/img[translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz") = "arrow"]',
        './/img[translate(normalize-space(@alt), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz") = "pnas logo"]',
        './/img[contains(@src, "-cov150h.")]',
        './/img[contains(@src, "/pb-assets/journal-banners/")]',
        # Author portrait photographs are biographical decoration rather
        # than scientific article figures.
        './/img[contains(@src, "silverchair-cdn.com")'
        ' and (contains(@src, "/bio/") or contains(@src, "_bio."))]',
        './/*[contains(concat(" ", normalize-space(@class), " "), " PeopleList ")]//img',
        # ScienceDirect's publication-details block includes the journal-cover
        # thumbnail inside an explicitly labelled homepage link.  It is site
        # chrome rather than a scientific article image.
        './/a[starts-with(@aria-label, "Homepage for ")]//img',
        # ScienceDirect's article masthead may also retain the journal logo
        # inside the journal-title link (separate from the cover/homepage
        # thumbnail above). It is publication chrome, not scientific content.
        './/a[starts-with(@title, "Go to ") and contains(@title, " on ScienceDirect")]//img',
        # Decorative/loading/account and document-type icons that can sit
        # inside otherwise article-focused publisher containers.
        './/img[@alt="Processing..." or @alt="Advertisement" or @alt="Account"'
        ' or @alt="Facebook icon" or @alt="X icon" or @alt="LinkedIn icon"'
        ' or @alt="Corresponding address" or @alt="Popup Image"'
        ' or @alt="Plum X logo" or @alt="PlumX Metrics Logo"]',
        './/img[starts-with(@alt, "The cover image for ")]',
        './/img[starts-with(@alt, "Creative Common License - ")]',
        './/img[@alt="Supplementary material: PDF"'
        ' or starts-with(@alt, "Download Kang and Dervan supplementary material")]',
        './/*[contains(@class, "gen-ai__settings")]',
    )
    for xpath in noise_xpaths:
        for element in list(root.xpath(xpath)):
            remove_element(element)
            removed_elements += 1

    # ACS currently represents inline figures as div-based widgets.  Promote
    # the article-level wrappers to semantic figure elements before class
    # stripping so the retained archival body has one identifiable figure per
    # embedded scientific image.  This is based only on observed publisher DOM
    # markers; no asset URL is inferred.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " fig ")'
        ' and contains(concat(" ", normalize-space(@class), " "), " fig-section ")]'
    ):
        images = element.xpath('.//img[@path-from-xml]')
        if not images:
            continue
        match = re.match(
            r"^(Figure|Fig\.|Scheme)\s+(\d+)",
            (images[0].get("alt") or "").strip(),
        )
        if not match:
            continue
        element.tag = "figure"
        prefix = "scheme" if match.group(1) == "Scheme" else "figure"
        element.set("id", f"{prefix}-{int(match.group(2))}")

    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " graphical-abstract ")]'
    ):
        if element.xpath('.//img[@path-from-xml]'):
            element.tag = "figure"
            element.set("id", "visual-abstract")

    # ACS sometimes publishes scientific tables as exact rendered image
    # assets rather than native HTML tables. Preserve those observed table
    # wrappers as semantic figures with stable table identifiers so they are
    # independently addressable in the archive.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " table-wrap ")'
        ' and .//div[contains(concat(" ", normalize-space(@class), " "), " fig-graphic ")]//img[@path-from-xml]]'
    ):
        labels = element.xpath(
            './/span[contains(concat(" ", normalize-space(@class), " "), " title-label ")][1]'
        )
        if not labels:
            continue
        match = re.match(r"^Table\s+(\d+)", " ".join(labels[0].itertext()).strip())
        if match:
            element.tag = "figure"
            element.set("id", f"table-{int(match.group(1))}")

    # J-STAGE inline scientific images use numbered div wrappers such as
    # ``id="figure1"``.  Promote those exact observed wrappers while retaining
    # their stable publisher numbering.
    for element in root.xpath(
        './/div[starts-with(@id, "figure") and .//img[starts-with(@src, "./Graphics/figure")]]'
    ):
        match = re.fullmatch(r"figure(\d+)", element.get("id") or "")
        if match:
            element.tag = "figure"
            element.set("id", f"figure-{int(match.group(1))}")

    # Some J-STAGE journal templates use compact observed markers such as
    # ``id="F1"`` with matching ``./Graphics/F1.png`` article assets.
    for element in root.xpath(
        './/div[starts-with(@id, "F") and .//img[starts-with(@src, "./Graphics/F")]]'
    ):
        match = re.fullmatch(r"F(\d+)", element.get("id") or "", re.IGNORECASE)
        if match:
            element.tag = "figure"
            element.set("id", f"figure-{int(match.group(1))}")

    # Wiley already uses a semantic ``figure`` for graphical abstracts, but
    # commonly leaves it without an id.  Give only the publisher-observed
    # ``-toc-`` figure a stable archival id so it remains distinguishable from
    # the numbered article figures after attributes are stripped.
    for element in root.xpath(
        './/figure[not(@id) and .//img[contains(@src, "-toc-")]]'
    ):
        element.set("id", "visual-abstract")

    # A few Wiley figures are split across continuation panels (for example,
    # ``0002a`` and ``0002b``); continuation wrappers may lack an id even
    # though the exact figure-viewer link carries the complete panel label.
    for element in root.xpath('.//figure[not(@id) and .//img]'):
        for link in element.xpath('.//a/@href'):
            match = re.search(r"-fig-0*(\d+)([a-z]*)-", link, re.IGNORECASE)
            if match:
                suffix = match.group(2).lower()
                element.set("id", f"figure-{int(match.group(1))}{suffix}")
                break

    # Elsevier renders some displayed equations and chemical expressions as
    # exact publisher image figures using stable ``-fxN`` asset names. Give
    # those observed semantic wrappers stable archival identifiers.
    for element in root.xpath('.//figure[not(@id) and .//img]'):
        for image_source in element.xpath('.//img/@src | .//img/@data-src'):
            match = re.search(
                r"-fx(\d+)\.(?:sml|jpg|jpeg|png)$", image_source, re.IGNORECASE
            )
            if match:
                element.set("id", f"equation-{int(match.group(1))}")
                break

    # Springer Nature places the stable observed figure marker on the
    # descendant caption node (for example, ``<b id="Fig3">``) while the
    # semantic ``figure`` wrapper itself has no id.  Preserve that numbering
    # on the wrapper before generic attribute stripping.
    for element in root.xpath('.//figure[not(@id) and .//*[@id]]'):
        marker = next(
            (
                match
                for value in element.xpath('.//*[@id]/@id')
                for match in [re.fullmatch(r"Fig(\d+)", value, re.IGNORECASE)]
                if match is not None
            ),
            None,
        )
        if marker is not None:
            element.set("id", f"figure-{int(marker.group(1))}")

    # Beilstein uses semantic figures without wrapper ids.  Its observed
    # graphical-abstract filename and caption prefixes provide stable labels
    # for the scientific images while leaving table-only figure wrappers
    # untouched.
    for element in root.xpath('.//figure[not(@id) and .//img]'):
        image_sources = element.xpath('.//img/@src')
        if any("graphical-abstract" in source for source in image_sources):
            element.set("id", "visual-abstract")
            continue
        caption_text = " ".join(element.xpath('.//figcaption//text()')).strip()
        match = re.match(r"^(Figure|Scheme)\s+(\d+)\s*:", caption_text, re.IGNORECASE)
        if match is not None:
            element.set("id", f"{match.group(1).lower()}-{int(match.group(2))}")

    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " html-fig-wrap ")]'
    ):
        if element.xpath('.//img'):
            element.tag = "figure"

    # JoVE places each numbered scientific image and its caption in a plain
    # paragraph. The image's publisher-authored ``data-alt`` value supplies
    # the stable figure number, so promote that exact observed wrapper before
    # generic data-attribute stripping.
    for element in root.xpath('.//p[.//img[starts-with(@data-alt, "Figure ")]]'):
        markers = element.xpath('.//img[starts-with(@data-alt, "Figure ")]/@data-alt')
        match = re.fullmatch(r"Figure\s+(\d+)", markers[0]) if markers else None
        if match is not None:
            element.tag = "figure"
            element.set("id", f"figure-{int(match.group(1))}")

    # Spandidos uses plain div wrappers whose stable ids begin with ``fN-``.
    # Promote those publisher-observed wrappers so numbered figures remain
    # semantically identifiable after classes and presentation attributes are
    # removed from the archival fragment.
    for element in root.xpath('.//div[starts-with(@id, "f") and .//img]'):
        match = re.match(r"^f(\d+)-", element.get("id") or "", re.IGNORECASE)
        if match is not None:
            element.tag = "figure"
            element.set("id", f"figure-{int(match.group(1))}")

    # Oncotarget wraps each scientific image and its caption in an
    # ``OncoFigure`` div.  The descendant image container exposes the exact
    # publisher figure marker (``F1``, ``F2``, ...), so preserve that observed
    # numbering on a semantic figure wrapper before generic class stripping.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " OncoFigure ") and .//img]'
    ):
        marker = next(
            (
                match
                for value in element.xpath('.//div[starts-with(@id, "F")]/@id')
                for match in [re.fullmatch(r"F(\d+)", value, re.IGNORECASE)]
                if match is not None
            ),
            None,
        )
        if marker is not None:
            element.tag = "figure"
            element.set("id", f"figure-{int(marker.group(1))}")

    # JCI wraps each numbered scientific image in a plain ``div.figure``.
    # The exact publisher figure page link supplies the stable number.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " figure ") and .//img]'
    ):
        marker = next(
            (
                match
                for link in element.xpath('.//a/@href')
                for match in [re.search(r"/figure/(\d+)(?:[/?#]|$)", link)]
                if match is not None
            ),
            None,
        )
        if marker is not None:
            element.tag = "figure"
            element.set("id", f"figure-{int(marker.group(1))}")

    # PLOS article figures and image-rendered tables are div wrappers. Their
    # exact image/DOI links carry the publisher object id (``.g001`` or
    # ``.t001``), which gives us a stable, observed number without relying on
    # surrounding layout text.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " figure ") and .//img]'
    ):
        object_kind = None
        object_number = None
        for link in element.xpath('.//a/@href'):
            match = re.search(r"\.([gt])(\d+)(?:\D|$)", link)
            if match:
                object_kind = "figure" if match.group(1) == "g" else "table"
                object_number = int(match.group(2))
                break
        if object_number is not None:
            element.tag = "figure"
            element.set("id", f"{object_kind}-{object_number}")

    # Frontiers places each inline scientific image inside a button used to
    # open its lightbox. Promote the observed numbered wrapper to a semantic
    # figure and make the image container inert before generic button removal.
    for element in root.xpath(
        './/div[starts-with(@id, "F")'
        ' and contains(concat(" ", normalize-space(@class), " "), " ArticleFigure ")'
        ' and .//img[contains(@src, "/files/Articles/")]]'
    ):
        match = re.fullmatch(r"F(\d+)", element.get("id") or "", re.IGNORECASE)
        if match is not None:
            element.tag = "figure"
            element.set("id", f"figure-{int(match.group(1))}")
        for button in element.xpath(
            './/button[.//img[contains(@src, "/files/Articles/")]]'
        ):
            button.tag = "div"

    # Oxford Academic/Silverchair uses ``div.fig`` wrappers. The exact
    # publisher image filenames carry the observed figure number.
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " fig ") and .//img]'
    ):
        for image_source in element.xpath('.//img/@src | .//img/@data-src'):
            match = re.search(
                r"(?:fig|f)0*(\d+)[a-z]?\.jpe?g(?:[?&#]|$)",
                image_source,
                re.IGNORECASE,
            )
            if match:
                element.tag = "figure"
                element.set("id", f"figure-{int(match.group(1))}")
                break

    # RSC/Silverchair wraps each numbered figure in an article section. The
    # exact publisher XML filename retained on the image supplies the stable
    # number (for example, ``c5cc05104e-f3.tif``).
    for element in root.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " article-section-wrapper ") and .//img[@path-from-xml]]'
    ):
        for image_path in element.xpath('.//img/@path-from-xml'):
            match = re.search(r"-f0*(\d+)\.tiff?$", image_path, re.IGNORECASE)
            if match:
                element.tag = "figure"
                element.set("id", f"figure-{int(match.group(1))}")
                break

    # ScienceDirect renders authors without public profile pages as buttons
    # inside the semantic author group. Preserve those publisher-authored
    # names as inert text while the generic button cleanup below still drops
    # navigation, sharing, and modal controls.
    for element in root.xpath(
        './/*[@id="author-group"]//button[normalize-space(string(.))]'
    ):
        element.tag = "span"

    for tag in UNWRAPPED_TAGS:
        for element in list(root.xpath(f".//{tag}")):
            element.drop_tag()
            removed_elements += 1

    for tag in REMOVED_TAGS:
        for element in list(root.xpath(f".//{tag}")):
            remove_element(element)
            removed_elements += 1

    # Publisher license badges are decorative controls, not scientific
    # content.  Remove them before the image pass so they do not become
    # misleading omitted-image markers in otherwise complete articles.
    for element in list(
        root.xpath(
            './/img[contains(concat(" ", normalize-space(@class), " "), " license-icon ")]'
        )
    ):
        remove_element(element)
        removed_elements += 1

    # Cell Press repeats article figures in collateral/collapsible viewer
    # rails after the semantic numbered figures.  Those copies are smaller
    # responsive derivatives and must not be embedded alongside the exact
    # best-resolution assets already retained in ``figure#figN``.
    for element in list(
        root.xpath(
            './/*[@id="core-collateral-figures" or @id="collapsible-figures_content"]'
        )
    ):
        remove_element(element)
        removed_elements += 1

    embedded_images = 0
    embedded_large_figures: set[int] = set()
    omitted_images = 0
    duplicate_images_removed = 0
    embedded_cache: dict[str, str] = {}
    seen_images: set[tuple[str, str]] = set()
    seen_asset_paths: dict[str, etree._Element] = {}
    seen_payloads: dict[str, etree._Element] = {}
    embedded_alts: set[str] = set()
    for image in list(root.xpath(".//img")):
        alt = (image.get("alt") or "").strip()
        figure_match = re.match(r"^(?:Figure|Fig\.)\s+(\d+)\.? ?", alt)
        figure_number = int(figure_match.group(1)) if figure_match else None

        # PLOS labels article images only as ``thumbnail`` in the rendered
        # DOM, while the enclosing figure exposes an exact original-image
        # link whose article identifier ends in ``.gNNN``. Use that observed
        # publisher link to associate the image with a supplied large-figure
        # asset; no filename or URL pattern is guessed.
        if figure_number is None:
            original_links = image.xpath(
                'ancestor::figure[1]//a[contains(@href, "size=original")]/@href'
                ' | ancestor::div[contains(concat(" ", normalize-space(@class), " "), " figure ")][1]'
                '//a[contains(@href, "size=original")]/@href'
            )
            if original_links:
                plos_match = re.search(r"\.g(\d+)(?:[&#]|$)", original_links[0])
                if plos_match:
                    figure_number = int(plos_match.group(1))
                    if not alt or alt.lower() == "thumbnail":
                        alt = f"Figure {figure_number}."
                        image.set("alt", alt)
        if figure_number is None:
            # Oxford Academic's Silverchair CDN uses stable article figure
            # filenames (for example, ``m_gkz153fig2.jpeg``). Associate that
            # observed number with a supplied full-size publisher asset.
            oup_match = next(
                (
                    match
                    for source_value in (
                        image.get("src", ""),
                        image.get("data-src", ""),
                    )
                    for match in [
                        re.search(
                            r"(?:fig|f)0*(\d+)[a-z]?\.jpe?g(?:[?&#]|$)",
                            source_value,
                            re.IGNORECASE,
                        )
                    ]
                    if match is not None and "silverchair-cdn.com" in source_value
                ),
                None,
            )
            if oup_match is not None:
                figure_number = int(oup_match.group(1))
        if figure_number is None:
            # Older Oxford Academic articles can use descriptive image alt
            # text and opaque GIF filenames while retaining the numbered
            # figure in the surrounding Silverchair ``data-id`` (for
            # example, ``cvn355f1``). Use that publisher-provided identifier
            # to associate the image with the captured full-size asset.
            oup_data_ids = image.xpath(
                'ancestor::div[contains(concat(" ", normalize-space(@class), " "), " fig ")][1]/@data-id'
            )
            if oup_data_ids:
                match = re.search(r"f0*(\d+)$", oup_data_ids[0], re.IGNORECASE)
                if match is not None:
                    figure_number = int(match.group(1))
        if figure_number is None:
            figure_links = image.xpath("ancestor::figure[1]//a/@href")
            wiley_match = next(
                (
                    match
                    for link in figure_links
                    for match in [re.search(r"-fig-0*(\d+)-", link)]
                    if match is not None
                ),
                None,
            )
            if wiley_match is not None:
                figure_number = int(wiley_match.group(1))
                if not alt or alt == "Details are in the caption following the image":
                    alt = f"Figure {figure_number}."
                    image.set("alt", alt)
        if figure_number is None:
            # JCI's thumbnail alt text is the figure title rather than its
            # number; the promoted wrapper retains the observed figure id.
            jci_wrapper = image.xpath('ancestor::figure[starts-with(@id, "figure-")][1]/@id')
            if jci_wrapper:
                match = re.fullmatch(r"figure-(\d+)", jci_wrapper[0])
                if match is not None:
                    figure_number = int(match.group(1))
        if figure_number is None:
            # Cell Press figures use descriptive alt text while the semantic
            # figure wrapper retains the exact numbered id (for example,
            # ``fig1``). Use that publisher-observed id to associate the
            # image with an explicitly captured large-figure asset.
            cell_wrapper = image.xpath(
                'ancestor::figure[starts-with(translate(@id, "FIG", "fig"), "fig")][1]/@id'
            )
            if cell_wrapper:
                match = re.fullmatch(r"fig(\d+)", cell_wrapper[0], re.IGNORECASE)
                if match is not None:
                    figure_number = int(match.group(1))
        asset = large_figure_assets.get(figure_number) if figure_number else None
        candidates = [
            image.get("src", ""),
            image.get("data-src", ""),
            image.get("data-original", ""),
            image.get("data-lg-src", ""),
        ]
        # Wiley responsive figures commonly keep the exact larger asset in
        # ``data-lg-src``, a sibling ``source[srcset]``, or the enclosing
        # figure-viewer link while ``img[src]`` points at a smaller PNG.
        # These are all publisher-observed URLs from the captured DOM; asset
        # lookup below still requires an exact manifest match.
        for srcset in image.xpath("ancestor::picture[1]/source/@srcset"):
            candidates.extend(
                part.strip().split()[0]
                for part in srcset.split(",")
                if part.strip()
            )
        candidates.extend(image.xpath("ancestor::a[1]/@href"))
        # Multi-panel ScienceDirect figures can place several images in one
        # semantic figure.  Each panel has its own immediate span containing
        # the exact high-resolution download links, so consider that observed
        # local wrapper before the enclosing figure's links.
        candidates.extend(image.xpath("ancestor::span[1]//a/@href"))
        # Elsevier places its exact high-resolution download control beside
        # the rendered image within the same figure rather than around it.
        # Consider those observed figure links too; only manifest-listed
        # assets can match.
        candidates.extend(image.xpath("ancestor::figure[1]//a/@href"))
        candidates.extend(
            image.xpath(
                'ancestor::div[contains(concat(" ", normalize-space(@class), " "), " figure ")][1]//a/@href'
            )
        )
        if asset is None:
            asset = next(
                (
                    found
                    for candidate in candidates
                    if candidate
                    for found in [find_asset(candidate, base_url, by_url, by_path)]
                    if found is not None
                ),
                None,
            )

        if asset is None:
            if alt and alt in embedded_alts:
                remove_element(image)
                duplicate_images_removed += 1
                continue
            if alt:
                replacement = html.Element("span")
                replacement.text = f"[Image omitted: {alt}]"
                replacement.tail = image.tail
                image.getparent().replace(image, replacement)
            else:
                remove_element(image)
            omitted_images += 1
            continue

        asset_path = str(asset["path"])
        # Some Elsevier/JBC pages duplicate every scientific image in a
        # second responsive-viewer tree without providing useful alt text.
        # A manifest asset is a single publisher image, so retain only its
        # first article-body occurrence.
        if asset_path in seen_asset_paths:
            prior = seen_asset_paths[asset_path]
            if alt and not (prior.get("alt") or "").strip():
                prior.set("alt", alt)
            remove_element(image)
            duplicate_images_removed += 1
            continue
        seen_asset_paths[asset_path] = image
        image_key = (asset_path, alt)
        if alt and image_key in seen_images:
            remove_element(image)
            duplicate_images_removed += 1
            continue
        seen_images.add(image_key)

        data_url = embedded_cache.get(asset_path)
        if data_url is None:
            payload = Path(asset_path).read_bytes()
            if not payload:
                raise ValueError(f"Empty image asset: {asset_path}")
            payload_digest = hashlib.sha256(payload).hexdigest()
            if payload_digest in seen_payloads:
                prior = seen_payloads[payload_digest]
                if alt and not (prior.get("alt") or "").strip():
                    prior.set("alt", alt)
                remove_element(image)
                duplicate_images_removed += 1
                continue
            seen_payloads[payload_digest] = image
            content_type = asset.get("contentType") or "application/octet-stream"
            encoded = base64.b64encode(payload).decode("ascii")
            data_url = f"data:{content_type};base64,{encoded}"
            embedded_cache[asset_path] = data_url

        image.set("src", data_url)
        embedded_images += 1
        if alt:
            embedded_alts.add(alt)
        if figure_number is not None and figure_number in large_figure_assets:
            embedded_large_figures.add(figure_number)

    removed_attributes = 0
    event_attribute = re.compile(r"^on", re.IGNORECASE)
    for element in root.iter():
        for attribute in list(element.attrib):
            lower = attribute.lower()
            value = element.attrib[attribute]
            remove = (
                lower in {"class", "style", "srcset", "sizes", "loading", "decoding"}
                or lower.startswith("data-")
                or event_attribute.match(lower) is not None
                or (lower in {"href", "src", "action", "formaction"} and value.strip().lower().startswith("javascript:"))
            )
            if remove:
                del element.attrib[attribute]
                removed_attributes += 1

    output = serialize_body_fragment(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8", newline="\n")

    return {
        "source_bytes": len(source.encode("utf-8")),
        "output_bytes": len(output.encode("utf-8")),
        "removed_elements": removed_elements,
        "removed_attributes": removed_attributes,
        "embedded_images": embedded_images,
        "embedded_large_figures": len(embedded_large_figures),
        "unique_embedded_assets": len(embedded_cache),
        "omitted_images": omitted_images,
        "duplicate_images_removed": duplicate_images_removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--large-figure-manifest", type=Path)
    args = parser.parse_args()

    stats = clean_html(
        args.input,
        args.manifest,
        args.output,
        args.base_url,
        args.large_figure_manifest,
    )
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
