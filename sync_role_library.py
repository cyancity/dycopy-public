import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dycopy

ROOT = Path(__file__).resolve().parent
LOCAL_ROOT = ROOT / '.local' / 'dycopy'
DEFAULT_PROFILES = LOCAL_ROOT / 'profile_sources.txt'
DEFAULT_STATE = LOCAL_ROOT / 'processed_aweme_ids.json'
DEFAULT_OUTPUT = ROOT / 'downloads'
DEFAULT_LEGACY = LOCAL_ROOT / 'legacy_archive.json'
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean_name(value, limit=80):
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '', str(value or '')).strip(' .')
    return value[:limit] or '无文案'


def caption_prefix(desc):
    return clean_name(str(desc or '').strip()[:6] or '无文案')


def atomic_save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(6):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


SEC_UID_RE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')


def validate_profile_input(text):
    """把用户输入(主页链接/分享文本/sec_uid)解析为 sec_uid。
    返回 (sec_uid, None) 或 (None, 错误说明)。只接受主页,不接受作品链接/短链。"""
    s = str(text or '').strip()
    if not s:
        return None, '空输入'
    if SEC_UID_RE.fullmatch(s):
        return s, None
    match = re.search(r'/user/([^/?#\s]+)', s) or re.search(r'/share/user/([^/?#\s]+)', s)
    if match and SEC_UID_RE.fullmatch(match.group(1)):
        return match.group(1), None
    match = re.search(r'[?&]sec_uid=([A-Za-z0-9_-]{20,})', s)
    if match:
        return match.group(1), None
    if re.search(r'douyin\.com/(?:video|note|share/video|share/note)/', s) or dycopy.BARE_ID_RE.search(s):
        return None, '这是作品链接/作品ID,不是主页链接;请粘贴主页地址(含 /user/)'
    if re.search(r'v\.douyin\.com|z\.douyin\.com', s):
        return None, '短链无法直接识别主页,请在浏览器打开主页后粘贴地址栏完整链接'
    return None, '无法识别的输入,请粘贴抖音主页链接或 sec_uid'


AWEME_ID_RE = re.compile(r'^\d{15,22}$')


def validate_work_input(text):
    """把用户输入(作品链接/裸ID)解析为 aweme_id。
    返回 (aweme_id, None) 或 (None, 错误说明)。只接受单个作品。"""
    s = str(text or '').strip()
    if not s:
        return None, '空输入'
    if AWEME_ID_RE.fullmatch(s):
        return s, None
    match = re.search(r'/(?:video|note|share/video|share/note)/(\d{15,22})', s)
    if match:
        return match.group(1), None
    if re.search(r'/user/|/share/user/|[?&]sec_uid=', s):
        return None, '这是主页链接,不是作品链接;请粘贴单个作品的地址(/video/ 或 /note/)'
    if re.search(r'v\.douyin\.com|z\.douyin\.com', s):
        return None, '短链无法直接识别作品,请在浏览器打开作品后粘贴地址栏完整链接'
    return None, '无法识别的输入,请粘贴作品链接或 aweme_id'


def parse_sources(path):
    seen, sources = set(), []
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        if not raw.strip() or raw.strip().startswith('#'):
            continue
        sec_uid, _err = validate_profile_input(raw)
        if sec_uid and sec_uid not in seen:
            seen.add(sec_uid)
            sources.append(sec_uid)
    return sources


def author_info(item):
    author = item.get('author') or {}
    return (str(author.get('nickname') or '').strip(),
            str(author.get('uid') or '').strip(),
            str(author.get('sec_uid') or '').strip())


def role_folder(output, nickname, uid, sec_uid):
    if uid:
        matches = [p for p in output.iterdir() if p.is_dir() and p.name.endswith('-' + uid)] if output.exists() else []
        if len(matches) > 1:
            raise RuntimeError(f'多个目录匹配 uid={uid}: {matches}')
        if matches:
            return matches[0]
    return output / f'{clean_name(nickname)}-{clean_name(uid or sec_uid)}'


def is_image_post(item):
    return str(item.get('aweme_type')) == '68' or (bool(item.get('images')) and not dycopy.pick_video_url(item))


