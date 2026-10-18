// dyCopy 主页入队 — popup 逻辑
// 仅与本机 dycopy_service (127.0.0.1:8788) 通信;读取当前抖音主页后入队。
'use strict';

const SERVICE = 'http://127.0.0.1:8788';
const STATUS_LABEL = {
  unprocessed: '未处理',
  queued: '排队中',
  processing: '处理中',
  complete: '完成',
  partial: '部分失败',
  failed: '失败',
};

const $ = (id) => document.getElementById(id);
let currentProfile = null; // {sec_uid, nickname, uid} 当前页解析结果
let pollTimer = null;

// ---------------------------------------------------------------- 服务通信

async function api(path, options) {
  const resp = await fetch(SERVICE + path, options);
  if (!resp.ok) {
    let msg = 'HTTP ' + resp.status;
    try { msg = (await resp.json()).error || msg; } catch (e) { /* 忽略 */ }
    throw new Error(msg);
  }
  return resp.json();
}

function apiPost(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function checkService() {
  try {
    const status = await api('/api/status');
    $('service-dot').className = 'dot on';
    $('service-dot').title = `服务已连接 v${status.version}`;
    $('offline-box').hidden = true;
    return status;
  } catch (e) {
    $('service-dot').className = 'dot off';
    $('service-dot').title = '服务未连接: ' + e.message;
    showOffline();
    return null;
  }
}

function showOffline() {
  const isWin = navigator.platform.toUpperCase().indexOf('WIN') >= 0;
  $('start-cmd').textContent = isWin
    ? '.venv\\Scripts\\python.exe dycopy_service.py'
    : '.venv/bin/python dycopy_service.py';
  $('offline-box').hidden = false;
}

// ---------------------------------------------------------------- 主页读取

// 在页面 MAIN world 执行: 从 RENDER_DATA/_ROUTER_DATA 深搜用户信息,
// 失败退回 DOM(h1 昵称 + "抖音号" 文本)。必须是自包含函数。
function readProfileInPage() {
  const secMatch = location.pathname.match(/\/user\/([^/?#]+)/);
  const secUid = secMatch ? secMatch[1] : '';
  const result = { sec_uid: secUid, nickname: '', uid: '' };

  const deepFind = (node, depth) => {
    if (!node || typeof node !== 'object' || depth > 8) return null;
    if (node.sec_uid === secUid && (node.nickname || node.uid || node.unique_id)) {
      return node;
    }
    for (const key of Object.keys(node)) {
      const found = deepFind(node[key], depth + 1);
      if (found) return found;
    }
    return null;
  };

  const tryParse = (text) => {
    try { return JSON.parse(text); } catch (e) { return null; }
  };

  // 候选数据源: RENDER_DATA script 标签 / window 全局
  const sources = [];
  const rd = document.getElementById('RENDER_DATA');
  if (rd && rd.textContent) {
    try { sources.push(JSON.parse(decodeURIComponent(rd.textContent))); }
    catch (e) { const raw = tryParse(rd.textContent); if (raw) sources.push(raw); }
  }
  for (const key of ['_ROUTER_DATA', '__RENDER_DATA__', 'RENDER_DATA', '_SSR_DATA']) {
    try { if (window[key]) sources.push(window[key]); } catch (e) { /* 页面隔离时忽略 */ }
  }

  for (const src of sources) {
    const user = deepFind(src, 0);
    if (user) {
      result.nickname = String(user.nickname || user.sec_name || '');
      result.uid = String(user.uid || user.unique_id || '');
      break;
    }
  }

  if (!result.nickname) {
    const h1 = document.querySelector('h1');
    if (h1) result.nickname = (h1.textContent || '').trim();
  }
  if (!result.uid) {
    const m = (document.body && document.body.innerText || '')
      .match(/抖音号[:：]?\s*([A-Za-z0-9_\-]+)/);
    if (m) result.uid = m[1];
  }
  return result;
}

// 在页面 MAIN world 自己翻页拿完整作品数据(登录态,不受匿名限流)
async function fetchAllWorksInPage(secUid) {
  const params = {
    sec_user_id: secUid, aid: '6383', device_platform: 'webapp',
    version_code: '170400', version_name: '17.4.0', publish_video_strategy_type: '2',
    channel: 'channel_pc_web', count: '50',
  };
  const countMatch = (document.body?.innerText || '').match(/作品\s*(\d+)/);
  const expectedCount = countMatch ? Number(countMatch[1]) : null;
  const itemsById = new Map();
  let cursor = '0';
  let incomplete = false;
  for (let page = 0; page < 100; page++) {
    const q = new URLSearchParams({...params, max_cursor: cursor});
    const r = await fetch('/aweme/v1/web/aweme/post/?' + q, {credentials: 'include'});
    const d = await r.json();
    if (d.status_code !== 0) return { error: '接口返回 status_code=' + d.status_code, expectedCount, items: [...itemsById.values()] };
    const items = d.aweme_list || [];
    for (const it of items) {
      const aid = String(it.aweme_id || '');
      if (aid && !itemsById.has(aid)) itemsById.set(aid, it);
    }
    const next = String(d.max_cursor || '');
    if (!d.has_more) break;
    if (!items.length || next === cursor || page === 99) { incomplete = true; break; }
    cursor = next;
    await new Promise(r => setTimeout(r, 800));
  }
  return { items: [...itemsById.values()], expectedCount, incomplete };
}

async function readActiveProfile() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return null;
  // 作品页: /video/xxxx 或 /note/xxxx,读 aweme_id 和关联主页
  const workM = tab.url.match(/https?:\/\/(?:www\.)?douyin\.com\/(?:video|note)\/(\d{15,22})/);
  if (workM) {
    const uidM = tab.url.match(/[?&]sec_uid=([A-Za-z0-9_-]{20,})/);
    return { kind: 'work', aweme_id: workM[1],
             sec_uid: uidM ? uidM[1] : '', url: tab.url };
  }
  if (!/https?:\/\/(www\.)?douyin\.com\/user\//.test(tab.url)) return null;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: 'MAIN',
      func: readProfileInPage,
    });
    return result && result.sec_uid ? Object.assign({kind: 'profile'}, result) : null;
  } catch (e) {
    const m = tab.url.match(/\/user\/([^/?#]+)/);
    return m ? { kind: 'profile', sec_uid: m[1], nickname: '', uid: '' } : null;
  }
}

// ---------------------------------------------------------------- 渲染

function setStatusBadge(status) {
  const badge = $('p-status');
  badge.textContent = STATUS_LABEL[status] || status;
  badge.className = 'badge ' + status;
}

async function renderProfile() {
  const profile = await readActiveProfile();
  currentProfile = profile;
  $('profile-box').hidden = false;
  $('not-profile').hidden = !!profile;
  const isProfile = profile && profile.kind === 'profile';
  const isWork = profile && profile.kind === 'work';
  $('profile-card').style.display = isProfile ? '' : 'none';
  $('work-card').hidden = !isWork;
  if (!profile) return;
  if (isWork) {
    $('w-aweme').textContent = profile.aweme_id;
    // 作品链接里没带 sec_uid 时,提示用户在对应主页操作
    const btn = $('btn-enqueue-work');
    btn.disabled = !profile.sec_uid;
    btn.textContent = profile.sec_uid ? '补录该作品' : '补录该作品(需在主页打开作品)';
    return;
  }

  $('p-nickname').textContent = profile.nickname || '(页面未提供)';
  $('p-uid').textContent = profile.uid || '(页面未提供)';
  $('p-secuid').textContent = profile.sec_uid;
  $('p-secuid').title = profile.sec_uid;

  let info = null;
  try {
    const resp = await api('/api/profile?sec_uid=' + encodeURIComponent(profile.sec_uid));
    info = resp.profile;
  } catch (e) { /* 服务刚掉线时由 checkService 提示 */ }

  if (!info) {
    setStatusBadge('unprocessed');
    $('p-detail-row').hidden = true;
  } else {
    setStatusBadge(info.status);
    const parts = [];
    if (info.processed_works) parts.push(`已提取 ${info.processed_works} 个作品`);
    if (info.failed_works) parts.push(`失败 ${info.failed_works} 个`);
    if (info.live_failed) parts.push(`Live 待重试 ${info.live_failed} 个`);
    if (info.last_scan) parts.push(`上次扫描 ${String(info.last_scan.last_scan_at || '').slice(0, 16).replace('T', ' ')}`);
    $('p-detail').textContent = parts.join(' · ') || '暂无记录';
    $('p-detail-row').hidden = false;
    if (!info.nickname && profile.nickname) info.nickname = profile.nickname;
    if (!info.uid && profile.uid) info.uid = profile.uid;
  }

  const busy = info && (info.status === 'queued' || info.status === 'processing');
  const btn = $('btn-enqueue');
  btn.disabled = !!busy;
  btn.textContent = busy ? (info.status === 'queued' ? '已在队列中' : '正在处理') : '加入队列';

  const canRetry = info && (info.failed_works > 0 || info.live_failed > 0 || info.status === 'failed');
  $('btn-retry-profile').hidden = !canRetry;

  // 主页时显示抓取全部按钮(登录态全量)
  $('btn-fetch-all').hidden = !(profile.kind === 'profile');
}

function renderBatchResults(results) {
  const box = $('batch-results');
  box.innerHTML = '';
  for (const r of results) {
    const div = document.createElement('div');
    const cls = r.status === 'invalid' ? 'bad' : (r.status === 'duplicate' ? 'dup' : 'ok');
    const label = { queued: '已入队', duplicate: '重复', invalid: '无效' }[r.status] || r.status;
    div.className = cls;
    const nick = r.sec_uid ? r.sec_uid.slice(0, 16) + '…' : String(r.input).slice(0, 40);
    div.textContent = `${label}  ${nick}${r.message ? ' — ' + r.message : ''}`;
    box.appendChild(div);
  }
}

function renderQueue(status) {
  const count = status.queue.length + (status.current ? 1 : 0);
  $('queue-count').textContent = count ? `(${count})` : '(空)';

  const cur = $('queue-current');
  if (status.current) {
    const c = status.current;
    const progress = c.total ? ` ${c.index}/${c.total}` : '';
    cur.textContent = `正在处理 ${c.sec_uid.slice(0, 20)}…${progress}`;
    cur.hidden = false;
  } else {
    cur.hidden = true;
  }

  const list = $('queue-list');
  list.innerHTML = '';
  for (const q of status.queue) {
    const li = document.createElement('li');
    li.textContent = q.sec_uid.slice(0, 24) + '…';
    list.appendChild(li);
  }

  const recent = status.recent[0];
  $('queue-recent').textContent = recent
    ? `最近完成: ${recent.sec_uid.slice(0, 16)}… 新增 ${recent.new} / 失败 ${recent.failed} / 跳过 ${recent.skipped}`
    : '';

  const counts = status.state_counts || {};
  const hasFailures = (counts.failed_works || 0) + (counts.live_failed || 0) > 0;
  $('btn-retry-all').hidden = !hasFailures;
}

async function refreshStatus() {
  const status = await checkService();
  if (status) renderQueue(status);
}

// ---------------------------------------------------------------- 事件

$('btn-reconnect').addEventListener('click', refreshStatus);

$('btn-update-all').addEventListener('click', async () => {
  const btn = $('btn-update-all');
  const progress = $('update-progress');
  btn.disabled = true;
  progress.textContent = '正在依次检查已登记主页…';
  try {
    const result = await chrome.runtime.sendMessage({ type: 'dycopy-update-all' });
    if (!result?.ok) throw new Error(result?.error || '更新失败');
    const r = result.summary;
    if (r.skipped) {
      progress.textContent = '暂未更新: ' + r.reason;
      return;
    }
    progress.textContent = `完成 ${r.profiles} 个主页：新增入队 ${r.queued}，重复 ${r.duplicates}` +
      (r.incomplete ? `；${r.incomplete} 个主页的接口结果少于页面作品数` : '') +
      (r.failures?.length ? `；失败 ${r.failures.length} 个` : '');
    await refreshStatus();
  } catch (e) {
    progress.textContent = '更新失败: ' + e.message;
  } finally {
    btn.disabled = false;
  }
});

$('btn-enqueue').addEventListener('click', async () => {
  if (!currentProfile) return;
  const btn = $('btn-enqueue');
  btn.disabled = true;
  try {
    const resp = await apiPost('/api/enqueue', { urls: [currentProfile.sec_uid] });
    renderBatchResults(resp.results);
    await refreshStatus();
    await renderProfile();
  } catch (e) {
    alert('入队失败: ' + e.message);
    btn.disabled = false;
  }
});

$('btn-fetch-all').addEventListener('click', async () => {
  if (!currentProfile || currentProfile.kind !== 'profile') return;
  const btn = $('btn-fetch-all');
  btn.disabled = true;
  const prog = $('fetch-progress');
  prog.hidden = false;
  prog.textContent = '正在页面内翻页抓取完整作品数据...';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: 'MAIN', func: fetchAllWorksInPage,
      args: [currentProfile.sec_uid],
    });
    if (result.error) { prog.textContent = '翻页失败: ' + result.error; btn.disabled = false; return; }
    const items = result.items || [];
    if (!items.length) { prog.textContent = '没有获取到作品'; btn.disabled = false; return; }
    prog.textContent = `找到 ${items.length} 个作品,正在提交媒体数据...`;
    const resp = await apiPost('/api/enqueue_items', {
      sec_uid: currentProfile.sec_uid,
      items,
    });
    const queued = resp.queued || 0;
    const dup = resp.results.filter(r => r.status === 'duplicate').length;
    const bad = resp.results.filter(r => r.status === 'invalid').length;
    const shortfall = result.incomplete || (result.expectedCount != null && items.length < result.expectedCount)
      ? `；页面显示 ${result.expectedCount} 条，接口仅返回 ${items.length} 条，需复核` : '';
    prog.textContent = `共 ${items.length} 个:新增入队 ${queued},已提取或重复 ${dup},无效 ${bad}${shortfall}`;
    await refreshStatus();
  } catch (e) {
    prog.textContent = '抓取失败: ' + e.message;
  }
  btn.disabled = false;
});

$('btn-retry-profile').addEventListener('click', async () => {
  if (!currentProfile) return;
  try {
    const resp = await apiPost('/api/retry', { sec_uid: currentProfile.sec_uid });
    $('retry-results').textContent = resp.targets.length
      ? `已重新入队 ${resp.targets.length} 个主页`
      : '该主页没有可重试的失败项';
    await refreshStatus();
    await renderProfile();
  } catch (e) {
    alert('重试失败: ' + e.message);
  }
});

$('btn-enqueue-work').addEventListener('click', async () => {
  if (!currentProfile || currentProfile.kind !== 'work') return;
  const btn = $('btn-enqueue-work');
  btn.disabled = true;
  try {
    const resp = await apiPost('/api/enqueue_work', { urls: [currentProfile.url] });
    const r = resp.results[0];
    if (r.status === 'queued') {
      btn.textContent = '已入队(主页处理前先补录)';
    } else if (r.status === 'duplicate') {
      btn.textContent = '已提取过,跳过';
    } else {
      alert('补录失败: ' + (r.message || ''));
      btn.disabled = false;
    }
    await refreshStatus();
  } catch (e) {
    alert('补录失败: ' + e.message);
    btn.disabled = false;
  }
});

$('btn-batch').addEventListener('click', async () => {
  const lines = $('batch-input').value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!lines.length) return;
  const btn = $('btn-batch');
  btn.disabled = true;
  try {
    const resp = await apiPost('/api/enqueue', { urls: lines });
    renderBatchResults(resp.results);
    $('batch-input').value = '';
    await refreshStatus();
  } catch (e) {
    alert('批量入队失败: ' + e.message);
  } finally {
    btn.disabled = false;
  }
});

