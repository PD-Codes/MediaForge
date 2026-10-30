const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/mediaforge/web/static/gap_downloads.js', 'utf8');
function setup(fetch) {
  const window = {t: (_, en) => en};
  const context = {window, fetch, AbortController, DOMException, setTimeout, clearTimeout};
  vm.runInNewContext(code, context);
  return window.MFGapDownloads;
}
function ctx() { return {cancel: new AbortController()}; }
function response(data, status = 200, type = 'application/json') {
  return {ok: status < 400, status, headers: {get: () => type}, json: async () => data};
}
test('proxy errors report HTTP status without parsing HTML', async () => {
  const api = setup(async () => ({ok: false, status: 502, headers: {get: () => 'text/html'}, json: () => { throw Error('invalid JSON'); }}));
  await assert.rejects(api.request('/api/series?url=x', ctx()), /HTTP 502/);
});
test('successful searches are cached and failures are not', async () => {
  let calls = 0;
  const api = setup(async () => { calls++; return response({}, calls === 1 ? 503 : 200); });
  await assert.rejects(api.request('/api/search', ctx(), {keyword:'Example'}));
  await api.request('/api/search', ctx(), {keyword:'Example'});
  await api.request('/api/search', ctx(), {keyword:'Example'});
  assert.equal(calls, 2);
});
test('missing IMDb information falls back to an exact title', async () => {
  const api = setup(async (url) => response(url === '/api/search' ? {results:[{title:'Example',url:'https://x'}]} : {imdb_id:''}));
  const found = await api.find({title:'Example [imdbid-tt123]'}, ctx(), [{id:'x',label:'X'}]);
  assert.equal(found.match, 0);
});
test('requests are limited and waiting requests can be cancelled', async () => {
  let active = 0, max = 0;
  const api = setup(async () => { active++; max = Math.max(active, max); await new Promise(r => setTimeout(r, 10)); active--; return response({}); });
  await Promise.all(Array.from({length: 12}, (_, i) => api.request('/api/series?url=' + i, ctx())));
  assert.equal(max, 3);
  const cancelled = ctx(); cancelled.cancel.abort();
  await assert.rejects(api.request('/api/series?url=x', cancelled), {name: 'AbortError'});
});
test('series selection sends missing slots instead of a permanent ignore', () => {
  const stats = fs.readFileSync('src/mediaforge/web/static/stats.js', 'utf8');
  const start = stats.indexOf('function mediaIgnoreSelected()');
  const end = stats.indexOf('/** Restore one slot', start);
  let payload;
  const context = {_ignSel:{series:{folder:['S1E2','S2']},slots:{},titles:{folder:'Example'}}, _gapRows:{folder:{missing:['S1E2','S2']}}, fetch: (_, options) => { payload = JSON.parse(options.body); return {then: () => ({then: () => ({catch: () => {}})})}; }, Object};
  vm.runInNewContext(stats.slice(start, end) + '\nmediaIgnoreSelected();', context);
  assert.deepEqual(JSON.parse(JSON.stringify(payload.items)), [{folder:'folder',title:'Example',slots:['S1E2','S2']}]);
});
test('batch stops searching after a source matches', async () => {
  const calls = [];
  const api = setup(async (url, options) => {
    calls.push([url, options && options.body]);
    if (url === '/api/search') return response({results:[{title:'Example',url:'https://x'}]});
    if (url.startsWith('/api/seasons')) return response({seasons:[{season_number:1,url:'https://x/s1'}]});
    if (url.startsWith('/api/episodes')) return response({episodes:[{episode_number:2,url:'https://x/e2'}]});
    return response({});
  });
  await api.prepare({title:'Example',missing:['S1E2']}, ctx(), [{id:'one',label:'One'},{id:'two',label:'Two'}]);
  assert.equal(calls.filter(call => call[0] === '/api/search').length, 1);
});
