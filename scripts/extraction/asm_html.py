"""Narrow support for archived ASM semantic article snapshots."""
from __future__ import annotations

import copy
import re
from urllib.parse import parse_qs, urlsplit

from lxml import etree


def article_root(document):
    from .html_extractor import plain_text

    roots = [document] if document.tag == 'article' else document.xpath('.//article')
    if len(roots) != 1:
        return None
    root = roots[0]
    titles = root.xpath('./header//h1[@property="name"]')
    dois = [a for a in root.xpath('./header//a[@property="sameAs"]')
            if re.fullmatch(r'https://doi\.org/10\.1128/\S+', a.get('href', ''), re.I)
            and plain_text(a) == a.get('href')]
    bodies = root.xpath('./div/section[@id="bodymatter" and @property="articleBody" and @typeof="Text"]')
    abstracts = root.xpath('./div/div[@id="abstracts"]//section[@id="abstract" and @role="doc-abstract" and @property="abstract"]')
    bibliographies = root.xpath('./div/section[@id="backmatter"]/div/section[@id="bibliography" and @role="doc-bibliography"]')
    if not all(len(items) == 1 for items in (titles, dois, bodies, abstracts, bibliographies)):
        return None
    if not plain_text(titles[0]):
        return None
    items = bibliographies[0].xpath('./div/div')
    if not items:
        return None
    reference_prefix = None
    reference_text_by_id = {}
    for item in items:
        reference_id = item[1].get('id', '') if len(item) == 2 else ''
        match = re.fullmatch(r'([BR])(\d+)', reference_id)
        if not reference_text_by_id and match:
            reference_prefix = match.group(1)
        number = int(match.group(2)) if match else -1
        if (len(item) != 2 or plain_text(item[0]) != f'{number}.'
                or reference_prefix is None or reference_id != f'{reference_prefix}{number}'):
            return None
        parts = item[1].xpath('./div/div')
        if len(parts) != 2 or not plain_text(parts[0]):
            return None
        reference_text = plain_text(parts[0])
        if reference_id in reference_text_by_id:
            if reference_text_by_id[reference_id] != reference_text:
                return None
            continue
        if number != len(reference_text_by_id) + 1:
            return None
        reference_text_by_id[reference_id] = reference_text
    figures = bodies[0].xpath('.//figure[starts-with(@id, "F")]')
    for number, figure in enumerate(figures, 1):
        captions = figure.xpath('./figcaption')
        if figure.get('id') != f'F{number}' or len(captions) != 1:
            return None
        labels = captions[0].xpath('./span[1]')
        if len(labels) != 1 or not re.fullmatch(rf'FIG\.?\s*{number}', plain_text(labels[0]), re.I):
            return None
        if not figure.xpath('.//img[@src]'):
            return None
    return root


def raster_table_details(element):
    """Recognize one image-only table in an authenticated ASM article."""

    from .html_extractor import plain_text

    if element.tag != 'figure':
        return None
    identifier = re.fullmatch(r'T([1-9]\d*)', element.get('id') or '')
    root = article_root(element.getroottree().getroot())
    if identifier is None or root is None or not any(
            ancestor is root for ancestor in element.iterancestors()):
        return None
    number = int(identifier.group(1))
    captions = element.xpath('./figcaption')
    images = element.xpath('./img[starts-with(@src, "data:image/")]')
    notes = element.xpath('./div')
    labels = captions[0].xpath('./span[1]') if len(captions) == 1 else []
    if (
        len(captions) != 1
        or len(images) != 1
        or len(notes) > 1
        or len(labels) != 1
        or re.fullmatch(rf'TABLE\s*{number}', plain_text(labels[0]), re.I) is None
        or not plain_text(captions[0])
    ):
        return None
    if notes:
        note_labels = {
            plain_text(node).strip('[]() .:')
            for node in notes[0].xpath('.//*[@id] | .//sup')
        }
        caption_labels = {
            plain_text(node).strip('[]() .:')
            for node in captions[0].xpath('.//a[@role="doc-noteref"]//sup')
        }
        if caption_labels and not caption_labels.issubset(note_labels):
            return None
    return {
        'number': number,
        'source_id': element.get('id'),
        'title': captions[0],
        'image': images[0],
        'notes': notes,
    }


