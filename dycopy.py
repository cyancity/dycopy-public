#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dycopy — 抖音视频文案提取器
============================
输入抖音分享链接/视频ID，提取：
  - 视频文案(desc) + 完整元数据(作者、数据、音乐、时间)
  - 无水印视频下载
  - 口播逐字稿 (本地 ASR，无需任何 API Key)

原理：
  1) 快路径(纯 HTTP)：抖音移动端分享页若服务端渲染，HTML 内嵌
     window._ROUTER_DATA，含完整作品信息与播放地址。
     (2026-09 起抖音已移除该 SSR 数据，此路径仅作兼容保留)
  2) 兜底(免登录无状态浏览器)：无头 Chromium 打开视频页，
     在页面上下文 fetch /aweme/v1/web/aweme/detail/，
     secsdk/bdms 会自动给请求加 uifid/a_bogus/msToken 签名。
     每次启动全新 browser context，不落盘 cookie，不登录。

用法示例：
  python dycopy.py "抖音作品链接或分享口令"
  python dycopy.py "抖音作品链接" --download
  python dycopy.py "抖音作品链接" --asr faster-whisper --download --srt
  python dycopy.py a.txt b.txt   # 批量(每行一个链接)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

APP_VERSION = "0.2.0"

# 移动端 UA：抖音分享页对移动端 UA 服务端渲染，返回完整 _ROUTER_DATA
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": MOBILE_UA,
    "Referer": "https://www.iesdouyin.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

ID_RE = re.compile(r"/(?:video|note|share/video|share/note)/(\d{15,22})")
PROFILE_RE = re.compile(r"/(?:user|share/user)/([A-Za-z0-9_-]{20,})")
BARE_ID_RE = re.compile(r"\b(\d{19})\b")


class DyError(Exception):
    pass


# ---------------------------------------------------------------- 链接解析

def extract_id(text: str, session: requests.Session) -> str:
    """从任意文本中提取抖音视频 ID。支持: 分享口令文本、短链、完整 URL、裸 ID。"""
    # 先找 URL (兼容无 scheme 的 "v.douyin.com/xxx" 形式)
    url_match = re.search(r"https?://[^\s\"'<>，。；、]+", text)
    if not url_match:
        m = re.search(r"\b((?:v|www|m|iesdouyin)\.douyin\.com/[^\s\"'<>，。；、]+)", text)
        if m:
            text = "https://" + m.group(1)
            url_match = re.search(r"https?://[^\s\"'<>，。；、]+", text)
    if url_match:
        url = url_match.group(0).rstrip("/").rstrip(")").rstrip("]")
        # 短链 / z.douyin.com 需要跟随重定向
        low = url.lower()
        if re.search(r"v\.douyin\.com|z\.douyin\.com", low):
            try:
                resp = session.get(url, headers={"User-Agent": MOBILE_UA}, allow_redirects=True, timeout=20)
                final_url = resp.url
            except Exception as e:
                raise DyError(f"短链解析失败: {e}")
        else:
            final_url = url
        m = ID_RE.search(final_url)
        if m:
            return m.group(1)
        profile = PROFILE_RE.search(final_url)
        if profile:
            return "user:" + profile.group(1)
        sec_uid = re.search(r"[?&]sec_uid=([A-Za-z0-9_-]{20,})", final_url)
        if sec_uid:
            return "user:" + sec_uid.group(1)
        # 重定向后仍是短链形态(如 z.douyin.com 的落地页), 尝试在响应里找
        if url_match and "z.douyin.com" in low:
            m2 = ID_RE.search(resp.text)
            if m2:
                return m2.group(1)
        raise DyError(f"无法从 URL 中提取视频 ID: {final_url[:120]}")
    # 裸 ID
    m = BARE_ID_RE.search(text)
    if m:
        return m.group(1)
    raise DyError("未找到有效的抖音链接或视频 ID")


# ---------------------------------------------------------------- 数据获取

_HTTP_DEAD = False  # 分享页 SSR 通道已挂时置 True, 同批次直接走浏览器


