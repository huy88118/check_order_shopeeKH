import os
import threading
import asyncio
import re
from typing import List, Dict, Any

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from order_service import fetch_orders, format_orders_for_telegram
from tracking_service import (
    detect_tracking_carrier,
    fetch_tracking_spx,
    fetch_tracking_ghn,
)

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
WAIT_INPUT = 1

# =======================
# UI
# =======================
BTN_CHECK = "📦 Check MVĐ"
CB_CONTINUE = "continue_check"

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_CHECK)]],
        resize_keyboard=True
    )

def continue_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Bấm để tiếp tục check", callback_data=CB_CONTINUE)]]
    )

# =======================
# Validation / Anti-placeholder
# =======================
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
# Tracking formatter
# =======================
def format_tracking_for_telegram(tdata: Dict[str, Any], max_events: int = 10) -> str:
    carrier = tdata.get("carrier", "")
    code = tdata.get("code", "")
    status = tdata.get("current_status", "")
    link = tdata.get("link", "")

    lines = []
    if carrier:
        lines.append(f"🚚 *Đơn vị*: {carrier}")
    if code:
        lines.append(f"🧾 *MVĐ*: `{code}`")
    if status:
        lines.append(f"📌 *Trạng thái*: {status}")

    # GHN extra
    if tdata.get("from_address") and tdata.get("to_address"):
        lines.append(f"📦 Tuyến: {tdata['from_address']} ➜ {tdata['to_address']}")
    if tdata.get("to_name"):
        lines.append(f"👤 Người nhận: {tdata['to_name']}")

    # SPX extra
    if tdata.get("raw_sls_tn"):
        lines.append(f"🔎 *Mã liên kết*: `{tdata['raw_sls_tn']}`")

    evs = tdata.get("events") or []
    if evs:
        lines.append("\n📍 *Hành trình gần nhất:*")
        for e in evs[:max_events]:
            t = (e.get("time") or "").strip()
            st = (e.get("status") or "").strip()
            de = (e.get("detail") or "").strip()
            one = " - ".join([x for x in [t, st, de] if x])
            if one:
                lines.append(f"• {one}")
        if len(evs) > max_events:
            lines.append(f"… +{len(evs)-max_events} dòng khác (xem link)")

    if link:
        lines.append(f"\n🔗 {link}")

    return "\n".join(lines).strip()

# =======================
# Helpers: ask input
# =======================
async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE, *, via_query: bool = False):
    text = (
        "🍪 Gửi Cookie theo định dạng:\n"
        "SPC_ST=....\n\n"
        "📦 Hoặc gửi *Mã vận đơn* để xem hành trình:\n"
        "- SPX / SPXVN... (Shopee Express)\n"
        "- GY... (GHN)\n\n"
        "💡 Cookie: tối đa 10 dòng (mỗi cookie 1 dòng)."
    )
    if via_query:
        # khi bấm inline button thì dùng edit_message_text cho gọn
        q = update.callback_query
        await q.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

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
    await ask_for_input(update, context)
    return WAIT_INPUT

async def continue_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # bấm nút => quay lại WAIT_INPUT
    await query.message.reply_text("🔁 OK, gửi Cookie hoặc MVĐ để check tiếp nhé!")
    await ask_for_input(update, context, via_query=True)
    return WAIT_INPUT

async def receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    if not raw:
        await update.message.reply_text("❌ Bạn chưa gửi gì cả. Gửi lại giúp mình nhé.")
        return WAIT_INPUT

    # =========================
    # 1) Nếu là mã vận đơn => tracking
    # =========================
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
                    reply_markup=continue_inline_keyboard()
                )
                return ConversationHandler.END

            msg = format_tracking_for_telegram(tdata, max_events=10)
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=continue_inline_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi check vận đơn: {e}", reply_markup=continue_inline_keyboard())

        return ConversationHandler.END

    # =========================
    # 2) Nếu là cookie => check đơn hàng
    # =========================
    cookies = [line.strip() for line in raw.splitlines() if line.strip()]

    if len(cookies) > 10:
        await update.message.reply_text("❌ Tối đa 10 cookie. Bạn gửi lại giúp mình nhé (<=10 dòng).")
        return WAIT_INPUT

    invalid = []
    for i, c in enumerate(cookies, start=1):
        if not is_probably_shopee_cookie(c):
            invalid.append(f"- Dòng {i}: sai định dạng (phải có SPC_ST=...)")

    if invalid:
        await update.message.reply_text(
            "❌ Không nhận diện được *MVĐ* và Cookie cũng không hợp lệ.\n\n"
            "✅ Bạn hãy gửi:\n"
            "• Cookie đúng dạng: `SPC_ST=....`\n"
            "• Hoặc MVĐ: `SPXVN...` / `SPX...` / `GY...`\n\n"
            "Chi tiết lỗi cookie:\n" + "\n".join(invalid),
            parse_mode="Markdown"
        )
        return WAIT_INPUT

    await update.message.reply_text("⏳ Đang check đơn hàng...")

    try:
        data = await asyncio.to_thread(fetch_orders, cookies)

        # Chặn placeholder “đang chờ” khi cookie sai/hết hạn
        if count_real_orders_from_api(data) == 0:
            await update.message.reply_text(
                "❌ Cookie sai / hết hạn hoặc không có dữ liệu đơn hợp lệ.\n"
                "👉 Hãy lấy lại SPC_ST mới và thử lại.",
                reply_markup=continue_inline_keyboard()
            )
            return ConversationHandler.END

        messages = format_orders_for_telegram(data, max_orders_per_cookie=5)
        for i, msg in enumerate(messages):
            # chỉ gắn nút continue ở tin cuối cho gọn
            if i == len(messages) - 1:
                await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=continue_inline_keyboard())
            else:
                await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}", reply_markup=continue_inline_keyboard())

    return ConversationHandler.END

def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong Environment Variables.")

    threading.Thread(target=run_web, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(rf"^{re.escape(BTN_CHECK)}$"), handle_check_button)],
        states={
            WAIT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_input),
                CallbackQueryHandler(continue_check_callback, pattern=f"^{CB_CONTINUE}$"),
            ]
        },
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