def semantic_table_details(element):
    """Recognize one native semantic table in an authenticated ASM article."""

    from .html_extractor import plain_text

    if element.tag != 'figure':
        return None
    identifier = re.fullmatch(r'T([1-9]\d*)', element.get('id') or '')
    root = article_root(element.getroottree().getroot())
    if identifier is None or root is None or not any(
            ancestor is root for ancestor in element.iterancestors()):
        return None
    number = int(identifier.group(1))
    captions = element.xpath('./figcaption')
    tables = element.xpath('./div/table')
    labels = captions[0].xpath('./span[1]') if len(captions) == 1 else []
    if (
        len(captions) != 1
        or len(tables) != 1
        or len(labels) != 1
        or re.fullmatch(rf'TABLE\s*{number}', plain_text(labels[0]), re.I) is None
        or not plain_text(captions[0])
        or not tables[0].xpath('./thead/tr | ./tbody/tr | ./tr')
    ):
        return None
    note_rows = []
    for note in element.xpath('./div/div[@role="doc-footnote"]'):
        note_children = [child for child in note if isinstance(child.tag, str)]
        if len(note_children) != 2:
            return None
        label = ''.join(note_children[0].itertext()).strip('[]() .:')
        body = note_children[1]
        if (
            re.fullmatch(r'[A-Za-z0-9*†‡§‖]+', label) is None
            or body.get('id') != f'T{number}F{len(note_rows) + 1}'
            or body.get('role') != 'paragraph'
            or not plain_text(body)
        ):
            return None
        note_rows.append({'label': label, 'body': body})
    caption_labels = [
        ''.join(node.itertext()).strip('[]() .:')
        for node in captions[0].xpath('.//a[@role="doc-noteref"]//sup')
    ]
    if caption_labels and any(
            label not in {note['label'] for note in note_rows}
            for label in caption_labels):
        return None
    return {
        'number': number,
        'source_id': element.get('id'),
        'title': captions[0],
        'table': tables[0],
        'notes': note_rows,
    }


def supplement_download_list(element):
    """Identify the publisher download-control list, not article prose."""

    from .html_extractor import plain_text

    if element.tag != 'ul':
        return False
    root = article_root(element.getroottree().getroot())
    if root is None or not any(ancestor is root for ancestor in element.iterancestors()):
        return False
    sections = element.xpath('ancestor::section[@id="supplementary-materials"]')
    parent = element.getparent()
    descriptor = parent.getprevious() if parent is not None else None
    list_item = parent.getparent() if parent is not None else None
    items = element.xpath('./li')
    anchors = items[0].xpath('./a[@href and @download]') if items else []
    return bool(
        len(sections) == 1
        and parent is not None
        and descriptor is not None
        and list_item is not None
        and list_item.get('role') == 'listitem'
        and len(items) == 2
        and len(anchors) == 1
        and plain_text(anchors[0]).casefold() == 'download'
        and re.fullmatch(r'\d+(?:\.\d+)?\s+(?:kb|mb|gb)', plain_text(items[1]), re.I)
        and descriptor.xpath('./span[1]')
        and plain_text(descriptor.xpath('./span[1]')[0]).casefold() == 'file'
        and descriptor.xpath('./span[2]')
        and plain_text(descriptor.xpath('./span[2]')[0]).startswith('(')
    )


def normalize(document):
    root = article_root(document)
    if root is None:
        return
    root.set('data-extraction-dialect', 'asm-semantic')
    scopes = root.xpath('./div/div[@id="abstracts"] | ./div/section[@id="bodymatter"]')
    for scope in scopes:
        for paragraph in scope.xpath('.//div[@role="paragraph"]'):
            # Figure bundles are structural, including publisher continued labels.
            # Do not turn the entire bundle into duplicate narrative captions.
            if not paragraph.xpath('.//figure'):
                paragraph.tag = 'p'
    body = root.xpath('./div/section[@id="bodymatter"]')[0]
    wrappers = body.xpath('./div')
    if len(wrappers) == 1 and len(wrappers[0]) and wrappers[0][0].tag == 'p':
        heading = etree.Element('h2')
        heading.text = 'Main text'
        wrappers[0].insert(0, heading)


def figure_caption(figure):
    from .html_extractor import plain_text, render_inline

    root = article_root(figure.getroottree().getroot())
    if root is None or not any(parent is root for parent in figure.iterancestors()):
        return None
    captions = figure.xpath('./figcaption')
    if len(captions) != 1:
        return None
    caption = copy.deepcopy(captions[0])
    label = caption[0]
    number = figure.get('id', '').removeprefix('F')
    caption.text = (label.tail or '').lstrip('. ')
    caption.remove(label)
    return f'Figure {number}', render_inline(caption), plain_text(caption)