def fetch_aweme(session: requests.Session, video_id: str) -> dict:
    """抓取分享页并解析 _ROUTER_DATA, 返回 item_list[0] 原始 dict。
    主用 m.douyin.com (WAF 较宽松), iesdouyin 兜底。"""
    last_err = ""
    for host in ("https://m.douyin.com/share/video/", "https://www.iesdouyin.com/share/video/"):
        url = f"{host}{video_id}"
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
            m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", resp.text, re.DOTALL)
            if not m:
                last_err = f"{host}: 未包含 _ROUTER_DATA (疑似 WAF 挑战页)"
                continue
            data = json.loads(m.group(1).strip().rstrip(";"))
            ld = data.get("loaderData", {})
            page_key = next((k for k in ld if k.endswith("/page")), None)
            if not page_key:
                last_err = f"{host}: loaderData 中无 page 数据"
                continue
            res = ld[page_key].get("videoInfoRes") or {}
            items = res.get("item_list") or []
            if not items:
                status = res.get("status_code")
                raise DyError(f"作品信息为空 (status_code={status}), 视频可能已删除/私密/违规")
            return items[0]
        except requests.RequestException as e:
            last_err = f"{host}: 请求失败 {e}"
    raise DyError(f"分享页抓取失败: {last_err} (可能被风控, 稍后重试或换 IP)")


# ---------------------------------------------------- 浏览器兜底 (playwright)

_DETAIL_PARAMS = {
    "aid": "6383", "device_platform": "webapp", "device_type": "web_h264",
    "version_code": "170400", "version_name": "17.4.0", "cookie_enabled": "true",
    "screen_width": "1280", "screen_height": "720", "browser_language": "zh-CN",
    "browser_platform": "Win32", "browser_name": "Chrome",
    "browser_version": "131.0.0.0", "os_name": "Windows", "os_version": "10",
    "cpu_core_num": "8", "device_memory": "16", "platform": "PC", "downlink": "10",
}


class BrowserSession:
    """无头 Chromium 会话，同一批次复用。
    抖音 API 数据都在 secsdk/bdms 签名后面，只有在真实 JS 环境里发 fetch
    才会被自动加 uifid/a_bogus/msToken。免登录无状态：每次全新 context。"""

    def __init__(self):
        self._pw = self._browser = self._ctx = self._page = None

    def _ensure(self):
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise DyError("需要 playwright: uv pip install playwright && python -m playwright install chromium")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            user_agent=DESKTOP_UA, locale="zh-CN",
            viewport={"width": 1280, "height": 720})
        self._page = self._ctx.new_page()

    def fetch_aweme(self, video_id: str) -> dict:
        self._ensure()
        nav_error = ""
        for _ in range(3):
            try:
                self._page.goto(f"https://www.douyin.com/video/{video_id}",
                                wait_until="domcontentloaded", timeout=45000)
                nav_error = ""
                break
            except Exception as e:
                nav_error = str(e)[:160]
                time.sleep(1)
        if nav_error:
            raise DyError(f"作品页打开失败: {nav_error}")
        q = dict(_DETAIL_PARAMS, aweme_id=video_id)
        script = ("async q => { const timeout = new Promise((_, rej) => "
                  "setTimeout(() => rej(new Error('detail-fetch-timeout-8s')), 8000)); "
                  "const req = fetch("
                  "'/aweme/v1/web/aweme/detail/?' + new URLSearchParams(q),"
                  " {credentials:'include'}).then(r => r.json()); "
                  "return await Promise.race([req, timeout]); }")
        last = ""
        for _ in range(4):
            try:
                data = self._page.evaluate(script, q)
            except Exception as e:
                last = str(e)[:200]
                time.sleep(1.5)
                continue
            if isinstance(data, dict) and data.get("aweme_detail"):
                return data["aweme_detail"]
            last = f"status_code={data.get('status_code') if isinstance(data, dict) else data}"
            time.sleep(1.5)
        raise DyError(f"浏览器内 detail API 未返回数据: {last} (可能触发登录墙/风控)")

    def fetch_user_posts(self, sec_uid: str) -> list[dict]:
        """读取公开主页作品列表并自动翻页,返回图文与视频作品原始数据。
        纯 API 分页:max_cursor 从响应的 max_cursor 透传,has_more=0 或空页即停。"""
        self._ensure()
        self._page.goto(f"https://www.douyin.com/user/{sec_uid}",
                        wait_until="domcontentloaded", timeout=45000)
        q = dict(_DETAIL_PARAMS, sec_user_id=sec_uid, count="50", max_cursor="0",
                 publish_video_strategy_type="2", channel="channel_pc_web")
        script = ("async q => { const timeout = new Promise((_, rej) => "
                  "setTimeout(() => rej(new Error('post-fetch-timeout-15s')), 15000)); "
                  "const get = async () => { const r = await fetch('/aweme/v1/web/aweme/post/?' + "
                  "new URLSearchParams(q), {credentials:'include'}); return await r.json(); }; "
                  "return await Promise.race([get(), timeout]); }")
        items, seen, cursor = [], set(), "0"
        while True:
            q["max_cursor"] = cursor
            data = None
            for attempt in range(5):
                try:
                    data = self._page.evaluate(script, q)
                    if isinstance(data, dict) and data.get("status_code") == 0:
                        break
                    last = f"status_code={data.get('status_code') if isinstance(data, dict) else data}"
                except Exception as e:
                    last = str(e)[:160]
                    if "post-fetch-timeout-15s" in last:
                        print(f"  [主页分页] {cursor} 等待15秒无结果,刷新主页后重试 ({attempt + 1}/5)", file=sys.stderr)
                        try:
                            self._page.reload(wait_until="domcontentloaded", timeout=45000)
                        except Exception:
                            try:
                                self._page.goto(f"https://www.douyin.com/user/{sec_uid}",
                                                wait_until="domcontentloaded", timeout=45000)
                            except Exception as reload_error:
                                last = f"刷新主页失败: {str(reload_error)[:120]}"
                    time.sleep(min(2 * (attempt + 1), 8))
            if not isinstance(data, dict) or data.get("status_code") != 0:
                raise DyError(f"主页作品列表获取失败: {last if 'last' in locals() else data}")
            page_items = data.get("aweme_list") or []
            if not page_items and not items:
                return []
            for item in page_items:
                aweme_id = str(item.get("aweme_id") or "")
                if aweme_id and aweme_id not in seen:
                    seen.add(aweme_id)
                    items.append(item)
            next_cursor = str(data.get("max_cursor") or "")
            if not data.get("has_more") or not page_items or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return items

    def close(self):
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = self._page = None


