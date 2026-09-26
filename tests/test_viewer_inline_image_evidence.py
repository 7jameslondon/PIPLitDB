"""The browser evidence runner must not hide table/inline scientific images."""
from pathlib import Path
import subprocess
import unittest

from scripts import check_extraction_completion as checks


class InlineImageEvidenceTests(unittest.TestCase):
    def test_inline_images_decode_without_being_parked(self):
        adapter = Path(checks.__file__).with_name('check_extraction_viewer.cjs').resolve()
        program = r'''
const assert = require('node:assert/strict');
const {parkedImageSelector,decodeInlineImages} = require(process.argv[1]);
assert.equal(parkedImageSelector, '.media-card img.zoomable-image');
(async () => {
  const inline = {src:'blob:sequence', naturalWidth:156, naturalHeight:34, alt:'PA sequence',
    closest:selector => {assert.equal(selector,'.media-card'); return null;},
    decode:async function() {assert.equal(this.loading,'eager');this.decoded=true;}};
  const media = {closest:() => ({}), decode:async () => {throw Error('must stay parked');}};
  assert.deepEqual(await decodeInlineImages([inline,media]),
    [{width:156,height:34,alt:'PA sequence'}]);
  assert.equal(inline.src,'blob:sequence');
  assert.equal(inline.decoded,true);
  assert.equal(inline.hidden,undefined);
  await assert.rejects(decodeInlineImages([{...inline,naturalWidth:0}]), /did not decode/);
  await assert.rejects(decodeInlineImages([{...inline,decode:async()=>{throw Error('corrupt')}}]), /corrupt/);
})().catch(error => {console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([checks._node_executable(None), '-e', program, str(adapter)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
