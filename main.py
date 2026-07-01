import tkinter as tk
from tkinter import ttk, messagebox
import threading
import urllib.parse
import sqlite3
import requests
import webbrowser
from pathlib import Path
from datetime import datetime

MY_PROFILE_ID = "9542364"
API_BASE      = "https://prod-api.lzt.market"
DB_PATH       = Path(__file__).parent / "lolsoft_stats.db"
CACHE_REFRESH  = 5 * 60 * 1000   # 5 хвилин у мс

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


_tag_id_map: dict[str, int] = {}   # title → tag_id


def fetch_my_tags(token: str) -> tuple[list[str], str]:
    """Завантажує теги з /me. Повертає (назви, raw debug)."""
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
    """Завантажує ВСІ активні лоти юзера через /user/items з пагінацією."""
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
    """Завантажує лоти конкретного тегу через /user/items?tag_id=X."""
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


# кеш по тегах: {tag_name: [items]}
_tag_items_cache: dict[str, list[dict]] = {}


def items_for_tag(cache: list[dict], tag: str) -> list[dict]:
    """Повертає лоти для тегу з tag-кешу."""
    return list(_tag_items_cache.get(tag.strip(), []))


def item_title(it: dict) -> str:
    iid = str(it.get("item_id", ""))
    return it.get("title") or it.get("title_en") or f"#{iid}"


def bump_item(token: str, item_id: str) -> tuple[bool, str]:
    """Повертає (успіх, причина). Також розпізнає продані лоти."""
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
        if code in (403, 404) or "not found" in ml or "deleted" in ml or "sold" in ml:
            return False, "продано/видалено"
        if code == 429 or "cooldown" in ml or "flood" in ml or "wait" in ml:
            return False, "кулдаун"
        if "limit" in ml or ("bump" in ml and "0" in ml):
            return False, f"ліміт: {msg[:60]}"
        return False, msg[:80] or f"HTTP {code}"
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


