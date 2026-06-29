import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
from bs4 import BeautifulSoup
import webbrowser

MY_PROFILE_ID = "9542364"
MY_PROFILE_URL = f"https://lolz.live/members/{MY_PROFILE_ID}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk,en;q=0.9",
}

DEFAULT_URL = "https://lzt.market/telegram/?origin[]=autoreg&origin[]=self_registration&country[]=UA&spam=no"


def fetch_listings(url: str, count: int = 6) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    # Each listing card on lzt.market
    cards = soup.select("a.item-title, div.market-item-title a, .accountCard a.title")
    # Broader fallback: find all item blocks
    if not cards:
        cards = soup.select("[class*='item'] a[href*='/lzt.market'], a[href*='lzt.market/']")

    # Try to find listing rows via the main listings container
    listing_blocks = soup.select("li.item, div.market-item, article.item, .saleCard")
    if not listing_blocks:
        # last resort: grab any links that look like item pages
        listing_blocks = [
            tag.parent for tag in soup.select("a[href]")
            if "/lzt.market/" in tag.get("href", "") or "lzt.market" not in tag.get("href", "")
        ]

    # Parse structured listing blocks
    parsed: list[dict] = []
    for block in soup.select("li[data-item-id], div[data-item-id]")[:count]:
        item_id = block.get("data-item-id", "")
        title_tag = block.select_one("a.title, a.item-title, h3 a, .accountTitle a")
        title = title_tag.get_text(strip=True) if title_tag else "—"
        link = title_tag["href"] if title_tag and title_tag.get("href") else ""
        if link and not link.startswith("http"):
            link = "https://lzt.market" + link

        seller_tag = block.select_one("a[href*='/members/'], .seller a, .username a")
        seller_name = seller_tag.get_text(strip=True) if seller_tag else "?"
        seller_href = seller_tag["href"] if seller_tag and seller_tag.get("href") else ""
        if seller_href and not seller_href.startswith("http"):
            seller_href = "https://lolz.live" + seller_href

        price_tag = block.select_one(".price, .cost, [class*='price']")
        price = price_tag.get_text(strip=True) if price_tag else "?"

        is_mine = MY_PROFILE_ID in seller_href
        parsed.append({
            "id": item_id,
            "title": title or f"Лот #{item_id}",
            "link": link,
            "seller": seller_name,
            "seller_url": seller_href,
            "price": price,
            "is_mine": is_mine,
        })

    return parsed[:count]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLZ Market Аналізатор")
        self.geometry("900x560")
        self.configure(bg="#1e1e2e")
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        top = tk.Frame(self, bg="#1e1e2e")
        top.pack(fill="x", **pad)

        tk.Label(top, text="URL пошуку:", bg="#1e1e2e", fg="#cdd6f4",
                 font=("Segoe UI", 10)).pack(side="left")

        self.url_var = tk.StringVar(value=DEFAULT_URL)
        url_entry = tk.Entry(top, textvariable=self.url_var, width=72,
                             bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                             relief="flat", font=("Segoe UI", 10))
        url_entry.pack(side="left", padx=(6, 6), ipady=4)

        self.count_var = tk.IntVar(value=6)
        tk.Spinbox(top, from_=1, to=20, textvariable=self.count_var, width=4,
                   bg="#313244", fg="#cdd6f4", buttonbackground="#45475a",
                   relief="flat", font=("Segoe UI", 10)).pack(side="left", padx=(0, 6))

        self.search_btn = tk.Button(
            top, text="Пошук", command=self._start_search,
            bg="#89b4fa", fg="#1e1e2e", activebackground="#74c7ec",
            relief="flat", font=("Segoe UI", 10, "bold"), padx=12, pady=4,
            cursor="hand2"
        )
        self.search_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Введіть URL і натисніть «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg="#1e1e2e", fg="#a6adc8",
                 font=("Segoe UI", 9)).pack(anchor="w", padx=10)

        cols = ("№", "Назва / Лот", "Продавець", "Ціна", "Мій?")
        frame = tk.Frame(self, bg="#1e1e2e")
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#181825", foreground="#cdd6f4",
                        fieldbackground="#181825", rowheight=28,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background="#313244", foreground="#89b4fa",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#45475a")])

        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="browse")
        widths = [30, 380, 180, 90, 60]
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="center" if w < 200 else "w")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("mine", background="#1e3a2e", foreground="#a6e3a1")
        self.tree.tag_configure("other", background="#181825", foreground="#cdd6f4")

        self.tree.bind("<Double-1>", self._open_link)

        self._listing_data: list[dict] = []

    def _start_search(self):
        self.search_btn.config(state="disabled")
        self.status_var.set("Завантаження…")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._listing_data = []
        threading.Thread(target=self._do_search, daemon=True).start()

    def _do_search(self):
        url = self.url_var.get().strip()
        count = self.count_var.get()
        try:
            listings = fetch_listings(url, count)
            self.after(0, self._populate, listings)
        except Exception as exc:
            self.after(0, self._show_error, str(exc))

    def _populate(self, listings: list[dict]):
        self._listing_data = listings
        for i, lot in enumerate(listings, 1):
            tag = "mine" if lot["is_mine"] else "other"
            mine_mark = "✓ Мій" if lot["is_mine"] else "—"
            self.tree.insert("", "end", iid=str(i - 1), tags=(tag,),
                             values=(i, lot["title"], lot["seller"], lot["price"], mine_mark))

        mine_count = sum(1 for l in listings if l["is_mine"])
        self.status_var.set(
            f"Знайдено: {len(listings)} лотів  •  Мої: {mine_count}  •  "
            f"Чужі: {len(listings) - mine_count}  •  Подвійний клік → відкрити в браузері"
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
        idx = int(item)
        lot = self._listing_data[idx]
        url = lot["link"] or f"https://lzt.market/{lot['id']}/"
        if url:
            webbrowser.open(url)


if __name__ == "__main__":
    app = App()
    app.mainloop()
