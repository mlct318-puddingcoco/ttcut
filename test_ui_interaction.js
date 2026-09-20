// Run with: node test_ui_interaction.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('ttcut_v2_3.py', 'utf8');
const html = source.split('HTML = r"""')[1].split('"""\n\n# ──')[0];
assert.match(html, /id="tailPad" value="2\.0"/);
assert.match(html, /id="leadPad" value="0\.8"/);
assert.match(html, /id="minCut" value="2\.5"/);
assert.match(html, /<kbd>H<\/kbd>精彩球/);
assert.match(html, /id="highlightCount">0<\/b>/);
assert.match(html, /\.stream\{height:clamp\(260px,36vh,330px\);flex:0 0 clamp\(260px,36vh,330px\);[\s\S]*overflow-y:auto/,
  'event viewport is independently scrollable and tall enough for review');
assert.match(html, /\.rail\{[\s\S]*overflow-y:auto/,
  'narrow-height layouts can scroll to the sections below the event viewport');
assert.match(html, /\.ev\.selected-point\{[\s\S]*box-shadow:inset 3px 0 var\(--ball\)/);
assert.match(html, /id="scoreboardStyle"[^>]*>[\s\S]*?<option value="koko" selected>/);
assert.match(html, /<option value="ttcut">ttcut 原版<\/option>/);
assert.match(html, /<option value="high" selected>標準<\/option>/);
assert.match(html, /極致（CPU，非常慢）/);
for (const id of ['newMatch', 'saveAs', 'introEnabled', 'thumbnail', 'exit'])
  assert.match(html, new RegExp(`id="${id}"`));
for (const id of ['openTournament', 'tournamentPanel', 'tournamentPick', 'thMatchList',
  'thTournament', 'thTitle', 'thProtagonist', 'thSchool', 'thSaveAs', 'thRender'])
  assert.match(html, new RegExp(`id="${id}"`));
assert.match(html, /建立賽事精彩集錦…/);
assert.match(html, /掃描 <strong id="thMatches">0<\/strong> 場比賽/);
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
  'globalThis.testUI = { setVideo: v => video = v, setCandidates: c => { rallyCandidates = c; paintRallies(); }, setEvents: e => { selectedPointEvent = null; events = normalizeEvents(e); }, events: () => events, setSource: (p,o) => {srcPath=p;srcName="old.mp4";outPath=o;}, setRoi: r => roi=r, setTournament: s => tournamentScan=s, tournament: () => tournamentScan, paintTournament, thReview, thSelectedCount, state: () => ({srcPath,outPath,customOutput,roi,rallyCandidates,rallyDiagnostics,video,activeSegment,pendingSeek,sources,eventScroll:pendingEventScroll,selectedPointIndex:selectedPointIndex()}), clearMatch, docPayload, optPayload, loadIntro, introFilename, safeFilenamePart, setSources, sourceForTime, seekGlobal, now, tick, normalizeEvents, completedPointIndexes, toggleHighlightAt, toggleLatestHighlight, toggleSelectedOrLatestHighlight, selectPointAt, clearSelectedPoint, undo, paint, handleKeydown, isEditableTarget };\n})();';

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, value: id === 'fps' ? '30' : '', innerHTML: '', textContent: '',
    scrollTop: 0, scrollHeight: 900,
    listeners: {}, style: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    querySelectorAll() { return []; },
    classList: {add() {}, remove() {}, toggle() {}},
  });
  return nodes.get(id);
}
const document = {
  getElementById: node,
  activeElement: null,
  documentElement: {style: {setProperty() {}}},
};
const globalListeners = {};
const context = {document, addEventListener(type, fn) { globalListeners[type] = fn; }, setTimeout() {}, clearTimeout() {},
  confirm() { return true; }, fetch() { throw new Error('unexpected fetch'); }};
vm.runInNewContext(script, context);

const video = {currentTime: 0, duration: 300, paused: false,
  pause() { this.paused = true; }, remove() { this.removed = true; }};
