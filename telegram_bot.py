import os
import threading
import asyncio
import re
from typing import List, Dict, Any

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from order_service import fetch_orders, format_orders_for_telegram

# =======================
# Flask keep-alive (Render)
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
# Config
# =======================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Conversation state
WAIT_COOKIE = 1

# =======================
# UI
# =======================
BTN_CHECK = "📦 Check MVĐ"

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_CHECK)]],
        resize_keyboard=True
    )

# =======================
# Validation / Anti-placeholder
# =======================
# SPC_ST phải có value đủ dài, đứng 1 mình hoặc nằm trong full cookie
SPC_ST_PATTERN = re.compile(r"(?:^|;\s*)SPC_ST=([^;]{15,})", re.IGNORECASE)

def is_probably_shopee_cookie(s: str) -> bool:
    if not s:
        return False
    t = s.strip()
    if len(t) < 20:
        return False
    return SPC_ST_PATTERN.search(t) is not None

def _get_any(d: Dict[str, Any], keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default

def is_real_order(order: Dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return False

    order_id = _get_any(order, ["order_id", "orderid", "id"], "")
    tracking = _get_any(order, ["tracking_number", "tracking_no", "tracking"], "")

    products = order.get("product_info") or order.get("products") or []
    has_product = False
    if isinstance(products, list) and products:
        p0 = products[0] if isinstance(products[0], dict) else {}
        pname = _get_any(p0, ["name", "product_name", "title"], "")
        has_product = bool(pname)

    return bool(order_id) or bool(tracking) or has_product

def count_real_orders_from_api(data: Dict[str, Any]) -> int:
    accs = data.get("allOrderDetails") or []
    total = 0
    for a in accs:
        orders = a.get("orderDetails") or []
        for od in orders:
            if is_real_order(od):
                total += 1
    return total

# =======================
# Handlers
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot Check Đơn Shopee\n\n"
        "Bấm nút bên dưới để bắt đầu.",
        reply_markup=main_keyboard()
    )

async def handle_check_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # chuyển sang trạng thái chờ cookie
    await update.message.reply_text(
        "🍪 Gửi Cookie theo định dạng:\n"
        "SPC_ST=....\n\n"
        "💡 Bạn có thể gửi tối đa 10 dòng (mỗi cookie 1 dòng)."
    )
    return WAIT_COOKIE

async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    cookies = [line.strip() for line in raw.splitlines() if line.strip()]

    if not cookies:
        await update.message.reply_text("❌ Cookie trống. Gửi lại (mỗi cookie 1 dòng).")
        return WAIT_COOKIE

    if len(cookies) > 10:
        await update.message.reply_text("❌ Tối đa 10 cookie. Bạn gửi lại giúp mình nhé (<=10 dòng).")
        return WAIT_COOKIE

    # Validate input
    invalid = []
    for i, c in enumerate(cookies, start=1):
        if not is_probably_shopee_cookie(c):
            invalid.append(f"- Dòng {i}: sai định dạng (phải có SPC_ST=...)")

    if invalid:
        await update.message.reply_text(
            "❌ Cookie không hợp lệ:\n" + "\n".join(invalid) +
            "\n\n🍪 Gửi đúng Cookie định dạng: SPC_ST=...."
        )
        return WAIT_COOKIE

    await update.message.reply_text("⏳ Đang check đơn hàng...")

    try:
        data = await asyncio.to_thread(fetch_orders, cookies)

        # Chặn placeholder “đang chờ” khi cookie sai/hết hạn
        if count_real_orders_from_api(data) == 0:
            await update.message.reply_text(
                "❌ Cookie sai / hết hạn hoặc không có dữ liệu đơn hợp lệ.\n"
                "👉 Hãy lấy lại SPC_ST mới và thử lại."
            )
            return ConversationHandler.END

        messages = format_orders_for_telegram(data, max_orders_per_cookie=5)
        for msg in messages:
            # format_orders_for_telegram có backtick => dùng Markdown
            await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")

    return ConversationHandler.END

def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong Environment Variables.")

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(rf"^{re.escape(BTN_CHECK)}$"), handle_check_button)],
        states={WAIT_COOKIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cookie)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # nếu user gõ linh tinh ngoài flow
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    print("✅ check_order_shopee bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()