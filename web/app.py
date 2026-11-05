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
from zoneinfo import ZoneInfo

_KYIV_TZ = ZoneInfo("Europe/Kyiv")
from pathlib import Path
from typing import Any

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
STATIC_DIR  = BASE_DIR / "static"
# If /data is mounted (Render Persistent Disk), store mutable files there
_DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
CONFIG_PATH = _DATA_DIR / "lolsoft_config.json"
DB_PATH     = _DATA_DIR / "lolsoft_stats.db"

MY_PROFILE_ID = "9542364"
API_BASE      = "https://prod-api.lzt.market"

# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    # 1. try CONFIG_JSON env var (persists across Render redeploys)
    env_json = os.environ.get("CONFIG_JSON", "")
    env_cfg: dict = {}
    if env_json:
        try:
            env_cfg = json.loads(env_json)
        except Exception:
            pass

    # 2. try local file (written by save_config during session)
    file_cfg: dict = {}
    try:
        file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

    # prefer file if it has markets (user saved during this session),
    # otherwise fall back to env var, otherwise defaults
    if file_cfg.get("markets"):
        cfg = file_cfg
    elif env_cfg:
        cfg = env_cfg
    else:
        cfg = file_cfg or {"token": "", "autorefresh": False,
                           "autobump": False, "markets": []}

    if not cfg.get("token"):
        cfg["token"] = os.environ.get("LOLZ_TOKEN", "")
    return cfg

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

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
        return datetime.fromtimestamp(int(ts), tz=_KYIV_TZ).strftime("%d.%m %H:%M")
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
_cache_loaded_at: str = ""

def items_for_tag(tag: str) -> list[dict]:
    return list(_tag_items_cache.get(tag.strip(), []))

def stick_item(token: str, item_id: str) -> tuple[bool, str]:
    try:
        resp = requests.post(f"{API_BASE}/{item_id}/stick",
                             headers=_headers(token), timeout=15)
        if resp.status_code in (200, 201):
            return True, ""
        try:
            msg = resp.json().get("message") or resp.json().get("error") or ""
        except Exception:
            msg = resp.text[:80]
        return False, f"[{resp.status_code}] {msg[:80]}"
    except Exception as e:
        return False, str(e)[:80]

def unstick_item(token: str, item_id: str) -> tuple[bool, str]:
    try:
        resp = requests.delete(f"{API_BASE}/{item_id}/stick",
                               headers=_headers(token), timeout=15)
        if resp.status_code in (200, 204):
            return True, ""
        try:
            msg = resp.json().get("message") or resp.json().get("error") or ""
        except Exception:
            msg = resp.text[:80]
        return False, f"[{resp.status_code}] {msg[:80]}"
    except Exception as e:
        return False, str(e)[:80]

def _is_pinned(it: dict) -> bool:
    return bool(it.get("is_sticky") or it.get("sticky") or it.get("is_pinned"))

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
         datetime.now(_KYIV_TZ).isoformat(timespec="seconds"))
    )
    conn.commit(); conn.close()

