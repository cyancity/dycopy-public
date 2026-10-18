import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../extension/background.js', import.meta.url), 'utf8');
const SEC_UID = 'TEST_PROFILE_ABCDEFGHIJKLMNOP';
const FIRST_ID = '1111111111111111111';
const SECOND_ID = '2222222222222222222';
const delays = [];
const listener = { addListener() {}, removeListener() {} };
const sandbox = {
  chrome: {
    runtime: { onMessage: listener, onInstalled: listener, onStartup: listener },
    alarms: { get: async () => ({}), onAlarm: listener },
  },
};
vm.createContext(sandbox);
vm.runInContext(source + '\nglobalThis.crawler = fetchAllWorksInPage; globalThis.update = updateAllProfiles;', sandbox);

async function inject(func, args, pages, expectedCount = 2) {
  const requests = [];
  const context = {
    args: structuredClone(args), URLSearchParams, AbortController,
    document: { body: { innerText: `作品 ${expectedCount}` } },
    setTimeout(callback, ms) {
      delays.push(ms);
      return setTimeout(callback, Math.min(ms, 1));
    },
    clearTimeout,
    fetch: async (url, options) => {
      assert.equal(options.credentials, 'include');
      assert.ok(options.signal instanceof AbortSignal);
      const query = new URL(url, 'https://www.douyin.com').searchParams;
      assert.equal(query.get('sec_user_id'), SEC_UID);
      requests.push(query.get('max_cursor'));
      assert.ok(pages.length, 'crawler must stop when pagination finishes');
      return { ok: true, status: 200, json: async () => pages.shift() };
    },
  };
  const result = await vm.runInNewContext('(' + func.toString() + ')(...args)', context);
  return { result: structuredClone(result), requests };
}

const config = { listTimeoutMs: 37, pageDelayMs: 11 };
let run = await inject(sandbox.crawler, [SEC_UID, config], [
  { status_code: 0, aweme_list: [{ aweme_id: FIRST_ID }], has_more: 1, max_cursor: 'next' },
  { status_code: 0, aweme_list: [{ aweme_id: FIRST_ID }, { aweme_id: SECOND_ID }], has_more: 0 },
]);
assert.deepEqual(run.requests, ['0', 'next']);
assert.deepEqual(run.result.items.map(item => item.aweme_id), [FIRST_ID, SECOND_ID]);
assert.equal(run.result.expectedCount, 2);
assert.equal(run.result.incomplete, false);
assert.ok(delays.includes(config.listTimeoutMs), 'injected timeout comes from explicit args');
assert.ok(delays.includes(config.pageDelayMs), 'injected delay comes from explicit args');
console.log('[PASS] isolated injection preserves configuration, deduplicates, and paginates');

run = await inject(sandbox.crawler, [SEC_UID, config], [
  { status_code: 0, aweme_list: [{ aweme_id: FIRST_ID }], has_more: 1, max_cursor: '0' },
]);
assert.equal(run.result.incomplete, true);
assert.equal(run.requests.length, 1);
console.log('[PASS] a repeated cursor stops pagination and reports an incomplete result');

run = await inject(sandbox.crawler, [SEC_UID, config], [
  { status_code: 0, aweme_list: [], has_more: 1, max_cursor: 'next' },
]);
assert.equal(run.result.incomplete, true);
assert.equal(run.requests.length, 1);
console.log('[PASS] an empty page stops pagination');

run = await inject(sandbox.crawler, [SEC_UID, config], [
  { status_code: 1, aweme_list: [] },
]);
assert.match(run.result.error, /status=1/);
console.log('[PASS] API errors return an explicit failure');

let captured;
const sent = [];
sandbox.setTimeout = (callback, ms) => setTimeout(callback, Math.min(ms, 1));
sandbox.clearTimeout = clearTimeout;
sandbox.fetch = async (url, options) => {
  const path = new URL(url).pathname;
  if (path === '/api/status') return { ok: true, json: async () => ({ ok: true, queue: [], current: null }) };
  if (path === '/api/profiles') return { ok: true, json: async () => ({ ok: true, profiles: [{ sec_uid: SEC_UID }] }) };
  assert.equal(path, '/api/enqueue_items');
  sent.push(JSON.parse(options.body));
  return { ok: true, json: async () => ({ ok: true, queued: 1, results: [] }) };
};
sandbox.chrome.storage = { local: { set: async () => {} } };
sandbox.chrome.tabs = {
  create: async () => ({ id: 1 }), get: async () => ({ status: 'complete' }),
  remove: async () => {}, onUpdated: listener,
};
sandbox.chrome.scripting = {
  async executeScript({ func, args }) {
    captured = structuredClone(args);
    const { result } = await inject(func, args, [
      { status_code: 0, aweme_list: [{ aweme_id: FIRST_ID }], has_more: 0 },
    ], 1);
    return [{ result }];
  },
};
const summary = await sandbox.update('test');
assert.equal(captured[0], SEC_UID);
assert.deepEqual(captured[1], { listTimeoutMs: 15000, pageDelayMs: 800 });
assert.equal(summary.profiles, 1);
assert.equal(summary.queued, 1);
assert.equal(summary.failures.length, 0);
assert.equal(sent[0].items[0].aweme_id, FIRST_ID);
console.log('[PASS] daily/manual update passes configuration through executeScript args');
