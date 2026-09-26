// Local, read-only browser adapter for check_extraction_completion.py.
// The application is unchanged; its directory picker receives only the selected
// record's canonical files. Diagnostics/screenshots are not sent into the page.
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const assert = require('node:assert/strict');

const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const mime = name => ({'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg',
  '.gif':'image/gif', '.webp':'image/webp', '.svg':'image/svg+xml', '.pdf':'application/pdf',
  '.json':'application/json', '.mp4':'video/mp4', '.mp3':'audio/mpeg'})[path.extname(name).toLowerCase()] || 'application/octet-stream';

function within(root, relative) {
  assert.equal(typeof relative, 'string');
  assert(relative && !relative.includes('\\') && !relative.includes(':') && !relative.startsWith('/'));
  assert(relative.split('/').every(part => part && part !== '.' && part !== '..'));
  const result = fs.realpathSync(path.join(root, relative));
  const rel = path.relative(fs.realpathSync(root), result);
  assert(rel && !rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel), 'File escapes extraction');
  return result;
}

function tree(directory, root=directory) {
  const children = Object.create(null);
  for (const entry of fs.readdirSync(directory, {withFileTypes:true})) {
    const file = path.join(directory, entry.name);
    assert(!fs.lstatSync(file).isSymbolicLink(), 'Linked files are not supported');
    if (entry.isDirectory()) children[entry.name] = tree(file, root);
    else if (entry.isFile()) children[entry.name] = {kind:'file', name:entry.name,
      type:mime(entry.name), relative:path.relative(root,file).split(path.sep).join('/')};
    else throw new Error('Unexpected extraction filesystem entry');
  }
  return {kind:'directory', name:path.basename(directory), children};
}

function playwright(modulePath) {
  if (modulePath) return require(modulePath);
  try { return require('playwright'); }
  catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
    return require(path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
  }
}

function expectedTitle(record) {
  const title = record.record?.title;
  const plain = typeof title === 'string' ? title : title?.plain_text ?? title?.text;
  assert.equal(typeof plain, 'string', 'Canonical record.record.title is missing');
  assert(plain.trim(), 'Canonical title is empty');
  return plain.trim();
}

const nonempty = value => typeof value === 'string' && value.trim().length > 0;
// Only large media cards are parked. Inline scientific/table images must remain
// visible in the reading and table screenshots and be decoded explicitly.
const parkedImageSelector = '.media-card img.zoomable-image';
async function decodeInlineImages(nodes) {
  const dimensions = [];
  for (const node of nodes) {
    if (node.closest('.media-card')) continue;
    node.loading = 'eager';
    await node.decode();
    if (!(node.naturalWidth > 0 && node.naturalHeight > 0)) {
      throw new Error('Inline scientific image did not decode');
    }
    dimensions.push({width:node.naturalWidth, height:node.naturalHeight, alt:node.alt});
  }
  return dimensions;
}
// Element screenshots may exceed the viewport; the sticky toolbar otherwise
// overlays table rows at its current scroll position. Playwright applies this
// style only while capturing, without changing application behavior or layout.
const screenshotOptions = file => ({path:file, style:'.topbar { visibility: hidden !important; }'});
const browserChannel = value => (!value || value === 'chromium' ? undefined : value);
async function captureElement(locator, file) {
  // Near-viewport-height sections can otherwise remain partially below the
  // viewport after Playwright's centered scroll, clipping their final lines.
  // Align before capture without changing the viewer's layout or content.
  await locator.evaluate(node => {
    const view = node.ownerDocument.defaultView;
    view.scrollTo({top:view.scrollY + node.getBoundingClientRect().top,
      // Element screenshots can move the page horizontally when a preceding
      // scientific heading is wider than the viewport.  Start every review
      // capture at the document's left edge so later first lines are not
      // silently clipped in the retained visual evidence.
      left:0, behavior:'instant'});
  });
  return locator.screenshot(screenshotOptions(file));
}
const figureCardId = value => 'figure-' + String(value).normalize('NFKC')
  .replace(/[^A-Za-z0-9_.:-]+/g, '-').replace(/^-+|-+$/g, '');