context.testUI.setVideo(video);
context.testUI.setSource('/camera/DJI_0005.MP4', '/output/match.mp4');
context.testUI.setSources({sources:[{path:'/camera/DJI_0005.MP4',duration:300,
  offset:0,end:300,w:1920,h:1080,codec:'h264',fps_frac:'30/1',audio:{codec_name:'aac'}}],
  geometryMismatch:false});
assert.equal(context.testUI.docPayload().source, 'old.mp4',
  'legacy single-file source field remains available');
assert.deepEqual(JSON.parse(JSON.stringify(context.testUI.docPayload().sources)), [{
  path:'/camera/DJI_0005.MP4',duration:300,offset:0,end:300}],
  'new single-file documents retain a resolvable absolute source path');
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

const tournament = {root:'/盃賽',scanned_matches:3,total_highlights:3,warnings:[],matches:[
  {id:'a',label:'甲 vs 乙',highlights:[
    {point_id:'1:2.000',point_time:2,label:'甲得分',selected:true},
    {point_id:'3:4.000',point_time:4,label:'乙得分',selected:true}]},
  {id:'b',label:'甲 vs 丙',highlights:[
    {point_id:'1:3.000',point_time:3,label:'甲得分',selected:true}]}
]};
context.testUI.setTournament(tournament);
context.testUI.paintTournament();
assert.equal(context.testUI.thSelectedCount(),3);
assert.match(node('thMatchList').innerHTML,/甲 vs 乙/);
const selectFirst = {dataset:{select:'0:0'},checked:false};
node('thMatchList').listeners.change({target:target({'[data-select]':selectFirst})});
assert.equal(context.testUI.thSelectedCount(),2,'builder checkbox updates this review only');
node('thMatchList').listeners.click({target:target({'[data-match-down]':{dataset:{matchDown:'0'}}})});
assert.deepEqual(Array.from(context.testUI.thReview().match_order),['b','a'],
  'match arrows define final render order');
node('thMatchList').listeners.click({target:target({'[data-highlight-down]':{dataset:{highlightDown:'1:0'}}})});
assert.deepEqual(Array.from(context.testUI.thReview().highlight_order.a),['3:4.000','1:2.000'],
  'highlight arrows define within-match render order');

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
assert.equal(context.testUI.state().selectedPointIndex, -1,
  'clicking a non-point event clears historical point selection');

const foldState = (count=0) => ({
  cur:{a:0,b:0,gA:0,gB:0,gameNo:1,server:0},
  snaps:Array(count).fill(null), cuts:{n:0,seconds:0,outSeconds:0,pct:0},
  ok:true, stats:null
});
context.testUI.setEvents([]);
assert.equal(context.testUI.toggleLatestHighlight(), false);
assert.match(node('note').textContent, /尚無可標記的完成回合/);

context.testUI.setEvents([{t:10,type:'serve'}]);
assert.equal(context.testUI.toggleLatestHighlight(), false);
assert.match(node('note').textContent, /先按 A 或 B 完成/);
assert.equal(context.testUI.events()[0].highlight, undefined);

context.testUI.setEvents([{t:10,type:'serve'},{t:14,type:'point',winner:'A'}]);
assert.equal(context.testUI.toggleLatestHighlight(), true);
assert.equal(context.testUI.events()[1].highlight, true, 'H marks latest completed rally');
assert.equal(context.testUI.toggleLatestHighlight(), true);
assert.equal(context.testUI.events()[1].highlight, undefined, 'H toggles highlight off');

context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'},
  {t:20,type:'serve'},{t:25,type:'point',winner:'B'}
]);
context.testUI.toggleLatestHighlight();
assert.equal(context.testUI.events()[1].highlight, undefined);
assert.equal(context.testUI.events()[3].highlight, true, 'latest of multiple rallies is selected');
context.testUI.paint(foldState(4));
assert.equal(node('highlightCount').textContent, 1);
assert.match(node('stream').innerHTML, /highlight-badge">★ 精彩球/);
assert.match(node('stream').innerHTML, /data-highlight="3" aria-pressed="true"/);
assert.match(node('stream').innerHTML, />★<\/button>/, 'active star has a clear button affordance');

// Historical point selection keeps seek behavior but changes H priority.
context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'},
  {t:20,type:'serve'},{t:25,type:'point',winner:'B'}
]);
context.testUI.paint(foldState(4));
node('stream').scrollTop = 126;
assert.match(node('stream').innerHTML, /title="標記精彩球"[^>]*>☆<\/button>/,
  'inactive star is visibly clickable and has a discoverability tooltip');
