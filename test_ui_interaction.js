// Run with: node test_ui_interaction.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('ttcut_v2_3.py', 'utf8');
const html = source.split('HTML = r"""')[1].split('"""\n\n# ──')[0];
assert.match(html, /id="tailPad" value="2\.0"/);
assert.match(html, /id="leadPad" value="0\.8"/);
assert.match(html, /id="minCut" value="2\.5"/);
assert.match(html, /id="scoreboardStyle"[^>]*>[\s\S]*?<option value="koko" selected>/);
assert.match(html, /<option value="ttcut">ttcut 原版<\/option>/);
assert.match(html, /<option value="high" selected>標準<\/option>/);
assert.match(html, /極致（CPU，非常慢）/);
for (const id of ['newMatch', 'saveAs', 'introEnabled', 'thumbnail', 'exit'])
  assert.match(html, new RegExp(`id="${id}"`));
for (const [id, label] of Object.entries({introTournament:'賽事名稱', introCategory:'組別',
  introPlayerA:'選手 A', introSchoolA:'學校 A', introPlayerB:'選手 B', introSchoolB:'學校 B'}))
  assert.match(html, new RegExp(`<label class="intro-field[^>]*>${label}<input id="${id}"`));
assert.match(html, /\.intro-field input,[\s\S]*background:#071C32/);
assert.match(html, /color:#F7FAFD/);
assert.match(html, /::placeholder\{color:#A9BCD0/);
assert.match(html, /\.intro-field input:focus[\s\S]*border-color:var\(--ball\)/);

let script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// Expose the real UI state to this isolated DOM check; skip its async startup.
script = script.split('  /* ───────────────────────── 起始：')[0] +
  'globalThis.testUI = { setVideo: v => video = v, setCandidates: c => { rallyCandidates = c; paintRallies(); }, setEvents: e => events = e, events: () => events, setSource: (p,o) => {srcPath=p;srcName="old.mp4";outPath=o;}, setRoi: r => roi=r, state: () => ({srcPath,outPath,roi,rallyCandidates,rallyDiagnostics,video}), clearMatch, docPayload, optPayload, loadIntro, tick };\n})();';

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
  confirm() { return true; }, fetch() { throw new Error('unexpected fetch'); }};
vm.runInNewContext(script, context);

const video = {currentTime: 0, duration: 300, paused: false,
  pause() { this.paused = true; }, remove() { this.removed = true; }};
context.testUI.setVideo(video);
node('scoreboardStyle').value = 'koko';
assert.equal(context.testUI.docPayload().scoreboard.style, 'koko');
node('scoreboardStyle').value = 'ttcut';
assert.equal(context.testUI.docPayload().scoreboard.style, 'ttcut');
node('scoreboardStyle').value = 'koko';
const candidate = {start: 151.8, end: 154, duration: 2.2, confidence: .4,
  confidenceTier: 'low', motionMean: 1, motionPeak: 2, sideBalance: 1,
  strongFrames: 2, supportFrames: 3, audioHits: 0};
context.testUI.setCandidates([candidate]);
assert.match(node('rallyList').innerHTML, /class="rallyrow low"/);
assert.match(node('rallyList').innerHTML, /class="confidence">低信心 · 40% · 請人工確認/);
assert.match(node('rallyList').innerHTML, /data-rally-seek="0"/);

const row = {dataset: {rally: '0'}};
const outerDetails = {id: 'rallyBox'};
const target = selected => ({closest(selector) { return selected[selector] || null; }});
const rallyClick = node('rallyList').listeners.click;
function clickRally(selected) {
  // All candidate targets are inside the outer <details id="rallyBox">.
  rallyClick({target: target({details: outerDetails, ...selected})});
}

clickRally({'[data-rally]': row});
assert.equal(video.currentTime, candidate.start);
assert.equal(node('tc').textContent, '02:31.80');
assert.equal(context.testUI.events().length, 0, 'row click only previews');

video.currentTime = 0;
clickRally({'[data-rally]': row, '[data-rally-seek]': {dataset: {rallySeek: '0'}}});
assert.equal(video.currentTime, candidate.start, 'candidate number button previews');
assert.equal(context.testUI.events().length, 0);

video.currentTime = 0;
clickRally({'[data-rally]': row, '.rallyinfo': {textContent: '02:31.80 → 02:34.00'}});
assert.equal(video.currentTime, candidate.start, 'timestamp text previews');
assert.equal(context.testUI.events().length, 0);

video.currentTime = 0;
clickRally({'[data-rally]': row, '.rallyrow summary': {}});
assert.equal(video.currentTime, 0, 'diagnostic disclosure only toggles');
assert.equal(context.testUI.events().length, 0);

clickRally({'[data-rally]': row, details: {id: 'innerValley'}});
assert.equal(video.currentTime, candidate.start, 'diagnostic text is a non-button row click');
assert.equal(context.testUI.events().length, 0);

context.testUI.setCandidates([candidate]); // repaint replaces rows, but delegation stays
video.currentTime = 0;
clickRally({'[data-rally]': row});
assert.equal(video.currentTime, candidate.start, 'seek survives candidate rerender');
assert.equal(context.testUI.events().length, 0);

video.currentTime = 0;
clickRally({'[data-rally]': row, '[data-rally-serve]': {dataset: {rallyServe: '0'}}});
assert.equal(context.testUI.events().length, 1, 'explicit confirmation adds S');
assert.equal(context.testUI.events()[0].type, 'serve');
assert.equal(video.currentTime, 0, 'confirmation does not seek');

context.testUI.setEvents([{t: 88.25, type: 'serve'}]);
node('stream').listeners.click({target: target({'.ev': {dataset: {i: '0'}}})});
assert.equal(video.currentTime, 88.25, 'event row seeks to event time');
assert.equal(node('tc').textContent, '01:28.25');
assert.equal(context.testUI.events().length, 1);
node('quality').value = 'max';
node('scoreboardStyle').value = 'koko';
node('tailPad').value = '2.0'; node('leadPad').value = '0.8';
node('minCut').value = '2.5';
node('introEnabled').checked = true; node('introDuration').value = '3.0';
node('intro1').value = '城市盃';
node('sgA').value = '2'; node('spA').value = '8';
node('nameA').value = '舊選手';
context.testUI.setSource('/old.mp4', '/custom/old.mp4');
context.testUI.setRoi({x:0,y:0,w:1,h:1});
context.testUI.setCandidates([candidate]);
context.testUI.clearMatch();
assert.equal(context.testUI.events().length, 0);
assert.equal(context.testUI.state().srcPath, '');
assert.equal(context.testUI.state().outPath, '');
assert.equal(context.testUI.state().roi, null);
assert.equal(context.testUI.state().rallyCandidates.length, 0);
assert.equal(context.testUI.state().video, null);
assert.equal(node('sgA').value, '0'); assert.equal(node('spA').value, '0');
assert.equal(node('nameA').value, '選手 A');
assert.equal(node('quality').value, 'max');
assert.equal(node('scoreboardStyle').value, 'koko');
assert.equal(node('tailPad').value, '2.0');
assert.equal(node('leadPad').value, '0.8');
assert.equal(node('minCut').value, '2.5');
assert.equal(context.testUI.optPayload().intro.duration, 3);
assert.equal(context.testUI.optPayload().intro.enabled, true);
assert.equal(context.testUI.optPayload().intro.tournament, '');
assert.equal(context.testUI.optPayload().intro.playerA, '');
context.testUI.loadIntro({lines:['城市盃','單打賽','許宸愷 VS 曾柏誠','光復國小    吉林國小']});
assert.equal(node('introPlayerA').value, '許宸愷');
assert.equal(node('introSchoolB').value, '吉林國小');
assert.equal(context.testUI.optPayload().intro.playerB, '曾柏誠');
context.testUI.loadIntro({lines:['自由標題','組別','無法辨識的對戰','只有一所學校']});
assert.equal(context.testUI.optPayload().intro.lines[2], '無法辨識的對戰');
assert.equal(node('introLegacy').hidden, false);
node('nameA').value = '許宸愷（光復國小）';
node('nameB').value = '曾柏誠(吉林國小)';
context.confirm = () => false;
node('introModernize').listeners.click();
assert.equal(context.testUI.optPayload().intro.lines[2], '無法辨識的對戰');
context.confirm = () => true;
node('introModernize').listeners.click();
node('introAutofill').listeners.click();
assert.equal(node('introPlayerA').value, '許宸愷');
assert.equal(node('introSchoolA').value, '光復國小');
assert.equal(node('introPlayerB').value, '曾柏誠');
assert.equal(node('introSchoolB').value, '吉林國小');
console.log('UI interaction checks passed');