def live_video_url(image):
    if str(image.get('live_photo_type') or '') != '1':
        return ''
    video = image.get('video')
    if not isinstance(video, dict):
        return ''
    url = dycopy.pick_video_url({'video': video})
    if not url or 'playwm' in url.lower() or 'watermark=1' in url.lower() or 'logo_name=' in url.lower():
        return ''
    return url


def ext_from_url(url, fallback='.jpg'):
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in IMAGE_EXTS else fallback


def choose_names(role_dir, prefix, sequence, kind, image_exts=(), live_indexes=()):
    while True:
        if kind == 'image':
            names = [f'{prefix}_{sequence:03d}_{i:02d}{ext}' for i, ext in enumerate(image_exts, 1)]
            names += [f'{prefix}_{sequence:03d}_live{i:02d}.mp4' for i in live_indexes]
        else:
            names = [f'{prefix}_{sequence:03d}.mp4']
        if not any((role_dir / name).exists() for name in names):
            return names, sequence
        sequence += 1


def download_to_temp(session, url, dest, video=False):
    if video:
        dycopy.download_video(session, url, dest)
    else:
        dycopy.download_image(session, url, dest)
    if not dest.is_file() or dest.stat().st_size < 1024:
        raise RuntimeError(f'下载文件缺失或过小: {dest.name}')


def seed_legacy(state, legacy_path):
    if state['processed_aweme_ids'] or not legacy_path.exists():
        return 0
    groups = json.loads(legacy_path.read_text(encoding='utf-8'))
    counters = defaultdict(int)
    seeded = 0
    for group in groups:
        sec = str(group.get('sec') or '')
        for item in group.get('items') or []:
            aid = str(item.get('aweme_id') or '')
            if not aid or aid in state['processed_aweme_ids']:
                continue
            nickname, uid, author_sec = author_info(item)
            prefix = caption_prefix(item.get('desc'))
            counters[(sec, prefix)] += 1
            images = item.get('images') or []
            kind = 'image' if is_image_post(item) else 'video'
            live_indexes = [i for i, image in enumerate(images, 1) if live_video_url(image)]
            state['processed_aweme_ids'][aid] = {
                'sec_uid': sec,
                'nickname': nickname,
                'uid': uid,
                'role_dir': f'{clean_name(nickname)}-{clean_name(uid or sec)}',
                'kind': kind,
                'caption_prefix': prefix,
                'work_index': counters[(sec, prefix)],
                'extracted_at': now(),
                'source': 'legacy_completed_batch',
                'live_expected': live_indexes,
            }
            seeded += 1
    if seeded:
        state.setdefault('next_work_index', {})
        for (sec, prefix), count in counters.items():
            key = f'{sec}\0{prefix}'
            state['next_work_index'][key] = max(int(state['next_work_index'].get(key, 1)), count + 1)
        state['legacy_seeded_at'] = now()
        atomic_save(STATE_PATH, state)
    return seeded


def seed_legacy_components(state, legacy_path):
    """Backfill motion clips for old static image posts without re-downloading stills."""
    if not legacy_path.exists():
        return 0, 0
    groups = json.loads(legacy_path.read_text(encoding='utf-8'))
    session = requests.Session()
    saved = failed = 0
    for group in groups:
        sec = str(group.get('sec') or '')
        for item in group.get('items') or []:
            aid = str(item.get('aweme_id') or '')
            record = state['processed_aweme_ids'].get(aid)
            if not record or record.get('kind') != 'image':
                continue
            images = item.get('images') or []
            for image_index, image in enumerate(images, 1):
                url = live_video_url(image)
                key = f'{aid}:{image_index}'
                if not url or key in state['live_photo_components']:
                    continue
                prefix = record.get('caption_prefix') or caption_prefix(item.get('desc'))
                sequence = int(record.get('work_index') or 1)
                dest_dir = OUTPUT_ROOT / record['role_dir']
                dest_dir.mkdir(parents=True, exist_ok=True)
                name = f'{prefix}_{sequence:03d}_live{image_index:02d}.mp4'
                while (dest_dir / name).exists():
                    name = f'{prefix}_{sequence:03d}_live{image_index:02d}_2.mp4'
                try:
                    with tempfile.TemporaryDirectory(prefix=f'dycopy-live-{aid}-', dir=str(STATE_PATH.parent)) as td:
                        temp = Path(td) / name
                        download_to_temp(session, url, temp, video=True)
                        shutil.move(str(temp), str(dest_dir / name))
                    state['live_photo_components'][key] = {'aweme_id': aid, 'image_index': image_index, 'file': name, 'role_dir': record['role_dir'], 'extracted_at': now()}
                    atomic_save(STATE_PATH, state)
                    saved += 1
                except Exception as exc:
                    failed += 1
                    state.setdefault('live_photo_failures', {})[key] = str(exc)[:240]
                    atomic_save(STATE_PATH, state)
                    print(f'[Live 图失败] {aid}#{image_index}: {exc}', flush=True)
    return saved, failed


