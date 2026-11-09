"""Spider-service.com auto-buyer + Telethon account registrar."""
import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

SPIDER_BASE = "https://api.spider-service.com"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"

_orders: list[dict] = []
_accounts: list[dict] = []
_lock = threading.Lock()
_worker_running = False
_loop: Optional[asyncio.AbstractEventLoop] = None
_broadcaster = None  # set from app.py after import


def set_broadcaster(b):
    global _broadcaster
    _broadcaster = b


def _emit(msg: str, level: str = "info"):
    if _broadcaster:
        try:
            _broadcaster.emit(msg, level)
        except Exception:
            pass


def _spider(action: str, **kwargs) -> dict:
    key = os.environ.get("SPIDER_API_KEY", "")
    if not key:
        raise ValueError("SPIDER_API_KEY не задано")
    params = {"apiKay": key, "action": action, **kwargs}
    r = requests.get(SPIDER_BASE, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def spider_balance() -> dict:
    return _spider("getBalance")


def spider_countries() -> list:
    data = _spider("getCountrys")
    if isinstance(data, list):
        return data
    # try common wrapper keys
    for key in ("result", "data", "countrys", "countries"):
        val = data.get(key)
        if val and isinstance(val, (list, dict)):
            return val if isinstance(val, list) else list(val.values())
    # return raw so frontend can inspect
    return [data]


def _load_accounts():
    global _accounts
    if ACCOUNTS_FILE.exists():
        try:
            _accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _accounts = []


def _save_accounts():
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(_accounts, f, ensure_ascii=False, indent=2)


def create_order(country: str, qty: int) -> dict:
    order = {
        "id": str(uuid.uuid4())[:8],
        "country": country,
        "qty": max(1, qty),
        "done": 0,
        "failed": 0,
        "status": "pending",
        "created_at": time.time(),
    }
    with _lock:
        _orders.append(order)
    _ensure_worker()
    return order


def get_orders() -> list:
    with _lock:
        return list(_orders)


def get_accounts() -> list:
    with _lock:
        return list(_accounts)


def delete_account(idx: int):
    with _lock:
        if 0 <= idx < len(_accounts):
            _accounts.pop(idx)
            _save_accounts()


def cancel_order(order_id: str):
    with _lock:
        for o in _orders:
            if o["id"] == order_id and o["status"] in ("pending", "running"):
                o["status"] = "cancelled"


def _ensure_worker():
    global _worker_running
    if not _worker_running:
        threading.Thread(target=_worker_loop, daemon=True, name="spider-buyer").start()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _run_async(coro):
    loop = _get_or_create_loop()
    return loop.run_until_complete(coro)


async def _register_one(country: str) -> dict:
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        from telethon.sessions import MemorySession
    except ImportError:
        raise Exception("telethon не встановлено — додайте у requirements.txt")

    api_id_str = os.environ.get("TG_API_ID", "")
    api_hash = os.environ.get("TG_API_HASH", "")
    if not api_id_str or not api_hash:
        raise ValueError("TG_API_ID або TG_API_HASH не задано")
    api_id = int(api_id_str)

    # 1. Отримуємо номер від Spider
    num_data = _spider("getNumber", country=country)
    phone = str(
        num_data.get("number") or num_data.get("phone") or
        num_data.get("data", {}).get("number", "") if isinstance(num_data.get("data"), dict) else ""
    ).strip().lstrip("+")
    hash_code = str(
        num_data.get("hash_code") or num_data.get("hashCode") or
        num_data.get("id") or num_data.get("activationId") or ""
    ).strip()
    if not phone:
        raise Exception(f"getNumber: не вдалось отримати номер. Відповідь: {num_data}")

    full_phone = f"+{phone}" if not phone.startswith("+") else phone
    _emit(f"📱 [{country}] номер: {full_phone}", "info")

    # 2. Telethon: підключення + запит коду
    client = TelegramClient(MemorySession(), api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(full_phone)
        phone_code_hash = result.phone_code_hash
        _emit(f"📨 [{country}] {full_phone} — чекаємо SMS...", "info")

        # 3. Поллінг Spider на код (до 90с)
        code = None
        for _ in range(45):
            await asyncio.sleep(2)
            try:
                code_data = _spider("getCode", hash_code=hash_code, auto_logout="no")
                raw = str(
                    code_data.get("code") or code_data.get("sms_code") or
                    code_data.get("data", {}).get("code", "") if isinstance(code_data.get("data"), dict) else ""
                ).strip()
                if raw and raw.isdigit():
                    code = raw
                    break
            except Exception:
                pass

        if not code:
            raise Exception(f"SMS timeout для {full_phone}")

        _emit(f"🔑 [{country}] {full_phone} — код: {code}", "info")

        # 4. Вхід
        await client.sign_in(full_phone, code, phone_code_hash=phone_code_hash)

        # 5. Витягуємо Auth Key + DC ID
        sess = client.session
        auth_key_hex = sess.auth_key.key.hex()
        dc_id = sess.dc_id

        return {
            "country": country,
            "phone": full_phone,
            "auth_key_hex": auth_key_hex,
            "dc_id": dc_id,
            "created_at": time.time(),
        }
    except Exception:
        raise
    finally:
        await client.disconnect()


def _worker_loop():
    global _worker_running
    _worker_running = True
    try:
        while True:
            order = None
            with _lock:
                for o in _orders:
                    if o["status"] == "pending":
                        o["status"] = "running"
                        order = o
                        break

            if order is None:
                with _lock:
                    has_work = any(o["status"] in ("pending", "running") for o in _orders)
                if not has_work:
                    break
                time.sleep(2)
                continue

            # Купуємо по 1 до виконання квоти
            while True:
                with _lock:
                    if order["status"] == "cancelled":
                        break
                    total = order["done"] + order["failed"]
                    if total >= order["qty"]:
                        break

                try:
                    acc = _run_async(_register_one(order["country"]))
                    with _lock:
                        _accounts.append(acc)
                        order["done"] += 1
                        _save_accounts()
                    _emit(
                        f"✅ [{order['country']}] куплено {order['done']}/{order['qty']}: {acc['phone']}", "ok"
                    )
                except Exception as e:
                    with _lock:
                        order["failed"] += 1
                    _emit(f"⚠ [{order['country']}] помилка: {e}", "warn")
                    time.sleep(3)

            with _lock:
                if order["status"] not in ("cancelled",):
                    order["status"] = "done"
    finally:
        _worker_running = False


_load_accounts()