def today_iso() -> str:
    return datetime.now(_KYIV_TZ).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()

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

    # ANSI colour codes for Render log viewer
    _ANSI = {
        "ok":    "\033[32m",   # green
        "error": "\033[31m",   # red
        "warn":  "\033[33m",   # yellow
        "info":  "\033[36m",   # cyan
    }
    _RESET = "\033[0m"

    def emit(self, text: str, level: str = "info"):
        msg = {"text": text, "level": level,
               "time": datetime.now(_KYIV_TZ).strftime("%H:%M:%S")}
        self._log = (self._log + [msg])[-200:]
        # Print to stdout for Render logs — skip noise (same filter as UI console)
        _noise = ("— ліміт, кулдаун", "не вдалось підняти", "⏰", "🔄")
        if not any(n in text for n in _noise):
            colour = self._ANSI.get(level, "")
            print(f"{colour}[{msg['time']}] {text}{self._RESET}", flush=True)
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

    ITEM_COOLDOWN_SEC = 3600  # 1 hour cooldown after failed bump

    def __init__(self, cfg: dict, token_fn):
        self.cfg          = cfg
        self.token_fn     = token_fn  # callable → str
        self._bump_idx:      dict[str, int]   = {}   # tag → next item index
        self._cooldowns:     dict[str, float] = {}   # item_id → expires_at
        self._cooldown_tags: dict[str, str]   = {}   # item_id → tag_name
        self._cycle_tag_idx: int = 0
        self._cycle_done:    int = 0
        self._unstick_blocked: set[str] = set()  # item_ids that returned 403 on unstick
        self._bump_lock = threading.Lock()       # prevent concurrent bump_tick runs

    def get_token(self) -> str:
        return self.token_fn()

    def _is_active_hours(self) -> bool:
        work_from = self.cfg.get("work_from", "")
        work_to   = self.cfg.get("work_to", "")
        if not work_from or not work_to:
            return True
        try:
            now = datetime.now(_KYIV_TZ).strftime("%H:%M")
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
        if not self._bump_lock.acquire(blocking=False):
            return  # previous tick still running, skip
        try:
            self._bump_tick_inner()
        finally:
            self._bump_lock.release()

    def _bump_tick_inner(self):
        if not self.cfg.get("enabled", True):
            return
        if not self._is_active_hours():
            name = self.cfg.get("name","?")
            broadcaster.emit(f"⏰ [{name}] поза робочим часом — пропуск", "info")
            return

        token = self.get_token()
        if not token:
            return
        name  = self.cfg.get("name", "?")
        url   = self.cfg.get("url", "")
        top_n = self.cfg.get("top_n", 1)

        # Build active tag list: [(tag, count), ...]
        active_tags = [
            (t.get("tag", "").strip(), max(1, t.get("count", 1)))
            for t in self.cfg.get("tags", [])
            if t.get("tag", "").strip()
        ]
        if not active_tags:
            broadcaster.emit(f"⚠ [{name}] немає тегів", "warn")
            return

        # Guard cycle index
        if self._cycle_tag_idx >= len(active_tags):
            self._cycle_tag_idx = 0
            self._cycle_done    = 0

        tag, bumps_needed = active_tags[self._cycle_tag_idx]

        # Skip tags with no items in cache — advance cycle and return
        my_items = items_for_tag(tag)
        if not my_items:
            broadcaster.emit(f"⚠ [{name}][{tag}] лоти не знайдено, пропуск тегу", "warn")
            self._advance_cycle(active_tags, force=True)
            return

        # Check if already in top-N — put matched items on cooldown, advance cycle and skip
        if url and top_n > 0:
            try:
                listings     = fetch_listings(url, token, top_n)
                top_ids      = {lot["id"] for lot in listings[:top_n]}
                my_ids       = {str(it.get("item_id", "")) for it in my_items}
                matched      = top_ids & my_ids
                if matched:
                    now_ts = time.time()
                    for iid in matched:
                        self._cooldowns[iid]     = now_ts + self.ITEM_COOLDOWN_SEC
                        self._cooldown_tags[iid] = tag
                    broadcaster.emit(f"✅ [{name}][{tag}] вже в топ-{top_n}, пропуск", "ok")
                    self._advance_cycle(active_tags, force=True)
                    return
            except Exception as e:
                broadcaster.emit(f"⚠ [{name}][{tag}] top-N перевірка: {e}", "warn")

        now       = time.time()
        # purge expired cooldowns to keep dict small
        expired = [k for k, v in self._cooldowns.items() if v <= now]
        for k in expired:
            del self._cooldowns[k]
            self._cooldown_tags.pop(k, None)

        total     = len(my_items)
        start_idx = self._bump_idx.get(tag, 0) % total
        bumped    = False

        api_tried  = 0
        scan_limit = total  # try all items until one bumps

        for scan in range(scan_limit):
            idx = (start_idx + scan) % total
            it  = my_items[idx]
            iid = str(it.get("item_id", ""))

            # skip items in cooldown (no API call)
            if self._cooldowns.get(iid, 0) > now:
                continue

            title = it.get("title") or it.get("title_en") or f"#{iid}"
            ok, reason = bump_item(token, iid)
            api_tried += 1  # count actual API calls (no hard cap)

            if ok:
                self._bump_idx[tag] = (idx + 1) % total
                self._cooldowns[iid] = now + self.ITEM_COOLDOWN_SEC
                self._cooldown_tags[iid] = tag
                # update bumped_at in cache so rotation picks correct oldest
                it["bumped_at"] = int(now)
                broadcaster.emit(f"✅ [{name}][{tag}] {title}  ({self._cycle_done+1}/{bumps_needed})", "ok")
                record_bump(name, tag, iid, title, True, "")
                bumped = True
                # ── auto-stick after bump ──────────────────────────────────
                self._auto_stick_after_bump(token, name, tag, iid, it)
                break

            elif reason.startswith("продано/видалено"):
                self._cooldowns[iid] = now + self.ITEM_COOLDOWN_SEC
                self._cooldown_tags[iid] = tag
                record_bump(name, tag, iid, title, False, reason)
                broadcaster.emit(f"🗑 [{name}][{tag}] {title} — не знайдено, перевіримо через 1 год", "warn")

            elif reason.startswith("ліміт"):
                self._cooldowns[iid] = now + self.ITEM_COOLDOWN_SEC
                self._cooldown_tags[iid] = tag
                record_bump(name, tag, iid, title, False, reason)
                broadcaster.emit(f"🚫 [{name}][{tag}] {title} — ліміт, кулдаун 1 год", "warn")

            elif reason.startswith("кулдаун"):
                self._cooldowns[iid] = now + self.ITEM_COOLDOWN_SEC
                self._cooldown_tags[iid] = tag
                broadcaster.emit(f"⏳ [{name}][{tag}] {title} — кулдаун, пропуск на 1 год", "warn")

            else:
                broadcaster.emit(f"⛔ [{name}][{tag}] {title} — {reason}", "error")
                record_bump(name, tag, iid, title, False, reason)
                # 5xx = server down, stop scanning immediately (no point trying more items)
                if any(reason.startswith(f"[{c}]") for c in ("500","502","503","504")):
                    break
                self._cooldowns[iid] = now + self.ITEM_COOLDOWN_SEC
                self._cooldown_tags[iid] = tag

        if not bumped:
            avail = sum(1 for it in my_items if self._cooldowns.get(str(it.get("item_id", "")), 0) <= now)
            broadcaster.emit(f"⚠ [{name}][{tag}] не вдалось підняти ({api_tried} спроб, {avail}/{total} доступних)", "warn")
            # if nothing available — skip to next tag immediately instead of wasting ticks
            self._advance_cycle(active_tags, force=(avail == 0))
        else:
            self._advance_cycle(active_tags)

    def _advance_cycle(self, active_tags: list, force: bool = False):
        _, needed = active_tags[self._cycle_tag_idx]
        self._cycle_done += 1
        if force or self._cycle_done >= needed:
            self._cycle_tag_idx = (self._cycle_tag_idx + 1) % len(active_tags)
            self._cycle_done    = 0

    def _auto_stick_after_bump(self, token: str, name: str, tag: str, iid: str, it: dict):
        """After a successful bump, pin the bumped item and rotate oldest pinned out."""
        cfg = load_config()
        target = cfg.get("tag_sticky", {}).get(tag, 0)
        if target <= 0:
            return

        tag_items = items_for_tag(tag)
        pinned = [x for x in tag_items if _is_pinned(x)]
        already_pinned = any(str(x.get("item_id", "")) == iid for x in pinned)

        # how many slots to free up:
        # if not already pinned → need 1 free slot; if already pinned → no new slot needed
        free_needed = 0 if already_pinned else 1
        # also free any excess over target (e.g. target was lowered)
        excess = len(pinned) - target
        to_unpin_count = max(free_needed + excess, 0) if not already_pinned else max(excess, 0)

        if to_unpin_count > 0:
            # sort pinned by bumped_at ascending (oldest first), exclude the just-bumped item
            # and items that previously returned 403 (can't be unstuck)
            candidates = sorted(
                [x for x in pinned
                 if str(x.get("item_id", "")) != iid
                 and str(x.get("item_id", "")) not in self._unstick_blocked],
                key=lambda x: x.get("bumped_at") or x.get("up_timestamp") or 0
            )
            removed = 0
            for oldest in candidates:
                if removed >= to_unpin_count:
                    break
                old_id = str(oldest.get("item_id", ""))
                ok_u, err_u = unstick_item(token, old_id)
                if ok_u:
                    oldest["is_pinned"] = False
                    oldest["is_sticky"] = False
                    broadcaster.emit(f"📌 [{name}][{tag}] знято: {oldest.get('title','?')[:40]}", "info")
                    removed += 1
                elif "[403]" in err_u:
                    self._unstick_blocked.add(old_id)
                    broadcaster.emit(f"⚠ [{name}][{tag}] {old_id} заблоковано для зняття (403), пропуск", "warn")
                else:
                    broadcaster.emit(f"⚠ [{name}][{tag}] не вдалось зняти {old_id}: {err_u}", "warn")

        # pin the just-bumped item
        if not already_pinned:
            ok_s, err_s = stick_item(token, iid)
            if ok_s:
                it["is_pinned"] = True
                it["is_sticky"] = True
                broadcaster.emit(f"📌 [{name}][{tag}] закріплено: {it.get('title','?')[:40]}", "ok")
            else:
                broadcaster.emit(f"⚠ [{name}][{tag}] не вдалось закріпити {iid}: {err_s}", "warn")

