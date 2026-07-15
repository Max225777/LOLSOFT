import tkinter as tk
from tkinter import ttk, messagebox
import threading
import urllib.parse
import sqlite3
import requests
import webbrowser
import json
import time
from pathlib import Path
from datetime import datetime

MY_PROFILE_ID = "9542364"
API_BASE      = "https://prod-api.lzt.market"
DB_PATH       = Path(__file__).parent / "lolsoft_stats.db"
CONFIG_PATH   = Path(__file__).parent / "lolsoft_config.json"
CACHE_REFRESH  = 5 * 60 * 1000
MAX_MARKETS    = 8

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

DEFAULT_URL = (
    "https://lzt.market/telegram/"
    "?origin[]=autoreg&origin[]=self_registration&country[]=UA&spam=no"
)

BG      = "#1a1a1a"
BG2     = "#202020"
CARD    = "#262626"
BORDER  = "#333333"
ACCENT  = "#a8c957"
ACCENT_D= "#8fae42"
GREEN   = "#a8c957"
TEXT    = "#e6e6e6"
SUBTEXT = "#888888"
RED_FG  = "#ff8a8a"
YELLOW  = "#ffc857"


# ─── API ──────────────────────────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "User-Agent": "LOLSOFT/1.0",
    }

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
        raise RuntimeError("Невірний API-токен (401).")
    resp.raise_for_status()
    parsed = []
    for it in resp.json().get("items", [])[:count]:
        item_id     = str(it.get("item_id", ""))
        title       = it.get("title") or it.get("title_en") or f"Лот #{item_id}"
        price       = it.get("price", "?")
        currency    = it.get("price_currency", "") or it.get("currency", "")
        seller      = it.get("seller") or {}
        seller_id   = str(seller.get("user_id", ""))
        seller_name = seller.get("username", "?")
        state       = it.get("item_state", "")
        bumped_at   = (it.get("bumped_at") or it.get("up_timestamp")
                       or it.get("refreshed_at") or it.get("updated_at"))
        parsed.append({
            "id":        item_id,
            "title":     title,
            "link":      f"https://lzt.market/{item_id}/",
            "seller":    seller_name,
            "seller_id": seller_id,
            "price":     f"{price} {currency}".strip(),
            "is_mine":   seller_id == MY_PROFILE_ID,
            "is_pinned": bool(it.get("is_sticky") or it.get("sticky")),
            "is_closed": state in ("sold", "closed", "deleted"),
            "bumped_at": bumped_at,
        })
    return parsed

_tag_id_map: dict[str, int] = {}

