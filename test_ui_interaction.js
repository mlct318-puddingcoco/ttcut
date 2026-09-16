// Run with: node test_ui_interaction.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('ttcut_v2_3.py', 'utf8');
const html = source.split('HTML = r"""')[1].split('"""\n\n# ──')[0];
assert.match(html, /id="tailPad" value="2\.0"/);
assert.match(html, /id="leadPad" value="0\.8"/);
assert.match(html, /id="minCut" value="2\.5"/);

let script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// Expose the real UI state to this isolated DOM check; skip its async startup.
script = script.split('  /* ───────────────────────── 起始：')[0] +
  'globalThis.testUI = { setVideo: v => video = v, setCandidates: c => { rallyCandidates = c; paintRallies(); }, setEvents: e => events = e, events: () => events, tick };\n})();';

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, value: id === 'fps' ? '30' : '', innerHTML: '', textContent: '',
    listeners: {}, style: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    classList: {add() {}, remove() {}, toggle() {}},
  });
  return nodes.get(id);
}
const document = {
  getElementById: node,
  activeElement: null,
  documentElement: {style: {setProperty() {}}},
};
const context = {document, addEventListener() {}, setTimeout() {}, clearTimeout() {},
  fetch() { throw new Error('unexpected fetch'); }};
vm.runInNewContext(script, context);

const video = {currentTime: 0, duration: 300, paused: false,
  pause() { this.paused = true; }};
context.testUI.setVideo(video);
const candidate = {start: 151.8, end: 154, duration: 2.2, confidence: .4,
  confidenceTier: 'low', motionMean: 1, motionPeak: 2, sideBalance: 1,
  strongFrames: 2, supportFrames: 3, audioHits: 0};
context.testUI.setCandidates([candidate]);
assert.match(node('rallyList').innerHTML, /class="rallyrow low"/);
assert.match(node('rallyList').innerHTML, /class="confidence">低信心 · 40% · 請人工確認/);
assert.match(node('rallyList').innerHTML, /data-rally-seek="0"/);

const row = {dataset: {rally: '0'}};
const target = selected => ({closest(selector) { return selected[selector] || null; }});
const rallyClick = node('rallyList').listeners.click;
function clickRally(selected) { rallyClick({target: target(selected)}); }

clickRally({'[data-rally]': row});
assert.equal(video.currentTime, candidate.start);
assert.equal(node('tc').textContent, '02:31.80');
assert.equal(context.testUI.events().length, 0, 'row click only previews');

video.currentTime = 0;
clickRally({'[data-rally]': row, '[data-rally-seek]': {dataset: {rallySeek: '0'}}});
assert.equal(video.currentTime, candidate.start, 'timestamp button previews');
assert.equal(context.testUI.events().length, 0);

video.currentTime = 0;
clickRally({'[data-rally]': row, details: {}});
assert.equal(video.currentTime, 0, 'diagnostics do not seek or confirm');
assert.equal(context.testUI.events().length, 0);

clickRally({'[data-rally]': row, '[data-rally-serve]': {dataset: {rallyServe: '0'}}});
assert.equal(context.testUI.events().length, 1, 'explicit confirmation adds S');
assert.equal(context.testUI.events()[0].type, 'serve');
assert.equal(video.currentTime, 0, 'confirmation does not seek');

context.testUI.setEvents([{t: 88.25, type: 'serve'}]);
node('stream').listeners.click({target: target({'.ev': {dataset: {i: '0'}}})});
assert.equal(video.currentTime, 88.25, 'event row seeks to event time');
assert.equal(node('tc').textContent, '01:28.25');
assert.equal(context.testUI.events().length, 1);
console.log('UI interaction checks passed');
