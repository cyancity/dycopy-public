// Logged-in profile crawler for one-click and daily incremental updates.
'use strict';

const SERVICE = 'http://127.0.0.1:8788';
const DAILY_ALARM = 'dycopy-daily-profile-update';
const PAGE_DELAY_MS = 800;
const PROFILE_DELAY_MS = 1000;
const LIST_TIMEOUT_MS = 15000;
const MAX_PROFILE_ATTEMPTS = 5;

function fetchAllWorksInPage(secUid, { listTimeoutMs, pageDelayMs }) {
  return (async () => {
    const params = {
      sec_user_id: secUid, aid: '6383', device_platform: 'webapp',
      version_code: '170400', version_name: '17.4.0', publish_video_strategy_type: '2',
      channel: 'channel_pc_web', count: '50',
    };
    const match = (document.body?.innerText || '').match(/作品\s*(\d+)/);
    const expectedCount = match ? Number(match[1]) : null;
    const itemsById = new Map();
    let cursor = '0';
    let incomplete = false;
    for (let page = 0; page < 100; page++) {
      const query = new URLSearchParams({ ...params, max_cursor: cursor });
      let response;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), listTimeoutMs);
      try {
        response = await fetch('/aweme/v1/web/aweme/post/?' + query, {
          credentials: 'include', signal: controller.signal,
        });
      } catch (error) {
        const message = controller.signal.aborted ? '列表请求超过15秒' : '列表请求失败: ' + String(error);
        return { error: message, expectedCount, items: [...itemsById.values()] };
      } finally {
        clearTimeout(timeout);
      }
      let data;
      try { data = await response.json(); }
      catch (error) {
        return { error: '列表响应无法解析: HTTP ' + response.status, expectedCount, items: [...itemsById.values()] };
      }
      if (!response.ok || data.status_code !== 0) {
        return { error: '列表接口状态异常: HTTP ' + response.status + ', status=' + data.status_code,
                 expectedCount, items: [...itemsById.values()] };
      }
      const pageItems = data.aweme_list || [];
      for (const item of pageItems) {
        const id = String(item.aweme_id || '');
        if (id && !itemsById.has(id)) itemsById.set(id, item);
      }
      const next = String(data.max_cursor || '');
      if (!data.has_more) break;
      if (!pageItems.length || next === cursor || page === 99) {
        incomplete = true;
        break;
      }
      cursor = next;
      await new Promise(resolve => setTimeout(resolve, pageDelayMs));
    }
    return { items: [...itemsById.values()], expectedCount, incomplete };
  })();
}

async function api(path, options) {
  const response = await fetch(SERVICE + path, options);
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || ('HTTP ' + response.status));
  return data;
}

function waitForTab(tabId, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      error ? reject(error) : resolve();
    };
    const onUpdated = (updatedId, changeInfo) => {
      if (updatedId === tabId && changeInfo.status === 'complete') finish();
    };
    const timer = setTimeout(() => finish(new Error('等待抖音主页加载超时')), timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId).then(tab => {
      if (tab.status === 'complete') finish();
    }).catch(finish);
  });
}