# ─── Scheduler manager ────────────────────────────────────────────────────────

def _safe_job(fn):
    """Wrap a scheduler job so an unhandled exception never kills the job thread."""
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:
            broadcaster.emit(f"⛔ Критична помилка в {fn.__name__}: {e}", "error")
    wrapper.__name__ = fn.__name__
    return wrapper


class SchedulerManager:
    def __init__(self):
        self._sched   = BackgroundScheduler(
            timezone="UTC",
            job_defaults={"misfire_grace_time": 300, "coalesce": True, "max_instances": 1},
        )
        self._engines: dict[str, MarketEngine] = {}
        self._cfg     = load_config()
        self._sched.start()

    def _token(self) -> str:
        return self._cfg.get("token", "")

    def reload(self, cfg: dict):
        self._cfg = cfg
        self._sched.remove_all_jobs()
        self._engines.clear()

        # always load cache on reload and schedule hourly refresh
        threading.Thread(target=self._warmup_cache, daemon=True).start()
        self._sched.add_job(_safe_job(self._warmup_cache), "interval", hours=1,
                            id="cache_refresh", replace_existing=True)

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
                self._sched.add_job(_safe_job(engine.refresh_tick), "interval", seconds=ri,
                                    id=f"refresh_{name}", replace_existing=True)

            if cfg.get("autobump", False):
                bi = max(30, mkt.get("bump_interval", 60))
                self._sched.add_job(_safe_job(engine.bump_tick), "interval", seconds=bi,
                                    id=f"bump_{name}", replace_existing=True)


    def _warmup_cache(self):
        global _tag_items_cache, _tag_id_map, _cache_loaded_at
        token = self._token()
        if not token:
            # Retry every 60s until token is available (e.g. env var may appear after container init)
            broadcaster.emit("⚠ Кеш: токен не задано — повтор через 60с", "warn")
            threading.Timer(60, self._warmup_cache).start()
            return
        try:
            broadcaster.emit("━━━ 📦 ОНОВЛЕННЯ КЕШУ ЛОТІВ ━━━", "ok")
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
            # merge: preserve bumped_at set after bumps; refresh pin state from API
            for tag_name, new_items in cache.items():
                old_by_id = {str(x.get("item_id","")): x for x in _tag_items_cache.get(tag_name, [])}
                for ni in new_items:
                    iid = str(ni.get("item_id", ""))
                    if iid in old_by_id:
                        old = old_by_id[iid]
                        if old.get("bumped_at"):
                            ni["bumped_at"] = old["bumped_at"]
            _tag_items_cache = cache
            _cache_loaded_at = datetime.now(_KYIV_TZ).strftime("%H:%M:%S")
            total = sum(len(v) for v in cache.values())
            broadcaster.emit(
                f"━━━ ✅ КЕШ ГОТОВИЙ: {len(all_items)} лотів | {total} по тегах | {_cache_loaded_at} ━━━", "ok")
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


