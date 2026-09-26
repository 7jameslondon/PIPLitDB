"""Hash-pinned selection of archived HTML pixels for reviewed PDF figures."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from lxml import html

from .html_extractor import _decode_data_image
from .models import EmbeddedAsset


def select_reviewed_html_assets(specs, crop_specs, sources, supplements):
    """Replace only an existing reviewed supplemental crop's image source.

    Captions remain at their reviewed PDF locators. Original HTML bytes and
    their own source locator/hash are recorded independently in the asset.
    No network requests, conversions, source edits, or ownership inference.
    """
    sources_by_path = {s.relative_path: s for s in sources}
    crops_by_id = {s['asset_id']: s for s in crop_specs}
    figures = [f for supplement in supplements for f in supplement.figures]
    selected = set(); pending = []
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError('reviewed_html_assets entries must be mappings')
        asset_id = spec.get('asset_id')
        crop = crops_by_id.get(asset_id)
        matches = [f for f in figures if f.figure_id == asset_id]
        if (asset_id in selected or crop is None or len(matches) != 1
                or crop.get('category') != 'supplement_figure'):
            raise ValueError('reviewed HTML asset must identify one existing supplemental crop')
        source = sources_by_path.get(spec.get('source_path'))
        if source is None or source.detected_format != 'text/html':
            raise ValueError('reviewed HTML asset requires a discovered HTML source')
        data = source.path.read_bytes()
        if (spec.get('source_sha256') != source.sha256
                or hashlib.sha256(data).hexdigest() != source.sha256):
            raise ValueError('reviewed HTML source hash mismatch')
        if not all(isinstance(spec.get(k), str) and spec[k].strip()
                   for k in ('xpath', 'reason', 'evidence', 'image_sha256', 'output_path')):
            raise ValueError('reviewed HTML asset requires locator, hashes, reason and evidence')
        document = html.fromstring(data.decode('utf-8'))
        nodes = document.xpath(spec['xpath'])
        if len(nodes) != 1 or getattr(nodes[0], 'tag', None) != 'img':
            raise ValueError('reviewed HTML image selector must match exactly one img')
        media_type, extension, pixels = _decode_data_image(nodes[0].get('src', ''))
        if hashlib.sha256(pixels).hexdigest() != spec['image_sha256']:
            raise ValueError('reviewed HTML image hash mismatch')
        if PurePosixPath(spec['output_path']).suffix != '.' + extension:
            raise ValueError('reviewed HTML output extension must match original bytes')
        pending.append(EmbeddedAsset(
            asset_id=asset_id, category='supplement_figure', label=matches[0].label,
            media_type=media_type, output_path=spec['output_path'], data=pixels,
            source_path=source.relative_path,
            source_locator=document.getroottree().getpath(nodes[0]),
        ))
        selected.add(asset_id)
    return pending, [s for s in crop_specs if s['asset_id'] not in selected]