$('btn-retry-all').addEventListener('click', async () => {
  try {
    const resp = await apiPost('/api/retry', {});
    $('retry-results').textContent = `已重新入队 ${resp.targets.length} 个有失败项的主页`;
    await refreshStatus();
    await renderProfile();
  } catch (e) {
    alert('重试失败: ' + e.message);
  }
});

// ---------------------------------------------------------------- 启动

(async () => {
  await refreshStatus();
  await renderProfile();
  const refreshUpdateProgress = async () => {
    try {
      const { updateProgress, lastUpdate } = await chrome.storage.local.get(['updateProgress', 'lastUpdate']);
      if (updateProgress?.running) {
        $('update-progress').textContent = `更新中 ${updateProgress.index}/${updateProgress.total} 个主页…`;
      } else if (updateProgress?.error) {
        $('update-progress').textContent = `上次更新失败: ${updateProgress.error}`;
      } else if (lastUpdate?.finishedAt) {
        $('update-progress').textContent = `上次更新 ${lastUpdate.finishedAt.slice(0, 16).replace('T', ' ')} · 新增入队 ${lastUpdate.queued}` +
          (lastUpdate.incomplete ? ` · ${lastUpdate.incomplete} 个主页需复核` : '') +
          (lastUpdate.failures?.length ? ` · 失败 ${lastUpdate.failures.length} 个` : '');
      }
    } catch (e) { /* 存储不可用时不影响主页入队 */ }
  };
  await refreshUpdateProgress();
  // popup 存活期间每 2s 轮询队列和增量更新状态
  pollTimer = setInterval(() => { refreshStatus(); refreshUpdateProgress(); }, 2000);
})();