@app.get("/health")
def health():
    """Render health check endpoint — always returns 200 if the process is alive."""
    return {"ok": True, "time": datetime.now(_KYIV_TZ).isoformat()}

# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    now = time.time()
    token_ok = bool(sched_mgr._token())
    jobs = sched_mgr._sched.get_jobs()
    engines = sched_mgr._engines
    cache_total = sum(len(v) for v in _tag_items_cache.values())
    markets_info = []
    for name, eng in engines.items():
        active_tags = [t.get("tag","") for t in eng.cfg.get("tags",[]) if t.get("tag","").strip()]
        cd_count = sum(1 for v in eng._cooldowns.values() if v > now)
        markets_info.append({
            "name": name,
            "enabled": eng.cfg.get("enabled", True),
            "tags": active_tags,
            "cooldowns": cd_count,
            "cycle_tag_idx": eng._cycle_tag_idx,
            "cycle_done": eng._cycle_done,
        })
    return {
        "token_ok": token_ok,
        "jobs_count": len(jobs),
        "cache_total": cache_total,
        "cache_loaded_at": _cache_loaded_at,
        "markets": markets_info,
        "server_time": datetime.now(_KYIV_TZ).strftime("%H:%M:%S"),
    }

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
    old = load_config()
    tok = cfg.get("token", "")
    if not tok or "…" in tok or tok == "****":
        cfg["token"] = old.get("token", "") or os.environ.get("LOLZ_TOKEN", "")

    # Detect if only layout changed (tag_positions) — avoid scheduler restart
    _sched_keys = {"autobump", "autorefresh", "markets"}
    old_sched = {k: old.get(k) for k in _sched_keys}
    new_sched = {k: cfg.get(k) for k in _sched_keys}
    scheduler_changed = (old_sched != new_sched)

    # always on
    cfg["autobump"] = True
    cfg["autorefresh"] = True

    save_config(cfg)

    if scheduler_changed:
        sched_mgr.reload(cfg)
        broadcaster.emit("⚙ Налаштування збережено", "info")

    return {"ok": True}

