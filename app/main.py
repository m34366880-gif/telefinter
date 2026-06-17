from __future__ import annotations

import os
import itertools
import requests
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, init_db, Gift, TelegramMedia, TelegramState
from .deps import admin_guard
from .telegram_client import TelegramClient


app = FastAPI(title="NFT Gifts Store")

# Static files and templates
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# Dependency: DB session per request

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    gifts: List[Gift] = list(db.execute(select(Gift).order_by(Gift.created_at.desc())).scalars())
    medias: List[TelegramMedia] = list(db.execute(select(TelegramMedia).order_by(TelegramMedia.id.desc()).limit(18)).scalars())
    return templates.TemplateResponse("index.html", {"request": request, "gifts": gifts, "medias": medias})


@app.post("/send", response_class=HTMLResponse)
def send_gift(
    request: Request,
    gift_id: Optional[int] = Form(default=None),
    recipient: str = Form(...),
    message: Optional[str] = Form(default=None),
    direct_animation_url: Optional[str] = Form(default=None),
    direct_file_id: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
):
    client = TelegramClient()

    animation: Optional[str] = None
    caption = message or ""

    if direct_file_id:
        # Prefer sending by Telegram file_id for reliability
        animation = direct_file_id
    elif direct_animation_url:
        animation = direct_animation_url
    elif gift_id is not None:
        gift = db.get(Gift, gift_id)
        if not gift:
            raise HTTPException(status_code=404, detail="Gift not found")
        # Prefer telegram file id if present (faster and more reliable)
        animation = gift.telegram_file_id or gift.gif_url
    else:
        raise HTTPException(status_code=400, detail="No gift or animation provided")

    if not animation:
        raise HTTPException(status_code=400, detail="Animation is missing for the selected gift")

    try:
        client.send_animation(chat_id_or_username=recipient, animation=animation, caption=caption)
    except Exception as e:
        return templates.TemplateResponse(
            "send_result.html",
            {"request": request, "ok": False, "error": str(e)},
            status_code=400,
        )

    return templates.TemplateResponse("send_result.html", {"request": request, "ok": True})


# Secure proxy to serve Telegram files without exposing bot token
@app.get("/media/telegram/{file_id}")
def media_proxy(file_id: str, db: Session = Depends(get_db)):
    client = TelegramClient()

    # Try to guess mime type from stored media
    mime: Optional[str] = None
    media: Optional[TelegramMedia] = db.execute(select(TelegramMedia).where(TelegramMedia.file_id == file_id)).scalar_one_or_none()
    if media and media.mime_type:
        mime = media.mime_type

    # Resolve path for the file_id
    fj = client.get_file(file_id)
    if not fj.get("ok"):
        raise HTTPException(status_code=404, detail="Telegram file not found")
    file_path: Optional[str] = fj["result"].get("file_path")
    if not file_path:
        raise HTTPException(status_code=404, detail="Telegram file path missing")

    url = client.build_file_url(file_path)
    try:
        tg_resp = requests.get(url, stream=True, timeout=30)
        tg_resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram fetch error: {e}")

    def iter_stream():
        for chunk in tg_resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    headers = {"Cache-Control": "public, max-age=86400"}
    content_type = mime or tg_resp.headers.get("Content-Type") or "application/octet-stream"
    return StreamingResponse(iter_stream(), headers=headers, media_type=content_type)


# Admin
@app.get("/admin")
def admin_root():
    return RedirectResponse(url="/admin/gifts", status_code=302)


@app.get("/admin/gifts", response_class=HTMLResponse)
def admin_gifts(request: Request, db: Session = Depends(get_db), _=Depends(admin_guard)):
    gifts: List[Gift] = list(db.execute(select(Gift).order_by(Gift.created_at.desc())).scalars())
    return templates.TemplateResponse("admin/gifts_list.html", {"request": request, "gifts": gifts})


@app.get("/admin/gifts/new", response_class=HTMLResponse)
def admin_new_gift(request: Request, _=Depends(admin_guard)):
    return templates.TemplateResponse("admin/gift_form.html", {"request": request, "gift": None})


@app.post("/admin/gifts", response_class=HTMLResponse)
def admin_create_gift(
    request: Request,
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    gif_url: Optional[str] = Form(default=None),
    telegram_file_id: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    _=Depends(admin_guard),
):
    gift = Gift(title=title.strip(), description=(description or "").strip() or None, gif_url=(gif_url or None), telegram_file_id=(telegram_file_id or None))
    db.add(gift)
    db.commit()
    return RedirectResponse(url="/admin/gifts", status_code=302)


@app.get("/admin/gifts/{gift_id}/edit", response_class=HTMLResponse)
def admin_edit_gift(gift_id: int, request: Request, db: Session = Depends(get_db), _=Depends(admin_guard)):
    gift = db.get(Gift, gift_id)
    if not gift:
        raise HTTPException(status_code=404, detail="Gift not found")
    return templates.TemplateResponse("admin/gift_form.html", {"request": request, "gift": gift})


