"""Lossless semantic extraction of CIF 1.x scalar fields and loop tables.

Numeric values, uncertainty suffixes, missing-value tokens, and CIF's own
scientific text escapes remain literal. This is not a crystal-model interpreter.
Unsupported STAR save frames and CIF 2.0 constructs fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .csv_supplement import _markup
from .models import ContentBlock, SourceFile, TableCell, TableItem, TablePart


@dataclass(frozen=True)
class Token:
    value: str
    line: int
    quoted: bool = False


def _tokens(text: str) -> tuple[list[Token], list[tuple[int, str]]]:
    if '\x00' in text or text.startswith('#\\#CIF_2.0'):
        raise ValueError('CIF requires a supported CIF 1.x text stream')
    lines = text.splitlines()
    tokens: list[Token] = []
    comments: list[tuple[int, str]] = []
    row = 0
    while row < len(lines):
        line = lines[row]
        number = row + 1
        if line.startswith(';'):
            value = [line[1:]]
            row += 1
            while row < len(lines) and not lines[row].startswith(';'):
                value.append(lines[row])
                row += 1
            if row == len(lines) or lines[row][1:].strip():
                raise ValueError(f'unterminated or invalid CIF text field at line {number}')
            tokens.append(Token('\n'.join(value), number, True))
            row += 1
            continue
        col = 0
        while col < len(line):
            if line[col].isspace():
                col += 1
                continue
            if line[col] == '#':
                comments.append((number, line[col:]))
                break
            start = col
            if line[col] in "\"'":
                quote = line[col]
                col += 1
                start = col
                while col < len(line) and not (line[col] == quote and (col+1 == len(line) or line[col+1].isspace())):
                    col += 1
                if col == len(line):
                    raise ValueError(f'unterminated CIF quoted value at line {number}')
                tokens.append(Token(line[start:col], number, True))
                col += 1
            else:
                while col < len(line) and not line[col].isspace():
                    col += 1
                tokens.append(Token(line[start:col], number))
        row += 1
    return tokens, comments


def _control(token: Token) -> bool:
    value = token.value.casefold()
    return not token.quoted and (value.startswith(('_', 'data_', 'save_')) or value in {'loop_', 'stop_', 'global_'})


def parse_cif(text: str):
    """Return ordered (name, scalar pairs, loop columns/rows) data blocks."""
    tokens, comments = _tokens(text)
    blocks = []
    names: set[str] = set()
    seen: set[str] = set()
    block = None
    pos = 0
    while pos < len(tokens):
        token = tokens[pos]
        key = token.value.casefold()
        if not token.quoted and key.startswith('data_'):
            if len(key) == 5 or key in names:
                raise ValueError('empty or duplicate CIF data block')
            names.add(key)
            block = {'name': token.value[5:], 'line': token.line, 'scalars': [], 'loops': []}
            blocks.append(block)
            seen = set()
            pos += 1
        elif block is None:
            raise ValueError('CIF content must begin with a data block')
        elif not token.quoted and key == 'loop_':
            start = token.line
            pos += 1
            tags = []
            while pos < len(tokens) and not tokens[pos].quoted and tokens[pos].value.startswith('_'):
                tag = tokens[pos].value
                if tag.casefold() in seen:
                    raise ValueError(f'duplicate CIF tag {tag}')
                seen.add(tag.casefold())
                tags.append(tag)
                pos += 1
            values = []
            while pos < len(tokens) and not _control(tokens[pos]):
                values.append(tokens[pos].value)
                pos += 1
            if not tags or not values or len(values) % len(tags):
                raise ValueError(f'incomplete CIF loop at line {start}')
            block['loops'].append((start, tags, [values[i:i+len(tags)] for i in range(0,len(values),len(tags))]))
        elif not token.quoted and key.startswith('_'):
            if key in seen or pos+1 == len(tokens) or _control(tokens[pos+1]):
                raise ValueError(f'duplicate or valueless CIF scalar {token.value}')
            seen.add(key)
            block['scalars'].append((token.line, token.value, tokens[pos+1].value))
            pos += 2
        else:
            raise ValueError(f'unsupported CIF token at line {token.line}: {token.value}')
    if not blocks:
        raise ValueError('CIF contains no data blocks')
    return blocks, comments


def is_cif_supplement(source: SourceFile) -> bool:
    return source.path.suffix.casefold() == '.cif' and source.detected_format in {
        'chemical/x-cif', 'chemical/x-mmcif', 'text/plain', 'application/octet-stream'
    }


def extract_cif_supplement(source: SourceFile, supplement_id: str, *, cif_path: Path):
    """Expose every scalar and loop cell, preserving the byte-identical original."""
    decoded = cif_path.read_bytes().decode('utf-8-sig', errors='strict')
    data, comments = parse_cif(decoded)
    blocks = []
    tables = []
    if comments:
        text = '\n'.join(value for _, value in comments)
        blocks.append(ContentBlock(f'{supplement_id}-cif-comments','text',_markup(text),text,
                                   source.relative_path,'cif;source-comments'))
    for bi, block in enumerate(data,1):
        name = block['name']
        label = f'CIF data_{name}'
        blocks.append(ContentBlock(f'{supplement_id}-cif-{bi:03}','subsection_heading',_markup(label),label,
                                   source.relative_path,f'cif;line={block["line"]};data={name}'))
        datasets = []
        if block['scalars']:
            datasets.append((block['line'],['CIF tag','Source value'],[[tag,value] for _,tag,value in block['scalars']], 'scalar fields'))
        datasets.extend((line,tags,values,f'loop {li}') for li,(line,tags,values) in enumerate(block['loops'],1))
        for ti,(line,tags,values,kind) in enumerate(datasets,1):
            table_id=f'{supplement_id}_cif_{bi:03}_table_{ti:03}'
            title=f'{label}: {kind}'
            rows=[[TableCell(v,_markup(v),True) for v in tags]]+[[TableCell(v,_markup(v),False) for v in row] for row in values]
            tables.append(TableItem(table_id,table_id,title,_markup(title),title,[TablePart(table_id+'_part_001',rows)],[],[],
                                    source.relative_path,f'cif;line={line};data={name};{kind}',source_kind='document'))
    return blocks,tables,[]
