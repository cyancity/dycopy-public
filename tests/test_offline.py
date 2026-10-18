# -*- coding: utf-8 -*-
"""离线验证: Live 失败隔离 + retry_live 新鲜详情 + validate_profile_input。
不访问网络,monkeypatch 下载与浏览器。"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_role_library as srl

FAILED = []


def check(name, cond, detail=''):
    if cond:
        print(f'  [PASS] {name}')
    else:
        print(f'  [FAIL] {name} {detail}')
        FAILED.append(name)


# ---------- 1. validate_profile_input ----------
print('== validate_profile_input ==')
sec = 'TEST_PROFILE_ABCDEFGHIJKLMNOP'
cases = [
    (sec, sec),
    (f'https://www.douyin.com/user/{sec}?vid=1', sec),
    (f'https://www.iesdouyin.com/share/user/{sec}', sec),
    (f'https://www.douyin.com/user/{sec}', sec),
    ('https://www.douyin.com/video/1111111111111111111', None),
    ('1111111111111111111', None),
    ('https://v.douyin.com/xxxxx/', None),
    ('随便一段话', None),
    ('', None),
]
for text, want in cases:
    got, err = srl.validate_profile_input(text)
    check(f'validate({text[:40]!r})', got == want, f'got={got} err={err}')

# ---------- 2. process_work 两阶段: Live 失败不影响静态图 ----------
print('== process_work Live 隔离 ==')
tmp = Path(tempfile.mkdtemp(prefix='dycopy-test-'))
state_path = tmp / 'state.json'
output = tmp / 'role'
temp_root = tmp / 'staging'
temp_root.mkdir()
output.mkdir()


class FakeBrowser:
    def fetch_aweme(self, aid):
        return self.detail


def fake_item():
    image = {
        'url_list': ['https://example.com/a.jpg', 'https://example.com/a_last.jpg'],
        'origin_image': {'url_list': ['https://example.com/a_origin.jpg']},
        'live_photo_type': '1',
        'video': {'play_addr': {'url_list': ['https://cdn.example.com/live1.mp4']}},
    }
    image2 = {
        'url_list': ['https://example.com/b.jpg'],
        'live_photo_type': '1',
        'video': {'play_addr': {'url_list': ['https://cdn.example.com/live2.mp4']}},
    }
    return {
        'aweme_id': '1111111111111111111',
        'desc': '测试图文作品标题',
        'aweme_type': '68',
        'author': {'nickname': '测试者', 'uid': 'TEST_AUTHOR', 'sec_uid': sec},
        'images': [image, image2],
    }


item = fake_item()
browser = FakeBrowser()
browser.detail = item
state = srl.create_state(state_path)

live_calls = []


def fake_download(session, url, dest, video=False):
    if video:
        live_calls.append(url)
        raise RuntimeError('模拟 Live 下载403(过期URL)')
    dest.write_bytes(b'x' * 2048)


orig_download, orig_live = srl.download_to_temp, None
srl.download_to_temp = fake_download
try:
    rec = srl.process_work(browser, None, state, sec, item, output, temp_root)
finally:
    srl.download_to_temp = orig_download

check('作品已登记 processed', '1111111111111111111' in state['processed_aweme_ids'])
role_dir = output / rec['role_dir']
statics = sorted(p.name for p in role_dir.iterdir())
check('静态原图2张已落盘', statics == ['测试图文作品_001_01.jpg', '测试图文作品_001_02.jpg'], str(statics))
check('live_expected 记录 [1,2]', rec['live_expected'] == [1, 2], str(rec['live_expected']))
check('live_photo_components 为空', not state['live_photo_components'])
check('live_photo_failures 有2条', len(state.get('live_photo_failures', {})) == 2,
      str(state.get('live_photo_failures')))
check('Live 尝试过2次', len(live_calls) == 2, str(live_calls))

# ---------- 3. retry_live 用新鲜详情补下成功 ----------
print('== retry_live 新鲜详情 ==')


class FakeBrowser2:
    def fetch_aweme(self, aid):
        return {'images': item['images']}


def fake_download_ok(session, url, dest, video=False):
    dest.write_bytes(b'y' * 2048)


srl.download_to_temp = fake_download_ok
try:
    saved, failed = srl.retry_live(state, state_path, FakeBrowser2(), sec, output, temp_root)
finally:
    srl.download_to_temp = orig_download

check('retry_live saved=2', saved == 2, f'saved={saved} failed={failed}')
check('failures 已清空', not state.get('live_photo_failures'), str(state.get('live_photo_failures')))
check('components 有2条', len(state['live_photo_components']) == 2)
live_files = sorted(p.name for p in role_dir.iterdir() if 'live' in p.name)
check('live 文件名正确', live_files == ['测试图文作品_001_live01.mp4', '测试图文作品_001_live02.mp4'], str(live_files))

# ---------- 4. 已登记作品即使文件被删也跳过(run_profile 语义) ----------
print('== 成功过即跳过 ==')
for p in role_dir.iterdir():
    p.unlink()
check('媒体文件已删空', not any(role_dir.iterdir()))
check('state 仍有记录', '1111111111111111111' in state['processed_aweme_ids'])

# ---------- 5. choose_names 稳定序号 ----------
print('== choose_names ==')
names1, seq1 = srl.choose_names(role_dir, '前缀', 1, 'video')
names2, seq2 = srl.choose_names(role_dir, '前缀', 1, 'video')
check('无冲突时序号不变', names1 == names2 == ['前缀_001.mp4'] and seq1 == seq2)
(role_dir / '前缀_001.mp4').write_bytes(b'z' * 10)
names3, seq3 = srl.choose_names(role_dir, '前缀', 1, 'video')
check('冲突时序号+1', names3 == ['前缀_002.mp4'] and seq3 == 2, f'{names3} {seq3}')

# ---------- 6. run_profile 失败主页状态 ----------
print('== run_profile 主页失败状态 ==')


class DeadBrowser:
    def fetch_user_posts(self, sec_uid):
        raise RuntimeError('模拟风控')


state2 = srl.create_state(tmp / 'state2.json')
stats = srl.run_profile(DeadBrowser(), None, state2, tmp / 'state2.json', sec, output, temp_root)
check('返回 failed', stats['status'] == 'failed')
check('profile_scans 记录 failed', state2['profile_scans'][sec]['status'] == 'failed')

print('== 可移植默认目录 ==')
root = Path(__file__).resolve().parent.parent
check('主页清单位于本项目', srl.DEFAULT_PROFILES == root / '.local' / 'dycopy' / 'profile_sources.txt')
check('作品状态位于本项目', srl.DEFAULT_STATE == root / '.local' / 'dycopy' / 'processed_aweme_ids.json')
check('旧归档默认位于本项目', srl.DEFAULT_LEGACY == root / '.local' / 'dycopy' / 'legacy_archive.json')
check('默认媒体输出 downloads', srl.DEFAULT_OUTPUT == root / 'downloads')

print('== ASR API Key 配置优先级 ==')
captured_keys = []


def capture_input(session, text, args):
    captured_keys.append(args.api_key)
    return [{'md_path': None, 'media_dir': str(args.out)}]


for cli_key in (None, 'test-command-line-key'):
    argv = ['dycopy.py', '1111111111111111111', '--asr', 'siliconflow', '--out', str(tmp / 'cli')]
    if cli_key:
        argv.extend(['--api-key', cli_key])
    with patch.dict(srl.dycopy.os.environ, {'SILICONFLOW_API_KEY': 'test-environment-key'}), \
            patch.object(sys, 'argv', argv), patch.object(srl.dycopy, 'process_input', capture_input):
        result = srl.dycopy.main()
    check('CLI 配置可完成离线调用', result == 0)
check('未传参数时读取环境变量', captured_keys[0] == 'test-environment-key')
check('显式 API Key 参数优先', captured_keys[1] == 'test-command-line-key')

shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f'共 {len(FAILED)} 项失败: {FAILED}')
    sys.exit(1)
print('全部通过')