def create_state(path):
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        state.setdefault('processed_aweme_ids', {})
        state.setdefault('live_photo_components', {})
        state.setdefault('next_work_index', {})
        for record in state['processed_aweme_ids'].values():
            sec = record.get('sec_uid')
            prefix = record.get('caption_prefix')
            work_index = int(record.get('work_index') or 0)
            if sec and prefix and work_index:
                key = f'{sec}\0{prefix}'
                state['next_work_index'][key] = max(int(state['next_work_index'].get(key, 1)), work_index + 1)
        return state
    return {'version': 1, 'created_at': now(), 'processed_aweme_ids': {}, 'live_photo_components': {}, 'profile_metadata': {}}


def process_work(browser, session, state, sec_uid, list_item, output, temp_root):
    aid = str(list_item.get('aweme_id') or '')
    # The post-list endpoint already returns the media payload. Prefer it to
    # avoid an extra detail-page navigation for every post; only fetch detail
    # when the list item is missing author or media fields.
    detail = list_item
    list_nickname, list_uid, _ = author_info(detail)
    if not list_nickname or not list_uid or not (detail.get('images') or dycopy.pick_video_url(detail)):
        detail = browser.fetch_aweme(aid)
    detail['_role_sec_uid'] = sec_uid
    nickname, uid, _ = author_info(detail)
    if not nickname or not uid:
        nickname2, uid2, _ = author_info(list_item)
        nickname, uid = nickname or nickname2, uid or uid2
    if not nickname or not uid:
        raise RuntimeError(f'作品作者信息不全: {aid}')
    desc = str(detail.get('desc') or list_item.get('desc') or '')
    prefix = caption_prefix(desc)
    role_dir = role_folder(output, nickname, uid, sec_uid)
    kind = 'image' if is_image_post(detail) else 'video'
    photo_urls, live_urls = [], []
    video_url = ''
    if kind == 'image':
        for image_index, image in enumerate(detail.get('images') or [], 1):
            urls = dycopy.image_urls({'images': [image]})
            if not urls:
                raise RuntimeError(f'图文第 {image_index} 张未提供可确认的干净原图源')
            photo_urls.append((image_index, urls[0]))
            motion = live_video_url(image)
            if motion:
                live_urls.append((image_index, motion))
        if not photo_urls:
            raise RuntimeError(f'图文作品无图片源: {aid}')
        photo_exts = [ext_from_url(url) for _, url in photo_urls]
    else:
        video_url = dycopy.pick_video_url(detail)
        if not video_url:
            raise RuntimeError(f'作品未提供可下载视频源: {aid}')
        photo_exts = []
    prefix_key = f'{sec_uid}\0{prefix}'
    sequence = int(state.setdefault('next_work_index', {}).get(prefix_key, 1))
    live_indexes = [i for i, _ in live_urls]
    names, sequence = choose_names(role_dir, prefix, sequence, kind, photo_exts, live_indexes)
    role_dir.mkdir(parents=True, exist_ok=True)
    # 阶段1: 静态资源(视频或全部无水印原图)全部成功才落盘并登记作品。
    with tempfile.TemporaryDirectory(prefix=f'dycopy-{aid}-', dir=str(temp_root)) as td:
        td = Path(td)
        staged = []
        if kind == 'image':
            for (image_index, url), name in zip(photo_urls, names[:len(photo_urls)]):
                tmp = td / name
                download_to_temp(session, url, tmp)
                staged.append((tmp, role_dir / name))
        else:
            name = names[0]
            tmp = td / name
            download_to_temp(session, video_url, tmp, video=True)
            staged.append((tmp, role_dir / name))
        for src, dest in staged:
            shutil.move(str(src), str(dest))
    live_expected = [i for i, _ in live_urls]
    record = {
        'sec_uid': sec_uid, 'nickname': nickname, 'uid': uid,
        'role_dir': role_dir.name, 'kind': kind, 'caption_prefix': prefix,
        'work_index': sequence, 'desc': desc, 'files': [dest.name for _, dest in staged],
        'live_expected': live_expected, 'extracted_at': now(), 'source': 'sync_role_library',
    }
    state['processed_aweme_ids'][aid] = record
    state.setdefault('next_work_index', {})[prefix_key] = sequence + 1
    # 阶段2: Live 动态片段逐个尝试;单个失败只记录,不影响已保存的静态图。
    for (image_index, url), name in zip(live_urls, names[len(photo_urls):]):
        key = f'{aid}:{image_index}'
        try:
            with tempfile.TemporaryDirectory(prefix=f'dycopy-live-{aid}-', dir=str(temp_root)) as td:
                tmp = Path(td) / name
                download_to_temp(session, url, tmp, video=True)
                shutil.move(str(tmp), str(role_dir / name))
            state['live_photo_components'][key] = {'aweme_id': aid, 'image_index': image_index, 'file': name, 'role_dir': role_dir.name, 'extracted_at': now()}
            state.setdefault('live_photo_failures', {}).pop(key, None)
        except Exception as exc:
            state.setdefault('live_photo_failures', {})[key] = f'{type(exc).__name__}: {str(exc)[:240]}'
    state.setdefault('profile_metadata', {})[sec_uid] = {'nickname': nickname, 'uid': uid, 'role_dir': role_dir.name, 'updated_at': now()}
    return record