@app.get("/api/stats")
def get_stats():
    return stat_summary()

@app.get("/api/tags")
def get_tags():
    return sorted(_tag_id_map.keys())

@app.get("/api/cache-status")
def cache_status():
    tags = [
        {"tag": tag, "count": len(items)}
        for tag, items in _tag_items_cache.items()
    ]
    return {
        "loaded_at": _cache_loaded_at,
        "total_items": sum(t["count"] for t in tags),
        "tags": tags,
    }

@app.get("/api/logs")
def get_logs():
    return broadcaster._log[-100:]

@app.get("/api/bump-log")
def get_bump_log():
    conn = db_conn()
    rows = conn.execute(
        "SELECT market_name, tag, item_id, item_title, success, reason, logged_at"
        " FROM bumps WHERE logged_at >= date('now','localtime')"
        " ORDER BY logged_at DESC LIMIT 500"
    ).fetchall()
    conn.close()
    return [{"market_name":r[0],"tag":r[1],"item_id":r[2],"item_title":r[3],
             "success":bool(r[4]),"reason":r[5],"logged_at":r[6]} for r in rows]

@app.post("/api/refresh-cache")
def refresh_cache():
    threading.Thread(target=sched_mgr._warmup_cache, daemon=True).start()
    return {"ok": True}

@app.post("/api/restart-jobs")
def restart_jobs():
    """Reload scheduler jobs from current config (recovery without redeploy)."""
    cfg = load_config()
    sched_mgr.reload(cfg)
    broadcaster.emit("🔄 Планувальник перезапущено", "info")
    return {"ok": True}

@app.post("/api/refresh-tags")
def refresh_tags():
    def _do():
        token = sched_mgr._token()
        if not token:
            broadcaster.emit("⚠ Немає токена для завантаження тегів", "warn")
            return
        try:
            broadcaster.emit("🏷 Оновлення тегів…", "info")
            tags = fetch_my_tags(token)
            broadcaster.emit(f"🏷 Теги завантажено: {', '.join(tags)}", "ok")
        except Exception as e:
            broadcaster.emit(f"⚠ Теги: {e}", "warn")
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}