function documentedSourceOmissions(record, evidence) {
  const figures = record.figures || [];
  const assets = new Set((record.assets || []).map(asset => asset.asset_id));
  const cards = figures.map(figure => figureCardId(figure.figure_id));
  assert.equal(new Set(cards).size, cards.length, 'Ambiguous figure card IDs');
  const missing = [];
  figures.forEach((figure, index) => {
    assert(nonempty(figure.figure_id), 'Figure ID is missing');
    if (Object.hasOwn(figure, 'asset_id')) {
      assert(nonempty(figure.asset_id) && assets.has(figure.asset_id), 'Dangling figure asset_id');
    } else missing.push({figure, index});
  });
  if (!missing.length) return [];
  assert.equal(missing.length, 1, 'Unexpected omitted figure count');
  const {figure, index} = missing[0];
  assert.equal(figure.kind, 'graphical_abstract', 'Only documented graphical-abstract absence is supported');
  assert(evidence, 'Source omission lacks diagnostic evidence');
  const one = (rows, predicate, label) => {
    assert(Array.isArray(rows), `Missing ${label}`);
    const matches = rows.filter(predicate);
    assert.equal(matches.length, 1, `Expected exactly one ${label}`);
    return matches[0];
  };
  const included = one(evidence.coverage, row => row.output_id === figure.figure_id
    && row.output_path === 'record.json' && row.output_locator?.json_pointer === `/figures/${index}`
    && row.content_kind === 'graphical_abstract' && row.status === 'included'
    && nonempty(row.source_path) && nonempty(row.source_locator), 'figure source binding');
  const omitted = one(evidence.coverage, row => row.coverage_id === 'unresolved-graphical-abstract-image'
    && row.content_kind === 'graphical_abstract_image' && row.status === 'intentionally_excluded'
    && row.source_path === included.source_path && nonempty(row.source_locator)
    && nonempty(row.reason), 'documented omission coverage');
  const anomaly = one(evidence.anomalies, row => row.coverage_id === omitted.coverage_id
    && row.source_path === included.source_path && ['anomaly_id', 'observed', 'assessment',
      'disposition', 'source_locator'].every(key => nonempty(row[key])), 'source anomaly');
  one(evidence.warnings, row => row.code === 'graphical_abstract_image_unavailable'
    && row.source_path === included.source_path && nonempty(row.message), 'source omission warning');
  const source = one(evidence.sources?.sources, row => row.path === included.source_path
    && ['main_html', 'main_pdf'].includes(row.role) && /^[a-f0-9]{64}$/.test(row.sha256), 'archived source');
  assert.equal(evidence.sources.record_id, record.record.record_id, 'Source record mismatch');
  return [{figure_id:figure.figure_id, card_id:cards[index], json_pointer:`/figures/${index}`,
    coverage_id:omitted.coverage_id, anomaly_id:anomaly.anomaly_id,
    source_path:source.path, source_sha256:source.sha256}];
}

function sourceOmissionEvidence(root, extraction, record, recordBytes) {
  if (!(record.figures || []).some(figure => !Object.hasOwn(figure, 'asset_id'))) {
    return {limitations:documentedSourceOmissions(record), files:[]};
  }
  const diagnostic = path.join(path.dirname(extraction), 'extraction_diagnostic');
  const files = [];
  function read(name, lines = false) {
    const relative = path.relative(root, path.join(diagnostic, name)).split(path.sep).join('/');
    const bytes = fs.readFileSync(within(root, relative));
    files.push({path:relative, sha256:sha(bytes)});
    return lines ? bytes.toString('utf8').split(/\r?\n/).filter(line => line.trim()).map(JSON.parse)
      : JSON.parse(bytes);
  }
  const evidence = {coverage:read('coverage.jsonl', true), warnings:read('warnings.jsonl', true),
    anomalies:read('source_anomalies.jsonl', true), sources:read('sources.json')};
  const manifest = read('manifest.json');
  assert.equal(manifest.record_id, record.record.record_id, 'Manifest record mismatch');
  assert(/^[a-f0-9]{64}$/.test(manifest.source_fingerprint), 'Missing source fingerprint');
  assert.equal(manifest.source_fingerprint, evidence.sources.source_fingerprint, 'Source fingerprint mismatch');
  const canonical = (manifest.files || []).filter(file => file.path === 'record.json');
  assert.equal(canonical.length, 1, 'Missing canonical manifest binding');
  assert.equal(canonical[0].sha256, sha(recordBytes), 'Stale source-omission manifest');
  assert.equal(canonical[0].bytes, recordBytes.length, 'Canonical manifest size mismatch');
  const limitations = documentedSourceOmissions(record, evidence);
  for (const limitation of limitations) {
    assert.equal(sha(fs.readFileSync(within(root, limitation.source_path))), limitation.source_sha256,
      'Documented omission source has changed');
  }
  return {limitations, files};
}

