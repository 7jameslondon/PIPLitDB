from pathlib import Path
import shutil
import subprocess
import unittest


class ViewerTablePartIdTests(unittest.TestCase):
    def test_machine_part_ids_never_become_display_headings(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable')
        source = (Path(__file__).resolve().parents[1] / 'extraction_viewer.html').read_text(encoding='utf-8')
        start = source.index('    function renderTablePart(')
        end = source.index('    function renderRemainingTableStructures(', start)
        script = r'''
const assert = require('node:assert/strict');
const element = (tag, cls = '', text = '') => ({tag, cls, children: [text], append(...children) {this.children.push(...children);}});
const asArray = value => Array.isArray(value) ? value : [];
const firstDefined = (...values) => values.find(value => value !== undefined && value !== null);
const richFragment = value => value;
const sanitizeMarkup = value => value;
const tableCellStructure = () => null;
const visibleText = node => typeof node === 'string' ? node : node.children.map(visibleText).join(' ');
const nodes = node => typeof node === 'string' ? [] : [node, ...node.children.flatMap(nodes)];
''' + source[start:end] + r'''
for (const identifier of ['synthetic-fragment-01', '', undefined]) {
  const part = {part_id: identifier, rows: [
    [{text:'(A) Authored group', html:'<em>(A) Authored group</em>', header:true, colspan:2, rowspan:1}],
    [{text:'Measurement', header:true}, {text:'Value', header:true}],
    [{text:'X', header:false}, {text:'1.25', header:false}]
  ]};
  const before = JSON.stringify(part);
  for (const total of [1, 2]) {
    const result = renderTablePart(part, 0, total, {}, new Set());
    const text = visibleText(result);
    assert.ok(!text.includes('synthetic-fragment-01'), text);
    assert.ok(!text.includes('Part 1'), text);
    assert.equal(nodes(result).filter(node => node.cls === 'table-part-label').length, 0);
    assert.ok(text.includes('(A) Authored group'), text);
    assert.ok(text.includes('Measurement') && text.includes('Value') && text.includes('1.25'), text);
    const cells = nodes(result).filter(node => node.tag === 'th' || node.tag === 'td');
    assert.equal(cells.length, 5);
    assert.equal(cells[0].colSpan, 2);
    assert.equal(cells[0].scope, 'col');
    assert.equal(nodes(result).filter(node => node.tag === 'tr').length, 3);
    assert.equal(JSON.stringify(part), before);
  }
}
'''
        result = subprocess.run([node, '-e', script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