def fetch_my_tags(token: str) -> tuple[list[str], str]:
    global _tag_id_map
    resp = requests.get(f"{API_BASE}/me", headers=_headers(token), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    raw  = str(data)[:300]
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
    return sorted(set(tags)), raw

def fetch_all_my_items(token: str) -> list[dict]:
    result   = []
    seen_ids = set()
    page     = 1
    while page <= 100:
        url  = f"{API_BASE}/user/items?page={page}"
        resp = requests.get(url, headers=_headers(token), timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        added = 0
        for it in items:
            iid = it.get("item_id")
            if iid not in seen_ids:
                seen_ids.add(iid)
                result.append(it)
                added += 1
        if added == 0:
            break
        page += 1
    return result

def fetch_items_by_tag(token: str, tag_id: int) -> list[dict]:
    result   = []
    seen_ids = set()
    page     = 1
    while page <= 100:
        url  = f"{API_BASE}/user/items?tag_id={tag_id}&page={page}"
        resp = requests.get(url, headers=_headers(token), timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        added = 0
        for it in items:
            iid = it.get("item_id")
            if iid not in seen_ids:
                seen_ids.add(iid)
                result.append(it)
                added += 1
        if added == 0:
            break
        page += 1
    return result

_tag_items_cache: dict[str, list[dict]] = {}

def items_for_tag(cache: list[dict], tag: str) -> list[dict]:
    return list(_tag_items_cache.get(tag.strip(), []))

def item_title(it: dict) -> str:
    iid = str(it.get("item_id", ""))
    return it.get("title") or it.get("title_en") or f"#{iid}"

def bump_item(token: str, item_id: str) -> tuple[bool, str]:
    url = f"{API_BASE}/{item_id}/bump"
    try:
        resp = requests.post(url, headers=_headers(token), timeout=15)
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
        if (code == 429 or "cooldown" in ml or "flood" in ml or "wait" in ml
                or "подожд" in ml or "нужно" in ml or "зачекайте" in ml):
            return False, f"кулдаун: {raw}"
        if code == 404 or "not found" in ml or "deleted" in ml or "sold" in ml:
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
        item_id TEXT, seller_id TEXT, seller_name TEXT,
        bumped_at INTEGER, logged_at TEXT,
        is_mine INTEGER, is_pinned INTEGER,
        auto_bumped INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS last_seen (
        item_id TEXT PRIMARY KEY, bumped_at INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logged ON bumps(logged_at)")
    try:
        conn.execute("ALTER TABLE bumps ADD COLUMN auto_bumped INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    return conn

_prev_ids: set[str]   = set()
_first_scan_done: bool = False

def record_snapshot(listings: list[dict], auto_bumped_ids: set[str] = None) -> list[dict]:
    global _prev_ids, _first_scan_done
    auto_bumped_ids = auto_bumped_ids or set()
    conn    = db_conn()
    now_iso = datetime.now().isoformat(timespec="seconds")
    events  = []
    current_ids = {lot["id"] for lot in listings if lot["id"]}
    if not _first_scan_done:
        _prev_ids = current_ids
        _first_scan_done = True
        conn.close()
        return []
    for lot in listings:
        if lot["id"] not in (current_ids - _prev_ids):
            continue
        conn.execute(
            "INSERT INTO bumps (item_id,seller_id,seller_name,bumped_at,"
            "logged_at,is_mine,is_pinned,auto_bumped) VALUES (?,?,?,?,?,?,?,?)",
            (lot["id"], lot.get("seller_id",""), lot.get("seller","?"),
             lot.get("bumped_at"), now_iso,
             int(lot.get("is_mine",False)), int(lot.get("is_pinned",False)),
             int(lot["id"] in auto_bumped_ids))
        )
        events.append(lot)
    _prev_ids = current_ids
    conn.commit(); conn.close()
    return events

def today_iso() -> str:
    return datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).isoformat(timespec="seconds")

def stat_summary() -> dict:
    conn = db_conn()
    t = today_iso()
    total_all   = conn.execute("SELECT COUNT(*) FROM bumps").fetchone()[0]
    total_today = conn.execute("SELECT COUNT(*) FROM bumps WHERE logged_at>=?", (t,)).fetchone()[0]
    my_today    = conn.execute("SELECT COUNT(*) FROM bumps WHERE is_mine=1 AND logged_at>=?", (t,)).fetchone()[0]
    auto_today  = conn.execute("SELECT COUNT(*) FROM bumps WHERE auto_bumped=1 AND logged_at>=?", (t,)).fetchone()[0]
    sellers     = conn.execute(
        "SELECT seller_name,COUNT(*),MAX(is_mine) FROM bumps "
        "WHERE logged_at>=? GROUP BY seller_id ORDER BY COUNT(*) DESC", (t,)
    ).fetchall()
    conn.close()
    return dict(total_all=total_all, total_today=total_today,
                my_today=my_today, auto_today=auto_today, sellers=sellers)


# ─── App ──────────────────────────────────────────────────────────────────────
def _default_market(name="УА ринок", url=None) -> dict:
    return {
        "name": name,
        "url":  url or DEFAULT_URL,
        "tags": [{"tag":"","count":1}, {"tag":"","count":1}, {"tag":"","count":1}],
        "count": 10,
        "top_n": 1,
    }

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Multi-Market")
        self.geometry("1400x920")
        self.configure(bg=BG)

        self._listing_data: list[dict]   = []
        self._auto_job                   = None
        self._refresh_job                = None
        self._cache_job                  = None
        self._show_token                 = False
        self._bump_count                 = 0
        self._last_auto_bumped: set[str] = set()
        self._cycle_log: list[str]       = []
        self._items_cache: list[dict]    = []
        self._cache_ts: str              = "не завантажено"

        # bump state per market name
        self._mkt_bump: dict[str, dict]  = {}

        self._cfg = load_config()

        # load markets from config or default
        saved = self._cfg.get("markets")
        if saved and isinstance(saved, list) and len(saved) > 0:
            self._markets: list[dict] = saved
        else:
            # migrate old single-market config
            self._markets = [_default_market(
                url=self._cfg.get("url", DEFAULT_URL)
            )]

        self._active_mkt: int = 0
        self._mkt_vars: list[dict]    = []
        self._mkt_panels: list[tk.Frame] = []

        self._build_ui()
        self._apply_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _mkt_state(self, name: str) -> dict:
        if name not in self._mkt_bump:
            self._mkt_bump[name] = {"cycle_tag_idx": 0, "cycle_done": 0, "tag_idx": {}}
        return self._mkt_bump[name]

    # ── Config ────────────────────────────────────────────────────────────────
    def _apply_config(self):
        c = self._cfg
        if c.get("token"):
            self.token_var.set(c["token"])
        if c.get("interval"):
            self.interval_var.set(c["interval"])
        if c.get("autorefresh"):
            self.autorefresh_var.set(True)
            self._toggle_autorefresh()
        if c.get("autobump"):
            self.autobump_var.set(True)
            self._toggle_autobump()
        for var in (self.token_var, self.interval_var, self.autorefresh_var, self.autobump_var):
            var.trace_add("write", self._save_config)

    def _save_config(self, *_):
        def _int(var, default=1):
            try: return var.get()
            except Exception: return default
        self._sync_vars_to_markets()
        cfg = {
            "token":       self.token_var.get().strip(),
            "interval":    _int(self.interval_var, 60),
            "autorefresh": self.autorefresh_var.get(),
            "autobump":    self.autobump_var.get(),
            "markets":     self._markets,
        }
        save_config(cfg)

    def _sync_vars_to_markets(self):
        for i, mv in enumerate(self._mkt_vars):
            if i >= len(self._markets):
                break
            def _int(v, d=1):
                try: return v.get()
                except: return d
            self._markets[i]["name"]  = mv["name_var"].get().strip()
            self._markets[i]["url"]   = mv["url_var"].get().strip()
            self._markets[i]["count"] = _int(mv["count_var"], 10)
            self._markets[i]["top_n"] = _int(mv["top_n_var"], 1)
            for j, (tv, cv) in enumerate(mv["tag_vars"]):
                self._markets[i]["tags"][j] = {"tag": tv.get().strip(), "count": _int(cv, 1)}

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(10,4))
        tk.Label(hdr, text="LOLSOFT", bg=BG, fg=ACCENT,
                 font=("Segoe UI",15,"bold")).pack(side="left")
        tk.Label(hdr, text="  Multi-Market Аналізатор", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI",10)).pack(side="left")

        # Token
        tr = tk.Frame(self, bg=BG)
        tr.pack(fill="x", padx=16, pady=(2,2))
        tk.Label(tr, text="Токен:", bg=BG, fg=SUBTEXT, font=("Segoe UI",10)).pack(side="left")
        self.token_var   = tk.StringVar()
        self.token_entry = tk.Entry(tr, textvariable=self.token_var, show="•",
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI",10), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(6,6), ipady=4)
        tk.Button(tr, text="👁", command=self._toggle_token,
                  bg=CARD, fg=TEXT, relief="flat", cursor="hand2",
                  activebackground=BORDER).pack(side="left", padx=(0,6))
        tk.Button(tr, text="Де взяти?",
                  command=lambda: webbrowser.open("https://lzt.market/account/api"),
                  bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
                  font=("Segoe UI",9), activebackground=BORDER).pack(side="left")

        # Global controls
        gr = tk.Frame(self, bg=BG)
        gr.pack(fill="x", padx=16, pady=(4,2))

        self.autorefresh_var = tk.BooleanVar(value=False)
        tk.Checkbutton(gr, text="Авто-оновлення", variable=self.autorefresh_var,
                       command=self._toggle_autorefresh,
                       bg=BG, fg=ACCENT, selectcolor=CARD,
                       activebackground=BG, activeforeground=ACCENT,
                       font=("Segoe UI",10), cursor="hand2").pack(side="left", padx=(0,10))

        self.autobump_var = tk.BooleanVar(value=False)
        tk.Checkbutton(gr, text="Авто-підняття", variable=self.autobump_var,
                       command=self._toggle_autobump,
                       bg=BG, fg=YELLOW, selectcolor=CARD,
                       activebackground=BG, activeforeground=YELLOW,
                       font=("Segoe UI",10,"bold"), cursor="hand2").pack(side="left", padx=(0,10))

        self.bump_count_var = tk.StringVar(value="Підняттів: 0")
        tk.Label(gr, textvariable=self.bump_count_var,
                 bg=BG, fg=YELLOW, font=("Segoe UI",10,"bold")).pack(side="left", padx=(0,14))

        tk.Label(gr, text="Інтервал:", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
        self.interval_var = tk.IntVar(value=60)
        tk.Spinbox(gr, from_=10, to=3600, textvariable=self.interval_var, width=5,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI",9)).pack(side="left", padx=(4,2))
        tk.Label(gr, text="сек", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left", padx=(0,14))

        self.load_tags_btn = tk.Button(gr, text="🔄 Теги", command=self._load_tags,
            bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
            font=("Segoe UI",9), activebackground=BORDER)
        self.load_tags_btn.pack(side="left", padx=(0,6))

        self.load_items_btn = tk.Button(gr, text="📦 Завантажити лоти", command=self._load_items,
            bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
            font=("Segoe UI",9), activebackground=BORDER)
        self.load_items_btn.pack(side="left")

        self.bump_status_var = tk.StringVar(value="")
        tk.Label(gr, textvariable=self.bump_status_var,
                 bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left", padx=(12,0))

        # Market tabs
        self._tabs_frame = tk.Frame(self, bg=BG)
        self._tabs_frame.pack(fill="x", padx=16, pady=(8,0))

        # Market content (swapped on tab click)
        self._mkt_content = tk.Frame(self, bg=BG)
        self._mkt_content.pack(fill="x", padx=16, pady=(4,4))

        for i, mkt in enumerate(self._markets):
            self._build_market_panel(i, mkt)
        self._rebuild_tabs()
        self._show_market(0)

        # Status bar
        self.status_var = tk.StringVar(value="Вибери ринок і натисни «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI",9), anchor="w").pack(fill="x", padx=18, pady=(2,2))

        # Log panel (shared for all markets)
        log_wrap = tk.Frame(self, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        log_wrap.pack(fill="x", padx=16, pady=(0,4))
        lh = tk.Frame(log_wrap, bg=CARD)
        lh.pack(fill="x", padx=4, pady=(3,2))
        tk.Label(lh, text="Лог підняттів — всі ринки", bg=CARD, fg=ACCENT,
                 font=("Segoe UI",9,"bold")).pack(side="left", padx=2)
        tk.Button(lh, text="📋 Копіювати", command=self._copy_logs,
                  bg=BORDER, fg=TEXT, relief="flat", cursor="hand2",
                  font=("Segoe UI",8), activebackground=CARD).pack(side="right", padx=2)
        self.cycle_log_box = tk.Text(log_wrap, bg=CARD, fg=TEXT,
            selectbackground="#3a4a60", selectforeground="#ffffff",
            relief="flat", font=("Consolas",9), height=4,
            highlightthickness=0, wrap="none", state="disabled",
            cursor="xterm")
        self.cycle_log_box.pack(fill="x", padx=4, pady=(0,4))
        # allow Ctrl+C to copy selected text from log
        self.cycle_log_box.bind("<Control-c>", lambda e: None)
        self.cycle_log_box.bind("<Control-C>", lambda e: None)

        # Listings + stats
        self._build_main_pane()
        self._enable_paste(self.token_entry)
        # global Ctrl+V paste for all Entry/Combobox widgets
        self.bind_all("<Control-v>", self._global_paste)
        self.bind_all("<Control-V>", self._global_paste)

    def _build_market_panel(self, idx: int, mkt: dict):
        panel = tk.Frame(self._mkt_content, bg=BG)
        self._mkt_panels.append(panel)

        # Row 1: name, url, count, top_n, search
        r1 = tk.Frame(panel, bg=BG)
        r1.pack(fill="x", pady=(0,3))

        tk.Label(r1, text="Назва:", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
        name_var = tk.StringVar(value=mkt.get("name",""))
        name_entry = tk.Entry(r1, textvariable=name_var, width=14,
            bg=CARD, fg=ACCENT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI",9,"bold"), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        name_entry.pack(side="left", padx=(4,10), ipady=3)
        name_var.trace_add("write", lambda *_: self._on_name_changed())

        tk.Label(r1, text="URL:", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
        url_var = tk.StringVar(value=mkt.get("url", DEFAULT_URL))
        tk.Entry(r1, textvariable=url_var,
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI",9), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT
            ).pack(side="left", fill="x", expand=True, padx=(4,8), ipady=3)

        tk.Label(r1, text="Лотів:", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
        count_var = tk.IntVar(value=mkt.get("count", 10))
        tk.Spinbox(r1, from_=1, to=100, textvariable=count_var, width=4,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI",9)).pack(side="left", padx=(4,8))

        tk.Label(r1, text="Топ:", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
        top_n_var = tk.IntVar(value=mkt.get("top_n", 1))
        tk.Spinbox(r1, from_=1, to=20, textvariable=top_n_var, width=3,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI",9)).pack(side="left", padx=(4,8))

        search_btn = tk.Button(r1, text="Пошук",
            command=lambda u=url_var, c=count_var: self._start_search(u.get().strip(), c),
            bg=ACCENT, fg="#1a1a1a", activebackground=ACCENT_D,
            relief="flat", font=("Segoe UI",9,"bold"), padx=8, pady=3, cursor="hand2")
        search_btn.pack(side="left", padx=(0,6))

        if len(self._markets) > 0:
            del_btn = tk.Button(r1, text="✕ Видалити ринок",
                command=lambda i=idx: self._remove_market(i),
                bg=CARD, fg=RED_FG, relief="flat", cursor="hand2",
                font=("Segoe UI",8), activebackground=BORDER)
            del_btn.pack(side="left")

        # Row 2: tags
        r2 = tk.Frame(panel, bg=BG)
        r2.pack(fill="x", pady=(0,2))

        tag_vars: list[tuple[tk.StringVar, tk.IntVar]] = []
        tag_count_vars: list[tk.StringVar] = []
        tags_data = list(mkt.get("tags") or [])
        while len(tags_data) < 3:
            tags_data.append({"tag":"","count":1})

        for j in range(3):
            tk.Label(r2, text=f"Тег {j+1}:", bg=BG, fg=SUBTEXT,
                     font=("Segoe UI",9), width=6, anchor="w").pack(side="left")
            tv = tk.StringVar(value=tags_data[j].get("tag",""))
            cb = ttk.Combobox(r2, textvariable=tv, width=22,
                              font=("Segoe UI",9), state="normal")
            cb.pack(side="left", padx=(2,2), ipady=2)

            cnt_lbl = tk.StringVar(value="")
            tk.Label(r2, textvariable=cnt_lbl, bg=BG, fg=ACCENT,
                     font=("Segoe UI",8)).pack(side="left", padx=(0,2))
            tag_count_vars.append(cnt_lbl)

            tk.Label(r2, text="×", bg=BG, fg=SUBTEXT, font=("Segoe UI",9)).pack(side="left")
            cv = tk.IntVar(value=tags_data[j].get("count",1))
            tk.Spinbox(r2, from_=1, to=50, textvariable=cv, width=3,
                       bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                       font=("Segoe UI",9)).pack(side="left", padx=(2,12))
            tag_vars.append((tv, cv))
            tv.trace_add("write", lambda *_, mi=idx, ti=j: self._update_tag_count_for(mi, ti))
            tv.trace_add("write", self._save_config)
            cv.trace_add("write", self._save_config)

        mv = {
            "name_var":       name_var,
            "url_var":        url_var,
            "count_var":      count_var,
            "top_n_var":      top_n_var,
            "tag_vars":       tag_vars,
            "tag_count_vars": tag_count_vars,
            "search_btn":     search_btn,
        }
        self._mkt_vars.append(mv)
        name_var.trace_add("write", self._save_config)
        url_var.trace_add("write",  self._save_config)
        count_var.trace_add("write", self._save_config)
        top_n_var.trace_add("write", self._save_config)

    def _rebuild_tabs(self):
        for w in self._tabs_frame.winfo_children():
            w.destroy()
        for i, mkt in enumerate(self._markets):
            name = mkt.get("name") or f"Ринок {i+1}"
            active = (i == self._active_mkt)
            tk.Button(self._tabs_frame, text=name,
                command=lambda i=i: self._show_market(i),
                bg=ACCENT if active else CARD,
                fg="#1a1a1a" if active else TEXT,
                relief="flat", cursor="hand2",
                font=("Segoe UI",9,"bold" if active else "normal"),
                padx=10, pady=4, activebackground=ACCENT_D
            ).pack(side="left", padx=(0,3))
        tk.Button(self._tabs_frame, text="＋ Ринок",
                  command=self._add_market,
                  bg=BORDER, fg=SUBTEXT, relief="flat", cursor="hand2",
                  font=("Segoe UI",9), padx=8, pady=4, activebackground=CARD
                  ).pack(side="left", padx=(6,0))

    def _show_market(self, idx: int):
        if idx >= len(self._mkt_panels):
            return
        self._active_mkt = idx
        for i, panel in enumerate(self._mkt_panels):
            panel.pack_forget()
        self._mkt_panels[idx].pack(fill="x")
        self._rebuild_tabs()
        self._update_all_tag_counts()

    def _add_market(self):
        if len(self._markets) >= MAX_MARKETS:
            messagebox.showinfo("Ліміт", f"Максимум {MAX_MARKETS} ринків")
            return
        mkt = _default_market(name=f"Ринок {len(self._markets)+1}")
        self._markets.append(mkt)
        self._build_market_panel(len(self._markets)-1, mkt)
        self._rebuild_tabs()
        self._show_market(len(self._markets)-1)
        self._refresh_all_comboboxes()
        self._save_config()

    def _remove_market(self, idx: int):
        if len(self._markets) <= 1:
            messagebox.showinfo("Не можна", "Має бути хоча б один ринок")
            return
        self._markets.pop(idx)
        panel = self._mkt_panels.pop(idx)
        panel.destroy()
        self._mkt_vars.pop(idx)
        self._active_mkt = min(self._active_mkt, len(self._markets)-1)
        self._rebuild_tabs()
        self._show_market(self._active_mkt)
        self._save_config()

    def _on_name_changed(self):
        self._sync_vars_to_markets()
        self._rebuild_tabs()
        self._save_config()

    def _build_main_pane(self):
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg=BG,
                               sashwidth=5, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=10, pady=(0,10))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=BG2, foreground=TEXT,
            fieldbackground=BG2, rowheight=28, borderwidth=0, font=("Segoe UI",10))
        style.configure("Treeview.Heading", background=CARD, foreground=ACCENT,
            relief="flat", font=("Segoe UI",10,"bold"))
        style.map("Treeview", background=[("selected","#2f3a52")],
                              foreground=[("selected","#ffffff")])
        style.configure("Stats.Treeview", background=BG2, foreground=TEXT,
            fieldbackground=BG2, rowheight=26, borderwidth=0, font=("Segoe UI",9))
        style.configure("Stats.Treeview.Heading", background=CARD, foreground=ACCENT,
            relief="flat", font=("Segoe UI",9,"bold"))

        top_frame = tk.Frame(paned, bg=BORDER)
        paned.add(top_frame, minsize=420)
        cols = ("№","Назва","Продавець","Ціна","📌","🔒","Піднято","Мій")
        self.tree = ttk.Treeview(top_frame, columns=cols, show="headings", selectmode="browse")
        for c, w, a in zip(cols, [32,290,140,90,38,38,100,45],
                           ["center","w","w","center","center","center","center","center"]):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=a)
        sb1 = ttk.Scrollbar(top_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb1.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb1.pack(side="right", fill="y")
        self.tree.tag_configure("mine",      background="#15311f", foreground=GREEN)
        self.tree.tag_configure("mine_bump", background="#15311f", foreground=YELLOW)
        self.tree.tag_configure("other",     background="#2b1717", foreground=RED_FG)
        self.tree.bind("<Double-1>", self._open_link)

        stat_frame = tk.Frame(paned, bg=BG)
        paned.add(stat_frame, minsize=280)
        self.stat_summary_var = tk.StringVar(value="—")
        tk.Label(stat_frame, textvariable=self.stat_summary_var, bg=BG, fg=TEXT,
                 font=("Segoe UI",9), anchor="w", justify="left", wraplength=400
                 ).pack(anchor="w", padx=8, pady=(4,4))
        stat_wrap = tk.Frame(stat_frame, bg=BORDER)
        stat_wrap.pack(fill="both", expand=True, padx=8, pady=(0,4))
        scols = ("Продавець","Сьогодні","Всього","Мій")
        self.stat_tree = ttk.Treeview(stat_wrap, columns=scols, show="headings", style="Stats.Treeview")
        for c, w in zip(scols, [190,80,70,40]):
            self.stat_tree.heading(c, text=c)
            self.stat_tree.column(c, width=w, anchor="w" if c=="Продавець" else "center")
        sb2 = ttk.Scrollbar(stat_wrap, orient="vertical", command=self.stat_tree.yview)
        self.stat_tree.configure(yscrollcommand=sb2.set)
        self.stat_tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb2.pack(side="right", fill="y")
        self.stat_tree.tag_configure("mine", background="#15311f", foreground=GREEN)

    # ── Cache ─────────────────────────────────────────────────────────────────
    def _load_items(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("Немає токена", "Спочатку встав API-токен.")
            return
        self.load_items_btn.config(state="disabled", text="Завантаження…")
        threading.Thread(target=self._do_load_items, args=(token,), daemon=True).start()

    def _do_load_items(self, token: str):
        global _tag_items_cache
        try:
            if not _tag_id_map:
                self.after(0, lambda: self.load_items_btn.config(text="Теги…"))
                fetch_my_tags(token)
            self.after(0, lambda: self.load_items_btn.config(text="Завантаження…"))
            all_items = fetch_all_my_items(token)
            id_to_name = {v: k for k, v in _tag_id_map.items()}
            tag_cache: dict[str, list[dict]] = {name: [] for name in _tag_id_map}
            for it in all_items:
                raw_tags = it.get("tags") or {}
                tag_objs = raw_tags.values() if isinstance(raw_tags, dict) else raw_tags
                for t in tag_objs:
                    if isinstance(t, dict):
                        tid = t.get("tag_id") or t.get("id")
                        if tid and tid in id_to_name:
                            tag_cache[id_to_name[tid]].append(it)
                    elif isinstance(t, int) and t in id_to_name:
                        tag_cache[id_to_name[t]].append(it)
            _tag_items_cache = tag_cache
            self.after(0, self._apply_items_cache, all_items)
        except Exception as exc:
            self.after(0, self._cache_error, str(exc))

    def _apply_items_cache(self, items: list[dict]):
        self._items_cache = items
        self._cache_ts    = datetime.now().strftime("%H:%M:%S")
        self._update_all_tag_counts()
        self.load_items_btn.config(state="normal", text="📦 Завантажити лоти")
        tag_counts = ", ".join(f"{n}:{len(v)}" for n, v in _tag_items_cache.items() if v)
        self._add_log(f"📦 Кеш: {len(items)} лотів [{self._cache_ts}]  теги: {tag_counts or '—'}")
        if self._cache_job:
            self.after_cancel(self._cache_job)
        self._cache_job = self.after(CACHE_REFRESH, self._auto_refresh_cache)

    def _cache_error(self, msg: str):
        self._add_log(f"⚠ Кеш: {msg}")
        self.load_items_btn.config(state="normal", text="📦 Завантажити лоти")

    def _auto_refresh_cache(self):
        token = self.token_var.get().strip()
        if token:
            threading.Thread(target=self._do_load_items, args=(token,), daemon=True).start()

    def _update_tag_count_for(self, mkt_idx: int, tag_idx: int):
        if mkt_idx >= len(self._mkt_vars):
            return
        mv  = self._mkt_vars[mkt_idx]
        tag = mv["tag_vars"][tag_idx][0].get().strip()
        lbl = mv["tag_count_vars"][tag_idx]
        if tag and _tag_items_cache:
            n = len(items_for_tag(self._items_cache, tag))
            lbl.set(f"[{n}]")
        else:
            lbl.set("")
        self._save_config()

    def _update_all_tag_counts(self):
        for mi in range(len(self._mkt_vars)):
            for ti in range(3):
                self._update_tag_count_for(mi, ti)

    # ── Tags ──────────────────────────────────────────────────────────────────
    def _load_tags(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("Немає токена", "Спочатку встав API-токен.")
            return
        self.load_tags_btn.config(state="disabled", text="…")
        threading.Thread(target=self._do_load_tags, args=(token,), daemon=True).start()

    def _do_load_tags(self, token: str):
        try:
            tags, raw = fetch_my_tags(token)
            self.after(0, self._apply_tags, tags, raw)
        except Exception as exc:
            self.after(0, self._add_log, f"⚠ Теги: {exc}")
            self.after(0, lambda: self.load_tags_btn.config(state="normal", text="🔄 Теги"))

    def _apply_tags(self, tags: list[str], raw: str = ""):
        self._refresh_all_comboboxes(tags)
        self.load_tags_btn.config(state="normal", text="🔄 Теги")
        if tags:
            self._add_log(f"🏷 Теги завантажено: {', '.join(tags)}")
        else:
            self._add_log(f"⚠ Теги не знайдено. Raw: {raw[:100]}")
        self._update_all_tag_counts()

    def _refresh_all_comboboxes(self, tags: list[str] = None):
        if tags is None:
            tags = list(_tag_id_map.keys())
        cbs = self._find_comboboxes(self)
        for cb in cbs:
            cb["values"] = tags

    def _find_comboboxes(self, parent) -> list:
        found = []
        for w in parent.winfo_children():
            if isinstance(w, ttk.Combobox):
                found.append(w)
            found += self._find_comboboxes(w)
        return found

    # ── Token / paste ─────────────────────────────────────────────────────────
    def _global_paste(self, event):
        w = event.widget
        if not isinstance(w, (tk.Entry, ttk.Combobox)):
            return
        try:
            txt = self.clipboard_get()
        except tk.TclError:
            return
        txt = txt.splitlines()[0].strip() if txt.strip() else ""
        try:
            w.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        w.insert("insert", txt)
        return "break"

    def _enable_paste(self, entry: tk.Entry):
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#1a1a1a")
        menu.add_command(label="Вставити", command=lambda: self._paste_into(entry))
        menu.add_command(label="Очистити", command=lambda: entry.delete(0,"end"))
        entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        def ctrl_key(e):
            if e.keycode in (86,55) and (e.state & 0x4):
                self._paste_into(entry); return "break"
        entry.bind("<Control-KeyPress>", ctrl_key)

    def _paste_into(self, entry: tk.Entry):
        try:
            txt = self.clipboard_get()
        except tk.TclError:
            return
        txt = txt.splitlines()[0].strip() if txt.strip() else ""
        try:
            entry.delete("sel.first","sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", txt)

    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "•")

    # ── Search ────────────────────────────────────────────────────────────────
    def _start_search(self, url: str, count_var: tk.IntVar):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("Немає токена", "Спочатку встав API-токен.")
            return
        if not url:
            messagebox.showwarning("Немає URL", "Вкажи URL ринку.")
            return
        def _int(v, d):
            try: return v.get()
            except: return d
        count = _int(count_var, 10)
        self.status_var.set("Завантаження…")
        threading.Thread(target=self._do_search, args=(url, token, count), daemon=True).start()

    def _do_search(self, url: str, token: str, count: int):
        try:
            listings = fetch_listings(url, token, count)
            self.after(0, self._populate, listings)
        except Exception as exc:
            self.after(0, lambda: self.status_var.set(f"Помилка: {exc}"))

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    def _toggle_autorefresh(self):
        if self.autorefresh_var.get():
            self._schedule_autorefresh()
        elif self._refresh_job:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None

    def _schedule_autorefresh(self):
        def _int(v, d):
            try: return v.get()
            except: return d
        interval = max(10, _int(self.interval_var, 60))
        self._refresh_job = self.after(interval * 1000, self._refresh_tick)

    def _refresh_tick(self):
        if not self.autorefresh_var.get():
            return
        token = self.token_var.get().strip()
        if token and self._markets:
            mkt   = self._markets[self._active_mkt]
            url   = mkt.get("url", "")
            count = mkt.get("count", 10)
            if url:
                threading.Thread(target=self._do_search,
                                 args=(url, token, count), daemon=True).start()
        self._schedule_autorefresh()

    # ── Auto-bump ─────────────────────────────────────────────────────────────
    def _toggle_autobump(self):
        if self.autobump_var.get():
            self._schedule_autobump()
        elif self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None

    def _schedule_autobump(self):
        def _int(v, d):
            try: return v.get()
            except: return d
        interval = max(10, _int(self.interval_var, 60))
        self._auto_job = self.after(interval * 1000, self._auto_tick)

    def _auto_tick(self):
        if not self.autobump_var.get():
            return
        token = self.token_var.get().strip()
        if not token:
            self._schedule_autobump()
            return
        for mkt in self._markets:
            threading.Thread(target=self._bump_market, args=(token, mkt), daemon=True).start()
        self._schedule_autobump()

    def _bump_market(self, token: str, mkt: dict):
        name = mkt.get("name","?")
        active_tags = [
            (t["tag"].strip(), t.get("count",1))
            for t in (mkt.get("tags") or [])
            if t.get("tag","").strip()
        ]
        if not active_tags:
            return

        global _tag_items_cache
        if not _tag_items_cache:
            try:
                if not _tag_id_map:
                    fetch_my_tags(token)
                all_items = fetch_all_my_items(token)
                tag_cache: dict[str, list[dict]] = {n: [] for n in _tag_id_map}
                id_to_name = {v: k for k, v in _tag_id_map.items()}
                for it in all_items:
                    raw_tags = it.get("tags") or {}
                    tag_objs = raw_tags.values() if isinstance(raw_tags, dict) else raw_tags
                    for t in tag_objs:
                        if isinstance(t, dict):
                            tid = t.get("tag_id") or t.get("id")
                            if tid and tid in id_to_name:
                                tag_cache[id_to_name[tid]].append(it)
                        elif isinstance(t, int) and t in id_to_name:
                            tag_cache[id_to_name[t]].append(it)
                _tag_items_cache = tag_cache
                self.after(0, self._apply_items_cache, all_items)
            except Exception as exc:
                self.after(0, self._add_log, f"⚠ [{name}] кеш: {exc}")
                return

        st = self._mkt_state(name)
        if st["cycle_tag_idx"] >= len(active_tags):
            st["cycle_tag_idx"] = 0
            st["cycle_done"]    = 0

        tag, bumps_needed = active_tags[st["cycle_tag_idx"]]
        now_str = datetime.now().strftime("%H:%M:%S")
        self.after(0, self._add_log,
                   f"▶ [{name}] «{tag}» {st['cycle_done']+1}/{bumps_needed}  [{now_str}]")

        my_items = items_for_tag(self._items_cache, tag)
        if not my_items:
            self.after(0, self._add_log, f"⚠ [{name}][{tag}] лоти не в кеші  {now_str}")
            self._advance_mkt_cycle(st, active_tags)
            return

        # check if we're already in top_n — if so, skip bump
        top_n   = mkt.get("top_n", 1)
        mkt_url = mkt.get("url", "")
        if mkt_url and top_n > 0:
            try:
                top_listings = fetch_listings(mkt_url, token, top_n)
                top_ids  = {lot["id"] for lot in top_listings[:top_n]}
                my_ids   = {str(it.get("item_id","")) for it in my_items}
                if top_ids & my_ids:
                    self.after(0, self._add_log,
                               f"✅ [{name}][{tag}] вже в топ-{top_n}, пропуск  {now_str}")
                    self.after(0, self.bump_status_var.set, f"✅ [{name}] в топ-{top_n}")
                    self._advance_mkt_cycle(st, active_tags)
                    return
            except Exception:
                pass  # if check fails — bump anyway

        MAX_TRIES    = 10
        total        = len(my_items)
        idx          = st["tag_idx"].get(tag, 0) % total
        bumped_id    = None
        tried        = 0
        limit_streak = 0

        while tried < MAX_TRIES:
            it    = my_items[idx]
            iid   = str(it.get("item_id", ""))
            title = item_title(it)
            ok, reason = bump_item(token, iid)
            tried += 1

            if ok:
                bumped_id = iid
                self._bump_count += 1
                self._last_auto_bumped = {iid}
                st["tag_idx"][tag] = (idx + 1) % total
                self.after(0, self._add_log, f"✅ [{name}][{tag}] {title}  {now_str}")
                self.after(0, self.bump_status_var.set, f"↑ [{name}] «{tag}»  {now_str}")
                break
            elif reason.startswith("продано/видалено"):
                self._items_cache = [x for x in self._items_cache
                                     if str(x.get("item_id","")) != iid]
                self.after(0, self._update_all_tag_counts)
                my_items = items_for_tag(self._items_cache, tag)
                total = len(my_items)
                self.after(0, self._add_log, f"🗑 [{name}][{tag}] {title} — {reason}  {now_str}")
                limit_streak = 0
                if total == 0:
                    break
                idx = idx % total
            elif reason.startswith("ліміт"):
                limit_streak += 1
                if limit_streak >= min(total, MAX_TRIES):
                    self.after(0, self._add_log,
                               f"🚫 [{name}][{tag}] ліміт на всіх лотах  {now_str}")
                    self.after(0, self.bump_status_var.set, f"🚫 [{name}] ліміт")
                    break
                idx = (idx + 1) % total
                st["tag_idx"][tag] = idx
            elif reason.startswith("кулдаун"):
                self.after(0, self.bump_status_var.set,
                           f"⏳ [{name}] «{tag}» кулдаун 3с…")
                time.sleep(3)
                idx = (idx + 1) % total
                st["tag_idx"][tag] = idx
            else:
                self.after(0, self._add_log,
                           f"⛔ [{name}][{tag}] {title} — {reason}  {now_str}")
                idx = (idx + 1) % total
                st["tag_idx"][tag] = idx
                break

        if not bumped_id:
            self._last_auto_bumped = set()
        self.after(0, self.bump_count_var.set, f"Підняттів: {self._bump_count}")
        self._advance_mkt_cycle(st, active_tags)

    def _advance_mkt_cycle(self, st: dict, active_tags: list):
        _, needed = active_tags[st["cycle_tag_idx"]]
        st["cycle_done"] += 1
        if st["cycle_done"] >= needed:
            st["cycle_tag_idx"] = (st["cycle_tag_idx"] + 1) % len(active_tags)
            st["cycle_done"]    = 0

    # ── Log ───────────────────────────────────────────────────────────────────
    def _add_log(self, *lines: str):
        self._cycle_log = (list(lines) + self._cycle_log)[:200]
        self.after(0, self._refresh_log)

    def _refresh_log(self):
        box = self.cycle_log_box
        box.config(state="normal")
        box.delete("1.0", "end")
        box.insert("end", "\n".join(self._cycle_log))
        box.see("1.0")
        box.config(state="disabled")

    def _copy_logs(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self._cycle_log))
        self._add_log("✅ Логи скопійовано")

    # ── Populate ─────────────────────────────────────────────────────────────
    def _populate(self, listings: list[dict]):
        self._listing_data = listings
        try:
            events = record_snapshot(listings, self._last_auto_bumped)
        except Exception as exc:
            events = []
            self.status_var.set(f"⚠ БД: {exc}")

        sel = self.tree.focus()
        for r in self.tree.get_children():
            self.tree.delete(r)

        for i, lot in enumerate(listings, 1):
            is_mine     = lot["is_mine"]
            auto_bumped = lot["id"] in self._last_auto_bumped
            if is_mine and auto_bumped:
                tag = "mine_bump"
            elif is_mine:
                tag = "mine"
            else:
                tag = "other"
            self.tree.insert("","end", iid=str(i-1), tags=(tag,),
                             values=(i, lot["title"], lot["seller"], lot["price"],
                                     "✅" if lot["is_pinned"] else "⬜",
                                     "🔒" if lot["is_closed"] else "",
                                     fmt_time(lot["bumped_at"]),
                                     "↑✓" if auto_bumped else ("✓" if is_mine else "")))

        mine_c   = sum(1 for l in listings if l["is_mine"])
        pinned_c = sum(1 for l in listings if l["is_pinned"])
        now_s    = datetime.now().strftime("%H:%M:%S")
        self.status_var.set(
            f"Оновлено: {now_s}   •   Лотів: {len(listings)}   •   "
            f"Мої: {mine_c}   •   Закріплені: {pinned_c}   •   "
            f"Нових підняттів: {len(events)}")

        if sel and self.tree.exists(sel):
            self.tree.focus(sel)
            self.tree.selection_set(sel)

        self._refresh_stats()

    def _refresh_stats(self):
        s = stat_summary()
        sellers = s["sellers"]
        top3 = "—"
        if sellers:
            n    = min(3, len(sellers))
            top3 = round(sum(r[1] for r in sellers[:n]) / n, 1)
        self.stat_summary_var.set(
            f"Всього в базі: {s['total_all']}   •   Сьогодні: {s['total_today']}   •   "
            f"Мої: {s['my_today']}   •   Авто: {s['auto_today']}   •   "
            f"Продавців: {len(sellers)}   •   Топ-3 ср.: {top3}")
        for r in self.stat_tree.get_children():
            self.stat_tree.delete(r)
        conn = db_conn()
        for name, cnt, is_mine in sellers:
            total = conn.execute(
                "SELECT COUNT(*) FROM bumps WHERE seller_name=?", (name,)).fetchone()[0]
            tag = "mine" if is_mine else ""
            self.stat_tree.insert("","end",
                                  values=(name, cnt, total, "✓" if is_mine else ""),
                                  tags=(tag,) if tag else ())
        conn.close()

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        webbrowser.open(self._listing_data[int(item)]["link"])

    def _on_close(self):
        self._save_config()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