function checkAssetPlaceholders(errorCount, unavailableCards, limitations) {
  assert.equal(errorCount, 0, 'Broken canonical assets are not source omissions');
  assert.deepEqual([...unavailableCards].sort(), limitations.map(item => item.card_id).sort(),
    'Unexpected or missing source-omission cards');
}

async function main() {
  const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  assert(/^\d{5}$/.test(config.record_id), 'Expected a five-digit record ID');
  const root = fs.realpathSync(config.repository_root);
  if (config.staged_run_id !== undefined) {
    assert(typeof config.staged_run_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(config.staged_run_id));
    assert(!config.staged_run_id.endsWith('.'), 'Invalid staged run ID');
  }
  const extractionRelative = config.staged_run_id === undefined
    ? `papers (private)/${config.record_id}/extraction`
    : `papers (private)/staging/${config.record_id}/${config.staged_run_id}/extraction`;
  const extraction = within(root, extractionRelative);
  const output = path.resolve(config.output);
  const outputRelative = path.relative(path.join(root, 'papers (private)', 'diagnostics'), output);
  assert(outputRelative && !outputRelative.startsWith('..') && !path.isAbsolute(outputRelative), 'Output must be private diagnostics');
  fs.mkdirSync(output);
  const started = performance.now();
  const report = {schema_version:'1.0', record_id:config.record_id, status:'failed',
    errors:[], blocked:[], screenshots:[], downloads:[], imageDimensions:[], timings:[]};
  const mark = phase => report.timings.push({phase, elapsed_seconds:Math.round(performance.now()-started)/1000});
  let browser;
  try {
    const recordBytes = fs.readFileSync(path.join(extraction, 'record.json'));
    const record = JSON.parse(recordBytes);
    const title = expectedTitle(record);
    assert.equal(record.record.record_id, config.record_id);
    report.record_sha256 = sha(recordBytes);
    const omissions = sourceOmissionEvidence(root, extraction, record, recordBytes);
    report.accepted_source_limitations = omissions.limitations;
    report.source_limitation_evidence = omissions.files;
    const {chromium} = playwright(config.playwright_module);
    mark('module_loaded');
    const downloadsPath = path.join(output, 'browser-downloads');
    fs.mkdirSync(downloadsPath);
    const channel = browserChannel(config.browser_channel);
    browser = await chromium.launch({channel, headless:true, downloadsPath});
    report.browser = {channel:config.browser_channel, version:browser.version()};
    mark('browser_launched');
    const context = await browser.newContext({viewport:{width:1500,height:1100},
      deviceScaleFactor:1, acceptDownloads:true, serviceWorkers:'block'});
    const page = await context.newPage();
    mark('page_created');
    page.setDefaultTimeout(30000);
    // Route the local document directly. No server process or listening socket
    // is needed, and every page request outside this document is blocked.
    const address = 'http://127.0.0.1/extraction-completion-check';
    const source = fs.readFileSync(path.join(root, 'extraction_viewer.html'));
    report.viewer_sha256 = sha(source);
    await context.route('**/*', route => {
      const url = route.request().url();
      if (url === address) return route.fulfill({status:200, contentType:'text/html; charset=utf-8', body:source});
      if (/^(blob:|data:)/.test(url)) return route.continue();
      report.blocked.push(url);
      return route.abort();
    });
    page.on('pageerror', error => report.errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') report.errors.push(message.text()); });
    await page.exposeFunction('__reviewReadFile', relative =>
      fs.readFileSync(within(extraction,relative)).toString('base64'));
    const handles = {kind:'directory', name:'papers (private)', children:{
      [config.record_id]:{kind:'directory', name:config.record_id, children:{extraction:tree(extraction)}}
    }};
    await page.addInitScript(data => {
      class FileHandle {
        constructor(node) { this.kind='file'; this.name=node.name; this.node=node; }
        async getFile() {
          const encoded=await window.__reviewReadFile(this.node.relative);
          return new File([Uint8Array.from(atob(encoded), c => c.charCodeAt(0))],
            this.name, {type:this.node.type, lastModified:0});
        }
      }
      class DirectoryHandle {
        constructor(node) { this.kind='directory'; this.name=node.name; this.node=node; }
        async getDirectoryHandle(name) {
          const node=this.node.children[name];
          if (!node || node.kind !== 'directory') throw new DOMException('Missing directory', 'NotFoundError');
          return new DirectoryHandle(node);
        }
        async getFileHandle(name) {
          const node=this.node.children[name];
          if (!node || node.kind !== 'file') throw new DOMException('Missing file', 'NotFoundError');
          return new FileHandle(node);
        }
        async *values() {
          for (const node of Object.values(this.node.children)) yield node.kind === 'file' ? new FileHandle(node) : new DirectoryHandle(node);
        }
        async *entries() { for await (const node of this.values()) yield [node.name,node]; }
      }
      window.showDirectoryPicker = async () => new DirectoryHandle(data.handles);
    }, {handles});
    await page.goto(address, {waitUntil:'load'});
    mark('document_loaded');
    await page.locator('#choose-root').click();
    await page.locator('#record-choice-' + config.record_id).click();
    await page.locator('#workspace:not([hidden])').waitFor();
    mark('record_opened');
    report.title = (await page.locator('#article-header h1').innerText()).trim();
    assert.equal(report.title, title);
    report.schemaMessage = (await page.locator('#schema-message').innerText()).trim();
    assert.equal(report.schemaMessage, '', 'Viewer schema message is not empty');
    const images = page.locator(parkedImageSelector);
    report.inlineImageDimensions = await page.locator('img.zoomable-image')
      .evaluateAll(decodeInlineImages);
    const parkedImage=await page.evaluate(() => URL.createObjectURL(new Blob([
      '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
    ], {type:'image/svg+xml'})));
    const imageSources=await images.evaluateAll((nodes, parked) => nodes.map((image,index) => {
      image.dataset.reviewImageIndex=String(index);
      const source=image.currentSrc || image.src;
      image.removeAttribute('srcset');
      image.loading='lazy';
      image.src=parked;
      return source;
    }), parkedImage);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    mark('images_parked');
    report.textOverflow = await page.locator('.text-block').evaluateAll(nodes =>
      nodes.filter(node => node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 1)
        .map(node => ({block:node.closest('[id]')?.id || '',
          clientWidth:node.clientWidth, scrollWidth:node.scrollWidth})));
    assert.deepEqual(report.textOverflow, [], 'Scientific text overflows its visible container');
    async function capture(locator, name) {
      await captureElement(locator, path.join(output,name));
      report.screenshots.push({path:name, sha256:sha(fs.readFileSync(path.join(output,name)))});
    }
    await capture(page.locator('#article-header'), 'header.png');
    const sections = page.locator('#front-matter, .equation-block, .table-card');
    for (let index=0; index<await sections.count(); index++) {
      await capture(sections.nth(index), `section-${String(index+1).padStart(3,'0')}.png`);
    }
    // Scientific prose and references need visual evidence as well as tables.
    const readingSections = page.locator('.content-section[id^="section-"], #references');
    for (let index=0; index<await readingSections.count(); index++) {
      await capture(readingSections.nth(index), `reading-${String(index+1).padStart(3,'0')}.png`);
    }
    const cards = page.locator('.media-card');
    report.lightbox = 'not_applicable';
    for (let index=0; index<await cards.count(); index++) {
      const prefix = `media-${String(index+1).padStart(3,'0')}`;
      const card = cards.nth(index);
      const cardImages=card.locator('img.zoomable-image');
      assert((await cardImages.count()) <= 1, 'Media card contains ambiguous viewer images');
      if (await cardImages.count()) {
        const image=cardImages.first();
        const imageIndex=Number(await image.getAttribute('data-review-image-index'));
        assert(Number.isInteger(imageIndex) && imageSources[imageIndex], 'Missing parked image source');
        const dimensions=await image.evaluate(async (node, source) => {
          node.loading='eager'; node.src=source; node.scrollIntoView({block:'center'}); await node.decode();
          node.hidden=false;
          return {width:node.naturalWidth,height:node.naturalHeight,alt:node.alt};
        }, imageSources[imageIndex]);
        assert(dimensions.width > 0 && dimensions.height > 0);
        report.imageDimensions.push(dimensions);
      }
      await capture(card.locator('.media-stage'), prefix+'.png');
      // Separate captions avoid full-card screenshot clipping on tall figures.
      await capture(card.locator('.media-caption'), prefix+'-caption.png');
      if (await cardImages.count() && report.lightbox === 'not_applicable') {
        await cardImages.first().click();
        await page.locator('#image-lightbox:not([hidden])').waitFor();
        await page.waitForFunction(() => document.querySelector('#image-lightbox-image')?.naturalWidth > 0);
        await capture(page.locator('#image-lightbox'), 'lightbox.png');
        await page.locator('#image-lightbox-close').click();
        await page.locator('#image-lightbox[hidden]').waitFor({state:'attached'});
        report.lightbox='passed';
      }
      if (await cardImages.count()) {
        await cardImages.first().evaluate((image, parked) => {
          image.hidden=true; image.loading='lazy'; image.src=parked;
        }, parkedImage);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      }
    }
    mark('images_decoded');
    mark('content_screenshots_saved');
    const downloadCards=page.locator('.download-card');
    for (let index=0; index<await downloadCards.count(); index++) {
      const card=downloadCards.nth(index);
      const link=card.locator('a');
      await link.waitFor();
      assert((await link.getAttribute('href')).startsWith('blob:'), 'Download is not local');
      const meta=await card.locator('.download-meta').innerText();
      const relative=meta.includes(' · ') ? meta.split(' · ').slice(1).join(' · ') : meta;
      const expected=fs.readFileSync(within(extraction, relative));
      const pending=page.waitForEvent('download');
      await link.click();
      const download=await pending;
      const filename=`download-${String(index+1).padStart(3,'0')}.bin`;
      await download.saveAs(path.join(output,filename));
      assert.equal(await download.failure(), null);
      const actual=fs.readFileSync(path.join(output,filename));
      assert(actual.equals(expected), 'Downloaded bytes differ from the canonical file');
      report.downloads.push({path:relative, saved_path:filename, bytes:actual.length,
        sha256:sha(actual), matches_canonical:true});
      await capture(card, filename.replace('.bin','.png'));
    }
    report.counts={references:await page.locator('.reference-item').count(),
      media:await cards.count(), images:report.imageDimensions.length,
      tables:await page.locator('.table-card').count(), supplements:await page.locator('.supplement-card').count(),
      downloads:report.downloads.length};
    mark('downloads_verified');
    assert.equal(report.counts.references, (record.references || []).length);
    checkAssetPlaceholders(await page.locator('.asset-error').count(),
      await page.locator('.asset-unavailable').evaluateAll(nodes =>
        nodes.map(node => node.closest('.media-card')?.id || null)), omissions.limitations);
    await page.locator('#view-toggle').click();
    await page.locator('#raw-view:not([hidden])').waitFor();
    assert.deepEqual(JSON.parse(await page.locator('#raw-json').textContent()), record);
    report.raw_matches=true;
    await page.locator('#back-to-records').click();
    await page.locator('#record-choice-' + config.record_id).waitFor();
    report.library_return='passed';
    await page.evaluate(parked => URL.revokeObjectURL(parked), parkedImage);
    assert.equal(report.errors.length, 0);
    assert.equal(report.blocked.length, 0);
    report.status='passed';
    report.visual_inspection='pending';
    mark('checks_passed');
    await context.close();
    mark('context_closed');
  } catch (error) {
    report.status='failed';
    report.failure=String(error.stack || error);
  } finally {
    if (browser) {
      try { await browser.close(); }
      catch (error) { report.status='failed'; report.shutdown_failure=String(error); }
    }
    mark('browser_closed');
    report.duration_seconds=Math.round((performance.now()-started))/1000;
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2)+'\n', {flag:'wx'});
  }
  console.log(JSON.stringify({status:report.status, duration_seconds:report.duration_seconds,
    report:path.join(output,'report.json')}));
  if (report.status !== 'passed') process.exitCode=2;
}

module.exports = {expectedTitle, within, tree, documentedSourceOmissions, sourceOmissionEvidence, checkAssetPlaceholders, screenshotOptions, captureElement, parkedImageSelector, decodeInlineImages, browserChannel};
if (require.main === module) {
  main().catch(error => { console.error(error.stack || error); process.exitCode=2; });
}
