"""Shopee order check: Vubel V7 + KiotProxy, ported from user's Apps Script.

Telegram bot imports fetch_orders, format_orders_for_telegram, KiotProxyError.
Vubel—not Autopee—receives Cookie header and JSON {"proxy":"http://..."}.
Do not log API keys, cookie contents, proxy IP or full responses.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlsplit
from zoneinfo import ZoneInfo

import requests

VUBEL_API_URL = "https://api.vubel.store/v1/shopee/get_order"
KIOT_API = "https://api.kiotproxy.com/api/v1/proxies"
MIN_PROXY_SECONDS = 120
REGIONS = {"random", "bac", "trung", "nam"}
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LOCK = threading.Lock()
COOKIE_ERRORS = {"cookie_expired", "invalid_cookie", "invalid_spc_st", "session_expired"}


class KiotProxyError(RuntimeError):
    """Safe-to-display proxy error without credentials."""


class VubelError(RuntimeError):
    """Safe-to-display Vubel error without cookies or keys."""


def _config():
    key = os.getenv("KIOTPROXY_KEY", "").strip()
    vubel = os.getenv("VUBEL_API_KEY", "").strip()
    region = os.getenv("KIOTPROXY_REGION", "random").strip().lower() or "random"
    if not key:
        raise KiotProxyError("Render chưa có KIOTPROXY_KEY.")
    if not vubel:
        raise VubelError("Render chưa có VUBEL_API_KEY (cùng loại key đang dùng ở Apps Script).")
    if region not in REGIONS:
        raise KiotProxyError("KIOTPROXY_REGION chỉ nhận random/bac/trung/nam.")
    return key, vubel, region


def _kiot_get(session: requests.Session, key: str, action: str, region: str = ""):
    params = {"key": key}
    if region:
        params["region"] = region
    try:
        response = session.get(
            KIOT_API + "/" + action, params=params,
            headers={"Accept": "application/json"}, timeout=(10, 20),
        )
    except requests.RequestException:
        raise KiotProxyError("Không kết nối được KiotProxy.") from None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise KiotProxyError("KiotProxy trả dữ liệu không phải JSON.") from None
    if not isinstance(payload, dict):
        raise KiotProxyError("Phản hồi KiotProxy không hợp lệ.")
    code = payload.get("error")
    if isinstance(code, dict):
        code = code.get("code")
    if action == "current" and code == "PROXY_NOT_FOUND_BY_KEY":
        return None
    if response.status_code != 200 or payload.get("success") is not True:
        raise KiotProxyError(
            f"KiotProxy {action} lỗi HTTP {response.status_code}; kiểm tra hạn key/ttc."
        )
    return payload


def _lease(payload: dict):
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KiotProxyError("KiotProxy thiếu data.")
    proxy = str(data.get("http") or "").strip()
    try:
        if not proxy or any(ch in proxy for ch in " /?#@\r\n\t"):
            raise ValueError()
        parsed = urlsplit("http://" + proxy)
        if not parsed.hostname or not parsed.port or parsed.username or parsed.password:
            raise ValueError()
        expiry_ms = float(data["expirationAt"])
        server_ms = float(payload.get("timestamp") or time.time() * 1000)
        if expiry_ms < 1e11 or server_ms < 1e11:
            raise ValueError()
        ttc = max(0.0, float(data.get("ttc") or 0))
        next_ms = float(data.get("nextRequestAt") or 0)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise KiotProxyError("KiotProxy trả proxy/thời hạn không hợp lệ.") from None
    now = time.monotonic()
    return {
        "proxy": "http://" + proxy,
        "remaining_until": now + max(0, (expiry_ms - server_ms) / 1000),
        "change_until": now + max(ttc, (next_ms - server_ms) / 1000, 0),
    }


def _remaining(lease):
    return max(0.0, lease["remaining_until"] - time.monotonic())


def _acquire(session, key, region):
    reply = _kiot_get(session, key, "current")
    lease = _lease(reply) if reply else None
    if lease and _remaining(lease) >= MIN_PROXY_SECONDS:
        return lease
    if lease and lease["change_until"] > time.monotonic():
        waiting = int(lease["change_until"] - time.monotonic() + 0.999)
        raise KiotProxyError(f"IP còn dưới 120 giây; Kiot chưa cho đổi. Thử lại sau {waiting} giây.")
    lease = _lease(_kiot_get(session, key, "new", region))
    if _remaining(lease) < MIN_PROXY_SECONDS:
        raise KiotProxyError("Proxy mới còn dưới 120 giây; không gửi request.")
    return lease


def _parse_cookie(raw):
    raw = str(raw or "").strip()
    raw = re.sub(r"^Cookie:\s*", "", raw, flags=re.I)
    st = re.search(r"(?:^|;\s*)SPC_ST\s*=\s*([^;\s]+)", raw, flags=re.I)
    if not st:
        raise ValueError("Cookie phải chứa SPC_ST=... hợp lệ.")
    fp = re.search(r"(?:^|;\s*)SPC_F\s*=\s*([^;\s]+)", raw, flags=re.I)
    return "SPC_ST=" + st.group(1) + (("; SPC_F=" + fp.group(1)) if fp else "")


def _error_code(payload):
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error")
    fields = [err if isinstance(err, str) else err.get("code") if isinstance(err, dict) else None,
              payload.get("error_code"), payload.get("errorCode"), payload.get("code")]
    data = payload.get("data")
    if isinstance(data, dict):
        fields.append(data.get("error"))
    for code in fields:
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", code.strip()):
            return code.strip().lower()
    return ""


def _fetch_vubel(session, api_key, cookie, proxy_url):
    # Exactly the same transport contract as user's Apps Script:
    # HTTP request Render -> Vubel direct; Vubel -> Shopee through proxy in JSON.
    try:
        response = session.post(
            VUBEL_API_URL,
            headers={"x-api-key": api_key, "Cookie": cookie,
                     "Accept": "application/json", "Content-Type": "application/json"},
            json={"proxy": proxy_url},
            allow_redirects=False,
            timeout=(15, 60),
        )
    except requests.RequestException:
        raise VubelError("Không kết nối được Vubel; không kết luận cookie chết.") from None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise VubelError(f"Vubel HTTP {response.status_code} không trả JSON hợp lệ.") from None
    if not isinstance(payload, dict):
        raise VubelError("Vubel trả JSON không phải object.")
    errcode = _error_code(payload)
    if payload.get("ok") is not True and errcode in COOKIE_ERRORS:
        return {"cookieInvalid": True, "orders": [], "account": {}}
    if response.status_code != 200 or payload.get("ok") is not True:
        detail = " | error=" + errcode if errcode else ""
        raise VubelError(
            f"Vubel HTTP {response.status_code}{detail}; lỗi API/key không đồng nghĩa cookie hết hạn."
        )
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
        raise VubelError("Vubel thiếu data.orders; không thể kết luận tài khoản không có đơn.")
    return {"cookieInvalid": False, "orders": data["orders"],
            "account": data.get("account") if isinstance(data.get("account"), dict) else {}}


def fetch_orders(cookies_list: list[str]) -> dict:
    """Call Vubel *once per SPC_ST*, not the Autopee cookies[] endpoint.

    Return a compatibility envelope for telegram_bot.py. Each account includes
    orderDetails (Vubel data.orders); cookieInvalid true only on explicit error.
    """
    if not cookies_list:
        raise ValueError("Danh sách Cookie trống.")
    kiot_key, vubel_key, region = _config()
    accounts = []
    # Lock prevents parallel checks from conflicting on a rotating shared proxy.
    with _LOCK:
        with requests.Session() as session:
            session.trust_env = False
            lease = None
            for index, raw in enumerate(cookies_list, start=1):
                try:
                    cookie = _parse_cookie(raw)
                except ValueError:
                    accounts.append({"cookie_index": index, "cookieInvalid": False,
                                     "error": "Cookie sai định dạng SPC_ST", "orderDetails": []})
                    continue
                if lease is None or _remaining(lease) < MIN_PROXY_SECONDS:
                    lease = _acquire(session, kiot_key, region)
                result = _fetch_vubel(session, vubel_key, cookie, lease["proxy"])
                account = dict(result["account"])
                account.update({"cookie_index": index, "cookieInvalid": result["cookieInvalid"],
                                "orderDetails": result["orders"]})
                accounts.append(account)
    return {"source": "vubel", "allOrderDetails": accounts}


def _first(obj, *keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _t(v, length=450):
    return html.escape(str(v).strip()[:length], quote=False) if v is not None else ""


def _time(v):
    if v in (None, ""):
        return ""
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{10,13}", v)):
            x = float(v)
            if x > 1e11:
                x /= 1000
            return datetime.fromtimestamp(x, tz=timezone.utc).astimezone(VN_TZ).strftime("%H:%M:%S %d/%m/%Y")
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt.astimezone(VN_TZ).strftime("%H:%M:%S %d/%m/%Y") if dt.tzinfo else v
    except (OverflowError, ValueError, TypeError):
        pass
    return str(v)[:80]


def _link(item, order):
    if not isinstance(item, dict):
        return ""
    for name in ("product_url", "product_link", "item_url", "item_link", "shopee_url", "share_url", "url", "link"):
        url = _first(item, name)
        if isinstance(url, str):
            try:
                p = urlparse(url)
                host = (p.hostname or "").lower()
                if p.scheme == "https" and (host == "shopee.vn" or host.endswith(".shopee.vn")
                    or host == "shope.ee" or host.endswith(".shope.ee")):
                    return url
            except ValueError:
                pass
    shop = _first(item, "shop_id", "shopid", "shopId") or _first(order, "shop_id", "shopid", "shopId")
    product = _first(item, "item_id", "itemid", "itemId", "product_id", "productId")
    if shop and product and str(shop).isdigit() and str(product).isdigit():
        return f"https://shopee.vn/product/{shop}/{product}"
    return ""


def _item_lists(order):
    items = _first(order, "items", "products", "product_info", "item_list") or []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    # Some Vubel payloads may group products in shops[].items.
    if not items:
        shops = _first(order, "shops") or []
        if isinstance(shops, list):
            for shop in shops:
                if isinstance(shop, dict):
                    nested = _first(shop, "items", "products", "item_list") or []
                    if isinstance(nested, list):
                        for item in nested:
                            if isinstance(item, dict):
                                items.append(dict(item, shop_id=item.get("shop_id") or shop.get("shopId") or shop.get("shop_id")))
    return [x for x in items if isinstance(x, dict)]


def _shipment(order):
    ship = _first(order, "shipping", "shipment", "logistics") or {}
    if not isinstance(ship, dict):
        return {}, {}
    shipments = ship.get("shipments") or []
    return ship, next((x for x in shipments if isinstance(x, dict)), {}) if isinstance(shipments, list) else {}


def _format_order(order, idx, max_products):
    ship, first_ship = _shipment(order)
    mvd = _first(order, "tracking", "trackingNumber", "tracking_number", "tracking_no") or _first(first_ship, "trackingNumber", "tracking_number")
    status = _first(order, "statusLabel", "statusText", "status_description", "statusKey", "status", "order_status")
    events = _first(first_ship, "events") or _first(order, "events") or []
    recent = ""
    if isinstance(events, list):
        ordered = sorted((e for e in events if isinstance(e, dict)), key=lambda e: float(e.get("timestamp") or 0) if str(e.get("timestamp") or "").replace(".", "", 1).isdigit() else 0, reverse=True)
        for evt in ordered:
            recent = _first(evt, "description", "buyer_description", "status_description") or ""
            if recent:
                break
    detail = recent or _first(order, "tracking_info_description", "delivery_status_description", "statusText")
    recipient = _first(order, "recipient", "receiver", "recipientAddress")
    rec = recipient if isinstance(recipient, dict) else {}
    person = _first(rec, "name", "recipient_name", "shipping_name") or (recipient if isinstance(recipient, str) else "")
    phone = _first(rec, "phone", "recipient_phone") or _first(order, "phone", "recipientPhone")
    address = _first(rec, "address", "full_address", "shipping_address") or _first(order, "address", "full_address")
    if isinstance(address, dict):
        address = _first(address, "full_address", "address")
    amount = _first(order, "total", "totalAmount", "total_amount", "totalPayment", "finalTotal")
    cod = _first(order, "cod_amount", "cash_on_delivery_amount", "amount_to_collect") or _first(ship, "cod_amount")
    placed = _time(_first(order, "createTime", "create_time", "orderTime", "order_time", "created_at", "ctime"))
    order_id = _first(order, "orderSn", "order_sn", "orderId", "order_id", "id")
    carrier = _first(first_ship, "carrierName", "carrier", "carrier_name") or _first(ship, "carrierName", "carrier", "carrier_name") or _first(order, "shippingCarrier", "carrier")
    if not carrier and mvd:
        carrier = "Shopee Express" if str(mvd).upper().startswith("SPX") else "GHN" if str(mvd).upper().startswith("GY") else ""
    lines = []
    if idx > 1:
        lines.append(f"📦 <b>ĐƠN HÀNG {idx}</b>")
    lines.append("Tình trạng: " + _t(status or "Chưa có dữ liệu"))
    if order_id:
        lines.append("Mã đơn hàng: <code>" + _t(order_id) + "</code>")
    if placed:
        lines.append("Thời gian đặt hàng: " + _t(placed))
    lines.append("\n📦 <b>ĐỊA CHỈ NHẬN HÀNG</b>")
    for value in (person, phone, address):
        if value:
            lines.append(_t(value))
    if not any((person, phone, address)):
        lines.append("API không trả thông tin nhận hàng")
    items = _item_lists(order)
    displayed = 0
    for item in items[:max_products]:
        name = _first(item, "name", "itemName", "product_name", "title", "item_name")
        variation = _first(item, "variation", "model_name", "modelName", "model", "variation_name")
        qty = _first(item, "quantity", "qty", "model_quantity_purchased")
        link = _link(item, order)
        lines_item = [f"\n🛍 <b>SẢN PHẨM {displayed + 1}</b>"]
        if name:
            lines_item.append("Tên sản phẩm: " + _t(name, 230))
        if variation:
            lines_item.append("Phân loại: " + _t(variation, 100))
        if qty is not None:
            lines_item.append("Số lượng: " + _t(qty))
        if link:
            lines_item.append("Liên kết: " + _t(link, 500))
        if len("\n".join(lines + lines_item)) > 2950:
            break
        lines.extend(lines_item)
        displayed += 1
    if not items:
        lines.append("\n🛍 <b>SẢN PHẨM</b>\nAPI không trả chi tiết sản phẩm/link")
    elif displayed < len(items):
        lines.append(f"… còn {len(items) - displayed} sản phẩm không hiển thị trong tin này")
    lines.append("\n🚚 <b>ĐƠN VỊ VẬN CHUYỂN</b>")
    if carrier:
        lines.append("Đơn vị vận chuyển: " + _t(carrier))
    if mvd:
        lines.append("Mã vận đơn: <code>" + _t(mvd) + "</code>")
    if detail:
        lines.append("Thông tin: " + _t(detail))
    if amount is not None:
        lines.append("Tổng thanh toán: " + _t(amount))
    if cod is not None:
        lines.append("💵 Tiền thu hộ (COD): " + _t(cod))
    return "\n".join(lines)


def format_orders_for_telegram(data: dict, max_orders_per_cookie: int = 5,
                               max_products_per_order: int = 5) -> list[str]:
    """Format Vubel data.orders per cookie, keeping plain Shopee product URL preview."""
    accounts = data.get("allOrderDetails") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return ["❌ API Vubel trả dữ liệu không hợp lệ."]
    messages = []
    multiple = len(accounts) > 1
    for account in accounts:
        if not isinstance(account, dict):
            continue
        index = account.get("cookie_index", 1)
        prefix = f"🍪 <b>COOKIE {index}</b>\n" if multiple else ""
        if account.get("cookieInvalid"):
            messages.append(prefix + "❌ Cookie die / Hết hạn (Vubel xác nhận).")
            continue
        if account.get("error"):
            messages.append(prefix + "❌ " + _t(account["error"]))
            continue
        orders = account.get("orderDetails") or []
        if not isinstance(orders, list):
            messages.append(prefix + "❌ Dữ liệu đơn hàng không hợp lệ.")
            continue
        if not orders:
            messages.append(prefix + "ℹ️ Vubel trả về 0 đơn hàng; không kết luận cookie hết hạn.")
            continue
        for idx, order in enumerate(orders[:max_orders_per_cookie], 1):
            if isinstance(order, dict):
                messages.append(prefix + _format_order(order, idx, max_products_per_order))
        if len(orders) > max_orders_per_cookie:
            messages.append(prefix + f"ℹ️ Chưa hiển thị {len(orders) - max_orders_per_cookie} đơn vì giới hạn tin nhắn.")
    return messages or ["ℹ️ Không có đơn hàng để hiển thị."]
