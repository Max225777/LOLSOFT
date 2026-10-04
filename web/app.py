"""LOLSOFT Web — FastAPI backend"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "lolsoft_config.json"
DB_PATH     = BASE_DIR / "lolsoft_stats.db"
STATIC_DIR  = BASE_DIR / "static"

MY_PROFILE_ID = "9542364"
API_BASE      = "https://prod-api.lzt.market"

# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"token": "", "interval": 60, "autorefresh": False,
                "autobump": False, "markets": []}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

# ─── API helpers ──────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token.strip()}",
            "Accept": "application/json", "User-Agent": "LOLSOFT/2.0"}

def url_to_api(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return API_BASE + p.path + ("?" + p.query if p.query else "")

def fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
    except Exception:
        return "—"

def fetch_listings(market_url: str, token: str, count: int) -> list[dict]:
    resp = requests.get(url_to_api(market_url), headers=_headers(token), timeout=20)
    if resp.status_code == 401:
        raise RuntimeError("Невірний токен (401)")
    resp.raise_for_status()
    parsed = []
    for it in resp.json().get("items", [])[:count]:
        item_id     = str(it.get("item_id", ""))
        title       = it.get("title") or it.get("title_en") or f"#{item_id}"
        price       = it.get("price", "?")
        currency    = it.get("price_currency", "") or it.get("currency", "")
        seller      = it.get("seller") or {}
        seller_id   = str(seller.get("user_id", ""))
        seller_name = seller.get("username", "?")
        state       = it.get("item_state", "")
        bumped_at   = (it.get("bumped_at") or it.get("up_timestamp")
                       or it.get("refreshed_at") or it.get("updated_at"))
        parsed.append({
            "id": item_id, "title": title,
            "link": f"https://lzt.market/{item_id}/",
            "seller": seller_name, "seller_id": seller_id,
            "price": f"{price} {currency}".strip(),
            "is_mine": seller_id == MY_PROFILE_ID,
            "is_pinned": bool(it.get("is_sticky") or it.get("sticky")),
            "is_closed": state in ("sold", "closed", "deleted"),
            "bumped_at": bumped_at,
        })
    return parsed

_tag_id_map: dict[str, int] = {}

def fetch_my_tags(token: str) -> list[str]:
    global _tag_id_map
    resp = requests.get(f"{API_BASE}/me", headers=_headers(token), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    tags = []
    user = data.get("user") or data
    for t in (user.get("tags") or []):
        if isinstance(t, dict):
            title  = (t.get("title") or t.get("name") or "").strip()
            tag_id = t.get("tag_id") or t.get("id")
            if title:
                tags.append(title)
                if tag_id is not None:
                    _tag_id_map[title] = int(tag_id)
        elif isinstance(t, str) and t.strip():
            tags.append(t.strip())
    return sorted(set(tags))

def fetch_all_my_items(token: str) -> list[dict]:
    result, seen, page = [], set(), 1
    while page <= 100:
        resp = requests.get(f"{API_BASE}/user/items?page={page}",
                            headers=_headers(token), timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        added = 0
        for it in items:
            iid = it.get("item_id")
            if iid not in seen:
                seen.add(iid); result.append(it); added += 1
        if added == 0:
            break
        page += 1
    return result

_tag_items_cache: dict[str, list[dict]] = {}

def items_for_tag(tag: str) -> list[dict]:
    return list(_tag_items_cache.get(tag.strip(), []))

def bump_item(token: str, item_id: str) -> tuple[bool, str]:
    try:
        resp = requests.post(f"{API_BASE}/{item_id}/bump",
                             headers=_headers(token), timeout=15)
        if resp.status_code in (200, 201):
            return True, ""
        try:
            body = resp.json()
            msg  = body.get("message") or body.get("error") or ""
        except Exception:
            msg = resp.text[:100]
        code = resp.status_code
        ml   = msg.lower()
        raw  = f"[{code}] {msg[:80]}"
        if "лимит" in ml or "limit" in ml or ("bump" in ml and "0" in ml):
            return False, f"ліміт: {raw}"
        if code == 429 or any(x in ml for x in ("cooldown","flood","wait","подожд","зачекайте")):
            return False, f"кулдаун: {raw}"
        if code == 404 or any(x in ml for x in ("not found","deleted","sold")):
            return False, f"продано/видалено: {raw}"
        if code == 403:
            return False, f"ліміт: {raw}"
        return False, raw
    except Exception as e:
        return False, str(e)[:80]

# ─── SQLite ───────────────────────────────────────────────────────────────────

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS bumps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_name TEXT, tag TEXT,
        item_id TEXT, item_title TEXT,
        success INTEGER DEFAULT 0, reason TEXT,
        logged_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logged ON bumps(logged_at)")
    conn.commit()
    return conn

def record_bump(market_name: str, tag: str, item_id: str,
                item_title: str, success: bool, reason: str):
    conn = db_conn()
    conn.execute(
        "INSERT INTO bumps (market_name,tag,item_id,item_title,success,reason,logged_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (market_name, tag, item_id, item_title, int(success), reason,
         datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit(); conn.close()

def today_iso() -> str:
    return datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).isoformat()

def stat_summary() -> dict:
    conn = db_conn()
    t = today_iso()
    total_all    = conn.execute("SELECT COUNT(*) FROM bumps").fetchone()[0]
    total_today  = conn.execute("SELECT COUNT(*) FROM bumps WHERE logged_at>=?", (t,)).fetchone()[0]
    ok_today     = conn.execute("SELECT COUNT(*) FROM bumps WHERE success=1 AND logged_at>=?", (t,)).fetchone()[0]
    fail_today   = conn.execute("SELECT COUNT(*) FROM bumps WHERE success=0 AND logged_at>=?", (t,)).fetchone()[0]
    # last 48 hourly buckets for chart
    chart = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:00', logged_at) AS h,"
        " SUM(success), SUM(1-success) FROM bumps"
        " WHERE logged_at >= date('now','localtime')"
        " GROUP BY h ORDER BY h"
    ).fetchall()
    conn.close()
    return dict(total_all=total_all, total_today=total_today,
                ok_today=ok_today, fail_today=fail_today,
                chart=[{"hour": r[0], "ok": r[1] or 0, "fail": r[2] or 0} for r in chart])


def default_market(name="Новий ринок") -> dict:
    return {
        "name": name, "url": "", "tags": [],
        "count": 10, "top_n": 1,
        "refresh_interval": 60, "bump_interval": 60,
        "enabled": True, "work_from": "", "work_to": "",
    }

# ─── WebSocket log broadcaster ────────────────────────────────────────────────

class LogBroadcaster:
    def __init__(self):
        self._clients: list[WebSocket] = []
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log: list[dict] = []

    def set_loop(self, loop):
        self._loop = loop

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._clients.append(ws)
        # replay last 50 messages
        for msg in self._log[-50:]:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            try:
                self._clients.remove(ws)
            except ValueError:
                pass

    def emit(self, text: str, level: str = "info"):
        msg = {"text": text, "level": level,
               "time": datetime.now().strftime("%H:%M:%S")}
        self._log = (self._log + [msg])[-200:]
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    async def _broadcast(self, msg: dict):
        async with self._lock:
            dead = []
            for ws in self._clients:
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                try:
                    self._clients.remove(ws)
                except ValueError:
                    pass

broadcaster = LogBroadcaster()

# ─── Per-market bump engine ───────────────────────────────────────────────────

class MarketEngine:
    """Runs refresh + bump for one market in background threads via APScheduler."""

    def __init__(self, cfg: dict, token_fn):
        self.cfg       = cfg
        self.token_fn  = token_fn  # callable → str
        self._bump_idx: dict[str, int] = {}

    def get_token(self) -> str:
        return self.token_fn()

    def _is_active_hours(self) -> bool:
        work_from = self.cfg.get("work_from", "")
        work_to   = self.cfg.get("work_to", "")
        if not work_from or not work_to:
            return True
        try:
            now = datetime.now().strftime("%H:%M")
            return work_from <= now <= work_to
        except Exception:
            return True

    def refresh_tick(self):
        if not self.cfg.get("enabled", True):
            return
        token = self.get_token()
        if not token:
            return
        name  = self.cfg.get("name", "?")
        url   = self.cfg.get("url", "")
        count = self.cfg.get("count", 10)
        try:
            listings = fetch_listings(url, token, count)
            broadcaster.emit(f"🔄 [{name}] оновлено {len(listings)} лотів", "info")
        except Exception as e:
            broadcaster.emit(f"⚠ [{name}] refresh: {e}", "warn")

    def bump_tick(self):
        if not self.cfg.get("enabled", True):
            return
        if not self._is_active_hours():
            name = self.cfg.get("name","?")
            broadcaster.emit(f"⏰ [{name}] поза робочим часом — пропуск", "info")
            return

        token  = self.get_token()
        if not token:
            return
        name   = self.cfg.get("name", "?")
        url    = self.cfg.get("url", "")
        top_n  = self.cfg.get("top_n", 1)
        tags   = self.cfg.get("tags", [])

        # smart bump: check if already in top-N
        if url and top_n > 0:
            try:
                listings = fetch_listings(url, token, top_n)
                top_ids  = {lot["id"] for lot in listings[:top_n]}
                my_cache_ids = set()
                for t in tags:
                    for it in items_for_tag(t.get("tag", "")):
                        my_cache_ids.add(str(it.get("item_id", "")))
                if top_ids & my_cache_ids:
                    broadcaster.emit(f"✅ [{name}] вже в топ-{top_n}, пропуск", "ok")
                    return
            except Exception:
                pass

        for tag_cfg in tags:
            tag   = tag_cfg.get("tag", "").strip()
            limit = tag_cfg.get("count", 1)
            if not tag:
                continue
            my_items = items_for_tag(tag)
            if not my_items:
                broadcaster.emit(f"⚠ [{name}][{tag}] лоти не знайдено в кеші", "warn")
                continue

            bumped = 0
            total  = len(my_items)
            idx    = self._bump_idx.get(tag, 0) % total
            tried  = 0
            limit_streak = 0

            while bumped < limit and tried < max(total, 10):
                it    = my_items[idx % total]
                iid   = str(it.get("item_id", ""))
                title = it.get("title") or it.get("title_en") or f"#{iid}"
                ok, reason = bump_item(token, iid)
                tried += 1

                if ok:
                    bumped += 1
                    self._bump_idx[tag] = (idx + 1) % total
                    broadcaster.emit(f"✅ [{name}][{tag}] {title}", "ok")
                    record_bump(name, tag, iid, title, True, "")
                    idx = (idx + 1) % total
                elif reason.startswith("продано/видалено"):
                    broadcaster.emit(f"🗑 [{name}][{tag}] {title} — видалено", "warn")
                    record_bump(name, tag, iid, title, False, reason)
                    _tag_items_cache[tag] = [
                        x for x in _tag_items_cache.get(tag, [])
                        if str(x.get("item_id","")) != iid
                    ]
                    total = len(_tag_items_cache.get(tag, []))
                    if total == 0:
                        break
                    idx = idx % total
                elif reason.startswith("ліміт"):
                    limit_streak += 1
                    record_bump(name, tag, iid, title, False, reason)
                    if limit_streak >= min(total, 10):
                        broadcaster.emit(f"🚫 [{name}][{tag}] ліміт на всіх лотах", "error")
                        break
                    idx = (idx + 1) % total
                    self._bump_idx[tag] = idx
                elif reason.startswith("кулдаун"):
                    time.sleep(3)
                    idx = (idx + 1) % total
                    self._bump_idx[tag] = idx
                else:
                    broadcaster.emit(f"⛔ [{name}][{tag}] {reason}", "error")
                    record_bump(name, tag, iid, title, False, reason)
                    idx = (idx + 1) % total
                    self._bump_idx[tag] = idx
                    break

# ─── Scheduler manager ────────────────────────────────────────────────────────

class SchedulerManager:
    def __init__(self):
        self._sched   = BackgroundScheduler(timezone="UTC")
        self._engines: dict[str, MarketEngine] = {}
        self._cfg     = load_config()
        self._sched.start()

    def _token(self) -> str:
        return self._cfg.get("token", "")

    def reload(self, cfg: dict):
        self._cfg = cfg
        self._sched.remove_all_jobs()
        self._engines.clear()

        if not cfg.get("autobump", False) and not cfg.get("autorefresh", False):
            return

        for mkt in cfg.get("markets", []):
            if not mkt.get("enabled", True):
                continue
            name    = mkt.get("name", "?")
            engine  = MarketEngine(mkt, self._token)
            self._engines[name] = engine

            if cfg.get("autorefresh", False):
                ri = max(30, mkt.get("refresh_interval", 60))
                self._sched.add_job(engine.refresh_tick, "interval", seconds=ri,
                                    id=f"refresh_{name}", replace_existing=True)

            if cfg.get("autobump", False):
                bi = max(30, mkt.get("bump_interval", 60))
                self._sched.add_job(engine.bump_tick, "interval", seconds=bi,
                                    id=f"bump_{name}", replace_existing=True)

        # load items cache once on (re)start
        threading.Thread(target=self._warmup_cache, daemon=True).start()

    def _warmup_cache(self):
        global _tag_items_cache, _tag_id_map
        token = self._token()
        if not token:
            return
        try:
            broadcaster.emit("📦 Завантаження кешу лотів…", "info")
            fetch_my_tags(token)
            all_items = fetch_all_my_items(token)
            id_to_name = {v: k for k, v in _tag_id_map.items()}
            cache: dict[str, list[dict]] = {n: [] for n in _tag_id_map}
            for it in all_items:
                raw_tags = it.get("tags") or {}
                tag_objs = raw_tags.values() if isinstance(raw_tags, dict) else raw_tags
                for t in tag_objs:
                    if isinstance(t, dict):
                        tid = t.get("tag_id") or t.get("id")
                        if tid and tid in id_to_name:
                            cache[id_to_name[tid]].append(it)
                    elif isinstance(t, int) and t in id_to_name:
                        cache[id_to_name[t]].append(it)
            _tag_items_cache = cache
            total = sum(len(v) for v in cache.values())
            broadcaster.emit(f"📦 Кеш готовий: {len(all_items)} лотів, {total} по тегах", "ok")
        except Exception as e:
            broadcaster.emit(f"⚠ Кеш: {e}", "warn")

sched_mgr = SchedulerManager()

# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(title="LOLSOFT Web")

@app.on_event("startup")
async def startup():
    broadcaster.set_loop(asyncio.get_event_loop())
    cfg = load_config()
    sched_mgr.reload(cfg)

# ─── REST endpoints ───────────────────────────────────────────────────────────

class ConfigIn(BaseModel):
    data: dict

@app.get("/api/config")
def get_config():
    cfg = load_config()
    # mask token
    masked = {**cfg}
    if masked.get("token"):
        t = masked["token"]
        masked["token"] = t[:8] + "…" + t[-4:] if len(t) > 12 else "****"
        masked["_token_set"] = True
    else:
        masked["_token_set"] = False
    return masked

@app.post("/api/config")
def set_config(body: ConfigIn):
    cfg = body.data
    # if token looks masked, keep old one
    old = load_config()
    if "…" in cfg.get("token","") or cfg.get("token","") == "****":
        cfg["token"] = old.get("token", "")
    save_config(cfg)
    sched_mgr.reload(cfg)
    broadcaster.emit("⚙ Налаштування збережено", "info")
    return {"ok": True}

@app.get("/api/stats")
def get_stats():
    return stat_summary()

@app.get("/api/logs")
def get_logs():
    return broadcaster._log[-100:]

@app.get("/api/bump-log")
def get_bump_log():
    conn = db_conn()
    rows = conn.execute(
        "SELECT market_name, tag, item_id, item_title, success, reason, logged_at"
        " FROM bumps WHERE logged_at >= date('now','localtime')"
        " ORDER BY logged_at DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return [{"market_name":r[0],"tag":r[1],"item_id":r[2],"item_title":r[3],
             "success":bool(r[4]),"reason":r[5],"logged_at":r[6]} for r in rows]

@app.post("/api/refresh-cache")
def refresh_cache():
    threading.Thread(target=sched_mgr._warmup_cache, daemon=True).start()
    return {"ok": True}

@app.post("/api/bump-now/{market_name}")
def bump_now(market_name: str):
    engine = sched_mgr._engines.get(market_name)
    if not engine:
        raise HTTPException(404, "Market not found or inactive")
    threading.Thread(target=engine.bump_tick, daemon=True).start()
    return {"ok": True}

@app.get("/api/market-listings/{market_name}")
def market_listings(market_name: str):
    cfg   = load_config()
    token = cfg.get("token", "")
    if not token:
        raise HTTPException(400, "No token")
    mkt = next((m for m in cfg.get("markets", []) if m["name"] == market_name), None)
    if not mkt:
        raise HTTPException(404, "Market not found")
    try:
        listings = fetch_listings(mkt["url"], token, mkt.get("count", 10))
        return listings
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/download-zip")
def download_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in BASE_DIR.rglob("*"):
            if f.is_file() and f.name not in ("lolsoft_stats.db",) \
                    and "__pycache__" not in str(f) \
                    and ".pyc" not in f.name:
                zf.write(f, f.relative_to(BASE_DIR.parent))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=lolsoft_web.zip"}
    )

# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await broadcaster.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(ws)

# ─── Static files ─────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