@app.get("/api/day-stats")
def day_stats():
    conn = db_conn()
    today = "date('now','localtime')"
    rows = conn.execute(
        f"SELECT market_name, success, reason FROM bumps"
        f" WHERE logged_at >= {today}"
    ).fetchall()
    conn.close()
    total = len(rows)
    ok    = sum(1 for r in rows if r[1])
    # failed due to limit/cooldown (actual bump attempts that hit wall)
    limit_fails = sum(1 for r in rows if not r[1] and r[2] and r[2].startswith("ліміт"))
    # per market
    by_market: dict[str, dict] = {}
    for r in rows:
        name = r[0] or "?"
        if name not in by_market:
            by_market[name] = {"ok": 0, "fail": 0}
        if r[1]:
            by_market[name]["ok"] += 1
        else:
            by_market[name]["fail"] += 1
    return {
        "total": total,
        "ok": ok,
        "fail": total - ok,
        "limit_fails": limit_fails,
        "by_market": [{"name": k, **v} for k, v in by_market.items()],
    }

@app.get("/api/market-status")
def market_status():
    cfg = load_config()
    token = cfg.get("token", "")
    result = []
    for mkt in cfg.get("markets", []):
        name = mkt.get("name", "")
        engine = sched_mgr._engines.get(name)
        listings = []
        if token:
            try:
                listings = fetch_listings(mkt["url"], token, 10)
            except Exception:
                pass
        result.append({
            "name": name,
            "enabled": mkt.get("enabled", True),
            "active": engine is not None,
            "work_from": mkt.get("work_from", ""),
            "work_to": mkt.get("work_to", ""),
            "listings": listings,
        })
    return result

@app.get("/api/cooldowns-detail")
def get_cooldowns_detail():
    """Return per-item cooldown details: title, tag, market, seconds_left."""
    now = time.time()
    rows = []
    for name, engine in sched_mgr._engines.items():
        for iid, exp in list(engine._cooldowns.items()):
            if exp <= now:
                continue
            tag = engine._cooldown_tags.get(iid, "?")
            secs_left = int(exp - now)
            # try to find title from cache
            title = iid
            for it in _tag_items_cache.get(tag, []):
                if str(it.get("item_id", "")) == iid:
                    title = it.get("title", iid)[:60]
                    break
            rows.append({
                "market": name,
                "tag": tag,
                "item_id": iid,
                "title": title,
                "secs_left": secs_left,
                "mins_left": round(secs_left / 60, 1),
            })
    rows.sort(key=lambda r: r["secs_left"])
    return {"total": len(rows), "items": rows}

@app.get("/api/cooldown-stats")
def get_cooldown_stats():
    now = time.time()
    cfg = load_config()
    tag_sticky: dict[str, int] = cfg.get("tag_sticky", {})
    result = []
    for tag, items in _tag_items_cache.items():
        total = len(items)
        on_cd = 0
        for engine in sched_mgr._engines.values():
            on_cd += sum(
                1 for iid, exp in engine._cooldowns.items()
                if exp > now and engine._cooldown_tags.get(iid) == tag
            )
        price_sum = 0.0
        currency = ""
        sticky_current = sum(1 for it in items if _is_pinned(it))
        for it in items:
            try:
                price_sum += float(it.get("price", 0) or 0)
                if not currency:
                    currency = "$"
            except (TypeError, ValueError):
                pass
        result.append({
            "tag": tag, "total": total, "cooldown": on_cd,
            "price_sum": round(price_sum, 2), "currency": currency,
            "sticky_current": sticky_current,
            "sticky_target": tag_sticky.get(tag, 0),
        })
    return sorted(result, key=lambda r: r["tag"])

@app.post("/api/tag-sticky")
async def set_tag_sticky(req: Request):
    body   = await req.json()
    tag    = body.get("tag", "")
    target = max(0, int(body.get("target", 0)))
    if not tag:
        raise HTTPException(400, "tag required")
    cfg = load_config()
    tag_sticky = cfg.get("tag_sticky", {})
    tag_sticky[tag] = target
    cfg["tag_sticky"] = tag_sticky
    save_config(cfg)
    return {"ok": True, "tag": tag, "target": target}

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