node('stream').listeners.click({target: target({'.ev': {dataset: {i: '1'}}})});
assert.equal(video.currentTime, 14, 'historical point click still seeks');
assert.equal(context.testUI.state().selectedPointIndex, 1, 'historical point is selected');
assert.equal(node('stream').scrollTop, 126, 'row selection does not jump the event list');
context.testUI.paint(foldState(4), {mode:'preserve', top:126});
assert.match(node('stream').innerHTML,
  /class="ev point selected-point" data-i="1" aria-selected="true"/,
  'selected point gets its own visual state');
assert.equal(context.testUI.docPayload().events.some(e => 'selected' in e), false,
  'selection is not persisted in JSON');
context.testUI.handleKeydown({key:'H',target:{tagName:'DIV'},preventDefault(){}});
assert.equal(context.testUI.events()[1].highlight, true,
  'H toggles selected historical point');
assert.equal(context.testUI.events()[3].highlight, undefined,
  'selected H does not toggle latest point');
assert.equal(context.testUI.state().eventScroll.mode, 'preserve');
assert.equal(context.testUI.state().eventScroll.top, 126,
  'selected-point H captures historical scroll position');
context.testUI.paint(foldState(4), context.testUI.state().eventScroll);
assert.equal(node('stream').scrollTop, 126, 'selected-point H preserves scrollTop after repaint');
assert.equal(node('highlightCount').textContent, 1);
assert.match(node('stream').innerHTML, /highlight-badge">★ 精彩球/);
assert.match(node('stream').innerHTML, /highlighted selected-point/,
  'highlight and selection styling remain distinct and can coexist');

const reloadedSelectedDoc = JSON.parse(JSON.stringify(context.testUI.docPayload()));
context.testUI.setEvents(reloadedSelectedDoc.events);
assert.equal(context.testUI.state().selectedPointIndex, -1,
  'JSON reload starts with no historical UI selection');
context.testUI.selectPointAt(1);

node('stream').listeners.click({target: target({'.ev': {dataset: {i: '0'}}})});
assert.equal(context.testUI.state().selectedPointIndex, -1,
  'serve click cannot leave an unsafe stale point selection');
context.testUI.handleKeydown({key:'H',target:{tagName:'DIV'},preventDefault(){}});
assert.equal(context.testUI.events()[3].highlight, true,
  'with no selection H retains latest-completed-rally behavior');

const roundTrip = JSON.parse(JSON.stringify(context.testUI.docPayload()));
const reopened = context.testUI.normalizeEvents(roundTrip.events);
assert.equal(reopened[3].highlight, true, 'JSON round-trip preserves highlight');
assert.deepEqual(context.testUI.normalizeEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'}
]).map(e => e.highlight), [undefined, undefined], 'old JSON has zero highlights');

context.testUI.setEvents([
  {t:5,type:'serve'},{t:9,type:'point',winner:'A'},
  {t:12,type:'serve'},{t:18,type:'point',winner:'B',highlight:true},
  {t:22,type:'serve'},{t:27,type:'point',winner:'A'}
]);
const syntheticRoundTrip = context.testUI.normalizeEvents(
  JSON.parse(JSON.stringify(context.testUI.docPayload())).events);
assert.equal(syntheticRoundTrip.filter(e => e.highlight === true).length, 1);
assert.equal(syntheticRoundTrip[3].highlight, true,
  'three-rally sample restores rally 2 as the only highlight');

context.testUI.setEvents([{t:10,type:'serve'},{t:14,type:'point',winner:'A',highlight:true}]);
context.testUI.undo();
assert.equal(context.testUI.events().length, 1);
assert.equal(context.testUI.events().some(e => e.highlight), false,
  'undoing highlighted point leaves no stale highlight');

