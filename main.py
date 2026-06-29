import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
from bs4 import BeautifulSoup
import webbrowser
from collections import Counter
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.patches as mpatches

MY_PROFILE_ID = "9542364"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk,en;q=0.9",
}

DEFAULT_URL = (
    "https://lzt.market/telegram/"
    "?origin[]=autoreg&origin[]=self_registration&country[]=UA&spam=no"
)

BG       = "#1e1e2e"
BG2      = "#181825"
SURFACE  = "#313244"
ACCENT   = "#89b4fa"
GREEN    = "#a6e3a1"
YELLOW   = "#f9e2af"
TEXT     = "#cdd6f4"
SUBTEXT  = "#a6adc8"


def fetch_listings(url: str, count: int = 10) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    parsed: list[dict] = []
    for block in soup.select("li[data-item-id], div[data-item-id]")[:count]:
        item_id = block.get("data-item-id", "")

        # title + link
        title_tag = block.select_one("a.title, a.item-title, h3 a, .accountTitle a")
        title = title_tag.get_text(strip=True) if title_tag else f"Лот #{item_id}"
        link = title_tag.get("href", "") if title_tag else ""
        if link and not link.startswith("http"):
            link = "https://lzt.market" + link

        # seller
        seller_tag = block.select_one("a[href*='/members/'], .seller a, .username a")
        seller_name = seller_tag.get_text(strip=True) if seller_tag else "?"
        seller_href = seller_tag.get("href", "") if seller_tag else ""
        if seller_href and not seller_href.startswith("http"):
            seller_href = "https://lolz.live" + seller_href

        # price
        price_tag = block.select_one(".price, .cost, [class*='price']")
        price = price_tag.get_text(strip=True) if price_tag else "?"

        # pinned — lzt.market marks sticky/featured items with these classes/attrs
        classes = " ".join(block.get("class", []))
        is_pinned = any(k in classes for k in ("sticky", "pinned", "featured", "closed--sticky"))
        # also check for a pin icon or label inside the block
        if not is_pinned:
            pin_tag = block.select_one(
                ".fa-thumbtack, .fa-pin, [class*='pin'], [class*='sticky'], "
                "[class*='featured'], [title*='закреп'], [title*='pin']"
            )
            is_pinned = pin_tag is not None

        # closed/sold
        is_closed = any(k in classes for k in ("closed", "sold", "inactive"))
        if not is_closed:
            closed_tag = block.select_one(
                ".label--closed, .label--sold, [class*='closed'], [class*='sold']"
            )
            is_closed = closed_tag is not None

        is_mine = MY_PROFILE_ID in seller_href

        parsed.append({
            "id": item_id,
            "title": title,
            "link": link,
            "seller": seller_name,
            "seller_url": seller_href,
            "price": price,
            "is_mine": is_mine,
            "is_pinned": is_pinned,
            "is_closed": is_closed,
        })

    return parsed[:count]


