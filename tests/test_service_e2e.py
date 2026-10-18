# -*- coding: utf-8 -*-
"""真实 loopback HTTP、临时状态库和合成作品的离线服务验收。"""
import copy
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dycopy_service as service_module
import sync_role_library as srl

PROFILE_A = 'TEST_PROFILE_A_ABCDEFGHIJKLMNOP'
PROFILE_B = 'TEST_PROFILE_B_ABCDEFGHIJKLMNOP'
OLD_ID = '1111111111111111111'
NEW_ID = '2222222222222222222'
OTHER_ID = '3333333333333333333'
RETRY_ID = '4444444444444444444'


def video_item(aweme_id, sec_uid=PROFILE_A):
    return {
        'aweme_id': aweme_id,
        'desc': '离线测试作品',
        'author': {'nickname': '合成测试作者', 'uid': 'TEST_AUTHOR', 'sec_uid': sec_uid},
        'video': {'play_addr': {'url_list': [f'https://media.example.invalid/{aweme_id}.mp4']}},
    }


class FakeBrowser:
    def __init__(self):
        self.profiles = {}
        self.details = {}
        self.started = []
        self.detail_calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.active = 0
        self.max_active = 0
        self.closed = False

    def fetch_user_posts(self, sec_uid):
        self.started.append(sec_uid)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(timeout=5):
                raise RuntimeError('test browser gate timed out')
            return copy.deepcopy(self.profiles.get(sec_uid, []))
        finally:
            self.active -= 1

    def fetch_aweme(self, aweme_id):
        self.detail_calls.append(aweme_id)
        return copy.deepcopy(self.details[aweme_id])

    def close(self):
        self.closed = True


