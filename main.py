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


# ─── API: отримати лоти ───────────────────────────────────────────────────────
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
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "User-Agent": "LOLSOFT/1.0",
    }
    resp = requests.get(url_to_api(market_url), headers=headers, timeout=20)
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


def fetch_my_listings(token: str, tag: str) -> list[dict]:
    """Отримує МОЇ активні лоти (з фільтром по тегу якщо вказано)."""
    params = f"user_id={MY_PROFILE_ID}&status=active"
    if tag.strip():
        params += f"&tag={urllib.parse.quote(tag.strip())}"
    url = f"{API_BASE}/items?{params}"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "User-Agent": "LOLSOFT/1.0",
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [str(it.get("item_id", "")) for it in items if it.get("item_id")]


def bump_item(token: str, item_id: str) -> bool:
    """Підіймає лот через API. Повертає True якщо успішно."""
    url = f"{API_BASE}/{item_id}/bump"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "User-Agent": "LOLSOFT/1.0",
    }
    try:
        resp = requests.post(url, headers=headers, timeout=15)
        return resp.status_code in (200, 201)
    except Exception:
        return False


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
    # міграція: додаємо колонку якщо старий DB
    try:
        conn.execute("ALTER TABLE bumps ADD COLUMN auto_bumped INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    return conn


_prev_ids: set[str] = set()
_first_scan_done: bool = False


def record_snapshot(listings: list[dict], auto_bumped_ids: set[str] = None) -> list[dict]:
    global _prev_ids, _first_scan_done
    auto_bumped_ids = auto_bumped_ids or set()

    conn = db_conn()
    now_iso = datetime.now().isoformat(timespec="seconds")
    events = []
    current_ids = {lot["id"] for lot in listings if lot["id"]}

    if not _first_scan_done:
        _prev_ids = current_ids
        _first_scan_done = True
        conn.close()
        return []

    new_ids = current_ids - _prev_ids
    for lot in listings:
        if lot["id"] not in new_ids:
            continue
        conn.execute(
            "INSERT INTO bumps (item_id, seller_id, seller_name, bumped_at, "
            "logged_at, is_mine, is_pinned, auto_bumped) VALUES (?,?,?,?,?,?,?,?)",
            (lot["id"], lot.get("seller_id",""), lot.get("seller","?"),
             lot.get("bumped_at"), now_iso,
             int(lot.get("is_mine", False)), int(lot.get("is_pinned", False)),
             int(lot["id"] in auto_bumped_ids))
        )
        events.append({
            "seller":      lot.get("seller", "?"),
            "title":       lot["title"],
            "item_id":     lot["id"],
            "logged_at":   now_iso,
            "is_mine":     lot.get("is_mine", False),
            "auto_bumped": lot["id"] in auto_bumped_ids,
        })

    _prev_ids = current_ids
    conn.commit()
    conn.close()
    return events


def today_iso() -> str:
    return datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")


def stat_summary() -> dict:
    conn = db_conn()
    t = today_iso()
    total_all    = conn.execute("SELECT COUNT(*) FROM bumps").fetchone()[0]
    total_today  = conn.execute(
        "SELECT COUNT(*) FROM bumps WHERE logged_at>=?", (t,)).fetchone()[0]
    my_today     = conn.execute(
        "SELECT COUNT(*) FROM bumps WHERE is_mine=1 AND logged_at>=?", (t,)).fetchone()[0]
    auto_today   = conn.execute(
        "SELECT COUNT(*) FROM bumps WHERE auto_bumped=1 AND logged_at>=?", (t,)).fetchone()[0]
    sellers      = conn.execute(
        "SELECT seller_name, COUNT(*), MAX(is_mine) FROM bumps "
        "WHERE logged_at>=? GROUP BY seller_id ORDER BY COUNT(*) DESC", (t,)
    ).fetchall()
    conn.close()
    return {
        "total_all":   total_all,
        "total_today": total_today,
        "my_today":    my_today,
        "auto_today":  auto_today,
        "sellers":     sellers,
    }


# ─── App ──────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Market Аналізатор")
        self.geometry("1120x760")
        self.configure(bg=BG)
        self._listing_data: list[dict] = []
        self._auto_job         = None
        self._show_token       = False
        self._bump_count       = 0
        self._last_auto_bumped: set[str] = set()
        # стан циклу підняттів: індекс поточного тега і скільки разів вже підняли за поточний крок
        self._cycle_tag_idx    = 0
        self._cycle_done_count = 0
        self._build_ui()

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Шапка
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
        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(
            tr, textvariable=self.token_var, show="•",
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI", 10), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(6, 6), ipady=4)
        tk.Button(tr, text="👁", command=self._toggle_token,
                  bg=CARD, fg=TEXT, relief="flat", cursor="hand2",
                  activebackground=BORDER).pack(side="left", padx=(0, 6))
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
                 ).pack(side="left", fill="x", expand=True, padx=(6, 6), ipady=4)
        tk.Label(ur, text="Лотів:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.count_var = tk.IntVar(value=10)
        tk.Spinbox(ur, from_=1, to=50, textvariable=self.count_var, width=4,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4, 8))
        tk.Label(ur, text="Авто:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_var = tk.IntVar(value=60)
        tk.Spinbox(ur, from_=10, to=3600, textvariable=self.interval_var, width=5,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4, 2))
        tk.Label(ur, text="сек", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))
        self.auto_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ur, text="Вкл", variable=self.auto_var,
                       command=self._toggle_auto,
                       bg=BG, fg=SUBTEXT, selectcolor=CARD,
                       activebackground=BG, activeforeground=ACCENT,
                       font=("Segoe UI", 10), cursor="hand2",
                       ).pack(side="left", padx=(0, 8))
        self.search_btn = tk.Button(
            ur, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg="#1a1a1a", activebackground=ACCENT_D,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=8, pady=4, cursor="hand2",
        )
        self.search_btn.pack(side="left")

        # ── Авто-підняття: заголовок + увімкнення ──
        bump_header = tk.Frame(self, bg=BG)
        bump_header.pack(fill="x", padx=16, pady=(6, 2))

        self.autobump_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bump_header, text="Авто-підняття (цикл по тегах)", variable=self.autobump_var,
            bg=BG, fg=YELLOW, selectcolor=CARD,
            activebackground=BG, activeforeground=YELLOW,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        ).pack(side="left", padx=(0, 16))

        self.bump_count_var = tk.StringVar(value="Підняттів скриптом: 0")
        tk.Label(bump_header, textvariable=self.bump_count_var,
                 bg=BG, fg=YELLOW, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 12))

        self.cycle_status_var = tk.StringVar(value="")
        tk.Label(bump_header, textvariable=self.cycle_status_var,
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 9)).pack(side="left")

        # ── 3 рядки тегів ──
        self.tags_cfg: list[tuple[tk.StringVar, tk.IntVar]] = []

        tags_frame = tk.Frame(self, bg=BG)
        tags_frame.pack(fill="x", padx=16, pady=(2, 2))

        for i in range(3):
            row = tk.Frame(tags_frame, bg=BG)
            row.pack(fill="x", pady=1)

            tk.Label(row, text=f"Тег {i+1}:", bg=BG, fg=SUBTEXT,
                     font=("Segoe UI", 10), width=6, anchor="w").pack(side="left")

            tag_var = tk.StringVar(value="")
            tk.Entry(row, textvariable=tag_var, width=20,
                     bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=("Segoe UI", 10), highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT,
                     ).pack(side="left", padx=(4, 8), ipady=3)

            tk.Label(row, text="підняттів за цикл:", bg=BG, fg=SUBTEXT,
                     font=("Segoe UI", 10)).pack(side="left")
            count_var = tk.IntVar(value=1)
            tk.Spinbox(row, from_=1, to=50, textvariable=count_var, width=4,
                       bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                       font=("Segoe UI", 10)).pack(side="left", padx=(4, 0))

            self.tags_cfg.append((tag_var, count_var))

        self.bump_status_var = tk.StringVar(value="")
        tk.Label(bump_row, textvariable=self.bump_status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(side="left")

        # Статус
        self.status_var = tk.StringVar(value="Введи токен і натисни «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(4, 2))

        # ── PanedWindow: велика таблиця зверху, статистика знизу ──
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=BG,
                               sashwidth=5, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Таблиця лотів (2/3 висоти)
        top_frame = tk.Frame(paned, bg=BORDER)
        paned.add(top_frame, minsize=300)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=30, borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background=CARD, foreground=ACCENT, relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2f3a52")],
                              foreground=[("selected", "#ffffff")])
        style.configure("Stats.Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=24, borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Stats.Treeview.Heading",
                        background=CARD, foreground=ACCENT, relief="flat",
                        font=("Segoe UI", 9, "bold"))

        cols = ("№", "Назва", "Продавець", "Ціна", "📌", "🔒", "Піднято", "Мій")
        self.tree = ttk.Treeview(top_frame, columns=cols,
                                 show="headings", selectmode="browse")
        for c, w, a in zip(cols,
                           [32, 320, 160, 100, 38, 38, 110, 50],
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

        # Статистика (1/3 висоти)
        bot_frame = tk.Frame(paned, bg=BG)
        paned.add(bot_frame, minsize=130)

        self.stat_summary_var = tk.StringVar(value="—")
        tk.Label(bot_frame, textvariable=self.stat_summary_var,
                 bg=BG, fg=TEXT, font=("Segoe UI", 9), anchor="w", justify="left"
                 ).pack(anchor="w", padx=8, pady=(4, 4))

        stat_wrap = tk.Frame(bot_frame, bg=BORDER)
        stat_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        scols = ("Продавець", "Підняттів сьогодні", "Всього в базі", "Мій")
        self.stat_tree = ttk.Treeview(stat_wrap, columns=scols,
                                      show="headings", style="Stats.Treeview")
        for c, w in zip(scols, [200, 150, 120, 50]):
            self.stat_tree.heading(c, text=c)
            self.stat_tree.column(c, width=w,
                                  anchor="w" if c == "Продавець" else "center")
        sb2 = ttk.Scrollbar(stat_wrap, orient="vertical", command=self.stat_tree.yview)
        self.stat_tree.configure(yscrollcommand=sb2.set)
        self.stat_tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb2.pack(side="right", fill="y")
        self.stat_tree.tag_configure("mine", background="#15311f", foreground=GREEN)

        self._enable_paste(self.token_entry)

    # ── Пасте + токен ────────────────────────────────────────────────────────
    def _enable_paste(self, entry: tk.Entry):
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT,
                       activebackground=ACCENT, activeforeground="#1a1a1a")
        menu.add_command(label="Вставити", command=lambda: self._paste_into(entry))
        menu.add_command(label="Очистити", command=lambda: entry.delete(0, "end"))
        entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        def ctrl_key(e):
            if e.keycode in (86, 55) and (e.state & 0x4):
                self._paste_into(entry)
                return "break"
        entry.bind("<Control-KeyPress>", ctrl_key)

    def _paste_into(self, entry: tk.Entry):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        try:
            entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", text.strip())

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
            max(10, self.interval_var.get()) * 1000, self._auto_tick
        )

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
                self.count_var.get(),
            )
            self.after(0, self._populate, listings)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    # ── Авто-підняття (цикл по тегах) ───────────────────────────────────────
    def _do_auto_bump(self, listings: list[dict]):
        """
        Цикл: тег1 → N разів, тег2 → M разів, тег3 → K разів → знову тег1.
        На кожному скані виконує один крок циклу (одне підняття одного тега).
        """
        token = self.token_var.get().strip()

        # збираємо активні теги (ті де є назва)
        active_tags = [
            (tag_var.get().strip(), count_var.get())
            for tag_var, count_var in self.tags_cfg
            if tag_var.get().strip()
        ]

        if not active_tags:
            self.after(0, self.cycle_status_var.set, "⚠ Вкажи хоча б один тег")
            return

        # поточний тег у циклі
        if self._cycle_tag_idx >= len(active_tags):
            self._cycle_tag_idx = 0
            self._cycle_done_count = 0

        tag, bumps_needed = active_tags[self._cycle_tag_idx]

        now_str = datetime.now().strftime("%H:%M:%S")
        self.after(0, self.cycle_status_var.set,
                   f"Крок: тег «{tag}» {self._cycle_done_count+1}/{bumps_needed}  [{now_str}]")

        # підняти лоти з цим тегом
        try:
            my_ids = fetch_my_listings(token, tag)
        except Exception as exc:
            self.after(0, self.bump_status_var.set, f"⚠ Помилка API: {exc}")
            return

        if not my_ids:
            self.after(0, self.bump_status_var.set, f"Лоти з тегом «{tag}» не знайдено")
            # пропускаємо цей тег
            self._advance_cycle(active_tags)
            return

        bumped_ids = set()
        for item_id in my_ids:
            if bump_item(token, item_id):
                bumped_ids.add(item_id)
                self._bump_count += 1

        self._last_auto_bumped = bumped_ids

        self.after(0, self.bump_count_var.set, f"Підняттів скриптом: {self._bump_count}")

        if bumped_ids:
            self.after(0, self.bump_status_var.set,
                       f"↑ «{tag}»: підняв {len(bumped_ids)} лот(ів) о {now_str}")
        else:
            self.after(0, self.bump_status_var.set,
                       f"⚠ «{tag}»: кулдаун або помилка API")

        self._advance_cycle(active_tags)

    def _advance_cycle(self, active_tags: list[tuple]):
        """Рухає лічильник циклу вперед."""
        _, bumps_needed = active_tags[self._cycle_tag_idx]
        self._cycle_done_count += 1
        if self._cycle_done_count >= bumps_needed:
            self._cycle_tag_idx = (self._cycle_tag_idx + 1) % len(active_tags)
            self._cycle_done_count = 0

    # ── Populate ─────────────────────────────────────────────────────────────
    def _populate(self, listings: list[dict]):
        self._listing_data = listings

        # авто-підняття в окремому потоці щоб не блокувати UI
        if self.autobump_var.get():
            threading.Thread(
                target=self._do_auto_bump, args=(listings,), daemon=True
            ).start()

        try:
            events = record_snapshot(listings, self._last_auto_bumped)
        except Exception as exc:
            events = []
            self.status_var.set(f"⚠ База статистики: {exc}")

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

            self.tree.insert("", "end", iid=str(i - 1), tags=(tag,),
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
            f"Нових підняттів виявлено: {len(events)}"
        )
        self.search_btn.config(state="normal")

        if sel and self.tree.exists(sel):
            self.tree.focus(sel)
            self.tree.selection_set(sel)

        self._refresh_stats()

    def _refresh_stats(self):
        s = stat_summary()
        sellers_today = s["sellers"]
        market_scale  = len(sellers_today)
        top3_avg = "—"
        if sellers_today:
            top_n    = min(3, len(sellers_today))
            top3_avg = round(sum(r[1] for r in sellers_today[:top_n]) / top_n, 1)

        self.stat_summary_var.set(
            f"Всього в базі: {s['total_all']}   •   "
            f"Сьогодні по всіх: {s['total_today']}   •   "
            f"Мої сьогодні: {s['my_today']}   •   "
            f"Авто-підняттів сьогодні: {s['auto_today']}   •   "
            f"Продавців у видачі: {market_scale}   •   "
            f"Топ-3 середнє/день: {top3_avg}"
        )

        for r in self.stat_tree.get_children():
            self.stat_tree.delete(r)

        conn = db_conn()
        for name, count_today, is_mine in sellers_today:
            total_all = conn.execute(
                "SELECT COUNT(*) FROM bumps WHERE seller_name=?", (name,)
            ).fetchone()[0]
            tag = "mine" if is_mine else ""
            self.stat_tree.insert("", "end",
                                  values=(name, count_today, total_all,
                                          "✓" if is_mine else ""),
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
