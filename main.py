import tkinter as tk
from tkinter import ttk, messagebox
import threading
import json
import urllib.parse
import requests
import webbrowser

# ─────────────────────────────────────────────────────────────────────────────
# Налаштування
# ─────────────────────────────────────────────────────────────────────────────
MY_PROFILE_ID = "9542364"          # твій user_id на lolz
API_BASE = "https://prod-api.lzt.market"

DEFAULT_URL = (
    "https://lzt.market/telegram/"
    "?origin[]=autoreg&origin[]=self_registration&country[]=UA&spam=no"
)

# ─── Палітра в стилі LOLZ (темна) ───────────────────────────────────────────
BG       = "#16161a"   # головний фон
BG2      = "#1d1d22"   # фон таблиці
CARD     = "#232329"   # картки / поля
BORDER   = "#2c2c34"
ACCENT   = "#4f8cff"   # фірмовий синій
ACCENT_D = "#3b6fd1"
GREEN    = "#3ddc84"
YELLOW   = "#ffc857"
RED      = "#ff5c5c"
TEXT     = "#e6e6ec"
SUBTEXT  = "#8a8a99"


def url_to_api(market_url: str) -> str:
    """Перетворює посилання lzt.market у відповідний ендпоінт API."""
    parts = urllib.parse.urlsplit(market_url)
    path = parts.path  # напр. /telegram/
    query = parts.query
    api_url = API_BASE + path
    if query:
        api_url += "?" + query
    return api_url


def fetch_listings(market_url: str, token: str, count: int = 10) -> list[dict]:
    """Запит до API lzt.market з Bearer-токеном. Повертає список лотів."""
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
        item_id = str(it.get("item_id", ""))
        title = it.get("title") or it.get("title_en") or f"Лот #{item_id}"

        price = it.get("price", "?")
        currency = it.get("price_currency", "") or it.get("currency", "")
        price_str = f"{price} {currency}".strip()

        seller = it.get("seller") or {}
        seller_id = str(seller.get("user_id", ""))
        seller_name = seller.get("username", "?")

        state = it.get("item_state", "")          # active / sold / closed ...
        is_closed = state in ("sold", "closed", "deleted")

        # деякі категорії віддають прапор закріплення
        is_pinned = bool(it.get("is_sticky") or it.get("sticky"))

        is_mine = (seller_id == MY_PROFILE_ID)

        parsed.append({
            "id": item_id,
            "title": title,
            "link": f"https://lzt.market/{item_id}/",
            "seller": seller_name,
            "seller_id": seller_id,
            "price": price_str,
            "is_mine": is_mine,
            "is_pinned": is_pinned,
            "is_closed": is_closed,
        })
    return parsed


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLSOFT — Market Аналізатор")
        self.geometry("980x620")
        self.configure(bg=BG)
        self._listing_data: list[dict] = []
        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Заголовок
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="LOLSOFT", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(header, text="  Market Аналізатор", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 11)).pack(side="left")

        # ── Поле токена ──
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
                  font=("Segoe UI", 9), activebackground=BORDER
                  ).pack(side="left")

        # ── Поле URL + кнопка ──
        url_row = tk.Frame(self, bg=BG)
        url_row.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(url_row, text="URL пошуку:", bg=BG, fg=SUBTEXT,
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

        self.search_btn = tk.Button(
            url_row, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg="#ffffff", activebackground=ACCENT_D,
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

        cols = ("№", "Назва", "Продавець", "Ціна", "📌", "🔒", "Мій")
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings",
                                 selectmode="browse")
        widths  = [34, 360, 170, 110, 44, 44, 70]
        anchors = ["center", "w", "w", "center", "center", "center", "center"]
        for c, w, a in zip(cols, widths, anchors):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor=a)

        sb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("mine",        background="#15311f", foreground=GREEN)
        self.tree.tag_configure("mine_pinned", background="#15311f", foreground=YELLOW)
        self.tree.tag_configure("pinned",      background="#2b2715", foreground=YELLOW)
        self.tree.tag_configure("closed",      background="#2b1717", foreground="#6b6b76")
        self.tree.tag_configure("other",       background=BG2,       foreground=TEXT)

        self.tree.bind("<Double-1>", self._open_link)

    # ── Дії токена ───────────────────────────────────────────────────────────
    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "•")

    def _open_token_help(self):
        webbrowser.open("https://lzt.market/account/api")
        messagebox.showinfo(
            "Як отримати токен",
            "1. Відкрий lzt.market → Налаштування → API\n"
            "   (відкрив у браузері автоматично)\n"
            "2. Створи токен з доступом до Market\n"
            "3. Скопіюй і встав у поле «API-токен»"
        )

    # ── Пошук ────────────────────────────────────────────────────────────────
    def _start_search(self):
        if not self.token_var.get().strip():
            messagebox.showwarning("Немає токена",
                                   "Спочатку встав API-токен.")
            return
        self.search_btn.config(state="disabled")
        self.status_var.set("Завантаження через API…")
        for r in self.tree.get_children():
            self.tree.delete(r)
        self._listing_data = []
        threading.Thread(target=self._do_search, daemon=True).start()

    def _do_search(self):
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        count = self.count_var.get()
        try:
            listings = fetch_listings(url, token, count)
            self.after(0, self._populate, listings)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    def _populate(self, listings: list[dict]):
        self._listing_data = listings
        for i, lot in enumerate(listings, 1):
            pin    = "📌" if lot["is_pinned"] else ""
            closed = "🔒" if lot["is_closed"] else ""
            mine   = "✓" if lot["is_mine"] else ""

            if lot["is_mine"] and lot["is_pinned"]:
                tag = "mine_pinned"
            elif lot["is_mine"]:
                tag = "mine"
            elif lot["is_pinned"]:
                tag = "pinned"
            elif lot["is_closed"]:
                tag = "closed"
            else:
                tag = "other"

            self.tree.insert("", "end", iid=str(i - 1), tags=(tag,),
                             values=(i, lot["title"], lot["seller"],
                                     lot["price"], pin, closed, mine))

        mine_c   = sum(1 for l in listings if l["is_mine"])
        pinned_c = sum(1 for l in listings if l["is_pinned"])
        closed_c = sum(1 for l in listings if l["is_closed"])
        self.status_var.set(
            f"Лотів: {len(listings)}   •   Мої: {mine_c}   •   "
            f"Закріплені: {pinned_c}   •   Закриті: {closed_c}   •   "
            f"Подвійний клік → відкрити"
        )
        self.search_btn.config(state="normal")

    def _show_error(self, msg: str):
        self.status_var.set(f"Помилка: {msg}")
        messagebox.showerror("Помилка", msg)
        self.search_btn.config(state="normal")

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        lot = self._listing_data[int(item)]
        webbrowser.open(lot["link"])


if __name__ == "__main__":
    App().mainloop()
