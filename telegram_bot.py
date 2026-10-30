"""Telegram bot kiểm tra đơn Shopee và vận đơn SPX/GHN.

Bản đồng bộ với order_service.py (KiotProxy bắt buộc cho Check Cookie, HTML preview).
Giữ Flask /api/ghn-track phục vụ Apps Script như mã gốc.
"""

import os
import threading
import asyncio
import re
import time
import html
from typing import List, Dict, Any, Tuple

from flask import Flask, request, jsonify
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import RetryAfter

from order_service import fetch_orders, format_orders_for_telegram, KiotProxyError
from tracking_service import detect_tracking_carrier, fetch_tracking_spx, fetch_tracking_ghn
# =======================
# Flask keep-alive (Render) — giữ nguyên endpoint
# =======================
web_app = Flask(__name__)


@web_app.get("/")
def home():
    return "check_order_shopee is running", 200


@web_app.get("/ping")
def ping():
    return "pong", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port)


# =======================
# GHN PROXY FOR GOOGLE SHEET (Apps Script) — giữ nguyên
# =======================
SHEET_API_KEY = os.getenv("SHEET_API_KEY", "").strip()
RL_MAX = int(os.getenv("SHEET_RL_MAX", "30"))
RL_WINDOW_SEC = int(os.getenv("SHEET_RL_WINDOW_SEC", "60"))
CACHE_TTL_SEC = int(os.getenv("SHEET_CACHE_TTL_SEC", "30"))
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_rl: Dict[str, Tuple[float, int]] = {}


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    win_start, cnt = _rl.get(ip, (now, 0))
    if now - win_start >= RL_WINDOW_SEC:
        _rl[ip] = (now, 1)
        return False
    if cnt >= RL_MAX:
        return True
    _rl[ip] = (win_start, cnt + 1)
    return False


def _cache_get(code: str) -> Dict[str, Any] | None:
    now = time.time()
    item = _cache.get(code)
    if not item:
        return None
    exp, data = item
    if now >= exp:
        _cache.pop(code, None)
        return None
    return data


def _cache_set(code: str, data: Dict[str, Any]) -> None:
    _cache[code] = (time.time() + CACHE_TTL_SEC, data)


@web_app.post("/api/ghn-track")
def ghn_track_proxy():
    if SHEET_API_KEY:
        if request.headers.get("x-api-key", "") != SHEET_API_KEY:
            return jsonify({"ok": False, "error": "Unauthorized"}), 403

    ip = _client_ip()
    if _rate_limited(ip):
        return jsonify({"ok": False, "error": "Rate limited"}), 429

    data = request.get_json(silent=True) or {}
    code = (data.get("order_code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "Missing order_code"}), 400

    cached = _cache_get(code)
    if cached:
        out = dict(cached)
        out["_cached"] = True
        return jsonify(out), 200

    try:
        result = fetch_tracking_ghn(code)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "code": code}), 500

    _cache_set(code, result)
    return jsonify(result), 200


# =======================
# Config / UI
# =======================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BTN_CHECK = "📦 Check MVĐ"
CB_CONTINUE = "continue_check"
# Giá trị cao hơn bản gốc: bot không còn chỉ trả 5 đơn mặc định.
MAX_ORDERS_PER_COOKIE = 100
MAX_PRODUCTS_PER_ORDER = 50


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_CHECK)]], resize_keyboard=True)


def continue_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Bấm để tiếp tục check", callback_data=CB_CONTINUE)]]
    )


PROMPT_TEXT = (
    "🍪 Gửi Cookie theo định dạng:\n"
    "SPC_ST=....\n\n"
    "📦 Hoặc gửi Mã vận đơn để xem hành trình:\n"
    "- SPX / SPXVN... (Shopee Express)\n"
    "- GY... (GHN)\n\n"
    "💡 Cookie: tối đa 10 dòng (mỗi cookie 1 dòng)."
)

# =======================
# Validation / Anti-placeholder
# =======================
SPC_ST_PATTERN = re.compile(r"(?:^|;\s*)SPC_ST=([^;]{15,})", re.IGNORECASE)
SHOPEE_LINK_PATTERN = re.compile(
    r"https://(?:[a-zA-Z0-9-]+\.)*(?:shopee\.vn|shope\.ee)/[^\s<>]+",
    re.IGNORECASE,
)


def is_probably_shopee_cookie(s: str) -> bool:
    if not s:
        return False
    t = s.strip()
    return len(t) >= 20 and SPC_ST_PATTERN.search(t) is not None


