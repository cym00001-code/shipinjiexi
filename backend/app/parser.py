"""小红书笔记解析核心.

仅依赖 httpx, 无浏览器无 JS 引擎. 工作原理:
1. PC UA 请求笔记 URL (xhslink 短链自动 follow_redirects 跳到 xiaohongshu.com)
2. 从 HTML 中正则提取 ``window.__INITIAL_STATE__`` JSON
3. 在 ``noteDetailMap[noteId].note`` 路径下取出标题/作者/媒体
4. 视频笔记: 把 stream.{h264,h265,av1} 数组完整暴露成多清晰度选项
5. 图文笔记: 取 ``imageList`` 的无水印 traceId 直链
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import httpx

from .config import DEFAULT_UA, REQUEST_TIMEOUT, XHS_COOKIE

INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*</script>",
    re.DOTALL,
)
NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([a-zA-Z0-9]+)")
URL_IN_TEXT_RE = re.compile(r"https?://[^\s一-龥]+")


@dataclass
class VideoStream:
    quality: str            # 友好名: 1080P / 720P 等
    codec: str              # h264 / h265 / av1
    format: str = "mp4"
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    bitrate: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    audio_channels: Optional[int] = None
    quality_type: Optional[str] = None
    stream_type: Optional[int] = None
    size: Optional[int] = None
    duration: Optional[float] = None
    master_url: str = ""
    backup_urls: List[str] = field(default_factory=list)
    decode_key: Optional[str] = None
    enc_limit: Optional[int] = None
    proxy_url: Optional[str] = None


@dataclass
class ImageItem:
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    live_video_url: Optional[str] = None  # 实况图(动图) 的 mp4 直链, 没有则 None


@dataclass
class XhsNote:
    note_id: str
    type: str               # video / normal
    platform: str = "xhs"
    title: str = ""
    description: str = ""
    author: str = ""
    author_id: str = ""
    avatar: str = ""
    cover: str = ""
    duration: Optional[float] = None
    liked_count: Optional[int] = None
    collected_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    tags: List[str] = field(default_factory=list)
    publish_time: Optional[int] = None
    source_url: str = ""
    status: str = "ok"
    message: Optional[str] = None
    videos: List[VideoStream] = field(default_factory=list)
    images: List[ImageItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class XhsParseError(Exception):
    pass


def extract_url(text: str) -> Optional[str]:
    """从分享文本中抓出第一个 http(s) URL."""
    m = URL_IN_TEXT_RE.search(text or "")
    return m.group(0) if m else None


def _resolve_quality(width: Optional[int], height: Optional[int]) -> str:
    if not height:
        return "未知"
    short = min(width or height, height)
    table = [(2160, "4K"), (1440, "2K"), (1080, "1080P"),
             (720, "720P"), (540, "540P"), (480, "480P"), (360, "360P")]
    for thr, name in table:
        if short >= thr:
            return name
    return f"{short}P"


def _pick(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("list", "items", "videos", "streams"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return []


def _duration_seconds(value) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1000 if number > 1000 else number


class XhsParser:
    def __init__(self, cookie: str = "") -> None:
        self.cookie = cookie or XHS_COOKIE

    async def parse(self, url: str) -> XhsNote:
        clean_url = extract_url(url) or url
        if not clean_url.startswith("http"):
            raise XhsParseError("链接格式不正确")
        if not ("xiaohongshu.com" in clean_url or "xhslink.com" in clean_url):
            raise XhsParseError("仅支持小红书链接 (xiaohongshu.com / xhslink.com)")

        headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        if self.cookie:
            headers["Cookie"] = self.cookie

        async with httpx.AsyncClient(
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        ) as client:
            resp = await client.get(clean_url)
            resp.raise_for_status()
            final_url = str(resp.url)
            html = resp.text

        note_id = self._extract_note_id(final_url) or self._extract_note_id(clean_url)
        if not note_id:
            raise XhsParseError("无法从链接中提取笔记 ID, 请确认是作品分享链接")

        state = self._extract_initial_state(html)
        note_raw = self._find_note(state, note_id)
        if not note_raw:
            raise XhsParseError(
                "未能在页面中找到笔记数据 — 可能笔记已删除/设为私密, 或小红书反爬触发, "
                "请稍后再试 (或在 .env 配置 XHS_COOKIE)."
            )

        return self._build_note(clean_url, note_id, note_raw)

    @staticmethod
    def _extract_note_id(url: str) -> Optional[str]:
        m = NOTE_ID_RE.search(url)
        return m.group(1) if m else None

    @staticmethod
    def _extract_initial_state(html: str) -> dict:
        m = INITIAL_STATE_RE.search(html)
        if not m:
            raise XhsParseError("未找到 __INITIAL_STATE__, 小红书可能反爬, 请稍后再试")
        raw = m.group(1).replace("undefined", "null")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise XhsParseError(f"解析 INITIAL_STATE 失败: {exc}") from exc

    @staticmethod
    def _find_note(state: dict, note_id: str) -> Optional[dict]:
        try:
            note_map = state["note"]["noteDetailMap"]
            return (note_map.get(note_id) or {}).get("note")
        except (KeyError, TypeError):
            return None

    def _build_note(self, source_url: str, note_id: str, raw: dict) -> XhsNote:
        user = raw.get("user") or {}
        interact = raw.get("interactInfo") or {}

        note = XhsNote(
            note_id=note_id,
            type=raw.get("type") or "normal",
            title=(raw.get("title") or "").strip(),
            description=(raw.get("desc") or "").strip(),
            author=user.get("nickname") or "",
            author_id=user.get("userId") or "",
            avatar=user.get("avatar") or "",
            tags=[t.get("name") for t in (raw.get("tagList") or []) if t.get("name")],
            publish_time=raw.get("time"),
            liked_count=_safe_int(interact.get("likedCount")),
            collected_count=_safe_int(interact.get("collectedCount")),
            comment_count=_safe_int(interact.get("commentCount")),
            share_count=_safe_int(interact.get("shareCount")),
            source_url=source_url,
        )

        if note.type == "video":
            self._fill_video(note, raw)
        else:
            self._fill_images(note, raw)

        if not note.cover:
            cover_info = (raw.get("imageList") or [{}])[0]
            note.cover = self._first_image_url(cover_info) or note.cover

        return note

    @staticmethod
    def _fill_video(note: XhsNote, raw: dict) -> None:
        video = raw.get("video") or {}
        capa = video.get("capa") or {}
        note.duration = _duration_seconds(capa.get("duration"))

        media = video.get("media") or {}
        stream = media.get("stream") or {}

        for codec in ("h264", "h265", "av1"):
            for item in _as_list(stream.get(codec)):
                master_url = _pick(item, "masterUrl", "master_url", "url") or ""
                backup = list(_pick(item, "backupUrls", "backup_urls", "backupUrl", "backup_url") or [])
                if not master_url and backup:
                    master_url = backup[0]
                if not master_url:
                    continue
                width, height = _pick(item, "width", "videoWidth"), _pick(item, "height", "videoHeight")
                format_name = "mp4"
                if ".m3u8" in master_url.split("?")[0].lower():
                    format_name = "hls"
                note.videos.append(VideoStream(
                    quality=_resolve_quality(width, height),
                    codec=codec,
                    format=format_name,
                    width=width,
                    height=height,
                    fps=_pick(item, "fps", "frameRate", "frame_rate"),
                    bitrate=_pick(item, "videoBitrate", "video_bitrate", "avgBitrate", "avg_bitrate", "bitrate"),
                    audio_codec=_pick(item, "audioCodec", "audio_codec", "audioFormat", "audio_format"),
                    audio_bitrate=_pick(item, "audioBitrate", "audio_bitrate"),
                    audio_channels=_pick(item, "audioChannels", "audio_channels"),
                    quality_type=_pick(item, "qualityType", "quality_type", "quality"),
                    stream_type=_pick(item, "streamType", "stream_type"),
                    size=_pick(item, "size", "fileSize", "file_size"),
                    duration=_duration_seconds(_pick(item, "videoDuration", "video_duration") or capa.get("duration")),
                    master_url=master_url,
                    backup_urls=backup,
                ))

        # 兜底: 多码率列表为空时尝试 originVideoKey (XHS-Downloader 同款方案)
        if not note.videos:
            origin_key = ((video.get("consumer") or {}).get("originVideoKey")) or ""
            if origin_key:
                note.videos.append(VideoStream(
                    quality="原画",
                    codec="h264",
                    format="mp4",
                    duration=_duration_seconds(capa.get("duration")),
                    master_url=f"https://sns-video-bd.xhscdn.com/{origin_key}",
                ))

        # 排序: 同 codec 内分辨率从高到低; codec 偏好 h264 (兼容性最好)
        codec_pref = {"h264": 0, "h265": 1, "av1": 2}
        note.videos.sort(key=lambda v: (codec_pref.get(v.codec, 9),
                                        -(v.height or 0),
                                        -(v.width or 0),
                                        -(v.bitrate or 0),
                                        -(v.audio_bitrate or 0),
                                        -(v.size or 0)))

        # cover: 优先用 video 自带封面, 否则用 imageList[0]
        first_img = (raw.get("imageList") or [{}])[0]
        note.cover = XhsParser._first_image_url(first_img) or ""

    @staticmethod
    def _fill_images(note: XhsNote, raw: dict) -> None:
        for img in raw.get("imageList") or []:
            url = XhsParser._first_image_url(img)
            if not url:
                continue
            live_url = None
            stream = (img.get("stream") or {})
            for codec in ("h264", "h265", "av1"):
                items = stream.get(codec) or []
                if items:
                    live_url = items[0].get("masterUrl") or (
                        (items[0].get("backupUrls") or [None])[0]
                    )
                    if live_url:
                        break
            note.images.append(ImageItem(
                url=url,
                width=img.get("width"),
                height=img.get("height"),
                live_video_url=live_url,
            ))

    @staticmethod
    def _first_image_url(img: dict) -> str:
        """选无水印原图. 优先 infoList 的 WB_DFT, 其次 traceId 拼 CDN."""
        for info in img.get("infoList") or []:
            if info.get("imageScene") == "WB_DFT" and info.get("url"):
                return info["url"]
        trace_id = img.get("traceId") or img.get("fileId")
        if trace_id:
            return f"https://sns-img-qc.xhscdn.com/{trace_id}"
        return img.get("urlDefault") or img.get("url") or ""


def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None