def retry_live(state, state_path, browser, sec_uid, output, temp_root, log=print):
    """用新鲜作品详情重试该主页失败的 Live 动态片段,不复用旧归档 URL。
    返回 (成功数, 失败数)。"""
    session = requests.Session()
    saved = failed = 0
    for key in list(state.get('live_photo_failures', {})):
        aid, _, idx_part = key.partition(':')
        record = state['processed_aweme_ids'].get(aid)
        if not record or record.get('sec_uid') != sec_uid or record.get('kind') != 'image':
            continue
        image_index = int(idx_part or 0)
        if image_index < 1:
            continue
        dest_dir = output / record['role_dir']
        name = f"{record['caption_prefix']}_{int(record['work_index']):03d}_live{image_index:02d}.mp4"
        component = {'aweme_id': aid, 'image_index': image_index, 'file': name,
                     'role_dir': record['role_dir'], 'extracted_at': now()}
        try:
            dest = dest_dir / name
            if dest.is_file() and dest.stat().st_size >= 1024:
                state['live_photo_components'][key] = component
                state['live_photo_failures'].pop(key, None)
                atomic_save(state_path, state)
                saved += 1
                continue
            detail = browser.fetch_aweme(aid)
            images = detail.get('images') or []
            if image_index > len(images):
                raise RuntimeError(f'新鲜详情只有 {len(images)} 张图,无第 {image_index} 张 Live')
            url = live_video_url(images[image_index - 1])
            if not url:
                raise RuntimeError('新鲜详情未提供可下载的 Live 地址')
            dest_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f'dycopy-live-{aid}-', dir=str(temp_root)) as td:
                tmp = Path(td) / name
                download_to_temp(session, url, tmp, video=True)
                shutil.move(str(tmp), str(dest))
            state['live_photo_components'][key] = component
            state['live_photo_failures'].pop(key, None)
            atomic_save(state_path, state)
            saved += 1
        except Exception as exc:
            state.setdefault('live_photo_failures', {})[key] = f'{type(exc).__name__}: {str(exc)[:240]}'
            atomic_save(state_path, state)
            failed += 1
            log(f'[Live 重试失败] {key}: {exc}')
    return saved, failed