context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A',highlight:true},
  {t:15,type:'game'}
]);
assert.equal(context.testUI.events()[1].highlight, true, 'N/new game preserves earlier highlight');
context.testUI.toggleLatestHighlight();
assert.equal(context.testUI.events()[1].highlight, undefined,
  'H after N still targets latest completed rally');

context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'},
  {t:20,type:'serve'}
]);
assert.equal(context.testUI.toggleLatestHighlight(), false, 'open rally blocks H');
assert.equal(context.testUI.events()[1].highlight, undefined,
  'open rally does not accidentally mark prior point');

for (const editable of [
  {tagName:'INPUT'}, {tagName:'TEXTAREA'}, {tagName:'DIV',isContentEditable:true},
  {tagName:'SPAN',closest: selector => selector.startsWith('[contenteditable]') ? {} : null}
]) {
  context.testUI.setEvents([{t:10,type:'serve'},{t:14,type:'point',winner:'A'}]);
  let prevented = false;
  context.testUI.handleKeydown({key:'H',target:editable,preventDefault(){prevented=true;}});
  assert.equal(context.testUI.events()[1].highlight, undefined,
    'H is ignored while typing/editing');
  assert.equal(prevented, false);
}
context.testUI.setEvents([{t:10,type:'serve'},{t:14,type:'point',winner:'A'}]);
let prevented = false;
context.testUI.handleKeydown({key:'H',target:{tagName:'DIV'},
  preventDefault(){prevented=true;}});
assert.equal(context.testUI.events()[1].highlight, true, 'global H toggles completed rally');
assert.equal(prevented, true);

context.testUI.setEvents([]);
for (const [key, t, expected] of [
  ['S',30,'serve'], ['A',34,'point'], ['N',35,'game'], ['Z',36,null], ['B',38,'point']
]) {
  video.currentTime = t;
  context.testUI.handleKeydown({key,target:{tagName:'DIV'},preventDefault(){}});
  if (expected) assert.equal(context.testUI.events().at(-1).type, expected);
}
assert.deepEqual(context.testUI.events().map(e => e.type), ['serve','point','point'],
  'S/A/B/N/Z keyboard behavior remains intact');

context.testUI.setEvents([{t:10,type:'serve'},{t:14,type:'point',winner:'A',highlight:true}]);
context.testUI.paint(foldState(2));
video.currentTime = 77;
const star = {dataset:{highlight:'1'}};
node('stream').scrollTop = 173;
let starPropagationStopped = false;
node('stream').listeners.click({target:target({
  '[data-highlight]':star, '.ev':{dataset:{i:'1'}}
}), stopPropagation(){starPropagationStopped=true;}});
assert.equal(context.testUI.events()[1].highlight, undefined, 'mouse star toggles');
assert.equal(video.currentTime, 77, 'mouse star does not trigger row seek');
assert.equal(starPropagationStopped, true, 'star click propagation is explicitly stopped');
assert.equal(context.testUI.state().eventScroll.mode, 'preserve');
assert.equal(context.testUI.state().eventScroll.top, 173,
  'star untoggle requests a scroll-preserving repaint');
context.testUI.paint(foldState(2), context.testUI.state().eventScroll);
assert.equal(node('stream').scrollTop, 173, 'star untoggle preserves scrollTop after repaint');
assert.equal(node('highlightCount').textContent, 0, 'highlight count updates after removal');
assert.doesNotMatch(node('stream').innerHTML, /highlight-badge/);

node('stream').scrollTop = 173;
node('stream').listeners.click({target:target({'[data-highlight]':star})});
assert.equal(context.testUI.events()[1].highlight, true, 'mouse star toggles on');
assert.equal(context.testUI.state().eventScroll.mode, 'preserve');
assert.equal(context.testUI.state().eventScroll.top, 173,
  'star toggle requests a scroll-preserving repaint');