class ServiceE2E(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dycopy-service-test-')
        self.root = Path(self.directory.name)
        args = SimpleNamespace(
            state=self.root / 'processed_aweme_ids.json',
            output=self.root / 'downloads',
            profiles=self.root / 'profile_sources.txt',
            port=0,
        )
        self.service = service_module.Service(args)
        self.browser = FakeBrowser()
        self.service.browser = self.browser
        self.downloads = Counter()
        self.fail_once = set()
        self.downloading = 0
        self.max_downloading = 0
        self.download_patch = patch.object(srl, 'download_to_temp', self.fake_download)
        self.download_patch.start()
        original_request = requests.sessions.Session.request

        def loopback_only(session, method, url, *args, **kwargs):
            if urlparse(str(url)).hostname != '127.0.0.1':
                raise AssertionError(f'external network request blocked: {urlparse(str(url)).hostname}')
            return original_request(session, method, url, *args, **kwargs)

        self.network_patch = patch.object(requests.sessions.Session, 'request', loopback_only)
        self.network_patch.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        args.port = self.server.server_port
        self.server.RequestHandlerClass = service_module.make_handler(self.service)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        self.http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.http_thread.start()
        self.worker = self.service.start_worker()
        self.base = f'http://127.0.0.1:{args.port}'
        self.client = requests.Session()
        self.client.trust_env = False
        self.wait_for(lambda: self.get('/api/status')['worker_alive'])

    def tearDown(self):
        self.browser.release.set()
        self.service._stop.set()
        self.worker.join(timeout=6)
        self.server.shutdown()
        self.http_thread.join(timeout=2)
        self.server.server_close()
        self.service.stop()
        self.client.close()
        self.network_patch.stop()
        self.download_patch.stop()
        self.directory.cleanup()
        self.assertFalse(self.worker.is_alive(), 'worker must stop before its fixture is removed')

    def fake_download(self, session, url, dest, video=False):
        aweme_id = Path(urlparse(url).path).stem
        self.downloads[aweme_id] += 1
        self.downloading += 1
        self.max_downloading = max(self.max_downloading, self.downloading)
        try:
            if aweme_id in self.fail_once:
                self.fail_once.remove(aweme_id)
                raise RuntimeError('synthetic first-attempt failure')
            dest.write_bytes(b'fixture-media' * 200)
        finally:
            self.downloading -= 1

    def request(self, method, path, **kwargs):
        return self.client.request(method, self.base + path, timeout=3, **kwargs)

    def get(self, path):
        response = self.request('GET', path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, path, body):
        response = self.request('POST', path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait_for(self, predicate, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.02)
        self.fail('timed out waiting for the loopback service')

    def wait_idle(self, completed):
        def idle():
            status = self.get('/api/status')
            return status if not status['queue'] and status['current'] is None and len(status['recent']) >= completed else None
        return self.wait_for(idle)

    def profile(self, sec_uid=PROFILE_A):
        return self.get('/api/profile?sec_uid=' + sec_uid)['profile']

    def test_source_and_input_validation(self):
        self.assertEqual(self.request('GET', '/api/status', headers={'Origin': 'https://untrusted.example'}).status_code, 403)
        self.assertEqual(self.request('GET', '/api/status', headers={'Host': 'untrusted.example'}).status_code, 403)
        self.assertEqual(self.request('GET', '/api/status', headers={'Origin': 'chrome-extension://abcdefghijklmnop'}).status_code, 200)
        self.assertEqual(self.request('GET', '/api/nope').status_code, 404)
        self.assertEqual(self.request('GET', '/api/profile?sec_uid=invalid').status_code, 400)
        self.assertEqual(self.request('POST', '/api/enqueue', data='not-json').status_code, 400)
        self.assertEqual(self.request('POST', '/api/enqueue', json={'urls': []}).status_code, 400)
        self.assertEqual(self.request('POST', '/api/retry', json={'sec_uid': 'invalid'}).status_code, 400)
        self.assertEqual(self.request('POST', '/api/enqueue_items', json={'sec_uid': PROFILE_A, 'items': []}).status_code, 400)
        self.assertEqual(self.request('POST', '/api/enqueue_items', json={'sec_uid': 'invalid', 'items': [{}]}).status_code, 400)
        invalid = self.post('/api/enqueue', {'urls': [
            f'https://www.douyin.com/video/{OLD_ID}', 'https://v.douyin.com/TEST/', 'invalid',
        ]})
        self.assertEqual([row['status'] for row in invalid['results']], ['invalid'] * 3)
        self.assertEqual(invalid['queued'], 0)
        self.assertEqual(self.profile()['status'], 'unprocessed')
        self.assertEqual(self.get('/api/profiles')['profiles'], [])

    def test_serial_queue_and_incremental_skip(self):
        state = srl.create_state(self.service.state_path)
        state['processed_aweme_ids'][OLD_ID] = {
            'sec_uid': PROFILE_A, 'nickname': '合成测试作者', 'uid': 'TEST_AUTHOR',
            'role_dir': '合成测试作者-TEST_AUTHOR', 'kind': 'video',
            'caption_prefix': '离线测试作品', 'work_index': 1,
        }
        srl.atomic_save(self.service.state_path, state)
        self.browser.profiles = {
            PROFILE_A: [video_item(OLD_ID), video_item(NEW_ID)],
            PROFILE_B: [video_item(OTHER_ID, PROFILE_B)],
        }
        self.browser.release.clear()
        queued = self.post('/api/enqueue', {'urls': [PROFILE_A, PROFILE_A, PROFILE_B]})
        self.assertEqual([row['status'] for row in queued['results']], ['queued', 'duplicate', 'queued'])
        self.assertTrue(self.browser.entered.wait(timeout=2))
        status = self.get('/api/status')
        self.assertEqual(status['current']['sec_uid'], PROFILE_A)
        self.assertEqual([row['sec_uid'] for row in status['queue']], [PROFILE_B])
        self.assertEqual(self.profile()['status'], 'processing')
        duplicates = self.post('/api/enqueue', {'urls': [PROFILE_A, PROFILE_B]})
        self.assertTrue(all(row['status'] == 'duplicate' for row in duplicates['results']))
        self.assertEqual(self.service.profiles_path.read_text(encoding='utf-8').splitlines(), [PROFILE_A, PROFILE_B])
        self.browser.release.set()
        status = self.wait_idle(2)
        self.assertEqual(self.browser.started, [PROFILE_A, PROFILE_B])
        self.assertEqual(self.browser.max_active, 1)
        self.assertEqual(self.max_downloading, 1)
        self.assertEqual(self.downloads, Counter({NEW_ID: 1, OTHER_ID: 1}))
        self.assertEqual(self.profile()['processed_works'], 2)
        self.assertEqual(self.profile()['last_scan']['skipped_by_aweme_id'], 1)
        self.assertEqual(status['recent'][0]['sec_uid'], PROFILE_B)
        for path in self.service.output.rglob('*.mp4'):
            path.unlink()
        self.post('/api/enqueue', {'urls': [PROFILE_A]})
        self.wait_idle(3)
        self.assertEqual(self.profile()['last_scan']['skipped_by_aweme_id'], 2)
        self.assertEqual(self.downloads, Counter({NEW_ID: 1, OTHER_ID: 1}))

    def test_retry_failed_work_and_keep_successful_work(self):
        self.browser.profiles = {PROFILE_A: [video_item(NEW_ID), video_item(RETRY_ID)]}
        self.fail_once.add(RETRY_ID)
        self.post('/api/enqueue', {'urls': [PROFILE_A]})
        self.wait_idle(1)
        failed = self.profile()
        self.assertEqual(failed['status'], 'partial')
        self.assertEqual(failed['failed_works'], 1)
        self.assertEqual(failed['processed_works'], 1)
        retry = self.post('/api/retry', {})
        self.assertEqual(retry['targets'], [PROFILE_A])
        self.assertEqual(retry['results'][0]['status'], 'queued')
        self.wait_idle(2)
        self.assertEqual(self.profile()['status'], 'complete')
        self.assertEqual(self.profile()['failed_works'], 0)
        self.assertEqual(self.profile()['processed_works'], 2)
        self.assertEqual(self.downloads, Counter({RETRY_ID: 2, NEW_ID: 1}))
        self.assertEqual(self.post('/api/retry', {})['targets'], [])
        self.assertEqual(self.post('/api/retry', {'sec_uid': PROFILE_A})['targets'], [PROFILE_A])
        self.wait_idle(3)
        self.assertEqual(self.downloads, Counter({RETRY_ID: 2, NEW_ID: 1}))

    def test_logged_in_payload_and_work_supplement(self):
        batch = self.post('/api/enqueue_items', {'sec_uid': PROFILE_A, 'items': [
            None, {'aweme_id': 'invalid'}, video_item(NEW_ID), video_item(NEW_ID),
        ]})
        self.assertEqual(Counter(row['status'] for row in batch['results']), Counter(invalid=2, duplicate=1, queued=1))
        self.wait_idle(1)
        self.assertEqual(self.downloads, Counter({NEW_ID: 1}))
        self.assertEqual(self.browser.started, [])
        self.assertEqual(self.profile()['last_scan']['source'], 'logged_in_payload')
        duplicate = self.post('/api/enqueue_items', {'sec_uid': PROFILE_A, 'items': [video_item(NEW_ID)]})
        self.assertEqual(duplicate['results'][0]['status'], 'duplicate')
        missing_profile = self.post('/api/enqueue_work', {'urls': [f'https://www.douyin.com/video/{OTHER_ID}']})
        self.assertEqual(missing_profile['results'][0]['status'], 'invalid')
        self.browser.details[OTHER_ID] = video_item(OTHER_ID)
        work = self.post('/api/enqueue_work', {'urls': [f'https://www.douyin.com/video/{OTHER_ID}?sec_uid={PROFILE_A}']})
        self.assertEqual(work['results'][0]['status'], 'queued')
        self.wait_idle(2)
        self.assertEqual(self.browser.detail_calls, [OTHER_ID])
        self.assertEqual(self.downloads, Counter({NEW_ID: 1, OTHER_ID: 1}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
