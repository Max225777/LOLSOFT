import tkinter as tk
from tkinter import ttk, messagebox
import threading
import urllib.parse
import requests
import webbrowser
from datetime import datetime

MY_PROFILE_ID = "9542364"
API_BASE = "https://prod-api.lzt.market"

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


def url_to_api(market_url: str) -> str:
    parts = urllib.parse.urlsplit(market_url)
    api_url = API_BASE + parts.path
    if parts.query:
        api_url += "?" + parts.query
    return api_url


def fmt_time(ts) -> str:
    """Unix timestamp → читабельний час."""
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
        item_id    = str(it.get("item_id", ""))
        title      = it.get("title") or it.get("title_en") or f"Лот #{item_id}"
        price      = it.get("price", "?")
        currency   = it.get("price_currency", "") or it.get("currency", "")
        price_str  = f"{price} {currency}".strip()

        seller     = it.get("seller") or {}
        seller_id  = str(seller.get("user_id", ""))
        seller_name = seller.get("username", "?")

        state     = it.get("item_state", "")
        is_closed = state in ("sold", "closed", "deleted")
        is_pinned = bool(it.get("is_sticky") or it.get("sticky"))
        is_mine   = seller_id == MY_PROFILE_ID

        # час останнього підняття (bump)
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Market Аналізатор")
        self.geometry("1100x640")
        self.configure(bg=BG)
        self._listing_data: list[dict] = []
        self._auto_job = None
        self._build_ui()

    def _build_ui(self):
        # ── Заголовок ──
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="LOLSOFT", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(header, text="  Market Аналізатор", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 11)).pack(side="left")

        # ── Токен ──
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

        # ── URL + лоти + автооновлення ──
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

        # автооновлення кожні N сек
        tk.Label(url_row, text="Авто:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.interval_var = tk.IntVar(value=30)
        tk.Spinbox(url_row, from_=10, to=3600, textvariable=self.interval_var, width=5,
                   bg=CARD, fg=TEXT, buttonbackground=BORDER, relief="flat",
                   font=("Segoe UI", 10)).pack(side="left", padx=(4, 2))
        tk.Label(url_row, text="сек", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))

        self.auto_var = tk.BooleanVar(value=False)
        self.auto_chk = tk.Checkbutton(
            url_row, text="Вкл", variable=self.auto_var,
            command=self._toggle_auto,
            bg=BG, fg=SUBTEXT, selectcolor=CARD,
            activebackground=BG, activeforeground=ACCENT,
            font=("Segoe UI", 10), cursor="hand2",
        )
        self.auto_chk.pack(side="left", padx=(0, 8))

        self.search_btn = tk.Button(
            url_row, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg="#1a1a1a", activebackground=ACCENT_D,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=8, pady=5, cursor="hand2",
        )
        self.search_btn.pack(side="left")

        # ── Статус ──
        self.status_var = tk.StringVar(value="Введи токен і натисни «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=18, pady=(6, 2))

        # ── Таблиця ──
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
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings",
                                 selectmode="browse")
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
        menu.add_command(label="Вставити",  command=lambda: self._paste_into(entry))
        menu.add_command(label="Очистити",  command=lambda: entry.delete(0, "end"))
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

    # ── Токен ────────────────────────────────────────────────────────────────
    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "•")

    def _open_token_help(self):
        webbrowser.open("https://lzt.market/account/api")

    # ── Автооновлення ────────────────────────────────────────────────────────
    def _toggle_auto(self):
        if self.auto_var.get():
            self._schedule_auto()
        else:
            if self._auto_job:
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

        # зберігаємо позицію скролу
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
        self.status_var.set(
            f"Оновлено: {now}   •   Лотів: {len(listings)}   •   "
            f"Мої: {mine_c}   •   Закріплені: {pinned_c}   •   "
            f"Подвійний клік → відкрити"
        )
        self.search_btn.config(state="normal")

        # відновлюємо виділення
        if sel and self.tree.exists(sel):
            self.tree.focus(sel)
            self.tree.selection_set(sel)

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
