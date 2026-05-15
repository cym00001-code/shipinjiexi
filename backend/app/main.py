"""FastAPI 入口 + 路由."""
from __future__ import annotations

import io
import re
import zipfile
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import history
from .config import DEFAULT_UA, FRONTEND_DIR, REQUEST_TIMEOUT
from .parser import XhsParseError, XhsParser


@asynccontextmanager
async def lifespan(app: FastAPI):
    history.init_db()
    yield


app = FastAPI(title="小红书视频解析", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ParseRequest(BaseModel):
    url: str
    cookie: Optional[str] = None
    save_history: bool = True


@app.post("/api/parse")
async def api_parse(req: ParseRequest):
    parser = XhsParser(cookie=(req.cookie or "").strip())
    try:
        note = await parser.parse(req.url)
    except XhsParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"请求小红书失败: {exc}")

    payload = note.to_dict()
    if req.save_history:
        try:
            payload["history_id"] = history.record(payload)
        except Exception:
            pass
    return payload


@app.get("/api/history")
async def api_history(limit: int = Query(50, ge=1, le=200)):
    return {"items": history.list_recent(limit=limit)}


@app.get("/api/history/{item_id}")
async def api_history_detail(item_id: int):
    data = history.get_full(item_id)
    if not data:
        raise HTTPException(404, "记录不存在")
    return data


@app.delete("/api/history/{item_id}")
async def api_history_delete(item_id: int):
    if not history.delete(item_id):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@app.delete("/api/history")
async def api_history_clear():
    return {"deleted": history.clear_all()}


_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_filename(name: str, fallback: str = "xhs") -> str:
    name = (name or "").strip()
    name = _FILENAME_BAD.sub("_", name)
    name = name.strip(" .")
    return name[:80] or fallback


@app.get("/api/download")
async def api_download(
    url: str = Query(..., description="远端 CDN 直链"),
    filename: Optional[str] = Query(None, description="保存文件名 (含扩展名)"),
):
    """流式代理下载. 解决 CDN Referer 防盗链 + 浏览器直接保存."""
    if not url.startswith("http"):
        raise HTTPException(400, "url 不合法")

    name = _safe_filename(filename or "xiaohongshu.mp4")
    content_type = "application/octet-stream"
    if name.lower().endswith(".mp4"):
        content_type = "video/mp4"
    elif name.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif name.lower().endswith(".png"):
        content_type = "image/png"
    elif name.lower().endswith(".webp"):
        content_type = "image/webp"

    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.xiaohongshu.com/",
    }
    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True)

    try:
        upstream = await client.send(
            client.build_request("GET", url, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"下载失败: {exc}")

    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(upstream.status_code, body[:200].decode("utf-8", "ignore"))

    async def streamer():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    quoted = quote(name)
    resp_headers = {
        "Content-Disposition": f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
    }
    if "Content-Length" in upstream.headers:
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]
    return StreamingResponse(streamer(), media_type=content_type, headers=resp_headers)


class ZipRequest(BaseModel):
    urls: List[str]
    filename: Optional[str] = None


@app.post("/api/zip")
async def api_zip(req: ZipRequest):
    """图集打包成 ZIP 一次性下载."""
    if not req.urls:
        raise HTTPException(400, "urls 为空")

    headers = {"User-Agent": DEFAULT_UA, "Referer": "https://www.xiaohongshu.com/"}
    buf = io.BytesIO()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, url in enumerate(req.urls, start=1):
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                except httpx.HTTPError:
                    continue
                ext = "jpg"
                ct = r.headers.get("content-type", "").lower()
                if "png" in ct: ext = "png"
                elif "webp" in ct: ext = "webp"
                elif "mp4" in ct: ext = "mp4"
                zf.writestr(f"{idx:02d}.{ext}", r.content)

    buf.seek(0)
    name = _safe_filename(req.filename or "xiaohongshu_images") + ".zip"
    quoted = quote(name)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
        },
    )


@app.get("/api/health")
async def health():
    return {"ok": True}


# 静态文件 (前端 SPA): /static 用来挂资源, / 直接给 index.html
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def root():
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index, media_type="text/html; charset=utf-8")
        raise HTTPException(404, "前端未构建")