_prev_ids: set[str]  = set()
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
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Market Аналізатор")
        self.geometry("1320x860")
        self.configure(bg=BG)
        self._listing_data: list[dict]  = []
        self._auto_job                  = None
        self._cache_job                 = None
        self._show_token                = False
        self._bump_count                = 0
        self._last_auto_bumped: set[str]= set()
        self._cycle_tag_idx             = 0
        self._cycle_done_count          = 0
        self._cycle_log: list[str]      = []
        # кеш ВСІХ моїх активних лотів (оновлюється кожні 5 хв)
        self._items_cache: list[dict]   = []
        self._cache_ts: str             = "не завантажено"
        self._build_ui()

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(hdr, text="LOLSOFT", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(hdr, text="  Market Аналізатор", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")

        # Токен
        tr = tk.Frame(self, bg=BG)
        tr.pack(fill="x", padx=16, pady=(2, 2))
        tk.Label(tr, text="Токен:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.token_var   = tk.StringVar()
        self.token_entry = tk.Entry(
            tr, textvariable=self.token_var, show="•",
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI", 10), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(6,6), ipady=4)
        tk.Button(tr, text="👁", command=self._toggle_token,
                  bg=CARD, fg=TEXT, relief="flat", cursor="hand2",
                  activebackground=BORDER).pack(side="left", padx=(0,6))
        tk.Button(tr, text="Де взяти?",
                  command=lambda: webbrowser.open("https://lzt.market/account/api"),
                  bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
                  font=("Segoe UI", 9), activebackground=BORDER).pack(side="left")

        # URL + лоти + авто-скан
        ur = tk.Frame(self, bg=BG)
        ur.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(ur, text="URL:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        tk.Entry(ur, textvariable=self.url_var,
                 bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 10), highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT,
                 ).pack(side="left", fill="x", expand=True, padx=(6,6), ipady=4)
        tk.Label(ur, text="Лотів:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.count_var = tk.IntVar(value=10)
        tk.Spinbox(ur, from_=1, to=50, textvariable=self.count_var, width=4,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4,8))
        tk.Label(ur, text="Авто:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_var = tk.IntVar(value=60)
        tk.Spinbox(ur, from_=10, to=3600, textvariable=self.interval_var, width=5,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4,2))
        tk.Label(ur, text="сек", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0,8))
        self.auto_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ur, text="Вкл", variable=self.auto_var,
                       command=self._toggle_auto, bg=BG, fg=SUBTEXT,
                       selectcolor=CARD, activebackground=BG,
                       activeforeground=ACCENT, font=("Segoe UI", 10),
                       cursor="hand2").pack(side="left", padx=(0,8))
        self.search_btn = tk.Button(
            ur, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg="#1a1a1a", activebackground=ACCENT_D,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=8, pady=4, cursor="hand2")
        self.search_btn.pack(side="left")

        # ── Рядок авто-підняття ──
        bh = tk.Frame(self, bg=BG)
        bh.pack(fill="x", padx=16, pady=(6, 2))

        self.autobump_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bh, text="Авто-підняття", variable=self.autobump_var,
                       bg=BG, fg=YELLOW, selectcolor=CARD,
                       activebackground=BG, activeforeground=YELLOW,
                       font=("Segoe UI", 10, "bold"), cursor="hand2",
                       ).pack(side="left", padx=(0,8))

        self.bump_count_var = tk.StringVar(value="Підняттів: 0")
        tk.Label(bh, textvariable=self.bump_count_var,
                 bg=BG, fg=YELLOW, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0,10))

        tk.Label(bh, text="Моїх у топ:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0,2))
        self.top_n_var = tk.IntVar(value=1)
        tk.Spinbox(bh, from_=1, to=20, textvariable=self.top_n_var, width=3,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 9)).pack(side="left", padx=(0,10))

        # кнопки завантаження
        self.load_tags_btn = tk.Button(
            bh, text="🔄 Теги", command=self._load_tags,
            bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
            font=("Segoe UI", 9), activebackground=BORDER)
        self.load_tags_btn.pack(side="left", padx=(0,6))

        self.load_items_btn = tk.Button(
            bh, text="📦 Завантажити лоти", command=self._load_items,
            bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
            font=("Segoe UI", 9), activebackground=BORDER)
        self.load_items_btn.pack(side="left")

        self.cycle_status_var = tk.StringVar(value="")
        self.cache_info_var   = tk.StringVar(value="")

        # ── Блок тегів + лог ──
        bump_body = tk.Frame(self, bg=BG)
        bump_body.pack(fill="x", padx=16, pady=(2,4))

        tags_frame = tk.Frame(bump_body, bg=BG)
        tags_frame.pack(side="left", fill="y")

        self.tags_cfg: list[tuple[tk.StringVar, tk.IntVar]] = []
        self._tag_count_vars: list[tk.StringVar] = []

        for i in range(3):
            row = tk.Frame(tags_frame, bg=BG)
            row.pack(fill="x", pady=2)

            tk.Label(row, text=f"Тег {i+1}:", bg=BG, fg=SUBTEXT,
                     font=("Segoe UI", 10), width=6, anchor="w").pack(side="left")

            tag_var = tk.StringVar(value="")
            cb = ttk.Combobox(row, textvariable=tag_var, width=24,
                              font=("Segoe UI", 10), state="normal")
            cb.pack(side="left", padx=(4,4), ipady=3)

            cnt_lbl_var = tk.StringVar(value="")
            tk.Label(row, textvariable=cnt_lbl_var, bg=BG, fg=ACCENT,
                     font=("Segoe UI", 9)).pack(side="left", padx=(0,8))
            self._tag_count_vars.append(cnt_lbl_var)

            tk.Label(row, text="підн/цикл:", bg=BG, fg=SUBTEXT,
                     font=("Segoe UI", 9)).pack(side="left")
            count_var = tk.IntVar(value=1)
            tk.Spinbox(row, from_=1, to=50, textvariable=count_var, width=4,
                       bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                       font=("Segoe UI", 10)).pack(side="left", padx=(4,0))

            self.tags_cfg.append((tag_var, count_var))
            # оновлювати лічильник при зміні тегу
            tag_var.trace_add("write", lambda *_, idx=i: self._update_tag_count(idx))

        self.bump_status_var = tk.StringVar(value="")
        tk.Label(tags_frame, textvariable=self.bump_status_var,
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 9)).pack(anchor="w", padx=2, pady=(2,0))

        # панель логів
        cycle_panel = tk.Frame(bump_body, bg=CARD, highlightthickness=1,
                               highlightbackground=BORDER)
        cycle_panel.pack(side="left", fill="both", expand=True, padx=(16,0))

        ch = tk.Frame(cycle_panel, bg=CARD)
        ch.pack(fill="x", padx=4, pady=(4,2))
        tk.Label(ch, text="Лог підняттів", bg=CARD, fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)
        tk.Button(ch, text="📋 Копіювати", command=self._copy_logs,
                  bg=BORDER, fg=TEXT, relief="flat", cursor="hand2",
                  font=("Segoe UI", 8), activebackground=CARD).pack(side="right", padx=2)

        self.cycle_log_box = tk.Listbox(
            cycle_panel, bg=CARD, fg=TEXT, selectbackground=BORDER,
            relief="flat", font=("Consolas", 9), height=5,
            activestyle="none", highlightthickness=0)
        self.cycle_log_box.pack(fill="both", expand=True, padx=4, pady=(0,4))

        # Статус
        self.status_var = tk.StringVar(value="Введи токен і натисни «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(2,2))

        # ── PanedWindow: лоти ліво, статистика право ──
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg=BG,
                               sashwidth=5, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=10, pady=(0,10))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=28, borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=CARD, foreground=ACCENT,
                        relief="flat", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected","#2f3a52")],
                              foreground=[("selected","#ffffff")])
        style.configure("Stats.Treeview", background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=26, borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Stats.Treeview.Heading", background=CARD, foreground=ACCENT,
                        relief="flat", font=("Segoe UI", 9, "bold"))

        top_frame = tk.Frame(paned, bg=BORDER)
        paned.add(top_frame, minsize=420)
        cols = ("№","Назва","Продавець","Ціна","📌","🔒","Піднято","Мій")
        self.tree = ttk.Treeview(top_frame, columns=cols,
                                 show="headings", selectmode="browse")
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
        paned.add(stat_frame, minsize=300)
        self.stat_summary_var = tk.StringVar(value="—")
        tk.Label(stat_frame, textvariable=self.stat_summary_var,
                 bg=BG, fg=TEXT, font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=400,
                 ).pack(anchor="w", padx=8, pady=(4,4))
        stat_wrap = tk.Frame(stat_frame, bg=BORDER)
        stat_wrap.pack(fill="both", expand=True, padx=8, pady=(0,4))
        scols = ("Продавець","Сьогодні","Всього","Мій")
        self.stat_tree = ttk.Treeview(stat_wrap, columns=scols,
                                      show="headings", style="Stats.Treeview")
        for c, w in zip(scols, [190,80,70,40]):
            self.stat_tree.heading(c, text=c)
            self.stat_tree.column(c, width=w,
                                  anchor="w" if c=="Продавець" else "center")
        sb2 = ttk.Scrollbar(stat_wrap, orient="vertical", command=self.stat_tree.yview)
        self.stat_tree.configure(yscrollcommand=sb2.set)
        self.stat_tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb2.pack(side="right", fill="y")
        self.stat_tree.tag_configure("mine", background="#15311f", foreground=GREEN)

        self._enable_paste(self.token_entry)

    # ── Кеш лотів ────────────────────────────────────────────────────────────
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
                self.after(0, lambda: self.load_items_btn.config(text="Завантаження тегів…"))
                fetch_my_tags(token)

            self.after(0, lambda: self.load_items_btn.config(text="Завантаження лотів…"))
            all_items = fetch_all_my_items(token)

            tag_cache: dict[str, list[dict]] = {}
            total_tags = len(_tag_id_map)
            for i, (tag_name, tag_id) in enumerate(_tag_id_map.items(), 1):
                self.after(0, lambda i=i, n=tag_name: self.load_items_btn.config(
                    text=f"Тег {i}/{total_tags}: {n[:15]}…"))
                tag_cache[tag_name] = fetch_items_by_tag(token, tag_id)

            _tag_items_cache = tag_cache
            self.after(0, self._apply_items_cache, all_items)
        except Exception as exc:
            self.after(0, self._cache_error, str(exc))

    def _apply_items_cache(self, items: list[dict]):
        self._items_cache = items
        self._cache_ts    = datetime.now().strftime("%H:%M:%S")
        total = len(items)

        self._update_all_tag_counts()

        self.load_items_btn.config(state="normal", text="📦 Завантажити лоти")
        tag_counts = ", ".join(
            f"{n}:{len(v)}" for n, v in _tag_items_cache.items() if v
        )
        self._add_log(f"📦 Кеш: {total} лотів [{self._cache_ts}]  теги: {tag_counts or '—'}")

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

    def _update_tag_count(self, idx: int):
        if not self._items_cache:
            return
        tag = self.tags_cfg[idx][0].get().strip()
        if tag:
            n = len(items_for_tag(self._items_cache, tag))
            self._tag_count_vars[idx].set(f"[{n} лот.]")
        else:
            self._tag_count_vars[idx].set("")

    def _update_all_tag_counts(self):
        for i in range(3):
            self._update_tag_count(i)

    # ── Завантажити теги ──────────────────────────────────────────────────────
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
            self.after(0, self.load_tags_btn.config, {"state":"normal","text":"🔄 Теги"})

    def _apply_tags(self, tags: list[str], raw: str = ""):
        cbs = self._find_comboboxes(self)
        for cb in cbs:
            cb["values"] = tags
        self.load_tags_btn.config(state="normal", text="🔄 Теги")
        if tags:
            self._add_log(f"🏷 Теги завантажено: {', '.join(tags)}")
        else:
            self._add_log(f"⚠ Теги не знайдено. Raw: {raw[:100]}")
        self._update_all_tag_counts()

    def _find_comboboxes(self, parent) -> list:
        found = []
        for w in parent.winfo_children():
            if isinstance(w, ttk.Combobox):
                found.append(w)
            found += self._find_comboboxes(w)
        return found

    # ── Paste / token ─────────────────────────────────────────────────────────
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
        # залишаємо тільки перший рядок (захист від випадкової вставки коду)
        txt = txt.splitlines()[0].strip() if txt.strip() else ""
        try:
            entry.delete("sel.first","sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", txt)

    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "•")

    # ── Авто-скан ────────────────────────────────────────────────────────────
    def _toggle_auto(self):
        if self.auto_var.get():
            self._schedule_auto()
        elif self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None

    def _schedule_auto(self):
        self._auto_job = self.after(
            max(10, self.interval_var.get()) * 1000, self._auto_tick)

    def _auto_tick(self):
        if self.auto_var.get():
            self._start_search(silent=True)
            self._schedule_auto()

    # ── Пошук ────────────────────────────────────────────────────────────────
    def _start_search(self, silent=False):
        if not self.token_var.get().strip():
            if not silent:
                messagebox.showwarning("Немає токена", "Спочатку встав API-токен.")
            return
        self.search_btn.config(state="disabled")
        self.status_var.set("Завантаження…")
        threading.Thread(target=self._do_search, daemon=True).start()

    def _do_search(self):
        try:
            listings = fetch_listings(
                self.url_var.get().strip(),
                self.token_var.get().strip(),
                self.count_var.get())
            self.after(0, self._populate, listings)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    # ── Авто-підняття ────────────────────────────────────────────────────────
    def _do_auto_bump(self, listings: list[dict]):
        token = self.token_var.get().strip()

        active_tags = [
            (tv.get().strip(), cv.get())
            for tv, cv in self.tags_cfg if tv.get().strip()
        ]
        if not active_tags:
            self.after(0, self._add_log, "⚠ Вкажи хоча б один тег")
            return

        # якщо кеш порожній — завантажуємо
        global _tag_items_cache
        if not _tag_items_cache:
            try:
                if not _tag_id_map:
                    fetch_my_tags(token)
                all_items = fetch_all_my_items(token)
                tag_cache: dict[str, list[dict]] = {}
                for tag_name, tag_id in _tag_id_map.items():
                    tag_cache[tag_name] = fetch_items_by_tag(token, tag_id)
                _tag_items_cache = tag_cache
                self.after(0, self._apply_items_cache, all_items)
            except Exception as exc:
                self.after(0, self._add_log, f"⚠ Не вдалось завантажити лоти: {exc}")
                return

        if self._cycle_tag_idx >= len(active_tags):
            self._cycle_tag_idx    = 0
            self._cycle_done_count = 0

        tag, bumps_needed = active_tags[self._cycle_tag_idx]
        now_str = datetime.now().strftime("%H:%M:%S")
        self.after(0, self._add_log,
                   f"▶ Крок: «{tag}» {self._cycle_done_count+1}/{bumps_needed}  [{now_str}]")

        my_items = items_for_tag(self._items_cache, tag)

        if not my_items:
            line = f"⚠ [{tag}] лоти не знайдено в кеші  {now_str}"
            self._add_log(line)
            self.after(0, self.bump_status_var.set, line)
            self._advance_cycle(active_tags)
            return

        # перебираємо лоти поки один не піднімється
        log_lines  = []
        bumped_id  = None

        for it in my_items:
            iid   = str(it.get("item_id", ""))
            title = item_title(it)
            ok, reason = bump_item(token, iid)

            if ok:
                bumped_id = iid
                self._bump_count += 1
                self._last_auto_bumped = {iid}
                log_lines.append(f"✅ [{tag}] {title}  {now_str}")
                break
            elif reason == "продано/видалено":
                # прибираємо з кешу
                self._items_cache = [x for x in self._items_cache
                                     if str(x.get("item_id","")) != iid]
                log_lines.append(f"🗑 [{tag}] {title} — продано/видалено  {now_str}")
                self.after(0, self._update_all_tag_counts)
            else:
                log_lines.append(f"⛔ [{tag}] {title} — {reason}  {now_str}")

        if not bumped_id:
            self._last_auto_bumped = set()

        self._add_log(*log_lines)
        self.after(0, self.bump_count_var.set, f"Підняттів: {self._bump_count}")

        if bumped_id:
            self.after(0, self.bump_status_var.set, f"↑ «{tag}» підняв лот  {now_str}")
        else:
            self.after(0, self.bump_status_var.set,
                       f"⛔ «{tag}»: всі лоти на кулдауні / проблема")

        self._advance_cycle(active_tags)

    def _advance_cycle(self, active_tags):
        _, needed = active_tags[self._cycle_tag_idx]
        self._cycle_done_count += 1
        if self._cycle_done_count >= needed:
            self._cycle_tag_idx    = (self._cycle_tag_idx + 1) % len(active_tags)
            self._cycle_done_count = 0

    def _add_log(self, *lines: str):
        self._cycle_log = (list(lines) + self._cycle_log)[:100]
        self.after(0, self._refresh_cycle_log)

    def _refresh_cycle_log(self):
        self.cycle_log_box.delete(0, "end")
        for line in self._cycle_log:
            self.cycle_log_box.insert("end", line)

    def _copy_logs(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self._cycle_log))
        self._add_log("✅ Логи скопійовано")

    # ── Populate ─────────────────────────────────────────────────────────────
    def _populate(self, listings: list[dict]):
        self._listing_data = listings

        if self.autobump_var.get():
            top_n = self.top_n_var.get()
            mine_in_top = sum(1 for l in listings[:top_n] if l["is_mine"])
            if mine_in_top >= top_n:
                now_s = datetime.now().strftime("%H:%M:%S")
                self._add_log(f"✅ Топ-{top_n} вже наш — бамп пропущено  [{now_s}]")
            else:
                threading.Thread(
                    target=self._do_auto_bump, args=(listings,), daemon=True).start()

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
        self.search_btn.config(state="normal")

        if sel and self.tree.exists(sel):
            self.tree.focus(sel)
            self.tree.selection_set(sel)

        self._refresh_stats()

    def _refresh_stats(self):
        s = stat_summary()
        sellers = s["sellers"]
        top3    = "—"
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

    def _show_error(self, msg: str):
        self.status_var.set(f"Помилка: {msg}")
        self.search_btn.config(state="normal")

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        webbrowser.open(self._listing_data[int(item)]["link"])


if __name__ == "__main__":
    App().mainloop()
