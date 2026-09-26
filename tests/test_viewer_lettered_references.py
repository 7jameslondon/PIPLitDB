from pathlib import Path
import shutil
import subprocess
import unittest


class ViewerLetteredReferencesTests(unittest.TestCase):
    def test_authored_reference_labels_suppress_only_generated_counter(self):
        node = shutil.which('node')
        if not node:
            bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
            if bundled.is_file():
                node = str(bundled)
        self.assertIsNotNone(node, 'Node.js is required to exercise the viewer renderer')
        source = (Path(__file__).resolve().parents[1] / 'extraction_viewer.html').read_text(encoding='utf-8')
        start = source.index('    function renderReferences(')
        end = source.index('    function renderContents(', start)
        script = '''const assert = require('node:assert/strict');
const asArray = value => value;
const richPlain = value => value;
const blockContent = value => value.content;
const renderBlock = value => value;
const createElement = (tag, cls, text) => ({tag, cls, text, dataset:{}, children:[],
  classes:[], classList:{add(value) { this.owner.classes.push(value); }},
  append(...items) { this.children.push(...items); }});
const element = (...args) => {
const node=createElement(...args); node.classList.owner=node; return node;
};
''' + source[start:end] + '''
const labels = ['1. First', '2a. Part A', '2b. Part B', '[3a] Bracketed',
 '4B) Uppercase part', 'Unnumbered author', '2024 article without label punctuation'];
const body=element('div');
renderReferences({references:labels.map(content=>({content}))},body);
const items=body.children[0].children[1].children;
assert.equal(items.length,labels.length);
items.forEach((item,index)=>{
 assert.equal(item.classes.includes('source-numbered'), index<5);
 assert.equal(item.children[0].content,labels[index]);
});
'''
        result = subprocess.run([node, '-e', script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
