#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dycopy_service — 本机 localhost 队列服务(Chrome 扩展的后端)
==========================================================
仅监听 127.0.0.1,给 Chrome 扩展提供四个接口:

  GET  /api/status    队列状态: 排队中/当前处理(含进度)/最近完成
  GET  /api/profile   单主页状态: 未处理/排队中/处理中/完成/部分失败/失败
  POST /api/enqueue   批量入队(主页链接或 sec_uid,按 sec_uid 去重)
  POST /api/enqueue_items 登录态完整作品数据入队(按 aweme_id 去重)
  POST /api/retry     重试: 指定主页或全部有失败项的主页重新入队

下载在本机由单 worker 线程严格串行执行,不开并发。
状态复用 sync_role_library 的 processed_aweme_ids.json:
  - 作品按 aweme_id 去重,成功提取过即永久跳过(即使手动删掉媒体文件)
  - 失败项留在 last_failures / live_photo_failures,重跑该主页即重试
  - 主页完成状态(profile_scans)与作品状态分开记录

安全限制:
  - 仅 bind 127.0.0.1;校验 Host 头(防 DNS rebinding)与 Origin 头
    (仅放行 chrome-extension:// 与 127.0.0.1/localhost,缺失时放行 curl)
  - 输入只接受主页链接 / sec_uid,拒绝作品链接与短链(逐条返回原因)
  - 请求体 ≤ 64MB,单主页 ≤ 2000 条作品

用法:
  .venv/bin/python dycopy_service.py [--port 8788] [--state ...] [--output ...]
  Ctrl+C 停止。停止后 Chrome 扩展会提示重新启动的方法。
"""

import argparse
import json
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dycopy
import sync_role_library as srl

APP_VERSION = '0.1.0'
MAX_BODY = 64 * 1024 * 1024  # 登录态作品批次可能包含大量媒体 URL
MAX_BATCH = 500            # 单次入队条数上限
MAX_ITEM_BATCH = 2000      # 单作者作品数上限

ALLOWED_ORIGIN_RE = re.compile(
    r'^(chrome-extension://[a-z]+|https?://(?:127\.0\.0\.1|localhost)(?::\d+)?)$',
    re.IGNORECASE)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class QueueManager:
    """串行队列 + 状态快照。所有变更在锁内,读取返回拷贝。
    pending 用 queue.Queue 保证入队通知与顺序,dedup 集合在锁内维护。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending_q = queue.Queue()
        self._pending_set = set()
        self._pending_display = []   # [(sec_uid, added_at)] 仅供快照展示
        self._current = None        # {sec_uid, started_at, index, total, aweme_id, progress_at}
        self._recent = []           # 最近完成的主页结果,最新在前
        self._enqueue_results = []  # 最近一次入队的逐条结果(供扩展展示)
        self.worker_alive = False
        self._pending_works = {}    # sec_uid -> [aweme_id] 待处理的作品补录(列表被过滤的)
        self._pending_items = {}    # sec_uid -> [完整登录态作品数据]
        self._queued_aweme_ids = set()

    # ---- 主页入队 ----
    def enqueue(self, sec_uids):
        """主页去重入队。返回逐条结果 [{sec_uid, status, message}]。"""
        results = []
        with self._lock:
            for sec_uid in sec_uids:
                if self._current and self._current['sec_uid'] == sec_uid:
                    results.append({'sec_uid': sec_uid, 'status': 'duplicate',
                                    'message': '正在处理中'})
                elif sec_uid in self._pending_set:
                    results.append({'sec_uid': sec_uid, 'status': 'duplicate',
                                    'message': '已在队列中'})
                else:
                    self._pending_set.add(sec_uid)
                    self._pending_display.append((sec_uid, now()))
                    self._pending_q.put(sec_uid)
                    results.append({'sec_uid': sec_uid, 'status': 'queued', 'message': '已加入队列'})
            self._enqueue_results = results[-MAX_BATCH:]
        return results

    # ---- 作品补录入队 ----
    def enqueue_work(self, aweme_id, sec_uid):
        """作品补录(列表接口被过滤的作品):按 aweme_id 去重,绑定到主页。
        若主页在队列/处理中则跟随同批;否则把主页入队并在处理前先补录该作品。"""
        with self._lock:
            if aweme_id in self._queued_aweme_ids:
                return {'aweme_id': aweme_id, 'sec_uid': sec_uid,
                        'status': 'duplicate', 'message': '该作品已在下载队列中'}
            self._queued_aweme_ids.add(aweme_id)
            if self._current and self._current['sec_uid'] == sec_uid:
                self._pending_works.setdefault(sec_uid, []).append(aweme_id)
                return {'aweme_id': aweme_id, 'sec_uid': sec_uid,
                        'status': 'queued', 'message': '已加入队列(主页处理前先补录该作品)'}
            if sec_uid in self._pending_set:
                self._pending_works.setdefault(sec_uid, []).append(aweme_id)
                return {'aweme_id': aweme_id, 'sec_uid': sec_uid,
                        'status': 'queued', 'message': '已加入队列(主页处理前先补录该作品)'}
            self._pending_set.add(sec_uid)
            self._pending_display.append((sec_uid, now()))
            self._pending_works.setdefault(sec_uid, []).append(aweme_id)
            self._pending_q.put(sec_uid)
            return {'aweme_id': aweme_id, 'sec_uid': sec_uid,
                    'status': 'queued', 'message': '已加入队列(主页处理前先补录该作品)'}

    def pending_uids(self):
        with self._lock:
            return [u for u, _ in self._pending_display]

    def pending_works(self, sec_uid):
        with self._lock:
            return list(self._pending_works.get(sec_uid, []))

    def take_works(self, sec_uid):
        with self._lock:
            return self._pending_works.pop(sec_uid, [])

    def enqueue_items(self, sec_uid, items):
        """原子加入一个主页的完整登录态作品批次,并按 aweme_id 防重复。"""
        with self._lock:
            results = []
            fresh = []
            for item in items:
                aid = str(item.get('aweme_id') or '')
                if aid in self._queued_aweme_ids:
                    results.append({'aweme_id': aid, 'sec_uid': sec_uid,
                                    'status': 'duplicate', 'message': '该作品已在下载队列中'})
                    continue
                self._queued_aweme_ids.add(aid)
                fresh.append(item)
                results.append({'aweme_id': aid, 'sec_uid': sec_uid,
                                'status': 'queued', 'message': '完整作品数据已加入队列'})
            if not fresh:
                return results
            self._pending_items.setdefault(sec_uid, []).extend(fresh)
            if self._current and self._current['sec_uid'] == sec_uid:
                return results
            if sec_uid not in self._pending_set:
                self._pending_set.add(sec_uid)
                self._pending_display.append((sec_uid, now()))
                self._pending_q.put(sec_uid)
            return results

    def mark_aweme_done(self, aweme_id):
        with self._lock:
            self._queued_aweme_ids.discard(str(aweme_id))

    def take_items(self, sec_uid):
        with self._lock:
            return self._pending_items.pop(sec_uid, [])

    def get(self, timeout=0.5):
        """worker 阻塞取任务;超时或停止返回 None。"""
        try:
            sec_uid = self._pending_q.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            self._pending_set.discard(sec_uid)
            self._pending_display = [(u, t) for u, t in self._pending_display if u != sec_uid]
            self._current = {'sec_uid': sec_uid, 'started_at': now(),
                             'index': 0, 'total': 0, 'aweme_id': None, 'progress_at': None}
        return sec_uid

    def set_progress(self, index, total, aweme_id):
        with self._lock:
            if self._current:
                self._current.update(index=index, total=total, aweme_id=aweme_id,
                                     progress_at=now())

    def finish(self, sec_uid, stats):
        with self._lock:
            self._current = None
            if self._pending_items.get(sec_uid) or self._pending_works.get(sec_uid):
                if sec_uid not in self._pending_set:
                    self._pending_set.add(sec_uid)
                    self._pending_display.append((sec_uid, now()))
                    self._pending_q.put(sec_uid)
            entry = {'sec_uid': sec_uid, 'finished_at': now(),
                     'status': stats.get('status'), 'new': stats.get('new', 0),
                     'failed': stats.get('failed', 0), 'skipped': stats.get('skipped', 0),
                     'listed': stats.get('listed', 0),
                     'live_saved': stats.get('live_saved', 0),
                     'live_failed': stats.get('live_failed', 0),
                     'error': stats.get('error')}
            self._recent = ([entry] + self._recent)[:20]

    # ---- 读取快照 ----
    def snapshot(self):
        with self._lock:
            return {
                'worker_alive': self.worker_alive,
                'queue': [{'sec_uid': u, 'added_at': t} for u, t in self._pending_display],
                'current': dict(self._current) if self._current else None,
                'recent': [dict(r) for r in self._recent],
                'last_enqueue_results': [dict(r) for r in self._enqueue_results],
            }


class Service:
    def __init__(self, args):
        self.args = args
        self.state_path = args.state
        self.output = args.output
        self.profiles_path = args.profiles
        self.temp_root = args.state.parent / 'staging'
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.queue = QueueManager()
        self.state = srl.create_state(self.state_path)
        self.browser = None   # 惰性创建,按 profile 复用
        self.session = requests.Session()
        self._stop = threading.Event()

    # ---- worker ----
    def worker_loop(self):
        self.queue.worker_alive = True
        try:
            while not self._stop.is_set():
                sec_uid = self.queue.get(timeout=0.5)
                if sec_uid is None:
                    continue
                try:
                    if self.browser is None:
                        self.browser = dycopy.BrowserSession()
                    self.state = srl.create_state(self.state_path)  # 保持与磁盘一致
                    stats = self.run_one_profile(sec_uid)
                except Exception as exc:  # 兜底: 单个主页异常不杀 worker
                    stats = {'status': 'failed', 'new': 0, 'failed': 0, 'skipped': 0,
                             'listed': 0, 'live_saved': 0, 'live_failed': 0,
                             'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
                    self._log(f'[worker 异常] {sec_uid}: {exc}')
                    # 浏览器实例可能已坏,重建避免后续主页连环失败
                    try:
                        if self.browser:
                            self.browser.close()
                    except Exception:
                        pass
                    self.browser = None
                self.queue.finish(sec_uid, stats)
        finally:
            self.queue.worker_alive = False

    def run_one_profile(self, sec_uid):
        """处理一个主页:先补录绑定作品(列表被过滤的),再全量翻页。"""
        full_items = self.queue.take_items(sec_uid)
        work_ids = self.queue.take_works(sec_uid)
        if full_items:
            total = len(full_items) + len(work_ids)
            added = skipped = failed = 0
            new_ids = []
            for index, item in enumerate(full_items, 1):
                aid = str(item.get('aweme_id') or '')
                try:
                    if not aid or aid in self.state['processed_aweme_ids']:
                        skipped += 1
                        continue
                    self.queue.set_progress(index, total, aid)
                    record = srl.process_work(
                        self.browser, self.session, self.state, sec_uid,
                        item, self.output, self.temp_root)
                    self.state.get('last_failures', {}).pop(aid, None)
                    srl.atomic_save(self.state_path, self.state)
                    added += 1
                    new_ids.append(aid)
                    self._log(f'  [登录态作品 {index}/{total}] {aid} -> {record["role_dir"]}')
                except Exception as exc:
                    failed += 1
                    self.state.setdefault('last_failures', {})[aid] = {
                        'sec_uid': sec_uid,
                        'error': f'{type(exc).__name__}: {str(exc)[:240]}',
                        'at': now(),
                    }
                    srl.atomic_save(self.state_path, self.state)
                    self._log(f'  [作品失败] {aid}: {type(exc).__name__}: {exc}')
                finally:
                    if aid:
                        self.queue.mark_aweme_done(aid)

            for offset, aweme_id in enumerate(work_ids, len(full_items) + 1):
                try:
                    if aweme_id in self.state['processed_aweme_ids']:
                        skipped += 1
                        continue
                    self.queue.set_progress(offset, total, aweme_id)
                    record = srl.process_work_by_id(
                        self.browser, self.session, self.state, self.state_path,
                        sec_uid, aweme_id, self.output, self.temp_root)
                    if record:
                        self.state.get('last_failures', {}).pop(aweme_id, None)
                        srl.atomic_save(self.state_path, self.state)
                        added += 1
                        new_ids.append(aweme_id)
                        self._log(f'  [补录] {aweme_id} -> {record["role_dir"]}')
                except Exception as exc:
                    failed += 1
                    self.state.setdefault('last_failures', {})[aweme_id] = {
                        'sec_uid': sec_uid,
                        'error': f'{type(exc).__name__}: {str(exc)[:240]}',
                        'at': now(),
                    }
                    srl.atomic_save(self.state_path, self.state)
                    self._log(f'  [补录失败] {aweme_id}: {type(exc).__name__}: {exc}')
                finally:
                    self.queue.mark_aweme_done(aweme_id)

            status = 'partial' if failed else 'complete'
            self.state.setdefault('profile_scans', {})[sec_uid] = {
                'status': status, 'last_scan_at': now(),
                'listed_aweme_count': len(full_items), 'new_works': added,
                'failed_works': failed, 'skipped_by_aweme_id': skipped,
                'live_saved': 0, 'live_failed': 0,
                'source': 'logged_in_payload',
            }
            srl.atomic_save(self.state_path, self.state)
            return {'status': status, 'listed': len(full_items), 'new': added,
                    'failed': failed, 'skipped': skipped, 'new_ids': new_ids,
                    'live_saved': 0, 'live_failed': 0}

        # 兼容作品 ID 补录接口;完整数据批次优先走上面的登录态路径。
        for aweme_id in work_ids:
            try:
                if aweme_id in self.state['processed_aweme_ids']:
                    continue
                record = srl.process_work_by_id(
                    self.browser, self.session, self.state, self.state_path,
                    sec_uid, aweme_id, self.output, self.temp_root)
                if record:
                    self.state.get('last_failures', {}).pop(aweme_id, None)
                    srl.atomic_save(self.state_path, self.state)
                    self._log(f'  [补录] {aweme_id} -> {record["role_dir"]}')
            except Exception as exc:
                self.state.setdefault('last_failures', {})[aweme_id] = {
                    'sec_uid': sec_uid,
                    'error': f'{type(exc).__name__}: {str(exc)[:240]}',
                    'at': now(),
                }
                srl.atomic_save(self.state_path, self.state)
                self._log(f'  [补录失败] {aweme_id}: {type(exc).__name__}: {exc}')
            finally:
                self.queue.mark_aweme_done(aweme_id)
        # 再全量翻页
        return srl.run_profile(
            self.browser, self.session, self.state, self.state_path,
            sec_uid, self.output, self.temp_root,
            progress=self.queue.set_progress, log=self._log)

    @staticmethod
    def _log(message):
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {message}', flush=True)

    def start_worker(self):
        thread = threading.Thread(target=self.worker_loop, name='dycopy-worker', daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()
        if self.browser:
            self.browser.close()
        self.session.close()

    # ---- 领域逻辑 ----
    def resolve_batch(self, lines):
        """逐条把用户输入解析为 sec_uid;入队并落盘 profile_sources.txt。"""
        sec_uids = []
        parsed = []
        seen = set()
        for line in lines:
            sec_uid, err = srl.validate_profile_input(line)
            if not sec_uid:
                parsed.append({'input': line, 'sec_uid': None, 'status': 'invalid', 'message': err})
                continue
            if sec_uid in seen:
                parsed.append({'input': line, 'sec_uid': sec_uid, 'status': 'duplicate', 'message': '本批次内重复'})
                continue
            seen.add(sec_uid)
            sec_uids.append(sec_uid)
            parsed.append({'input': line, 'sec_uid': sec_uid, 'status': 'ok', 'message': ''})
        results = self.queue.enqueue(sec_uids) if sec_uids else []
        result_by_sec = {r['sec_uid']: r for r in results}
        for item in parsed:
            if item['status'] == 'ok':
                r = result_by_sec.get(item['sec_uid'], {'status': 'queued', 'message': ''})
                item['status'], item['message'] = r['status'], r.get('message', '')
        self._persist_sources(sec_uids)
        return parsed

    def _persist_sources(self, sec_uids):
        """新主页追加进 profile_sources.txt,CLI 批处理同步可见。"""
        if not sec_uids:
            return
        try:
            known = set(srl.parse_sources(self.profiles_path)) if self.profiles_path.exists() else set()
            fresh = [u for u in sec_uids if u not in known]
            if fresh:
                self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.profiles_path, 'a', encoding='utf-8') as f:
                    for u in fresh:
                        f.write(u + '\n')
        except Exception as exc:
            self._log(f'[sources 写入失败] {exc}')

    def resolve_work_batch(self, lines):
        """逐条把作品链接/裸ID解析为 aweme_id + 目标主页 sec_uid,绑定入队。
        sec_uid 取链接里的参数(/user/、sec_uid=),缺省则取当前主页。"""
        parsed = []
        for line in lines:
            aid, err = srl.validate_work_input(line)
            if not aid:
                parsed.append({'input': line, 'aweme_id': None, 'sec_uid': None,
                               'status': 'invalid', 'message': err})
                continue
            # 作品的目标主页:链接里的 sec_uid/secUid 参数或 /user/ 段
            m = re.search(r'[?&][sS][eE][cC]_?[uU][iI][dD]=([A-Za-z0-9_-]{20,})', line) or re.search(r'/user/([^/?#\s]+)', line)
            sec_uid = m.group(1) if m else None
            if not sec_uid:
                parsed.append({'input': line, 'aweme_id': aid, 'sec_uid': None,
                               'status': 'invalid',
                               'message': '作品链接里没有 sec_uid 参数;请在作品所在主页操作,或粘贴含 sec_uid 的链接'})
                continue
            if aid in self.state['processed_aweme_ids']:
                parsed.append({'input': line, 'aweme_id': aid, 'sec_uid': sec_uid,
                               'status': 'duplicate', 'message': '已提取过(状态文件有记录,即使删了文件也跳过)'})
                continue
            result = self.queue.enqueue_work(aid, sec_uid)
            result['input'] = line
            parsed.append(result)
        return parsed

    def resolve_item_batch(self, sec_uid, items):
        """校验登录态主页列表里的完整作品对象,按作品 ID 入队。"""
        parsed = []
        fresh = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                parsed.append({'aweme_id': None, 'sec_uid': sec_uid,
                               'status': 'invalid', 'message': '作品数据必须是对象'})
                continue
            aid = str(item.get('aweme_id') or '')
            if not srl.AWEME_ID_RE.fullmatch(aid):
                parsed.append({'aweme_id': aid or None, 'sec_uid': sec_uid,
                               'status': 'invalid', 'message': '作品 ID 格式非法'})
                continue
            if aid in seen:
                parsed.append({'aweme_id': aid, 'sec_uid': sec_uid,
                               'status': 'duplicate', 'message': '本批次作品 ID 重复'})
                continue
            seen.add(aid)
            if aid in self.state['processed_aweme_ids']:
                parsed.append({'aweme_id': aid, 'sec_uid': sec_uid,
                               'status': 'duplicate', 'message': '已按作品 ID 提取过'})
                continue
            fresh.append(item)
        if fresh:
            queued = self.queue.enqueue_items(sec_uid, fresh)
            parsed.extend(queued)
            self._persist_sources([sec_uid])
        return parsed

    def retry_targets(self, sec_uid=None):
        """重试目标: 指定主页,或全部存在失败项的主页。返回入队结果。"""
        if sec_uid:
            targets = [sec_uid]
        else:
            targets = []
            seen = set()
            for rec in self.state.get('last_failures', {}).values():
                u = rec.get('sec_uid')
                if u and u not in seen:
                    seen.add(u)
                    targets.append(u)
            for key in self.state.get('live_photo_failures', {}):
                aid = key.partition(':')[0]
                rec = self.state['processed_aweme_ids'].get(aid)
                u = rec.get('sec_uid') if rec else None
                if u and u not in seen:
                    seen.add(u)
                    targets.append(u)
        return targets, self.queue.enqueue(targets) if targets else []

    def profile_view(self, sec_uid):
        """单主页状态视图(从磁盘 state 读,避免与 worker 写冲突)。"""
        state = srl.create_state(self.state_path)
        scan = (state.get('profile_scans') or {}).get(sec_uid)
        meta = (state.get('profile_metadata') or {}).get(sec_uid) or {}
        snap = self.queue.snapshot()
        if snap['current'] and snap['current']['sec_uid'] == sec_uid:
            status = 'processing'
        elif sec_uid in {q['sec_uid'] for q in snap['queue']}:
            status = 'queued'
        elif scan:
            status = scan.get('status', 'unprocessed')  # complete / partial / failed
        else:
            status = 'unprocessed'
        works = [r for r in state['processed_aweme_ids'].values() if r.get('sec_uid') == sec_uid]
        failed_works = {aid: f for aid, f in (state.get('last_failures') or {}).items()
                        if f.get('sec_uid') == sec_uid}
        live_failed = 0
        for key in (state.get('live_photo_failures') or {}):
            rec = state['processed_aweme_ids'].get(key.partition(':')[0])
            if rec and rec.get('sec_uid') == sec_uid:
                live_failed += 1
        return {
            'sec_uid': sec_uid,
            'status': status,
            'nickname': meta.get('nickname') or (works[-1]['nickname'] if works else ''),
            'uid': meta.get('uid') or (works[-1]['uid'] if works else ''),
            'role_dir': meta.get('role_dir') or '',
            'processed_works': len(works),
            'failed_works': len(failed_works),
            'live_failed': live_failed,
            'failed_details': [{'aweme_id': aid, 'error': f.get('error'), 'at': f.get('at')}
                               for aid, f in list(failed_works.items())[:20]],
            'last_scan': scan,
            'current_progress': snap['current'] if status == 'processing' else None,
        }

    def all_profiles_view(self):
        state = srl.create_state(self.state_path)
        known = {}
        for sec_uid in self._known_sec_uids(state):
            known[sec_uid] = self.profile_view(sec_uid)
        return list(known.values())

    def _known_sec_uids(self, state):
        seen = []
        seen_set = set()

        def add(u):
            if u and u not in seen_set:
                seen_set.add(u)
                seen.append(u)

        for u in self.queue.pending_uids():
            add(u)
        for u in (state.get('profile_scans') or {}):
            add(u)
        for u in (state.get('profile_metadata') or {}):
            add(u)
        for rec in state['processed_aweme_ids'].values():
            add(rec.get('sec_uid'))
        if self.profiles_path.exists():
            for u in srl.parse_sources(self.profiles_path):
                add(u)
        return seen


# ---------------------------------------------------------------- HTTP 层

def make_handler(service: Service):
    allowed_hosts = {f'127.0.0.1:{service.args.port}', f'localhost:{service.args.port}',
                     '127.0.0.1', 'localhost'}

    class Handler(BaseHTTPRequestHandler):
        server_version = f'dycopy_service/{APP_VERSION}'

        # ---- 安全校验 ----
        def _check_source(self):
            host = (self.headers.get('Host') or '').strip()
            if host not in allowed_hosts:
                self._send_json(403, {'ok': False, 'error': f'禁止的 Host: {host}'})
                return False
            origin = self.headers.get('Origin')
            if origin and not ALLOWED_ORIGIN_RE.match(origin):
                self._send_json(403, {'ok': False, 'error': f'禁止的 Origin: {origin}'})
                return False
            return True

        def _send_json(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0:
                return {}, None
            if length > MAX_BODY:
                return None, f'请求体超过 {MAX_BODY // (1024 * 1024)}MB 限制'
            try:
                return json.loads(self.rfile.read(length).decode('utf-8')), None
            except Exception:
                return None, '请求体不是合法 JSON'

        def log_message(self, fmt, *args):  # 精简日志,绝不抛异常(参数可能是 HTTPStatus 等对象)
            try:
                text = (str(fmt) % tuple(args)) if args else str(fmt)
            except Exception:
                text = ' '.join([str(fmt)] + [str(a) for a in args])
            if '/api/' in text or text.startswith('"'):
                print(f'[{datetime.now().strftime("%H:%M:%S")}] HTTP {text}', flush=True)

        # ---- 路由 ----
        def do_GET(self):
            if not self._check_source():
                return
            parsed = urlparse(self.path)
            if parsed.path == '/api/status':
                snap = service.queue.snapshot()
                snap.update({
                    'ok': True, 'service': 'dycopy', 'version': APP_VERSION,
                    'state_counts': {
                        'processed_works': len(service.state['processed_aweme_ids']),
                        'profiles': len(service.state.get('profile_scans') or {}),
                        'failed_works': len(service.state.get('last_failures') or {}),
                        'live_failed': len(service.state.get('live_photo_failures') or {}),
                    },
                    'output': str(service.output),
                })
                self._send_json(200, snap)
            elif parsed.path == '/api/profiles':
                self._send_json(200, {'ok': True, 'profiles': service.all_profiles_view()})
            elif parsed.path == '/api/profile':
                sec_uid = (parse_qs(parsed.query).get('sec_uid') or [''])[0].strip()
                if not srl.SEC_UID_RE.fullmatch(sec_uid):
                    self._send_json(400, {'ok': False, 'error': '缺少或非法的 sec_uid 参数'})
                    return
                self._send_json(200, {'ok': True, 'profile': service.profile_view(sec_uid)})
            else:
                self._send_json(404, {'ok': False, 'error': '未知接口'})

        def do_POST(self):
            if not self._check_source():
                return
            parsed = urlparse(self.path)
            if parsed.path not in ('/api/enqueue', '/api/enqueue_work', '/api/enqueue_items', '/api/retry'):
                self._send_json(404, {'ok': False, 'error': '未知接口'})
                return
            payload, err = self._read_json()
            if err:
                self._send_json(400, {'ok': False, 'error': err})
                return
            if parsed.path == '/api/enqueue':
                raw = payload.get('urls')
                if not isinstance(raw, list) or not raw:
                    self._send_json(400, {'ok': False, 'error': '需要 urls 数组(主页链接或 sec_uid)'})
                    return
                if len(raw) > MAX_BATCH:
                    self._send_json(400, {'ok': False, 'error': f'单次最多 {MAX_BATCH} 条'})
                    return
                lines = [str(x)[:500] for x in raw]
                results = service.resolve_batch(lines)
                queued = sum(1 for r in results if r['status'] == 'queued')
                self._send_json(200, {'ok': True, 'results': results, 'queued': queued,
                                      'queue': service.queue.snapshot()['queue']})
            elif parsed.path == '/api/enqueue_work':
                raw = payload.get('urls')
                if not isinstance(raw, list) or not raw:
                    self._send_json(400, {'ok': False, 'error': '需要 urls 数组(作品链接或 aweme_id)'})
                    return
                if len(raw) > MAX_BATCH:
                    self._send_json(400, {'ok': False, 'error': f'单次最多 {MAX_BATCH} 条'})
                    return
                lines = [str(x)[:500] for x in raw]
                results = service.resolve_work_batch(lines)
                queued = sum(1 for r in results if r['status'] == 'queued')
                self._send_json(200, {'ok': True, 'results': results, 'queued': queued,
                                      'queue': service.queue.snapshot()['queue']})
            elif parsed.path == '/api/enqueue_items':
                sec_uid = str(payload.get('sec_uid') or '').strip()
                items = payload.get('items')
                if not srl.SEC_UID_RE.fullmatch(sec_uid):
                    self._send_json(400, {'ok': False, 'error': 'sec_uid 格式非法'})
                    return
                if not isinstance(items, list) or not items:
                    self._send_json(400, {'ok': False, 'error': '需要非空 items 数组'})
                    return
                if len(items) > MAX_ITEM_BATCH:
                    self._send_json(400, {'ok': False, 'error': f'单主页最多 {MAX_ITEM_BATCH} 条作品'})
                    return
                results = service.resolve_item_batch(sec_uid, items)
                queued = sum(1 for r in results if r['status'] == 'queued')
                self._send_json(200, {'ok': True, 'results': results, 'queued': queued,
                                      'queue': service.queue.snapshot()['queue']})
            else:  # /api/retry
                sec_uid = str(payload.get('sec_uid') or '').strip()
                if sec_uid and not srl.SEC_UID_RE.fullmatch(sec_uid):
                    self._send_json(400, {'ok': False, 'error': 'sec_uid 格式非法'})
                    return
                targets, results = service.retry_targets(sec_uid or None)
                self._send_json(200, {'ok': True, 'targets': targets, 'results': results,
                                      'queue': service.queue.snapshot()['queue']})

    return Handler


def main():
    parser = argparse.ArgumentParser(description='dycopy 本机队列服务(Chrome 扩展后端,仅监听 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8788, help='监听端口(默认 8788,需与扩展一致)')
    parser.add_argument('--state', type=Path, default=srl.DEFAULT_STATE, help='本地作品 ID 状态 JSON')
    parser.add_argument('--output', type=Path, default=srl.DEFAULT_OUTPUT, help='role 素材目录')
    parser.add_argument('--profiles', type=Path, default=srl.DEFAULT_PROFILES, help='主页清单(入队会追加)')
    args = parser.parse_args()

    service = Service(args)
    worker = service.start_worker()
    handler = make_handler(service)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    print(f'[dycopy_service] v{APP_VERSION} 已启动: http://127.0.0.1:{args.port}', flush=True)
    print(f'  状态文件: {args.state}', flush=True)
    print(f'  素材目录: {args.output}', flush=True)
    print('  接口: GET /api/status | GET /api/profiles | GET /api/profile?sec_uid='
          ' | POST /api/enqueue | POST /api/retry', flush=True)
    print('  Ctrl+C 停止(停止后 Chrome 扩展会提示重新启动的方法)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[dycopy_service] 正在停止...', flush=True)
    finally:
        server.shutdown()
        service.stop()
        worker.join(timeout=10)
        print('[dycopy_service] 已停止', flush=True)


if __name__ == '__main__':
    main()