# ── colour palette for sellers in the chart ──────────────────────────────────
PALETTE = [
    "#89b4fa", "#a6e3a1", "#f38ba8", "#fab387",
    "#f9e2af", "#cba6f7", "#94e2d5", "#eba0ac",
    "#b4befe", "#74c7ec",
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOLZ Market Аналізатор")
        self.geometry("1200x680")
        self.configure(bg=BG)
        self._listing_data: list[dict] = []
        self._build_ui()

    # ── UI layout ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        # top bar
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=10, pady=8)

        tk.Label(top, text="URL пошуку:", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10)).pack(side="left")

        self.url_var = tk.StringVar(value=DEFAULT_URL)
        tk.Entry(top, textvariable=self.url_var, width=68,
                 bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=("Segoe UI", 10)
                 ).pack(side="left", padx=(6, 6), ipady=4)

        tk.Label(top, text="Лотів:", bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 10)).pack(side="left")
        self.count_var = tk.IntVar(value=10)
        tk.Spinbox(top, from_=1, to=30, textvariable=self.count_var, width=4,
                   bg=SURFACE, fg=TEXT, buttonbackground="#45475a",
                   relief="flat", font=("Segoe UI", 10)
                   ).pack(side="left", padx=(4, 8))

        self.search_btn = tk.Button(
            top, text="  Пошук  ", command=self._start_search,
            bg=ACCENT, fg=BG, activebackground="#74c7ec",
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=4, pady=4, cursor="hand2",
        )
        self.search_btn.pack(side="left")

        # status line
        self.status_var = tk.StringVar(value="Введіть URL і натисніть «Пошук»")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=SUBTEXT,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12)

        # main split: table left, chart right
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._build_table(body)
        self._build_chart_panel(body)

    def _build_table(self, parent):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(side="left", fill="both", expand=True)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=BG2, foreground=TEXT,
                        fieldbackground=BG2, rowheight=26,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=SURFACE, foreground=ACCENT,
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#45475a")])

        cols = ("№", "Назва / Лот", "Продавець", "Ціна", "Закріп", "Закрит", "Мій?")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")

        widths = [28, 340, 150, 80, 60, 60, 55]
        anchors = ["center", "w", "w", "center", "center", "center", "center"]
        for col, w, a in zip(cols, widths, anchors):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor=a)

        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # row colour tags
        self.tree.tag_configure("mine",        background="#1b3a28", foreground=GREEN)
        self.tree.tag_configure("mine_pinned", background="#1b3a28", foreground=YELLOW)
        self.tree.tag_configure("pinned",      background="#2a2a1a", foreground=YELLOW)
        self.tree.tag_configure("closed",      background="#2a1a1a", foreground="#585b70")
        self.tree.tag_configure("other",       background=BG2,       foreground=TEXT)

        self.tree.bind("<Double-1>", self._open_link)

    def _build_chart_panel(self, parent):
        panel = tk.Frame(parent, bg=BG, width=310)
        panel.pack(side="right", fill="y", padx=(8, 0))
        panel.pack_propagate(False)

        tk.Label(panel, text="Розподіл лотів по продавцям",
                 bg=BG, fg=SUBTEXT, font=("Segoe UI", 9)).pack(pady=(4, 0))

        fig_bg = "#181825"
        self._fig, self._ax = plt.subplots(figsize=(3.0, 3.0), dpi=90)
        self._fig.patch.set_facecolor(fig_bg)
        self._ax.set_facecolor(fig_bg)
        self._ax.axis("off")

        self._canvas = FigureCanvasTkAgg(self._fig, master=panel)
        self._canvas.get_tk_widget().pack(fill="both", expand=True)

        # legend frame below chart
        self._legend_frame = tk.Frame(panel, bg=BG)
        self._legend_frame.pack(fill="x", padx=4, pady=(0, 4))

    # ── search logic ──────────────────────────────────────────────────────────
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
            pin_mark    = "📌" if lot["is_pinned"] else "—"
            closed_mark = "🔒" if lot["is_closed"] else "—"
            mine_mark   = "✓ Мій" if lot["is_mine"] else "—"

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
                                     lot["price"], pin_mark, closed_mark, mine_mark))

        mine_count   = sum(1 for l in listings if l["is_mine"])
        pinned_count = sum(1 for l in listings if l["is_pinned"])
        closed_count = sum(1 for l in listings if l["is_closed"])

        self.status_var.set(
            f"Лотів: {len(listings)}  •  "
            f"Мої: {mine_count}  •  "
            f"Закріплені: {pinned_count}  •  "
            f"Закриті: {closed_count}  •  "
            f"Подвійний клік → відкрити"
        )
        self.search_btn.config(state="normal")
        self._update_chart(listings)

    def _update_chart(self, listings: list[dict]):
        self._ax.clear()
        self._ax.set_facecolor("#181825")

        for w in self._legend_frame.winfo_children():
            w.destroy()

        if not listings:
            self._canvas.draw()
            return

        counts = Counter(lot["seller"] for lot in listings)
        sellers = list(counts.keys())
        values  = [counts[s] for s in sellers]
        colors  = [PALETTE[i % len(PALETTE)] for i in range(len(sellers))]

        # highlight "mine" slices with a golden edge
        edge_colors = []
        edge_widths = []
        for s in sellers:
            is_mine = any(l["seller"] == s and l["is_mine"] for l in listings)
            edge_colors.append(YELLOW if is_mine else "#181825")
            edge_widths.append(2.5 if is_mine else 0.5)

        wedges, texts, autotexts = self._ax.pie(
            values,
            colors=colors,
            autopct="%1.0f%%",
            startangle=90,
            wedgeprops={"linewidth": 1, "edgecolor": "#181825"},
            textprops={"color": TEXT, "fontsize": 8},
            pctdistance=0.75,
        )
        for wdg, ec, ew in zip(wedges, edge_colors, edge_widths):
            wdg.set_edgecolor(ec)
            wdg.set_linewidth(ew)

        self._ax.set_title("", color=TEXT)
        self._fig.tight_layout(pad=0.5)
        self._canvas.draw()

        # legend chips below chart
        for seller, color in zip(sellers, colors):
            is_mine = any(l["seller"] == seller and l["is_mine"] for l in listings)
            row = tk.Frame(self._legend_frame, bg=BG)
            row.pack(anchor="w", pady=1)
            chip = tk.Label(row, text="  ", bg=color, width=2)
            chip.pack(side="left", padx=(0, 4))
            label_text = f"{seller} ({counts[seller]})" + (" ← ВИ" if is_mine else "")
            fg = YELLOW if is_mine else TEXT
            tk.Label(row, text=label_text, bg=BG, fg=fg,
                     font=("Segoe UI", 8, "bold" if is_mine else "normal")
                     ).pack(side="left")

    def _show_error(self, msg: str):
        self.status_var.set(f"Помилка: {msg}")
        messagebox.showerror("Помилка", msg)
        self.search_btn.config(state="normal")

    def _open_link(self, event):
        item = self.tree.focus()
        if not item:
            return
        lot = self._listing_data[int(item)]
        url = lot["link"] or f"https://lzt.market/{lot['id']}/"
        if url:
            webbrowser.open(url)


if __name__ == "__main__":
    app = App()
    app.mainloop()