_BROWSER = None


def get_browser() -> BrowserSession:
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = BrowserSession()
    return _BROWSER


def close_browser() -> None:
    global _BROWSER
    if _BROWSER is not None:
        _BROWSER.close()
        _BROWSER = None


def fetch_aweme_auto(session: requests.Session, video_id: str) -> dict:
    """先纯 HTTP 快路径，失败自动切浏览器兜底。"""
    global _HTTP_DEAD
    if not _HTTP_DEAD:
        try:
            return fetch_aweme(session, video_id)
        except DyError as e:
            _HTTP_DEAD = True
            print(f"  [HTTP 通道不可用, 切浏览器兜底] {e}", file=sys.stderr)
    return get_browser().fetch_aweme(video_id)


def fetch_user_posts_auto(session: requests.Session, sec_uid: str) -> list[dict]:
    """通过共享的浏览器会话获取作者主页作品。"""
    return get_browser().fetch_user_posts(sec_uid)


def pick_video_url(item: dict) -> str:
    """挑选无水印播放地址: 优先最高码率, 退化为 play_addr 换 play。"""
    video = item.get("video") or {}
    if not video:
        return ""
    # bit_rate 里通常是无水印地址(play, 非 playwm)
    bit_rates = video.get("bit_rate") or []
    if bit_rates:
        cands = sorted(bit_rates, key=lambda b: b.get("bit_rate") or 0, reverse=True)
        for br in cands:
            pa = (br.get("play_addr") or {}).get("url_list") or []
            for u in pa:
                if "playwm" not in u:
                    return u
    # 兜底: play_addr 首个, 换掉 watermark
    url_list = (video.get("play_addr") or {}).get("url_list") or []
    if url_list:
        return url_list[0].replace("playwm", "play")
    return ""


def download_video(session: requests.Session, url: str, dest: Path) -> None:
    dl_headers = {**HEADERS, "Referer": "https://www.douyin.com/"}
    _download_binary(session, url, dest, dl_headers, "视频")