def references(root, source_path):
    from .html_extractor import plain_text, render_inline
    from .models import ContentBlock

    if article_root(root.getroottree().getroot()) is not root:
        return None
    article_doi = root.xpath('./header//a[@property="sameAs"]')[0].get('href', '').removeprefix('https://doi.org/')
    result = []
    grouped = {}
    for item in root.xpath('./div/section[@id="backmatter"]/div/section[@id="bibliography"]/div/div'):
        grouped.setdefault(item[1].get('id'), []).append(item)
    for reference_id, candidates in grouped.items():
        def direct_doi_count(candidate):
            controls = candidate[1].xpath('./div/div')[1]
            return sum(
                1 for anchor in controls.xpath('.//a[@href]')
                if re.fullmatch(r'https://doi\.org/10\.\d{4,9}/\S+', anchor.get('href', ''), re.I)
            )
        item = max(candidates, key=direct_doi_count)
        number = int(re.fullmatch(r'[BR](\d+)', reference_id).group(1))
        authored, controls = item[1].xpath('./div/div')
        rendered, visible = render_inline(authored), plain_text(authored)
        urls = set()
        for anchor in controls.xpath('.//a[@href]'):
            href = anchor.get('href', '')
            if re.fullmatch(r'https://doi\.org/10\.\d{4,9}/\S+', href, re.I):
                urls.add(href)
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            if (parsed.path == '/servlet/linkout' and not parsed.scheme and not parsed.netloc
                    and query.get('doi', [''])[0].casefold() == article_doi.casefold()
                    and query.get('dbid') == ['4'] and query.get('site') == ['asmj']
                    and len(query.get('key', [])) == 1
                    and re.fullmatch(r'10\.\d{4,9}/\S+', query['key'][0], re.I)):
                urls.add('https://doi.org/' + query['key'][0])
        for url in sorted(urls):
            rendered += f' DOI: {url}'
            visible += f' DOI: {url}'
        result.append(ContentBlock(block_id=f'reference-{number:03d}', kind='reference',
                                   markdown=f'{number}. {rendered}', plain_text=f'{number}. {visible}',
                                   source_path=source_path, source_locator=item.getroottree().getpath(item)))
    return result


def front_matter(document, source_path):
    from .html_extractor import plain_text, _safe_text
    from .models import ContentBlock

    root = article_root(document)
    if root is None:
        return []
    blocks = []

    def add(label, value, node):
        if value:
            blocks.append(ContentBlock(block_id=f'asm-front-matter-{len(blocks)+1:03d}', kind='front_matter',
                          markdown=f'**{label}:** {_safe_text(value, markup="markdown")}',
                          plain_text=f'{label}: {value}', source_path=source_path,
                          source_locator=node.getroottree().getpath(node)))

    for author in root.xpath('.//section[@id="tab-contributors"]/section/div[@property="author" and @typeof="Person"]'):
        names = author.xpath('.//h5/span[@property="givenName"] | .//h5/span[@property="familyName"]')
        affiliations = author.xpath('.//div[@property="affiliation" and @typeof="Organization"]/span[@property="name"]')
        if names and affiliations:
            name = ' '.join(plain_text(n) for n in names)
            add(f'Affiliation — {name}', '; '.join(plain_text(n) for n in affiliations), author)
    for info in root.xpath('.//section[@id="tab-information"]/section'):
        headings = info.xpath('./h4')
        if len(headings) != 1:
            continue
        heading = plain_text(headings[0])
        if heading == 'Copyright':
            for node in info.xpath('./div[@role="paragraph"]'):
                add('Copyright', plain_text(node), node)
        elif heading == 'History':
            values = [plain_text(n) for n in info.xpath('./div')]
            add('Publication history', '; '.join(values), info)
        elif heading == 'Published In':
            properties = ['name', 'volumeNumber', 'issueNumber', 'datePublished', 'pageStart', 'pageEnd']
            groups = [info.xpath(f'.//span[@property="{prop}"]') for prop in properties]
            if all(len(group) == 1 for group in groups):
                journal, volume, issue, date, start, end = [plain_text(g[0]) for g in groups]
                add('Published in', f'{journal}; volume {volume}; issue {issue}; {date}; pages {start}–{end}', info)
    return blocks