def _get_any(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def is_real_order(order: Dict[str, Any]) -> bool:
    """Bao gồm orderSn/orderId và MVD nằm trong shipping.shipments[]."""
    if not isinstance(order, dict):
        return False
    order_id = _get_any(
        order, ["order_id", "orderid", "order_sn", "orderSn", "orderId", "id"], ""
    )
    tracking = _get_any(
        order, ["tracking_number", "tracking_no", "tracking", "trackingNumber"], ""
    )
    shipping = order.get("shipping") or {}
    if not tracking and isinstance(shipping, dict):
        shipments = shipping.get("shipments") or []
        if isinstance(shipments, list):
            for shipment in shipments:
                if isinstance(shipment, dict):
                    tracking = _get_any(shipment, ["trackingNumber", "tracking_number"], "")
                    if tracking:
                        break
    products = _get_any(order, ["product_info", "products", "items", "item_list"], [])
    if isinstance(products, dict):
        products = [products]
    has_product = False
    if isinstance(products, list):
        for p in products:
            if isinstance(p, dict) and _get_any(p, ["name", "product_name", "title", "item_name"], ""):
                has_product = True
                break
    return bool(order_id) or bool(tracking) or has_product


def count_real_orders_from_api(data: Dict[str, Any]) -> int:
    if not isinstance(data, dict):
        return 0
    accs = data.get("allOrderDetails") or []
    total = 0
    if not isinstance(accs, list):
        return 0
    for account in accs:
        if not isinstance(account, dict):
            continue
        orders = account.get("orderDetails") or []
        if isinstance(orders, list):
            for order in orders:
                if is_real_order(order):
                    total += 1
    return total


def _link_preview_options(msg: str) -> LinkPreviewOptions:
    """Ưu tiên URL Shopee có trong tin nhắn; không tải/gửi ảnh riêng."""
    match = SHOPEE_LINK_PATTERN.search(msg or "")
    if match:
        url = html.unescape(match.group(0)).rstrip(".,;)")
        return LinkPreviewOptions(
            is_disabled=False,
            url=url,
            prefer_large_media=True,
            show_above_text=False,
        )
    # Không có URL: không yêu cầu Telegram tạo preview.
    return LinkPreviewOptions(is_disabled=True)


# =======================
# Tracking formatter (plain text) — giữ nguyên
# =======================
def format_tracking_for_telegram(tdata: Dict[str, Any], max_events: int = 5) -> str:
    carrier = tdata.get("carrier", "")
    code = tdata.get("code", "")
    status = tdata.get("current_status", "")
    link = tdata.get("link", "")
    lines: List[str] = []
    if carrier:
        lines.append(f"🚚 Đơn vị: {carrier}")
    if code:
        lines.append(f"🧾 MVĐ: {code}")
    if status:
        lines.append(f"📌 Trạng thái: {status}")

    if tdata.get("from_address") and tdata.get("to_address"):
        lines.append(f"📦 Tuyến: {tdata['from_address']} ➜ {tdata['to_address']}")
    if tdata.get("to_name"):
        lines.append(f"👤 Người nhận: {tdata['to_name']}")
    if tdata.get("raw_sls_tn"):
        lines.append(f"🔎 Mã liên kết: {tdata['raw_sls_tn']}")

    evs = tdata.get("events") or []
    if evs:
        lines.append("\n📍 Hành trình gần nhất (mới nhất ở trên):")
        for e in evs[:max_events]:
            t = (e.get("time") or "").strip()
            st = (e.get("status") or "").strip()
            de = (e.get("detail") or "").strip()
            one = " - ".join([x for x in [t, st, de] if x])
            if one:
                lines.append(f"• {one}")
        remain = len(evs) - max_events
        if remain > 0:
            lines.append(f"… +{remain} dòng khác (Mở link để xem full hành trình)")

    if link:
        lines.append(f"\n🔗 {link}")
    return "\n".join(lines).strip()


# =======================
# Helpers / Handlers
# =======================
async def send_prompt(update: Update, *, via_query: bool = False):
    if via_query and update.callback_query:
        await update.callback_query.message.reply_text(PROMPT_TEXT)
    else:
        await update.message.reply_text(PROMPT_TEXT)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = False
    await update.message.reply_text(
        "✅ Bot Check Đơn Shopee\n\nBấm nút bên dưới để bắt đầu.",
        reply_markup=main_keyboard(),
    )


async def handle_check_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = True
    await send_prompt(update)


async def continue_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["awaiting"] = True
    await q.message.reply_text("🔁 OK, gửi Cookie hoặc MVĐ để check tiếp nhé!")
    await send_prompt(update, via_query=True)


async def _reply_order_html(update: Update, text: str, *, last: bool = False):
    """Một đơn/tin, preview Shopee, chỉ đính kèm nút Tiếp tục ở tin cuối."""
    kwargs = {
        "parse_mode": "HTML",
        "link_preview_options": _link_preview_options(text),
    }
    if last:
        kwargs["reply_markup"] = continue_inline_keyboard()
    for attempt in range(2):
        try:
            await update.message.reply_text(text, **kwargs)
            return
        except RetryAfter as ex:
            if attempt:
                raise
            delay = ex.retry_after
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            await asyncio.sleep(max(0.5, float(delay)) + 0.25)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == BTN_CHECK:
        await handle_check_button(update, context)
        return

    if not context.user_data.get("awaiting"):
        await start(update, context)
        return

    raw = text
    if not raw:
        await update.message.reply_text("❌ Bạn chưa gửi gì cả. Gửi lại giúp mình nhé.")
        return

    # 1) MVĐ? — giữ luồng SPX/GHN cũ
    single = raw.replace(" ", "").strip()
    carrier = detect_tracking_carrier(single)
    if carrier:
        await update.message.reply_text("⏳ Đang check hành trình vận đơn...")
        try:
            if carrier == "SPX":
                tdata = await asyncio.to_thread(fetch_tracking_spx, single, "vi")
            else:
                tdata = await asyncio.to_thread(fetch_tracking_ghn, single)
            if not tdata.get("ok"):
                await update.message.reply_text(
                    "❌ Không lấy được hành trình vận đơn.\n"
                    f"Chi tiết: {tdata.get('error','')}",
                    reply_markup=continue_inline_keyboard(),
                )
                return
            msg = format_tracking_for_telegram(tdata, max_events=5)
            await update.message.reply_text(msg, reply_markup=continue_inline_keyboard())
        except Exception as e:
            await update.message.reply_text(
                f"❌ Lỗi check vận đơn: {e}", reply_markup=continue_inline_keyboard()
            )
        return

    # 2) Cookie (có thể nhiều dòng)
    cookies = [line.strip() for line in raw.splitlines() if line.strip()]
    if not cookies:
        await update.message.reply_text("❌ Cookie trống. Gửi lại giúp mình nhé.")
        return

    if len(cookies) > 10:
        await update.message.reply_text("❌ Tối đa 10 cookie. Bạn gửi lại giúp mình nhé (<=10 dòng).")
        return

    invalid = []
    for i, c in enumerate(cookies, start=1):
        if not is_probably_shopee_cookie(c):
            invalid.append(f"- Dòng {i}: sai định dạng (phải có SPC_ST=...)")
    if invalid:
        await update.message.reply_text(
            "❌ Không nhận diện được MVĐ và Cookie cũng không hợp lệ.\n\n"
            "✅ Gửi:\n"
            "• Cookie: SPC_ST=....\n"
            "• Hoặc MVĐ: SPXVN... / SPX... / GY...\n\n"
            "Chi tiết lỗi cookie:\n" + "\n".join(invalid),
            reply_markup=continue_inline_keyboard(),
        )
        return

    await update.message.reply_text("⏳ Đang check đơn hàng qua KiotProxy...")
    try:
        data = await asyncio.to_thread(fetch_orders, cookies)
        if not isinstance(data, dict) or not isinstance(data.get("allOrderDetails"), list):
            await update.message.reply_text(
                "❌ API trả dữ liệu không đúng cấu trúc allOrderDetails; không kết luận cookie hết hạn.",
                reply_markup=continue_inline_keyboard(),
            )
            return

        if count_real_orders_from_api(data) == 0:
            await update.message.reply_text(
                "ℹ️ API không trả về đơn hàng hợp lệ. Có thể tài khoản chưa có đơn, "
                "cookie không còn hợp lệ hoặc API thiếu dữ liệu.\n"
                "Không thể kết luận cookie hết hạn chỉ từ kết quả rỗng.",
                reply_markup=continue_inline_keyboard(),
            )
            return

        messages = format_orders_for_telegram(
            data,
            max_orders_per_cookie=MAX_ORDERS_PER_COOKIE,
            max_products_per_order=MAX_PRODUCTS_PER_ORDER,
        )
        for i, msg in enumerate(messages):
            await _reply_order_html(update, msg, last=(i == len(messages) - 1))
            # Chống gửi dồn khi một cookie có nhiều đơn; không thay đổi dữ liệu.
            if i < len(messages) - 1:
                await asyncio.sleep(0.15)
    except KiotProxyError as e:
        await update.message.reply_text(
            f"❌ Lỗi KiotProxy: {e}", reply_markup=continue_inline_keyboard()
        )
    except Exception as e:
        # order_service.py đã giới hạn chi tiết lỗi API để không lộ cookie.
        await update.message.reply_text(
            f"❌ Lỗi xử lý: {e}", reply_markup=continue_inline_keyboard()
        )


def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong Environment Variables.")

    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(continue_check_callback, pattern=f"^{CB_CONTINUE}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ check_order_shopee bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