def process_work_by_id(browser, session, state, state_path, sec_uid, aweme_id, output, temp_root):
    """按作品链接补录:用作品自己的详情取完整数据(含列表接口被过滤的情况),
    走同一个 process_work(含 Live 动态片段尝试)。已登记过直接返回 None。"""
    if aweme_id in state['processed_aweme_ids']:
        return None
    detail = browser.fetch_aweme(aweme_id)
    record = process_work(browser, session, state, sec_uid, detail, output, temp_root)
    state.get('last_failures', {}).pop(aweme_id, None)
    atomic_save(state_path, state)
    return record


def run_profile(browser, session, state, state_path, sec_uid, output, temp_root, progress=None, log=print):
    """处理单个主页: Live 失败重试 → 全量翻页 → 逐作品串行处理 → 写主页状态。
    返回 {'status': 'complete'|'partial'|'failed', 'listed', 'new', 'failed',
           'skipped', 'new_ids', 'live_saved', 'live_failed', 'error'?}"""
    live_saved = live_failed = 0
    if state.get('live_photo_failures'):
        live_saved, live_failed = retry_live(state, state_path, browser, sec_uid, output, temp_root, log=log)
    try:
        items = browser.fetch_user_posts(sec_uid)
    except Exception as exc:
        state.setdefault('profile_scans', {})[sec_uid] = {
            'status': 'failed', 'last_scan_at': now(),
            'error': f'{type(exc).__name__}: {str(exc)[:240]}',
        }
        atomic_save(state_path, state)
        log(f'[主页失败] {sec_uid}: {type(exc).__name__}: {exc}')
        return {'status': 'failed', 'listed': 0, 'new': 0, 'failed': 0, 'skipped': 0,
                'new_ids': [], 'live_saved': live_saved, 'live_failed': live_failed,
                'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
    log(f'[主页] {sec_uid}: {len(items)} 个作品')
    seen = set()
    new_works = profile_failed = skipped = 0
    new_ids = []
    for index, item in enumerate(items, 1):
        aid = str(item.get('aweme_id') or '')
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if aid in state['processed_aweme_ids']:
            skipped += 1
            continue
        if progress:
            progress(index, len(items), aid)
        try:
            process_work(browser, session, state, sec_uid, item, output, temp_root)
            state.get('last_failures', {}).pop(aid, None)
            atomic_save(state_path, state)
            new_works += 1
            new_ids.append(aid)
            log(f'  [新作品 {index}/{len(items)}] {aid}')
            time.sleep(0.8)
        except Exception as exc:
            profile_failed += 1
            state.setdefault('last_failures', {})[aid] = {'sec_uid': sec_uid, 'error': f'{type(exc).__name__}: {str(exc)[:240]}', 'at': now()}
            atomic_save(state_path, state)
            log(f'  [作品失败] {aid}: {type(exc).__name__}: {exc}')
    state.setdefault('profile_scans', {})[sec_uid] = {
        'status': 'partial' if profile_failed else 'complete',
        'last_scan_at': now(), 'listed_aweme_count': len(items),
        'new_works': new_works, 'failed_works': profile_failed,
        'skipped_by_aweme_id': skipped,
        'live_saved': live_saved, 'live_failed': live_failed,
    }
    atomic_save(state_path, state)
    return {'status': 'partial' if profile_failed else 'complete', 'listed': len(items),
            'new': new_works, 'failed': profile_failed, 'skipped': skipped,
            'new_ids': new_ids, 'live_saved': live_saved, 'live_failed': live_failed}


def main():
    parser = argparse.ArgumentParser(description='串行同步抖音主页图文、视频与 Live 图到 role 目录;按作品 ID 去重。')
    parser.add_argument('--profiles', type=Path, default=DEFAULT_PROFILES, help='每行一个主页 URL 或 sec_uid')
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE, help='本地作品 ID 状态 JSON')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='role 素材目录')
    parser.add_argument('--legacy-archive', type=Path, default=DEFAULT_LEGACY, help='用于初始化已提取作品 ID 的旧批次数据')
    parser.add_argument('--skip-legacy-live-backfill', action='store_true', help='跳过旧归档 Live 图动态片段补齐')
    parser.add_argument('--works', type=Path, default=None, help='可选:每行一个作品链接或 aweme_id,先补录这些作品再处理主页(用于列表接口被过滤的作品)')
    args = parser.parse_args()
    global STATE_PATH, OUTPUT_ROOT
    STATE_PATH, OUTPUT_ROOT = args.state, args.output
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    temp_root = args.state.parent / 'staging'
    temp_root.mkdir(exist_ok=True)
    state = create_state(args.state)
    seeded = seed_legacy(state, args.legacy_archive)
    if seeded:
        print(f'[初始化] 从已完成旧批次记录 {seeded} 个作品 ID', flush=True)
    legacy_live_saved, legacy_live_failed = (0, 0)
    if not args.skip_legacy_live_backfill:
        legacy_live_saved, legacy_live_failed = seed_legacy_components(state, args.legacy_archive)
    if legacy_live_saved or legacy_live_failed:
        print(f'[Live 图补齐] 新增动态片段 {legacy_live_saved} 个，失败 {legacy_live_failed} 个', flush=True)
    sources = parse_sources(args.profiles)
    print(f'[主页] 去重后 {len(sources)} 个 sec_uid;已记录作品 {len(state["processed_aweme_ids"])} 个', flush=True)
    browser = dycopy.BrowserSession()
    session = requests.Session()
    fetched_profiles = failed_profiles = new_works = failed_works = skipped_works = 0
    processed_this_run = []
    # --works: 列表接口被过滤的作品(详情可达但不在主页列表),按 sec_uid 逐个补录
    if args.works and args.works.exists():
        work_map = {}
        for raw in args.works.read_text(encoding='utf-8-sig').splitlines():
            aid, _ = validate_work_input(raw)
            if not aid:
                continue
            match = re.search(r'[?&][sS][eE][cC]_?[uU][iI][dD]=([A-Za-z0-9_-]{20,})', raw) or re.search(r'/user/([^/?#]+)', raw)
            work_map.setdefault(match.group(1) if match else None, []).append(aid)
        try:
            for sec_uid, aids in work_map.items():
                for aid in aids:
                    if not sec_uid or aid in state['processed_aweme_ids']:
                        skipped_works += 1
                        continue
                    try:
                        record = process_work_by_id(browser, session, state, args.state,
                                                    sec_uid, aid, args.output, temp_root)
                        if record:
                            new_works += 1
                            processed_this_run.append(aid)
                            print(f'  [补录] {aid} -> {record["role_dir"]}', flush=True)
                        time.sleep(0.8)
                    except Exception as exc:
                        failed_works += 1
                        state.setdefault('last_failures', {})[aid] = {'sec_uid': sec_uid, 'error': f'{type(exc).__name__}: {str(exc)[:240]}', 'at': now()}
                        atomic_save(args.state, state)
                        print(f'  [补录失败] {aid}: {type(exc).__name__}: {exc}', flush=True)
        except Exception as exc:
            print(f'  [补录阶段异常] {type(exc).__name__}: {exc}', flush=True)
    try:
        for profile_index, sec_uid in enumerate(sources, 1):
            print(f'[主页 {profile_index}/{len(sources)}] 处理: {sec_uid}', flush=True)

            def _progress(index, total, aid, _sec=sec_uid):
                if index % 20 == 0:
                    print(f'  [{_sec[:12]}…] 进度 {index}/{total}', flush=True)

            stats = run_profile(browser, session, state, args.state, sec_uid, args.output,
                                temp_root, progress=_progress)
            if stats['status'] == 'failed':
                failed_profiles += 1
            else:
                fetched_profiles += 1
            new_works += stats['new']
            failed_works += stats['failed']
            skipped_works += stats['skipped']
            processed_this_run.extend(stats['new_ids'])
    finally:
        browser.close()
    summary = {
        'finished_at': now(), 'unique_profiles': len(sources), 'fetched_profiles': fetched_profiles,
        'failed_profiles': failed_profiles, 'new_works': new_works, 'skipped_by_aweme_id': skipped_works,
        'failed_works': failed_works, 'legacy_live_clips_added': legacy_live_saved,
        'legacy_live_clip_failures': legacy_live_failed, 'processed_id_count': len(state['processed_aweme_ids']),
        'new_aweme_ids': processed_this_run,
    }
    state['last_run'] = summary
    atomic_save(args.state, state)
    summary_path = args.state.with_name('last_run.json')
    atomic_save(summary_path, summary)
    print('[完成] ' + json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