def image_urls(item: dict) -> list[str]:
    """返回图文作品中优先级最高的原图地址。"""
    result = []
    for image in item.get("images") or []:
        candidates = []
        # Douyin's download_url_list can point to a rendered copy with the
        # account watermark baked in. Prefer origin_image and the final URL
        # variants from url_list, which commonly expose the original JPEG.
        for key in ("watermark_free_download_url_list", "origin_image", "url_list", "origin_url", "download_url_list", "display_image"):
            value = image.get(key)
            if isinstance(value, dict):
                values = value.get("url_list") or []
            elif isinstance(value, list):
                values = value
            elif isinstance(value, str):
                values = [value]
            else:
                values = []
            # URL lists may include transformed previews before original-size
            # URLs; try the last entry first, then retain fallbacks.
            candidates.extend(reversed(values))
        # Do not fall back to an explicitly watermarked rendition in the
        # no-watermark archive; skip this image if no clean source is exposed.
        url = next((u for u in candidates
                    if isinstance(u, str) and u.startswith("http")
                    and not re.search(r"dy[-_]?water|watermark(?:=|%3d)|logo_name", u, re.I)), "")
        if url and url not in result:
            result.append(url)
    return result


def download_image(session: requests.Session, url: str, dest: Path) -> None:
    dl_headers = {**HEADERS, "Referer": "https://www.douyin.com/"}
    _download_binary(session, url, dest, dl_headers, "图片")


def _download_binary(session: requests.Session, url: str, dest: Path,
                     headers: dict, label: str) -> None:
    last_error = None
    for attempt in range(3):
        dest.unlink(missing_ok=True)
        try:
            with session.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            size = dest.stat().st_size
            if size < 1024:
                raise DyError(f"{label}下载文件过小({size}B), 疑似失败")
            return
        except (requests.RequestException, DyError) as e:
            last_error = e
            dest.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(attempt + 1)
    raise DyError(f"{label}下载失败，重试3次后仍未成功: {last_error}")


