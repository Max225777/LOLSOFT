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
API_BASE = "https://prod-api.lzt.market"
DB_PATH = Path(__file__).parent / "lolsoft_stats.db"

DEFAULT_URL = (
    "https://lzt.market/telegram/"
    "?origin[]=autoreg&origin[]=self_registration&country[]=UA&spam=no"
)

BG       = "#1a1a1a"
BG2      = "#202020"
CARD     = "#262626"
BORDER   = "#333333"
ACCENT   = "#a8c957"
ACCENT_D = "#8fae42"
GREEN    = "#a8c957"
YELLOW   = "#ffc857"
RED      = "#ff5c5c"
TEXT     = "#e6e6e6"
SUBTEXT  = "#888888"


# ── Мережа: запит до API ──────────────────────────────────────────────────────
def url_to_api(market_url: str) -> str:
    parts = urllib.parse.urlsplit(market_url)
    api_url = API_BASE + parts.path
    if parts.query:
        api_url += "?" + parts.query
    return api_url


def fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def fetch_listings(market_url: str, token: str, count: int = 10) -> list[dict]:
    api_url = url_to_api(market_url)
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/json",
        "User-Agent": "LOLSOFT/1.0",
    }
    resp = requests.get(api_url, headers=headers, timeout=20)
    if resp.status_code == 401:
        raise RuntimeError("Невірний або прострочений API-токен (401).")
    resp.raise_for_status()

    data = resp.json()
    items = data.get("items", [])

    parsed: list[dict] = []
    for it in items[:count]:
        item_id     = str(it.get("item_id", ""))
        title       = it.get("title") or it.get("title_en") or f"Лот #{item_id}"
        price       = it.get("price", "?")
        currency    = it.get("price_currency", "") or it.get("currency", "")
        price_str   = f"{price} {currency}".strip()

        seller      = it.get("seller") or {}
        seller_id   = str(seller.get("user_id", ""))
        seller_name = seller.get("username", "?")

        state       = it.get("item_state", "")
        is_closed   = state in ("sold", "closed", "deleted")
        is_pinned   = bool(it.get("is_sticky") or it.get("sticky"))
        is_mine     = seller_id == MY_PROFILE_ID

        bumped_at = (
            it.get("bumped_at")
            or it.get("up_timestamp")
            or it.get("refreshed_at")
            or it.get("updated_at")
        )

        parsed.append({
            "id":         item_id,
            "title":      title,
            "link":       f"https://lzt.market/{item_id}/",
            "seller":     seller_name,
            "seller_id":  seller_id,
            "price":      price_str,
            "is_mine":    is_mine,
            "is_pinned":  is_pinned,
            "is_closed":  is_closed,
            "bumped_at":  bumped_at,
        })
    return parsed