@app.post("/admin/gifts/{gift_id}/update", response_class=HTMLResponse)
def admin_update_gift(
    gift_id: int,
    request: Request,
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    gif_url: Optional[str] = Form(default=None),
    telegram_file_id: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    _=Depends(admin_guard),
):
    gift = db.get(Gift, gift_id)
    if not gift:
        raise HTTPException(status_code=404, detail="Gift not found")
    gift.title = title.strip()
    gift.description = (description or "").strip() or None
    gift.gif_url = gif_url or None
    gift.telegram_file_id = telegram_file_id or None
    db.add(gift)
    db.commit()
    return RedirectResponse(url="/admin/gifts", status_code=302)


@app.post("/admin/gifts/{gift_id}/delete")
def admin_delete_gift(gift_id: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    stmt = delete(Gift).where(Gift.id == gift_id)
    db.execute(stmt)
    db.commit()
    return RedirectResponse(url="/admin/gifts", status_code=302)


# Telegram import pages
@app.get("/admin/telegram", response_class=HTMLResponse)
def admin_telegram(request: Request, db: Session = Depends(get_db), _=Depends(admin_guard)):
    medias: List[TelegramMedia] = list(db.execute(select(TelegramMedia).order_by(TelegramMedia.id.desc()).limit(50)).scalars())
    return templates.TemplateResponse("admin/telegram.html", {"request": request, "medias": medias})


@app.post("/admin/telegram/fetch")
def admin_telegram_fetch(db: Session = Depends(get_db), _=Depends(admin_guard)):
    client = TelegramClient()

    # Get last update id
    state: Optional[TelegramState] = db.get(TelegramState, 1)
    offset: Optional[int] = (state.last_update_id + 1) if state and state.last_update_id is not None else None

    j = client.get_updates(offset=offset, timeout=0)
    if not j.get("ok"):
        raise HTTPException(status_code=502, detail=str(j))

    result = j.get("result", [])
    last_update_id: Optional[int] = None
    saved = 0

    for upd in result:
        last_update_id = upd.get("update_id", last_update_id)
        msg = upd.get("message") or upd.get("channel_post") or {}
        if not msg:
            continue
        caption = msg.get("caption")

        # animations
        animation = msg.get("animation")
        documents = msg.get("document")

        candidates = []
        if animation:
            candidates.append(animation)
        if isinstance(documents, dict):
            candidates.append(documents)

        for item in candidates:
            mime = item.get("mime_type")
            if not mime:
                # Accept likely animations by extension too
                pass
            file_id = item.get("file_id")
            file_unique_id = item.get("file_unique_id")
            width = item.get("width")
            height = item.get("height")
            size = item.get("file_size")

            if not file_id:
                continue

            # Resolve file_path
            file_path = None
            try:
                fj = client.get_file(file_id)
                if fj.get("ok"):
                    file_path = fj["result"].get("file_path")
            except Exception:
                file_path = None

            tm = TelegramMedia(
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_path=file_path,
                mime_type=mime,
                width=width,
                height=height,
                size=size,
                caption=caption,
            )
            try:
                db.add(tm)
                db.commit()
                saved += 1
            except Exception:
                db.rollback()
                # likely unique constraint
                pass

    # Update offset state
    if last_update_id is not None:
        if not state:
            state = TelegramState(id=1, last_update_id=last_update_id)
        else:
            state.last_update_id = last_update_id
        db.add(state)
        db.commit()

    return RedirectResponse(url="/admin/telegram", status_code=302)


@app.post("/admin/telegram/import")
def admin_telegram_import(
    file_id: str = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    _=Depends(admin_guard),
):
    # Find media and build URL (if available)
    media: Optional[TelegramMedia] = db.execute(select(TelegramMedia).where(TelegramMedia.file_id == file_id)).scalar_one_or_none()
    gif_url: Optional[str] = None
    if media and media.file_path:
        client = TelegramClient()
        gif_url = client.build_file_url(media.file_path)

    gift = Gift(title=title.strip(), description=(description or "").strip() or None, gif_url=gif_url, telegram_file_id=file_id)
    db.add(gift)
    db.commit()
    return RedirectResponse(url="/admin/gifts", status_code=302)


# JSON APIs
@app.get("/api/gifts")
def api_gifts(db: Session = Depends(get_db)):
    gifts: List[Gift] = list(db.execute(select(Gift).order_by(Gift.created_at.desc())).scalars())
    return [
        {
            "id": g.id,
            "title": g.title,
            "description": g.description,
            "gif_url": g.gif_url,
            "telegram_file_id": g.telegram_file_id,
        }
        for g in gifts
    ]


@app.get("/api/telegram/animations")
def api_telegram_animations(db: Session = Depends(get_db)):
    medias: List[TelegramMedia] = list(db.execute(select(TelegramMedia).order_by(TelegramMedia.id.desc()).limit(100)).scalars())
    client = TelegramClient()
    def to_url(m: TelegramMedia) -> Optional[str]:
        if m.file_path:
            return client.build_file_url(m.file_path)
        return None
    return [
        {
            "file_id": m.file_id,
            "file_unique_id": m.file_unique_id,
            "file_path": m.file_path,
            "url": to_url(m),
            "mime_type": m.mime_type,
            "caption": m.caption,
            "width": m.width,
            "height": m.height,
            "size": m.size,
        }
        for m in medias
    ]