context.testUI.paint(foldState(2), context.testUI.state().eventScroll);
assert.equal(node('stream').scrollTop, 173, 'star toggle preserves scrollTop after repaint');
assert.equal(node('highlightCount').textContent, 1);
assert.match(node('stream').innerHTML, /highlight-badge">★ 精彩球/);

// Candidate review, confirmed serve, and normal scoring all clear old selection.
context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'},
  {t:20,type:'serve'},{t:25,type:'point',winner:'B'}
]);
context.testUI.selectPointAt(1);
clickRally({'[data-rally]': row});
assert.equal(context.testUI.state().selectedPointIndex, -1, 'candidate preview clears selection');
context.testUI.selectPointAt(1);
clickRally({'[data-rally]': row, '[data-rally-serve]': {dataset: {rallyServe: '0'}}});
assert.equal(context.testUI.state().selectedPointIndex, -1, 'confirm serve clears selection');
context.testUI.setEvents([
  {t:10,type:'serve'},{t:14,type:'point',winner:'A'},
  {t:20,type:'serve'},{t:25,type:'point',winner:'B'}
]);
context.testUI.selectPointAt(1);
video.currentTime = 40;
context.testUI.handleKeydown({key:'S',target:{tagName:'DIV'},preventDefault(){}});
video.currentTime = 44;
context.testUI.handleKeydown({key:'A',target:{tagName:'DIV'},preventDefault(){}});
assert.equal(context.testUI.state().selectedPointIndex, -1, 'new scoring clears selection');
context.testUI.handleKeydown({key:'H',target:{tagName:'DIV'},preventDefault(){}});
assert.equal(context.testUI.events().at(-1).highlight, true,
  'H naturally targets the newly completed rally after scoring');

context.testUI.setEvents([{t:1.8,type:'serve'},{t:2.7,type:'point',winner:'B',highlight:true}]);
const crossFile = context.testUI.normalizeEvents(context.testUI.docPayload().events);
assert.equal(crossFile[1].t, 2.7);
assert.equal(crossFile[1].highlight, true, 'global-time highlight reloads unchanged');
context.testUI.clearMatch();
assert.equal(context.testUI.events().length, 0, 'new match clears highlights with events');
assert.equal(context.testUI.events().some(e => e.highlight), false);
assert.equal(context.testUI.state().selectedPointIndex, -1, 'new match clears UI selection');

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