def extract_audio(video_path: Path, audio_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DyError("未找到 ffmpeg, 请先安装: brew install ffmpeg")
    cmd = [ffmpeg, "-y", "-i", str(video_path), "-vn",
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DyError(f"ffmpeg 提取音频失败: {proc.stderr[-300:]}")


# ---------------------------------------------------------------- 转写

FW_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def get_model_dir(model_size: str) -> str:
    """返回本地模型目录: 支持模型名 / HF仓库名 / 本地路径。
    下载走 hf-mirror 镜像, trust_env=False 强制直连(绕过 Clash 代理破坏直连的问题),
    彻底绕开 huggingface_hub 的主机校验。"""
    if os.path.sep in model_size or os.path.isdir(model_size):
        return model_size
    repo = FW_MODELS.get(model_size, model_size)
    cache = Path.home() / ".cache" / "dycopy" / "models" / repo.split("/")[-1]
    if (cache / "model.bin").exists():
        return str(cache)
    cache.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.trust_env = False  # 忽略 HTTP(S)_PROXY 环境变量, 直连镜像
    api = f"https://hf-mirror.com/api/models/{repo}"
    r = s.get(api, timeout=30)
    r.raise_for_status()
    files = [x["rfilename"] for x in r.json().get("siblings", [])]
    if not files:
        raise DyError(f"模型仓库 {repo} 文件列表为空")
    for fn in files:
        url = f"https://hf-mirror.com/{repo}/resolve/main/{fn}"
        print(f"  下载模型 {repo}/{fn} ...", file=sys.stderr)
        with s.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(cache / fn, "wb") as f:
                for chunk in resp.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
    print(f"  模型就绪: {cache}", file=sys.stderr)
    return str(cache)


def transcribe_faster_whisper(audio_path: Path, model_size: str) -> tuple[str, str]:
    """本地 faster-whisper。返回 (段落文本, srt)。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise DyError("需要 faster-whisper: uv pip install faster-whisper")
    model_dir = get_model_dir(model_size)
    model = WhisperModel(model_dir, device="cpu", compute_type="int8")
    # initial_prompt 强制简体中文输出, 并显著提升中文识别准确率
    segments, info = model.transcribe(
        str(audio_path), language="zh", vad_filter=True,
        initial_prompt="以下是普通话的句子，请用简体中文输出。")
    lines, srt, idx = [], [], 1
    for seg in segments:
        t = seg.text.strip()
        if not t:
            continue
        lines.append(t)
        srt.append(f"{idx}")
        srt.append(f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}")
        srt.append(t)
        srt.append("")
        idx += 1
    return "\n".join(lines), "\n".join(srt)


def transcribe_sensevoice(audio_path: Path) -> tuple[str, str]:
    """本地 SenseVoiceSmall (funasr + modelscope), 中文短视频场景更优。"""
    try:
        from funasr import AutoModel
    except ImportError:
        raise DyError("需要 funasr: uv pip install funasr modelscope")
    model = AutoModel(
        model="iic/SenseVoiceSmall",
        trust_remote_code=True,
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        device="cpu",
        disable_update=True,
    )
    res = model.generate(input=str(audio_path), language="zh", use_itn=True, batch_size_s=60)
    text = (res[0].get("text") or "").strip() if res else ""
    # SenseVoice 输出含 <|zh|> 等标签, 去掉; 按句末标点断行便于阅读
    text = re.sub(r"<\|[^|]*\|>", "", text).strip()
    text = re.sub(r"([。？！])", r"\1\n", text).strip()
    return text, ""


def transcribe_siliconflow(audio_path: Path, api_key: str) -> tuple[str, str]:
    """硅基流动 SenseVoice API。"""
    with open(audio_path, "rb") as f:
        files = {"file": (audio_path.name, f, "audio/wav"),
                 "model": (None, "FunAudioLLM/SenseVoiceSmall")}
        r = requests.post(
            "https://api.siliconflow.cn/v1/audio/transcriptions",
            files=files, headers={"Authorization": f"Bearer {api_key}"}, timeout=300)
    if r.status_code != 200:
        raise DyError(f"硅基流动 API 失败 (HTTP {r.status_code}): {r.text[:200]}")
    return (r.json().get("text") or "").strip(), ""


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},000"


# ---------------------------------------------------------------- 输出

def safe_name(s: str, max_len: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")
    return s[:max_len] or "video"


def summarize(item: dict) -> dict:
    v = item.get("video") or {}
    st = item.get("statistics") or {}
    author = item.get("author") or {}
    music = item.get("music") or {}
    return {
        "aweme_id": item.get("aweme_id"),
        "aweme_type": item.get("aweme_type"),
        "desc": (item.get("desc") or "").strip(),
        "author": author.get("nickname", ""),
        "author_uid": author.get("uid", ""),
        "author_sec_uid": author.get("sec_uid", ""),
        "create_time": item.get("create_time"),
        "duration_ms": v.get("duration"),
        "stats": {k: st.get(k) for k in ("digg_count", "comment_count", "share_count", "collect_count")},
        "music": music.get("title", ""),
        # Image posts also carry a preview video object (often zero duration),
        # so the official aweme_type is the most reliable discriminator.
        "is_image_post": str(item.get("aweme_type")) == "68" or (bool(item.get("images")) and not pick_video_url(item)),
        "image_urls": image_urls(item),
        "video_url": pick_video_url(item),
    }


def render_markdown(info: dict, transcript: str = "", srt: str = "", video_file: str = "") -> str:
    lines = []
    lines.append(f"# {info['desc'] or '(无文案)'}")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append(f"| 作者 | {info['author']} |")
    lines.append(f"| 作品ID | {info['aweme_id']} |")
    if info["create_time"]:
        lines.append(f"| 发布时间 | {time.strftime('%Y-%m-%d %H:%M', time.localtime(info['create_time']))} |")
    if info["duration_ms"]:
        lines.append(f"| 时长 | {info['duration_ms'] / 1000:.1f}s |")
    lines.append(f"| 点赞 | {info['stats'].get('digg_count', 0)} |")
    lines.append(f"| 评论 | {info['stats'].get('comment_count', 0)} |")
    lines.append(f"| 转发 | {info['stats'].get('share_count', 0)} |")
    lines.append(f"| 收藏 | {info['stats'].get('collect_count', 0)} |")
    if info["music"]:
        lines.append(f"| 音乐 | {info['music']} |")
    if info["is_image_post"]:
        lines.append("| 类型 | 图文作品 |")
    if video_file:
        lines.append(f"| 视频文件 | {video_file} |")
    lines.append("")
    lines.append("## 文案")
    lines.append("")
    lines.append(info["desc"] or "（无描述文案）")
    if transcript:
        lines.append("")
        lines.append("## 口播逐字稿")
        lines.append("")
        lines.append(transcript)
    if srt:
        lines.append("")
        lines.append("## 字幕 (SRT)")
        lines.append("")
        lines.append("```")
        lines.append(srt)
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def process_one(session, text: str, args, item: dict | None = None) -> dict:
    video_id = extract_id(text, session) if item is None else str(item.get("aweme_id") or text)
    print(f"  [1/3] 视频ID: {video_id}", file=sys.stderr)
    if item is None:
        item = fetch_aweme_auto(session, video_id)
    info = summarize(item)
    print(f"  [2/3] 作者: {info['author']} | 文案: {info['desc'][:50] or '(无)'}", file=sys.stderr)

    # 每个作品归集到独立子目录: 输出根目录/<作者ID>/<作品名>_<作品ID>/
    stem = f"{safe_name(info['desc'])}_{info['aweme_id']}"
    role_id = safe_name(str(item.get("_role_sec_uid") or info["author_sec_uid"] or info["author_uid"] or "unknown"), 80)
    vdir = args.out / role_id / stem if args.role_folders else args.out / stem

    video_file = ""
    if args.download or args.media_only or args.asr:
        if info["is_image_post"]:
            # Older dycopy versions could save the image post's preview stream as MP4.
            # In a media-only archive, remove that incompatible output when replacing it with images.
            if args.media_only:
                (vdir / f"{stem}.mp4").unlink(missing_ok=True)
            if not info["image_urls"]:
                print("  [警告] 图文作品未找到图片地址", file=sys.stderr)
            else:
                vdir.mkdir(parents=True, exist_ok=True)
                print(f"  [3/3] 下载图文图片 ({len(info['image_urls'])} 张)...", file=sys.stderr)
                image_files = []
                for idx, url in enumerate(info["image_urls"], 1):
                    # 原始链接通常带有真实格式扩展名；没有时按 jpg 保存。
                    ext = Path(url.split("?", 1)[0]).suffix.lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                        ext = ".jpg"
                    name = f"{safe_name(str(info['aweme_id']), 30)}_{idx:02d}{ext}"
                    image_path = vdir / name
                    try:
                        if not image_path.exists() or image_path.stat().st_size < 1024:
                            download_image(session, url, image_path)
                        image_files.append(name)
                    except (DyError, requests.RequestException) as e:
                        print(f"        [图片失败] {name}: {e}", file=sys.stderr)
                video_file = ", ".join(image_files)
                print(f"        保存: {vdir}", file=sys.stderr)
        elif not info["video_url"]:
            print("  [警告] 未找到可下载的视频或图集媒体", file=sys.stderr)
        else:
            vdir.mkdir(parents=True, exist_ok=True)
            vpath = vdir / f"{stem}.mp4"
            if not vpath.exists() or vpath.stat().st_size < 1024:
                print(f"  [3/3] 下载无水印视频 ({(info['duration_ms'] or 0) / 1000:.0f}s)...", file=sys.stderr)
                download_video(session, info["video_url"], vpath)
            else:
                print(f"  [3/3] 已有视频，跳过重复下载: {vpath.name}", file=sys.stderr)
            video_file = vpath.name
            print(f"        保存: {vpath}", file=sys.stderr)

    transcript, srt = "", ""
    if args.asr and info["video_url"]:
        apath = vdir / f".audio_{info['aweme_id']}.wav"
        try:
            extract_audio(vpath, apath)
            print(f"  [4/4] ASR 转写 ({args.asr})...", file=sys.stderr)
            if args.asr == "faster-whisper":
                transcript, srt = transcribe_faster_whisper(apath, args.model)
            elif args.asr == "sensevoice":
                transcript, srt = transcribe_sensevoice(apath)
            elif args.asr == "siliconflow":
                transcript, srt = transcribe_siliconflow(apath, args.api_key)
            print(f"        转写 {len(transcript)} 字", file=sys.stderr)
        finally:
            apath.unlink(missing_ok=True)

    md = render_markdown(info, transcript, srt, video_file)
    mpath = None
    if not args.media_only and not args.no_md:
        vdir.mkdir(parents=True, exist_ok=True)
        mpath = vdir / f"{stem}.md"
        mpath.write_text(md, encoding="utf-8")
        if args.srt and srt:
            (vdir / f"{stem}.srt").write_text(srt, encoding="utf-8")
    return {"info": info, "md": md, "md_path": str(mpath), "media_dir": str(vdir), "video_file": video_file}


def process_input(session, text: str, args) -> list[dict]:
    target = extract_id(text, session)
    if not target.startswith("user:"):
        return [process_one(session, target, args)]
    sec_uid = target.removeprefix("user:")
    print(f"  [主页] 作者ID: {sec_uid}", file=sys.stderr)
    posts = fetch_user_posts_auto(session, sec_uid)
    print(f"  [主页] 找到 {len(posts)} 条作品", file=sys.stderr)
    results = []
    for idx, item in enumerate(posts, 1):
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            continue
        print(f"  [主页 {idx}/{len(posts)}] {aweme_id}", file=sys.stderr)
        try:
            results.append(process_one(session, aweme_id, args, item=item))
        except DyError as e:
            print(f"  [失败] {e}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  [失败] 网络错误: {e}", file=sys.stderr)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description="dycopy — 抖音视频文案提取器 (desc + 无水印视频 + 口播逐字稿)")
    p.add_argument("inputs", nargs="+", help="抖音链接 / 分享口令文本 / 视频ID / 含链接的 .txt 文件(每行一条)")
    p.add_argument("--asr", choices=["faster-whisper", "sensevoice", "siliconflow"], default=None,
                   help="启用语音转写: faster-whisper(本地,默认) / sensevoice(本地,中文更优) / siliconflow(云端需key)")
    p.add_argument("--model", default="small", help="faster-whisper 模型大小 (tiny/base/small/medium, 默认 small)")
    p.add_argument("--api-key", default=os.environ.get("SILICONFLOW_API_KEY"),
                   help="siliconflow 的 API Key (默认读取 SILICONFLOW_API_KEY 环境变量)")
    p.add_argument("--download", action="store_true", help="下载无水印视频或图文原图, 不转写")
    p.add_argument("--media-only", action="store_true", help="只保存视频或图文图片，不生成文案等文件")
    p.add_argument("--role-folders", action="store_true", help="按作者ID归档: 输出根目录/<作者ID>/<作品>/")
    p.add_argument("--srt", action="store_true", help="额外保存 SRT 字幕文件")
    p.add_argument("--out", default="result", help="输出根目录 (默认 ./result, 每个视频一个子目录)")
    p.add_argument("--no-md", action="store_true", help="不写 Markdown 文件, 仅打印")
    p.add_argument("--no-proxy", action="store_true",
                   help="去除 HTTP(S)_PROXY 环境变量(国内环境下载 HF 模型时 Clash 代理会破坏直连)")
    args = p.parse_args()

    if args.no_proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)

    args.out = Path(args.out).expanduser()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.asr == "siliconflow" and not args.api_key:
        p.error("--asr siliconflow 需要 --api-key 或 SILICONFLOW_API_KEY 环境变量")

    session = requests.Session()
    results = []
    try:
        for inp in args.inputs:
            print(f"==> 处理: {inp[:80]}", file=sys.stderr)
            try:
                pth = Path(inp)
                if pth.exists() and pth.suffix.lower() == ".json":
                    payload = json.loads(pth.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        payload = payload.get("items", [])
                    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "items" in payload[0]:
                        payload = [dict(item, _role_sec_uid=group.get("sec"))
                                   for group in payload for item in (group.get("items") or [])]
                    if not isinstance(payload, list):
                        raise DyError("JSON 输入应为作品数组，或包含 items 的分组数组")
                    print(f"  [作品数据] 加载 {len(payload)} 条", file=sys.stderr)
                    for idx, item in enumerate(payload, 1):
                        if not isinstance(item, dict) or not item.get("aweme_id"):
                            print(f"  [跳过] JSON 第 {idx} 项缺少 aweme_id", file=sys.stderr)
                            continue
                        try:
                            results.append(process_one(session, str(item["aweme_id"]), args, item=item))
                        except DyError as e:
                            print(f"  [失败] {e}", file=sys.stderr)
                        except requests.RequestException as e:
                            print(f"  [失败] 网络错误: {e}", file=sys.stderr)
                    continue
                texts = pth.read_text(encoding="utf-8").splitlines() if pth.exists() and pth.suffix == ".txt" else [inp]
                for t in texts:
                    t = t.strip()
                    if not t or t.startswith("#"):
                        continue
                    try:
                        results.extend(process_input(session, t, args))
                    except DyError as e:
                        print(f"  [失败] {e}", file=sys.stderr)
                    except requests.RequestException as e:
                        print(f"  [失败] 网络错误: {e}", file=sys.stderr)
                    except Exception as e:
                        print(f"  [失败] {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            except DyError as e:
                print(f"  [失败] {e}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"  [失败] 网络错误: {e}", file=sys.stderr)
    finally:
        close_browser()

    if args.no_md:
        for r in results:
            print(r["md"])
    else:
        print(f"\n完成: {len(results)} 个作品", file=sys.stderr)
        for r in results:
            print(f"  - {r['md_path'] or r['media_dir']}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