# ── Статистика: SQLite ────────────────────────────────────────────────────────
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bumps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            seller_id TEXT,
            seller_name TEXT,
            bumped_at INTEGER,
            logged_at TEXT NOT NULL,
            is_mine INTEGER NOT NULL,
            is_pinned INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS last_seen (
            item_id TEXT PRIMARY KEY,
            bumped_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bumps_seller ON bumps(seller_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bumps_logged ON bumps(logged_at)")
    return conn


def record_snapshot(listings: list[dict]) -> int:
    """Логує нові підняття (зміна bumped_at з попереднього скану). Повертає кількість нових."""
    conn = db_conn()
    new_bumps = 0
    now_iso = datetime.now().isoformat(timespec="seconds")

    for lot in listings:
        item_id = lot["id"]
        bumped_at = lot.get("bumped_at")
        if not item_id:
            continue

        row = conn.execute(
            "SELECT bumped_at FROM last_seen WHERE item_id = ?", (item_id,)
        ).fetchone()
        prev_bumped = row[0] if row else None
        is_new_bump = bool(bumped_at) and (row is None or bumped_at != prev_bumped)

        if is_new_bump:
            conn.execute(
                "INSERT INTO bumps (item_id, seller_id, seller_name, bumped_at, "
                "logged_at, is_mine, is_pinned) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, lot.get("seller_id", ""), lot.get("seller", "?"),
                 bumped_at, now_iso, int(lot.get("is_mine", False)),
                 int(lot.get("is_pinned", False))),
            )
            new_bumps += 1

        if row:
            conn.execute("UPDATE last_seen SET bumped_at = ? WHERE item_id = ?",
                        (bumped_at, item_id))
        else:
            conn.execute("INSERT INTO last_seen (item_id, bumped_at) VALUES (?, ?)",
                        (item_id, bumped_at))

    conn.commit()
    conn.close()
    return new_bumps


def today_start_iso() -> str:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def stat_total_bumps(since_iso: str | None = None) -> int:
    conn = db_conn()
    if since_iso:
        n = conn.execute("SELECT COUNT(*) FROM bumps WHERE logged_at >= ?", (since_iso,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM bumps").fetchone()[0]
    conn.close()
    return n


def stat_by_seller(since_iso: str | None = None) -> list[tuple]:
    conn = db_conn()
    if since_iso:
        rows = conn.execute(
            "SELECT seller_id, seller_name, COUNT(*), MAX(is_mine) "
            "FROM bumps WHERE logged_at >= ? GROUP BY seller_id ORDER BY COUNT(*) DESC",
            (since_iso,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT seller_id, seller_name, COUNT(*), MAX(is_mine) "
            "FROM bumps GROUP BY seller_id ORDER BY COUNT(*) DESC"
        ).fetchall()
    conn.close()
    return rows


def stat_by_hour(since_iso: str) -> list[tuple]:
    conn = db_conn()
    rows = conn.execute(
        "SELECT substr(logged_at, 12, 2) as hour, COUNT(*) "
        "FROM bumps WHERE logged_at >= ? GROUP BY hour ORDER BY hour",
        (since_iso,),
    ).fetchall()
    conn.close()
    return rows


def stat_by_day(limit_days: int = 14) -> list[tuple]:
    conn = db_conn()
    rows = conn.execute(
        "SELECT substr(logged_at, 1, 10) as day, COUNT(*) "
        "FROM bumps GROUP BY day ORDER BY day DESC LIMIT ?",
        (limit_days,),
    ).fetchall()
    conn.close()
    return list(reversed(rows))


def stat_my_today() -> int:
    conn = db_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM bumps WHERE is_mine = 1 AND logged_at >= ?",
        (today_start_iso(),),
    ).fetchone()[0]
    conn.close()
    return n


# ── Головне вікно ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Market Аналізатор")
        self.geometry("1100x640")
        self.configure(bg=BG)
        self._listing_data: list[dict] = []
        self._auto_job = None
        self._stats_win = None
        self._build_ui()

    def _build_ui(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="LOLSOFT", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(header, text="  Market Аналізатор", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 11)).pack(side="left")

        token_row = tk.Frame(self, bg=BG)
        token_row.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(token_row, text="API-токен:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(
            token_row, textvariable=self.token_var, show="•",
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Segoe UI", 10), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self.token_entry.pack(side="left", fill="x", expand=True, padx=(8, 6), ipady=5)
        self._show_token = False
        tk.Button(token_row, text="👁", command=self._toggle_token,
                  bg=CARD, fg=TEXT, relief="flat", cursor="hand2",
                  activebackground=BORDER).pack(side="left", padx=(0, 6))
        tk.Button(token_row, text="Де взяти?", command=self._open_token_help,
                  bg=CARD, fg=ACCENT, relief="flat", cursor="hand2",
                  font=("Segoe UI", 9), activebackground=BORDER).pack(side="left")

        url_row = tk.Frame(self, bg=BG)
        url_row.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(url_row, text="URL:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        tk.Entry(url_row, textvariable=self.url_var,
                 bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI", 10), highlightthickness=1,
                 highlightbackground=BORDER, highlightcolor=ACCENT,
                 ).pack(side="left", fill="x", expand=True, padx=(8, 6), ipady=5)

        tk.Label(url_row, text="Лотів:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.count_var = tk.IntVar(value=10)
        tk.Spinbox(url_row, from_=1, to=50, textvariable=self.count_var, width=4,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4, 8))

        tk.Label(url_row, text="Авто:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_var = tk.IntVar(value=30)
        tk.Spinbox(url_row, from_=10, to=3600, textvariable=self.interval_var, width=5,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4, 2))
        tk.Label(url_row, text="сек", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))

        self.auto_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            url_row, text="Вкл", variable=self.auto_var, command=self._toggle_auto,
            bg=BG, fg=SUBTEXT, selectcolor=CARD,
            activebackground=BG, activeforeground=ACCENT,
            font=("Segoe UI", 10), cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        self.search_btn = tk.Button(
            url_row, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg="#1a1a1a", activebackground=ACCENT_D,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        )
        self.search_btn.pack(side="left")

        tk.Button(
            url_row, text="  📊 Статистика  ", command=self._open_stats,
            bg=CARD, fg=ACCENT, activebackground=BORDER,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        ).pack(side="left", padx=(6, 0))

        self.status_var = tk.StringVar(value="Введи токен і натисни «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(6, 2))

        table_wrap = tk.Frame(self, bg=BORDER)
        table_wrap.pack(fill="both", expand=True, padx=16, pady=(4, 16))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=30, borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background=CARD, foreground=ACCENT, relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview",
                  background=[("selected", "#2f3a52")],
                  foreground=[("selected", "#ffffff")])

        cols = ("№", "Назва", "Продавець", "Ціна", "📌", "🔒", "Піднято", "Мій")
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings", selectmode="browse")
        widths  = [34, 310, 150, 100, 40, 40, 110, 55]
        anchors = ["center", "w", "w", "center", "center", "center", "center", "center"]
        for c, w, a in zip(cols, widths, anchors):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=a)

        sb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("mine",  background="#15311f", foreground=GREEN)
        self.tree.tag_configure("other", background="#2b1717", foreground="#ff8a8a")

        self.tree.bind("<Double-1>", self._open_link)
        self._enable_paste(self.token_entry)

    # ── Вставка з буфера ─────────────────────────────────────────────────────
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

    def _open_token_help(self):
        webbrowser.open("https://lzt.market/account/api")

    # ── Автооновлення ────────────────────────────────────────────────────────
    def _toggle_auto(self):
        if self.auto_var.get():
            self._schedule_auto()
        elif self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None

    def _schedule_auto(self):
        secs = max(10, self.interval_var.get())
        self._auto_job = self.after(secs * 1000, self._auto_tick)

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

    def _populate(self, listings: list[dict]):
        self._listing_data = listings
        try:
            new_bumps = record_snapshot(listings)
            db_error = None
        except Exception as exc:
            new_bumps = 0
            db_error = str(exc)

        sel = self.tree.focus()
        for r in self.tree.get_children():
            self.tree.delete(r)

        for i, lot in enumerate(listings, 1):
            pin    = "✅" if lot["is_pinned"] else "⬜"
            closed = "🔒" if lot["is_closed"] else ""
            mine   = "✓"  if lot["is_mine"]   else ""
            bumped = fmt_time(lot["bumped_at"])
            tag    = "mine" if lot["is_mine"] else "other"

            self.tree.insert("", "end", iid=str(i - 1), tags=(tag,),
                             values=(i, lot["title"], lot["seller"],
                                     lot["price"], pin, closed, bumped, mine))

        mine_c   = sum(1 for l in listings if l["is_mine"])
        pinned_c = sum(1 for l in listings if l["is_pinned"])
        now      = datetime.now().strftime("%H:%M:%S")
        status = (
            f"Оновлено: {now}   •   Лотів: {len(listings)}   •   "
            f"Мої: {mine_c}   •   Закріплені: {pinned_c}   •   "
            f"Нових підняттів за скан: {new_bumps}   •   "
            f"Подвійний клік → відкрити"
        )
        if db_error:
            status += f"   •   ⚠ База статистики: {db_error}"
        self.status_var.set(status)
        self.search_btn.config(state="normal")

        if sel and self.tree.exists(sel):
            self.tree.focus(sel)
            self.tree.selection_set(sel)

        if self._stats_win and self._stats_win.winfo_exists():
            self._refresh_stats_window()

    def _show_error(self, msg: str):
        self.status_var.set(f"Помилка: {msg}")
        self.search_btn.config(state="normal")

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        webbrowser.open(self._listing_data[int(item)]["link"])

    # ── Вікно статистики ─────────────────────────────────────────────────────
    def _open_stats(self):
        if self._stats_win and self._stats_win.winfo_exists():
            self._stats_win.lift()
            self._refresh_stats_window()
            return

        win = tk.Toplevel(self)
        win.title("LOLSOFT — Статистика")
        win.geometry("560x600")
        win.configure(bg=BG)
        self._stats_win = win

        tk.Label(win, text="📊 Статистика підняттів", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 4))

        self._stats_summary_var = tk.StringVar()
        tk.Label(win, textvariable=self._stats_summary_var, bg=BG, fg=TEXT,
                 font=("Segoe UI", 10), justify="left", anchor="w"
                 ).pack(anchor="w", padx=16, pady=(0, 10))

        tk.Label(win, text="По продавцям (сьогодні):", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16)

        style = ttk.Style(win)
        style.configure("Stats.Treeview", background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=26, font=("Segoe UI", 9))
        style.configure("Stats.Treeview.Heading", background=CARD, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"))

        wrap = tk.Frame(win, bg=BORDER)
        wrap.pack(fill="both", expand=True, padx=16, pady=(4, 10))

        cols = ("Продавець", "Підняттів", "Мій")
        self._stats_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                        style="Stats.Treeview", height=10)
        for c, w in zip(cols, [260, 100, 60]):
            self._stats_tree.heading(c, text=c)
            self._stats_tree.column(c, width=w, anchor="center" if c != "Продавець" else "w")
        self._stats_tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        self._stats_tree.tag_configure("mine", background="#15311f", foreground=GREEN)

        tk.Label(win, text="По годинах (сьогодні):", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16)

        self._stats_hours_var = tk.StringVar()
        tk.Label(win, textvariable=self._stats_hours_var, bg=BG, fg=TEXT,
                 font=("Consolas", 9), justify="left", anchor="w", wraplength=520
                 ).pack(anchor="w", padx=16, pady=(2, 14))

        self._refresh_stats_window()

    def _refresh_stats_window(self):
        if not (self._stats_win and self._stats_win.winfo_exists()):
            return

        today_iso = today_start_iso()
        total_all   = stat_total_bumps()
        total_today = stat_total_bumps(today_iso)
        my_today    = stat_my_today()
        sellers_today = stat_by_seller(today_iso)
        market_scale = len(sellers_today)

        avg_top = "—"
        if sellers_today:
            top_n = min(3, len(sellers_today))
            avg_top = round(sum(r[2] for r in sellers_today[:top_n]) / top_n, 1)

        self._stats_summary_var.set(
            f"Всього зафіксовано підняттів (увесь час): {total_all}\n"
            f"Підняттів сьогодні (по всіх продавцях у видачі): {total_today}\n"
            f"Моїх підняттів сьогодні: {my_today}\n"
            f"Унікальних продавців у видачі сьогодні: {market_scale} (масштаб ринку)\n"
            f"Середнє підняттів у топ-3 продавців сьогодні: {avg_top}  ← орієнтир, "
            f"скільки треба щоб бути топ-1"
        )

        for r in self._stats_tree.get_children():
            self._stats_tree.delete(r)
        for seller_id, seller_name, count, is_mine in sellers_today:
            tag = "mine" if is_mine else ""
            self._stats_tree.insert("", "end", values=(seller_name, count, "✓" if is_mine else ""),
                                    tags=(tag,) if tag else ())

        hours = stat_by_hour(today_iso)
        if hours:
            line = "  ".join(f"{h}:00→{c}" for h, c in hours)
        else:
            line = "Даних ще немає — почни сканування."
        self._stats_hours_var.set(line)


if __name__ == "__main__":
    App().mainloop()