(async () => {
  const exact = '北港媽祖盃全國桌球錦標賽_國小男童一年級以下單打賽_許宸愷(光復國小)VS曾柏誠(吉林國小).mp4';
  assert.equal(context.testUI.safeFilenamePart(' ._賽/\\:*?"<>|\x00\x1f\x7f\u202e事__._ '), '賽_事');
  assert.equal(context.testUI.introFilename({tournament:'賽事'}), null);
  assert.equal(context.testUI.introFilename({...Object.fromEntries(
    [['tournament','賽事'],['category','組別'],['playerA','甲'],['schoolA','()'],
     ['playerB','乙'],['schoolB','學校']])}), null);
  let calls = [];
  let suggestedName = exact;
  node('organization').value = 'same-folder';
  context.fetch = async (url, options) => {
    calls.push({url, body: options.body && JSON.parse(options.body)});
    if (url === '/suggest-output') {
      const base = calls.at(-1).body.customOutput ? '/chosen/custom' : '/matches/' + suggestedName.replace(/\.mp4$/, '');
      const out = base + '.mp4';
      return {json: async () => ({path: out, layout:{out,
        thumbnail:base+'.thumbnail.jpg', tags:base+'.tags.json'}})};
    }
    if (url === '/save-as') return {ok: true, json: async () => ({path: '/chosen/custom.mp4'})};
    throw Error('unexpected fetch: ' + url);
  };
  context.testUI.setSource('/matches/raw.MOV', '/matches/raw.cut.mp4');
  const fields = {introTournament:'北港媽祖盃全國桌球錦標賽',
    introCategory:'國小男童一年級以下單打賽', introPlayerA:'許宸愷',
    introSchoolA:'光復國小', introPlayerB:'曾柏誠', introSchoolB:'吉林國小'};
  for (const [id, value] of Object.entries(fields)) node(id).value = value;
  node('introEnabled').checked = false;
  node('introSchoolB').listeners.input();
  assert.equal(context.testUI.state().outPath, '/matches/' + exact);
  assert.equal(node('outPath').textContent, '/matches/' + exact);
  await Promise.resolve(); await Promise.resolve();
  assert.equal(calls.at(-1).url, '/suggest-output');
  assert.equal(calls.at(-1).body.intro.enabled, false);
  suggestedName = exact.replace(/\.mp4$/, '_2.mp4');
  node('introSchoolB').listeners.input();
  await new Promise(setImmediate);
  assert.equal(node('outPath').textContent, '/matches/' + suggestedName);
  assert.equal(context.testUI.state().outPath, '/matches/' + exact);
  await node('saveAs').listeners.click();
  assert.equal(calls.at(-2).url, '/save-as');
  assert.equal(calls.at(-2).body.intro.schoolB, '吉林國小');
  assert.equal(context.testUI.state().outPath, '/chosen/custom.mp4');
  assert.equal(context.testUI.state().customOutput, true);
  const before = calls.length;
  node('introTournament').value = '新賽事';
  node('introTournament').listeners.input();
  assert.equal(context.testUI.state().outPath, '/chosen/custom.mp4');
  assert.equal(calls.length, before + 1);
  context.testUI.clearMatch();
  assert.equal(context.testUI.state().customOutput, false);
  assert.equal(context.testUI.state().outPath, '');
  assert.equal(context.testUI.state().sources.length, 0);
  context.testUI.setSource('/matches/raw.MOV', '/matches/raw.cut.mp4');
  node('introTournament').listeners.input();
  assert.equal(context.testUI.state().outPath, '/matches/raw.cut.mp4');
  node('organization').value = 'match-folder';
  node('introTournament').listeners.input();
  assert.equal(node('outPath').textContent, '/matches/raw.cut/raw.cut.mp4');
  assert.equal(node('tagsPath').textContent,
    '/matches/raw.cut/ttcut-data/raw.cut.tags.json');
  const segments = [
    {path:'/matches/DJI_0015.MP4',duration:2,offset:0,end:2,w:320,h:180,codec:'h264',fps_frac:'30/1',audio:{codec_name:'aac'}},
    {path:'/matches/DJI_0016.MP4',duration:3,offset:2,end:5,w:320,h:180,codec:'h264',fps_frac:'30/1',audio:{codec_name:'aac'}}
  ];
  const multiVideo = {currentTime:0,duration:2,paused:true,clientWidth:0,
    pause(){this.paused=true;},play(){this.paused=false;return Promise.resolve();},
    load(){this.loads=(this.loads||0)+1;},remove(){}};
  context.testUI.setVideo(multiVideo);
  context.testUI.setSources({sources:segments,geometryMismatch:false});
  assert.equal(context.testUI.sourceForTime(2).index, 1);
  assert.equal(context.testUI.sourceForTime(2).local, 0);
  assert.equal(context.testUI.sourceForTime(3.25).local, 1.25);
  context.testUI.seekGlobal(2.4);
  assert.equal(context.testUI.state().activeSegment, 1);
  assert.ok(Math.abs(context.testUI.state().pendingSeek-.4)<.001);
  assert.equal(context.testUI.now(), 2.4);
  assert.match(multiVideo.src, /segment=1/);
  assert.equal(multiVideo.loads, 1);
  context.testUI.setEvents([{t:2.7,type:'serve'}]);
  node('stream').listeners.click({target: target({'.ev': {dataset:{i:'0'}}})});
  assert.ok(Math.abs(context.testUI.state().pendingSeek-.7)<.001);
  assert.equal(context.testUI.now(), 2.7);
  context.testUI.setCandidates([{start:1.8,end:2.2,duration:.4,confidence:.8,
    confidenceTier:'normal',motionMean:1,motionPeak:2,sideBalance:1}]);
  clickRally({'[data-rally]': row});
  assert.equal(context.testUI.state().activeSegment, 0);
  context.testUI.clearMatch();
  assert.equal(node('organization').value, 'match-folder');
  console.log('UI interaction checks passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
