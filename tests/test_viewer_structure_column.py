import json
from pathlib import Path
import shutil
import subprocess
import unittest


class ViewerStructureColumnTests(unittest.TestCase):
    def test_numeric_measurements_do_not_receive_row_structure(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable')
        source = (Path(__file__).resolve().parents[1] / 'extraction_viewer.html').read_text(encoding='utf-8')
        start = source.index('    function tableCellStructure(')
        end = source.index('    function renderTablePart(', start)
        script = '''const assert = require('node:assert/strict');
const richPlain = String;
const firstDefined = (...values) => values.find(value => value !== undefined && value !== null);
const element = () => ({append() {}});
const resolveForNode = () => {};
''' + source[start:end] + '''
const seen = new Set();
assert.equal(tableCellStructure({text:'1'}, {'1':'one.png'}, seen, 3), null);
assert.equal(seen.size, 0);
assert.equal(tableCellStructure({text:'1', header:true}, {'1':'one.png'}, seen, 0), null);
assert.ok(tableCellStructure({text:'1'}, {'1':'one.png'}, seen, 0));
assert.deepEqual([...seen], ['1']);
const graphical = {'part_01_row_008_column_002':'cell.png',
 'part_01_row_008_column_003_image_01':'a.png',
 'part_01_row_008_column_003_image_02':'b.png'};
assert.ok(tableCellStructure({text:''}, graphical, seen, 1, 0, 7));
assert.ok(seen.has('part_01_row_008_column_002'));
assert.equal(tableCellStructure({text:''}, graphical, seen, 1, 0, 6), null);
assert.equal(tableCellStructure({text:''}, graphical, seen, 1, 1, 7), null);
assert.ok(tableCellStructure({text:'',header:true}, graphical, seen, 2, 0, 7));
assert.ok(seen.has('part_01_row_008_column_003_image_01'));
assert.ok(seen.has('part_01_row_008_column_003_image_02'));
'''
        result = subprocess.run([node, '-e', script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tableCellStructure(cell, structureAssets, displayedStructures, columnIndex, index, rowIndex)', source)