async function updateAllProfiles(source) {
  const status = await api('/api/status');
  if (status.current || status.queue?.length) {
    return { skipped: true, reason: 'dyCopy 下载队列正在处理初始任务，请队列清空后再更新' };
  }
  const data = await api('/api/profiles');
  const profiles = [...new Map((data.profiles || []).map(p => [p.sec_uid, p])).values()];
  const summary = { source, profiles: 0, queued: 0, duplicates: 0, incomplete: 0,
                    incompleteProfiles: [], failures: [] };
  let tabId = null;
  try {
    for (let index = 0; index < profiles.length; index++) {
      const profile = profiles[index];
      await chrome.storage.local.set({ updateProgress: {
        running: true, index: index + 1, total: profiles.length, sec_uid: profile.sec_uid,
      } });
      try {
        const url = 'https://www.douyin.com/user/' + encodeURIComponent(profile.sec_uid);
        let result = null;
        let lastError = '';
        for (let attempt = 0; attempt < MAX_PROFILE_ATTEMPTS; attempt++) {
          try {
            if (tabId === null) {
              const tab = await chrome.tabs.create({ url, active: false });
              tabId = tab.id;
              await waitForTab(tabId);
            } else {
              await chrome.tabs.update(tabId, { url });
              await waitForTab(tabId);
            }
            await new Promise(resolve => setTimeout(resolve, 1000));
            const [injected] = await chrome.scripting.executeScript({
              target: { tabId }, world: 'MAIN', func: fetchAllWorksInPage,
              args: [profile.sec_uid, { listTimeoutMs: LIST_TIMEOUT_MS, pageDelayMs: PAGE_DELAY_MS }],
            });
            const candidate = injected?.result || {};
            if (candidate.error) throw new Error(candidate.error);
            const candidateItems = candidate.items || [];
            const expected = candidate.expectedCount;
            const incomplete = candidate.incomplete ||
              (expected !== null && expected !== undefined && candidateItems.length < expected);
            if (incomplete) {
              lastError = `列表不完整: 页面显示${expected ?? '未知'}条,接口返回${candidateItems.length}条`;
              if (attempt + 1 < MAX_PROFILE_ATTEMPTS) continue;
              summary.incomplete++;
              summary.incompleteProfiles.push({ sec_uid: profile.sec_uid,
                expected, found: candidateItems.length, attempts: MAX_PROFILE_ATTEMPTS });
              break;
            }
            result = candidate;
            break;
          } catch (error) {
            lastError = String(error).slice(0, 180);
            if (attempt + 1 < MAX_PROFILE_ATTEMPTS) continue;
          }
        }
        if (!result) {
          if (lastError) summary.failures.push({ sec_uid: profile.sec_uid, error: lastError });
          continue;
        }
        const items = result.items || [];
        let profileQueued = 0;
        let profileDuplicates = 0;
        for (let offset = 0; offset < items.length; offset += 2000) {
          const batch = await api('/api/enqueue_items', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sec_uid: profile.sec_uid, items: items.slice(offset, offset + 2000) }),
          });
          profileQueued += batch.queued || 0;
          profileDuplicates += (batch.results || []).filter(item => item.status === 'duplicate').length;
        }
        summary.profiles++;
        summary.queued += profileQueued;
        summary.duplicates += profileDuplicates;
        await chrome.storage.local.set({ updateProgress: {
          running: true, index: index + 1, total: profiles.length, sec_uid: profile.sec_uid,
          found: items.length, queued: profileQueued,
        } });
      } catch (error) {
        summary.failures.push({ sec_uid: profile.sec_uid, error: String(error).slice(0, 180) });
      }
      await new Promise(resolve => setTimeout(resolve, PROFILE_DELAY_MS));
    }
  } finally {
    if (tabId !== null) await chrome.tabs.remove(tabId).catch(() => {});
  }
  summary.finishedAt = new Date().toISOString();
  await chrome.storage.local.set({ lastUpdate: summary, updateProgress: { running: false, ...summary } });
  return summary;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'dycopy-update-all') return false;
  updateAllProfiles('manual').then(summary => sendResponse({ ok: true, summary }))
    .catch(error => sendResponse({ ok: false, error: String(error) }));
  return true;
});

async function ensureDailyAlarm() {
  const alarm = await chrome.alarms.get(DAILY_ALARM);
  if (!alarm) chrome.alarms.create(DAILY_ALARM, { delayInMinutes: 1440, periodInMinutes: 1440 });
}

chrome.runtime.onInstalled.addListener(ensureDailyAlarm);
chrome.runtime.onStartup.addListener(ensureDailyAlarm);
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === DAILY_ALARM) updateAllProfiles('daily').catch(async error => {
    await chrome.storage.local.set({ updateProgress: {
      running: false, error: String(error).slice(0, 180), finishedAt: new Date().toISOString(),
    } });
  });
});

ensureDailyAlarm().catch(() => {});
