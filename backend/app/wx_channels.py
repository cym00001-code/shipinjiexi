"""微信视频号解析与下载解密辅助.

第一版不尝试在服务端直接破解分享页. 视频号可下载字段通常来自微信 PC
端页面/本地采集器, 这里负责把那份 JSON 或下载命令归一化成前端可用结果.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import struct
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.etree import ElementTree

import httpx

from .config import DEFAULT_UA, REQUEST_TIMEOUT
from .parser import VideoStream, XhsNote, extract_url, _resolve_quality

WX_ENC_LIMIT = 131072

_M64 = 0xFFFFFFFFFFFFFFFF
_GOLDEN64 = 0x9E3779B97F4A7C13
_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
_KEY_RE = re.compile(
    r"(?:decodeKey|decode_key|decrypt_key|decryptKey|key)\s*[:=]\s*['\"]?(\d+)['\"]?",
    re.IGNORECASE,
)


class WxChannelsParseError(Exception):
    pass


class WxChannelsParser:
    async def parse_text(self, text: str, cookie: str = "") -> XhsNote:
        payload = self._coerce_payload(text)
        share_url = payload.get("_share_url")
        if share_url:
            return await self._parse_share_url(share_url, cookie=cookie)
        return self._build_checked_note(payload, text)

    def parse(self, text: str) -> XhsNote:
        payload = self._coerce_payload(text)
        if payload.get("_share_url"):
            return self._build_share_stub(payload["_share_url"])
        return self._build_checked_note(payload, text)

    def _build_checked_note(self, payload: dict, text: str) -> XhsNote:
        note = self._build_note(payload, text)
        if not note.videos:
            raise WxChannelsParseError(
                "未找到视频号媒体字段. 请粘贴包含 objectDesc.media.url / urlToken / decodeKey "
                "的详情 JSON, 或粘贴媒体直链并附带 decodeKey=数字."
            )
        return note

    @staticmethod
    def looks_like(text: str) -> bool:
        low = (text or "").lower()
        return any(
            marker in low
            for marker in (
                "channels.weixin.qq.com",
                "finder",
                "objectdesc",
                "decodekey",
                "urltoken",
                "wxapp.tc.qq.com",
                "finder.video.qq.com",
                "weixin.qq.com",
            )
        )

    def _coerce_payload(self, text: str) -> dict:
        raw = (text or "").strip()
        if not raw:
            raise WxChannelsParseError("请粘贴视频号内容数据")

        data = self._loads_json(raw)
        if data is not None:
            return self._pick_object(data)

        match = _JSON_RE.search(raw)
        if match:
            data = self._loads_json(match.group(1))
            if data is not None:
                return self._pick_object(data)

        xml_payload = self._loads_xml(raw)
        if xml_payload is not None:
            return xml_payload

        url = extract_url(raw)
        if not url:
            raise WxChannelsParseError("未找到视频号媒体链接")

        key_match = _KEY_RE.search(raw)
        if not key_match and not _is_media_url(url):
            return {"_share_url": url}

        media = {"url": url}
        if key_match:
            media["decodeKey"] = key_match.group(1)
        return {
            "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
            "source_url": url,
            "objectDesc": {
                "description": "视频号视频",
                "media": [media],
            },
        }

    async def _parse_share_url(self, url: str, cookie: str = "") -> XhsNote:
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://channels.weixin.qq.com/",
        }
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            stub = self._build_share_stub(url)
            stub.message = f"链接已识别, 但请求视频号分享页失败: {exc}"
            return stub

        html = resp.text
        for payload in self._candidate_payloads_from_html(html):
            note = self._build_note(payload, url)
            if note.videos:
                note.source_url = url
                return note

        stub = self._build_share_stub(url, html=html)
        stub.message = (
            "已识别视频号分享链接, 但分享页没有公开下发可下载的 urlToken / decodeKey. "
            "这类字段通常只在微信 PC 登录环境或本地采集器里出现。"
        )
        return stub

    def _candidate_payloads_from_html(self, html: str) -> Iterable[dict]:
        decoder = json.JSONDecoder()
        markers = ("objectDesc", "decodeKey", "urlToken", "fullVideoUrl")
        for marker in markers:
            start = 0
            while True:
                idx = html.find(marker, start)
                if idx < 0:
                    break
                brace = html.rfind("{", 0, idx)
                if brace >= 0:
                    data = self._decode_json_at(html, brace, decoder)
                    if isinstance(data, dict):
                        yield self._pick_object(data)
                bracket = html.rfind("[", 0, idx)
                if bracket >= 0:
                    data = self._decode_json_at(html, bracket, decoder)
                    if data is not None:
                        yield self._pick_object(data)
                start = idx + len(marker)

    @staticmethod
    def _decode_json_at(text: str, index: int, decoder: json.JSONDecoder):
        snippet = text[index:]
        try:
            data, _ = decoder.raw_decode(snippet)
            return data
        except json.JSONDecodeError:
            return None

    def _build_share_stub(self, url: str, html: str = "") -> XhsNote:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        object_id = _first_qs(params, "objectId", "objectid", "id", "feedId", "exportid")
        nonce = _first_qs(params, "objectNonceId", "objectnonceid", "nonceid")
        title = self._html_meta(html, "og:title") or self._html_title(html) or "视频号分享链接"
        desc = self._html_meta(html, "description") or title
        cover = self._html_meta(html, "og:image") or ""
        note_key = object_id or nonce or url
        return XhsNote(
            note_id=f"wx_channels:{_compact_id(note_key)}",
            type="video",
            platform="wx_channels",
            title=unquote(title).strip() or "视频号分享链接",
            description=unquote(desc).strip(),
            cover=cover,
            source_url=url,
            status="needs_capture",
            message="已识别视频号链接, 正在尝试从分享页提取可下载媒体字段。",
        )

    @staticmethod
    def _html_meta(html: str, name: str) -> str:
        if not html:
            return ""
        pattern = re.compile(
            rf"<meta[^>]+(?:property|name)=['\"]{re.escape(name)}['\"][^>]+content=['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        match = pattern.search(html)
        return _unescape_html(match.group(1)) if match else ""

    @staticmethod
    def _html_title(html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
        return _unescape_html(match.group(1).strip()) if match else ""

    @staticmethod
    def _loads_json(text: str):
        try:
            return json.loads(text.lstrip("\ufeff").strip())
        except json.JSONDecodeError:
            return None

    def _loads_xml(self, text: str) -> Optional[dict]:
        raw = text.lstrip("\ufeff").strip()
        if not raw.startswith("<"):
            start = raw.find("<")
            if start < 0:
                return None
            raw = raw[start:]
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError:
            return None

        values: Dict[str, List[str]] = {}
        for elem in root.iter():
            tag = elem.tag.split("}", 1)[-1].lower()
            value = (elem.text or "").strip()
            if value:
                values.setdefault(tag, []).append(value)

        def first(*names):
            for name in names:
                items = values.get(name.lower())
                if items:
                    return items[0]
            return None

        media = {
            "url": first("url", "mediaurl", "fullvideourl"),
            "urlToken": first("urltoken"),
            "decodeKey": first("decodekey", "decode_key"),
            "coverUrl": first("coverurl", "thumburl", "thumbUrl"),
            "width": first("width"),
            "height": first("height"),
            "fileSize": first("filesize", "size"),
            "videoPlayLen": first("videoplaylen", "videoplayduration", "duration"),
        }
        return {
            "id": first("objectid", "objectnonceid", "id"),
            "objectNonceId": first("objectnonceid"),
            "contact": {
                "username": first("username"),
                "nickname": first("nickname"),
                "headUrl": first("avatar", "headurl", "headimgurl"),
            },
            "objectDesc": {
                "description": first("desc", "description", "title") or "视频号视频",
                "media": [media],
            },
        }

    def _pick_object(self, data: Any) -> dict:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return self._pick_object(item)
            raise WxChannelsParseError("JSON 数组里没有可解析对象")
        if not isinstance(data, dict):
            raise WxChannelsParseError("视频号数据不是 JSON 对象")

        for key in ("object", "feed", "finderFeed", "data", "detail", "item"):
            nested = data.get(key)
            if isinstance(nested, dict) and (
                "objectDesc" in nested or "media" in nested or "url" in nested
            ):
                return self._pick_object(nested)
        return data

    def _build_note(self, data: dict, original_text: str) -> XhsNote:
        object_desc = _pick_dict(data, "objectDesc", "object_desc", "desc") or {}
        contact = _pick_dict(data, "contact", "user", "author") or {}
        media_items = list(self._iter_media(data, object_desc))
        source_url = _pick(data, "source_url", "sourceUrl")
        if not source_url and not original_text.lstrip("\ufeff").strip().startswith(("{", "[", "<")):
            source_url = extract_url(original_text)
        source_url = source_url or ""

        first_media = media_items[0] if media_items else {}
        title = (
            _pick(data, "title", "description", "desc")
            or _pick(object_desc, "description", "title", "desc")
            or "视频号视频"
        )
        note_id = (
            _pick(data, "id", "objectId", "object_id", "objectNonceId", "object_nonce_id")
            or _pick(first_media, "id", "mediaId", "media_id")
            or source_url
            or title
        )
        note = XhsNote(
            note_id=f"wx_channels:{_compact_id(str(note_id))}",
            type="video",
            platform="wx_channels",
            title=str(title).strip(),
            description=str(_pick(object_desc, "description", "desc") or title).strip(),
            author=str(_pick(contact, "nickname", "nickName", "name", "username") or ""),
            author_id=str(_pick(contact, "username", "userName", "id") or ""),
            avatar=str(_pick(contact, "headUrl", "head_url", "avatar", "avatarUrl") or ""),
            cover=str(_pick(first_media, "coverUrl", "cover_url", "thumbUrl", "thumb_url") or ""),
            publish_time=_safe_int(_pick(data, "createtime", "createTime", "publish_time")),
            source_url=str(source_url),
        )

        for media in media_items:
            note.videos.extend(self._streams_from_media(media, note.title))

        if not note.cover:
            for video in note.videos:
                if video.backup_urls:
                    note.cover = ""
                    break
        note.duration = next((v.duration for v in note.videos if v.duration), None)
        note.videos.sort(
            key=lambda v: (
                -(v.height or 0),
                -(v.width or 0),
                -(v.bitrate or 0),
                -(v.size or 0),
            )
        )
        return note

    def _iter_media(self, data: dict, object_desc: dict) -> Iterable[dict]:
        candidates = [
            _pick(object_desc, "media", "mediaList", "media_list"),
            _pick(data, "media", "mediaList", "media_list", "videos", "video"),
        ]
        for value in candidates:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
            elif isinstance(value, dict):
                yield value
        if _pick(data, "url", "fullVideoUrl", "full_video_url"):
            yield data

    def _streams_from_media(self, media: dict, title: str) -> List[VideoStream]:
        base_items = [media]
        specs = _pick(media, "spec", "specs", "mediaSpec", "media_spec")
        if isinstance(specs, list):
            base_items.extend(item for item in specs if isinstance(item, dict))

        streams = []
        seen = set()
        for item in base_items:
            url = (
                _pick(item, "fullVideoUrl", "full_video_url", "masterUrl", "master_url", "url")
                or _pick(media, "fullVideoUrl", "full_video_url", "masterUrl", "master_url", "url")
            )
            if not url:
                continue
            token = _pick(item, "urlToken", "url_token") or _pick(media, "urlToken", "url_token")
            full_url = _join_url_token(str(url), str(token or ""))
            if full_url in seen:
                continue
            seen.add(full_url)

            width = _safe_int(_pick(item, "width", "videoWidth") or _pick(media, "width", "videoWidth"))
            height = _safe_int(_pick(item, "height", "videoHeight") or _pick(media, "height", "videoHeight"))
            duration = _duration_seconds(
                _pick(item, "duration", "durationMs", "videoPlayLen", "videoPlayDuration")
                or _pick(media, "duration", "durationMs", "videoPlayLen", "videoPlayDuration")
            )
            decode_key = _pick(item, "decodeKey", "decode_key") or _pick(media, "decodeKey", "decode_key")
            fmt = str(_pick(item, "fileFormat", "format") or _pick(media, "fileFormat", "format") or "mp4").lower()
            if fmt in ("1", "h264"):
                codec = "h264"
            elif fmt in ("2", "h265", "hevc"):
                codec = "h265"
            else:
                codec = "h264"

            proxy_url = None
            if decode_key:
                proxy_url = (
                    f"/api/wx-download?url={quote(full_url, safe='')}"
                    f"&decode_key={quote(str(decode_key), safe='')}"
                    f"&filename={quote(_safe_media_name(title), safe='')}"
                )

            streams.append(VideoStream(
                quality=_resolve_quality(width, height),
                codec=codec,
                format="mp4",
                width=width,
                height=height,
                bitrate=_safe_int(_pick(item, "bitrate", "videoBitrate")),
                quality_type=str(_pick(item, "qualityType", "quality", "profile") or "") or None,
                size=_safe_int(_pick(item, "fileSize", "file_size", "size") or _pick(media, "fileSize", "file_size", "size")),
                duration=duration,
                master_url=full_url,
                decode_key=str(decode_key) if decode_key else None,
                enc_limit=WX_ENC_LIMIT if decode_key else None,
                proxy_url=proxy_url,
            ))
        return streams


class Isaac64:
    def __init__(self, key: int):
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.cnt = 255
        self.seed[0] = key & _M64
        self._init()

    def _init(self):
        a = b = c = d = e = f = g = h = _GOLDEN64
        for _ in range(4):
            a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)
        for i in range(0, 256, 8):
            a = (a + self.seed[i]) & _M64
            b = (b + self.seed[i + 1]) & _M64
            c = (c + self.seed[i + 2]) & _M64
            d = (d + self.seed[i + 3]) & _M64
            e = (e + self.seed[i + 4]) & _M64
            f = (f + self.seed[i + 5]) & _M64
            g = (g + self.seed[i + 6]) & _M64
            h = (h + self.seed[i + 7]) & _M64
            a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]
        for i in range(0, 256, 8):
            a = (a + self.mm[i]) & _M64
            b = (b + self.mm[i + 1]) & _M64
            c = (c + self.mm[i + 2]) & _M64
            d = (d + self.mm[i + 3]) & _M64
            e = (e + self.mm[i + 4]) & _M64
            f = (f + self.mm[i + 5]) & _M64
            g = (g + self.mm[i + 6]) & _M64
            h = (h + self.mm[i + 7]) & _M64
            a, b, c, d, e, f, g, h = _mix64(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]
        self._generate()

    def _generate(self):
        self.cc = (self.cc + 1) & _M64
        self.bb = (self.bb + self.cc) & _M64
        for i in range(256):
            x = self.mm[i]
            if i % 4 == 0:
                self.aa = ((self.aa ^ ((self.aa << 21) & _M64)) ^ _M64) & _M64
            elif i % 4 == 1:
                self.aa = (self.aa ^ (self.aa >> 5)) & _M64
            elif i % 4 == 2:
                self.aa = (self.aa ^ ((self.aa << 12) & _M64)) & _M64
            else:
                self.aa = (self.aa ^ (self.aa >> 33)) & _M64
            self.aa = (self.mm[(i + 128) % 256] + self.aa) & _M64
            y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & _M64
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & _M64
            self.seed[i] = self.bb

    def next(self) -> int:
        value = self.seed[self.cnt]
        if self.cnt == 0:
            self._generate()
            self.cnt = 255
        else:
            self.cnt -= 1
        return value


def decrypt_wx_chunk(data: bytes, decode_key: str, offset: int = 0, enc_limit: int = WX_ENC_LIMIT) -> bytes:
    key = int(decode_key)
    if offset >= enc_limit or not data:
        return data
    out = bytearray(data)
    n = min(len(out), enc_limit - offset)
    rng = Isaac64(key)
    for _ in range(offset // 8):
        rng.next()
    key_bytes = struct.pack(">Q", rng.next())
    key_pos = offset % 8
    for i in range(n):
        if key_pos == 8:
            key_bytes = struct.pack(">Q", rng.next())
            key_pos = 0
        out[i] ^= key_bytes[key_pos]
        key_pos += 1
    return bytes(out)


def _mix64(a, b, c, d, e, f, g, h):
    a = (a - e) & _M64; f ^= (h >> 9) & _M64; h = (h + a) & _M64
    b = (b - f) & _M64; g ^= (a << 9) & _M64; a = (a + b) & _M64
    c = (c - g) & _M64; h ^= (b >> 23) & _M64; b = (b + c) & _M64
    d = (d - h) & _M64; a ^= (c << 15) & _M64; c = (c + d) & _M64
    e = (e - a) & _M64; b ^= (d >> 14) & _M64; d = (d + e) & _M64
    f = (f - b) & _M64; c ^= (e << 20) & _M64; e = (e + f) & _M64
    g = (g - c) & _M64; d ^= (f >> 17) & _M64; f = (f + g) & _M64
    h = (h - d) & _M64; e ^= (g << 14) & _M64; g = (g + h) & _M64
    return a, b, c, d, e, f, g, h


def _pick(data: dict, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _pick_dict(data: dict, *keys) -> Optional[dict]:
    value = _pick(data, *keys)
    return value if isinstance(value, dict) else None


def _safe_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _duration_seconds(value) -> Optional[float]:
    number = _safe_int(value)
    if number is None:
        return None
    return number / 1000 if number > 1000 else float(number)


def _join_url_token(url: str, token: str) -> str:
    if not token or token in url:
        return url
    if token.startswith("http"):
        return token
    if token.startswith(("?", "&")):
        return url + token
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{token}"


def _compact_id(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "", value)[:64]
    if clean:
        return clean
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _safe_media_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title or "wx_channels").strip(" .")
    return (name[:60] or "wx_channels") + ".mp4"


def _is_media_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        host.endswith("wxapp.tc.qq.com")
        or "finder.video.qq.com" in host
        or "wxvideo" in host
        or path.endswith((".mp4", ".m3u8"))
    )


def _first_qs(params: dict, *names: str) -> str:
    lowered = {k.lower(): v for k, v in params.items()}
    for name in names:
        values = lowered.get(name.lower())
        if values:
            return values[0]
    return ""


def _unescape_html(value: str) -> str:
    return html_lib.unescape(re.sub(r"\s+", " ", value or "")).strip()
