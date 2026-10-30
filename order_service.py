"""Shopee order API via KiotProxy + Telegram HTML product-link preview.

Replace order_service.py in the repository with this file.
Compatible with existing telegram_bot.py importing fetch_orders and
format_orders_for_telegram; returns one Telegram message per order so each order
can have its own Shopee link preview. No product images are sent separately.
"""

import html
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlsplit
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://us-central1-get-feedback-a0119.cloudfunctions.net/app"
API_ENDPOINT = "/api/shopee/getOrderDetailsForCookie"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://autopee.vercel.app",
    "Referer": "https://autopee.vercel.app/",
}
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


# Cùng quy tắc proxy với Google Apps Script, nhưng đây là kết nối HTTP(S)
# thực qua proxy (không đưa {"proxy": ...} vào JSON của API Autopee).
KIOT_API = "https://api.kiotproxy.com/api/v1/proxies"
KIOT_MIN_REMAINING_SECONDS = 120
KIOT_REGIONS = frozenset({"random", "bac", "trung", "nam"})
# Ngăn 2 lượt Check Cookie trong cùng tiến trình đổi IP khi lượt kia đang chạy.
_PROXY_REQUEST_LOCK = threading.Lock()


class KiotProxyError(RuntimeError):
    """Lỗi proxy đã làm sạch thông tin: không chứa API key hoặc cookie."""


def _kiot_config() -> tuple[str, str]:
    key = os.getenv("KIOTPROXY_KEY", "").strip()
    region = os.getenv("KIOTPROXY_REGION", "random").strip().lower() or "random"
    if not key:
        raise KiotProxyError("Chưa cấu hình KIOTPROXY_KEY trong Render Environment.")
    if region not in KIOT_REGIONS:
        raise KiotProxyError("KIOTPROXY_REGION chỉ nhận random, bac, trung hoặc nam.")
    return key, region


def _kiot_get(session: requests.Session, key: str, action: str,
              region: Optional[str] = None) -> Optional[dict]:
    """GET /current hoặc /new. Tuyệt đối không log URL chứa key."""
    params = {"key": key}
    if region:
        params["region"] = region
    try:
        response = session.get(
            f"{KIOT_API}/{action}", params=params,
            headers={"Accept": "application/json"}, timeout=(10, 15),
        )
    except requests.RequestException:
        raise KiotProxyError("Không kết nối được KiotProxy. Đã ngừng check.") from None
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise KiotProxyError("KiotProxy không trả JSON hợp lệ. Đã ngừng check.") from None
    if not isinstance(payload, dict):
        raise KiotProxyError("Dữ liệu KiotProxy không đúng định dạng.")
    code = payload.get("error")
    if isinstance(code, dict):
        code = code.get("code")
    if action == "current" and code == "PROXY_NOT_FOUND_BY_KEY":
        return None
    if response.status_code != 200 or payload.get("success") is not True:
        raise KiotProxyError(
            f"KiotProxy {action} lỗi HTTP {response.status_code}; "
            "kiểm tra key, thời hạn hoặc thời gian chờ đổi IP."
        )
    return payload


def _kiot_lease(payload: dict) -> dict:
    """Kiot timestamps theo mili-giây như Code.gs; tính thời gian bằng monotonic."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KiotProxyError("KiotProxy thiếu dữ liệu proxy.")
    raw_proxy = str(data.get("http") or "").strip()
    if not raw_proxy or any(ch in raw_proxy for ch in " /?#@\r\n\t"):
        raise KiotProxyError("KiotProxy trả host:port HTTP không hợp lệ.")
    try:
        parsed = urlsplit("http://" + raw_proxy)
        if not parsed.hostname or parsed.port is None or not (1 <= parsed.port <= 65535):
            raise ValueError()
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise KiotProxyError("KiotProxy trả host:port HTTP không hợp lệ.") from None
    try:
        expiry = float(data["expirationAt"])
        server_ms = float(payload.get("timestamp") or time.time() * 1000)
        if expiry < 1e11 or server_ms < 1e11:
            raise ValueError()
        ttc = max(0.0, float(data.get("ttc") or 0))
        next_at = float(data.get("nextRequestAt") or 0)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise KiotProxyError("KiotProxy thiếu thời gian expirationAt/ttc hợp lệ.") from None
    now = time.monotonic()
    return {
        "proxy_url": "http://" + raw_proxy,
        "remaining_until": now + max(0.0, (expiry - server_ms) / 1000),
        "change_until": now + max(0.0, ttc, (next_at - server_ms) / 1000),
    }


def _remaining(lease: dict) -> float:
    return max(0.0, lease["remaining_until"] - time.monotonic())


def _acquire_kiot_proxy(session: requests.Session, key: str, region: str) -> str:
    current = _kiot_get(session, key, "current")
    lease = _kiot_lease(current) if current is not None else None
    if lease and _remaining(lease) >= KIOT_MIN_REMAINING_SECONDS:
        return lease["proxy_url"]
    if lease:
        wait = max(0.0, lease["change_until"] - time.monotonic())
        if wait > 0:
            raise KiotProxyError(
                f"Proxy còn dưới {KIOT_MIN_REMAINING_SECONDS} giây và chưa thể đổi IP; "
                f"thử lại sau khoảng {int(wait + 0.999)} giây."
            )
    new_payload = _kiot_get(session, key, "new", region)
    new_lease = _kiot_lease(new_payload)
    if _remaining(new_lease) < KIOT_MIN_REMAINING_SECONDS:
        raise KiotProxyError("Proxy mới còn dưới 120 giây; dừng check để tránh lỗi.")
    return new_lease["proxy_url"]


def fetch_orders(cookies_list: List[str]) -> Dict[str, Any]:
    """Check Cookie Shopee bắt buộc đi qua KiotProxy; không direct fallback.

    - Dùng lại IP nếu còn >= 120s, tôn trọng ttc/nextRequestAt.
    - Gửi đúng JSON {"cookies": [...]} tới Autopee qua requests proxies.
    - Không gửi Vubel key/proxy JSON tới Autopee và không log cookie/key/IP.
    - GHN/SPX trong tracking_service không bị thay đổi.
    """
    if not cookies_list:
        raise ValueError("Danh sách cookie trống.")
    key, region = _kiot_config()
    # Khóa bao cả thời gian POST, tránh tự đổi IP giữa hai check chạy đồng thời.
    with _PROXY_REQUEST_LOCK:
        with requests.Session() as session:
            # Không sử dụng proxy môi trường của máy chủ thay thế KiotProxy.
            session.trust_env = False
            proxy_url = _acquire_kiot_proxy(session, key, region)
            try:
                response = session.post(
                    BASE_URL + API_ENDPOINT,
                    json={"cookies": cookies_list},
                    headers=HEADERS,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=(15, 60),
                )
            except requests.exceptions.ProxyError:
                raise KiotProxyError("Không kết nối được HTTP proxy KiotProxy; dừng check.") from None
            except requests.exceptions.Timeout:
                raise KiotProxyError("Quá thời gian chờ API qua KiotProxy; thử lại sau.") from None
            except requests.RequestException:
                raise RuntimeError("Lỗi kết nối API đơn hàng qua proxy; không gửi trực tiếp.") from None
    if response.status_code != 200:
        # Không đưa response.text ra chat/log (API có thể echo Cookie).
        raise RuntimeError(f"API kiểm tra đơn hàng lỗi HTTP {response.status_code} qua KiotProxy.")
    try:
        result = response.json()
    except (ValueError, TypeError):
        raise RuntimeError("API đơn hàng không trả JSON hợp lệ.") from None
    if not isinstance(result, dict):
        raise RuntimeError("API đơn hàng trả dữ liệu không đúng cấu trúc JSON object.")
    return result


def h(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=False)


def _get(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for key in keys:
        if key in d and d[key] not in (None, "", [], {}):
            return d[key]
    return default


def _fmt_ts(ts: Any) -> str:
    if ts in (None, ""):
        return ""
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)) or (isinstance(ts, str) and re.fullmatch(r"\d{10,13}", ts.strip())):
        n = float(ts)
        dt = datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)
    elif isinstance(ts, str):
        s = ts.strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            # Preserve already-human-readable API dates; don't invent timestamps.
            return s[:80]
    else:
        return str(ts)[:80]
    if dt.tzinfo is None:
        # API could already be returning local naive time; keep wall time.
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    return dt.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M:%S")


def _fmt_money_from_api(v: Any) -> str:
    """Display currency only when API provides a value; supports 1e5 Shopee scale."""
    if v in (None, ""):
        return ""
    try:
        raw = str(v).replace(",", "").replace("₫", "").replace("đ", "").strip()
        amount = float(raw)
        if abs(amount) >= 1_000_000_000:
            # Shopee integer prices are often in 1/100000 VND units.
            amount /= 100_000
        return f"{amount:,.0f}".replace(",", ".") + "đ"
    except (ValueError, TypeError, OverflowError):
        return str(v)[:80]


def _build_shopee_link(shop_id: Any, item_id: Any) -> Optional[str]:
    try:
        if shop_id and item_id:
            return f"https://shopee.vn/product/{int(shop_id)}/{int(item_id)}"
    except (TypeError, ValueError):
        pass
    return None


def _safe_trim(s: Any, n: int) -> str:
    text = "" if s is None else str(s).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _split_address_for_ui(full_address: Any) -> Tuple[str, str]:
    address = "" if full_address is None else str(full_address).strip()
    if not address:
        return "", ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) >= 2:
        return ", ".join(parts[:-1]), parts[-1]
    return address, ""


def _detect_carrier_from_tracking(tracking_id: Any) -> str:
    code = str(tracking_id or "").strip().upper()
    if code.startswith("SPX"):
        return "Shopee Express"
    if code.startswith("GY"):
        return "Giao Hàng Nhanh"
    return ""


def _safe_shopee_url(url: Any) -> Optional[str]:
    """Allow real Shopee product links (including shortened Shopee URLs)."""
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme == "https" and
                (host == "shopee.vn" or host.endswith(".shopee.vn") or
                 host == "shope.ee" or host.endswith(".shope.ee")) and
                not any(ch in candidate for ch in "\n\r\t <>\"'")):
            return candidate
    except ValueError:
        pass
    return None


def _product_link(product: Dict[str, Any], order: Dict[str, Any]) -> Optional[str]:
    # Respect a direct/short product URL from API first, if available.
    for key in ("product_url", "product_link", "item_url", "item_link", "link", "url", "shopee_url", "share_url"):
        url = _safe_shopee_url(product.get(key))
        if url:
            return url
    return _build_shopee_link(
        _get(product, ["shop_id", "shopid", "shopId"], _get(order, ["shop_id", "shopid", "shopId"])),
        _get(product, ["item_id", "itemid", "itemId", "product_id", "productId"]),
    )


def _first_object(d: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    value = _get(d, keys, {})
    return value if isinstance(value, dict) else {}


def _payment_description(order: Dict[str, Any], shipping: Dict[str, Any]) -> str:
    name = _get(shipping, ["shipping_method", "shipping_service", "shipping_service_name", "shipping_option_name", "shipping_name", "service_name"], "")
    method = _get(order, ["payment_method_name", "payment_method", "payment_type"], "")
    if not name:
        name = _get(order, ["shipping_method", "shipping_service", "shipping_option_name"], "")
    if name and method:
        return f"{name} ({method})"
    return str(name or method or "")


def _cod_amount(order: Dict[str, Any], shipping: Dict[str, Any]) -> str:
    # Do not substitute order total for COD; they are different concepts.
    fields = ["cod_amount", "cod_price", "cod_total", "cash_on_delivery_amount", "amount_to_collect"]
    raw = _get(order, fields, None)
    if raw is None:
        raw = _get(shipping, fields, None)
    return _fmt_money_from_api(raw)


def _format_one_order(order: Dict[str, Any], idx: int, max_products_per_order: int) -> str:
    order_id = _get(order, ["order_id", "orderid", "order_sn", "orderSn", "id"], "")
    status = _get(order, ["status_description", "status", "order_status", "tracking_info_description"], "")
    delivery_status = _get(order, ["tracking_info_description", "delivery_status_description", "shipping_status"], "")
    placed = _fmt_ts(_get(order, ["create_time", "order_time", "ctime", "created_at", "created_time", "order_created_at"], ""))
    address = _first_object(order, ["address", "recipient", "shipping_address"])
    person = _get(address, ["shipping_name", "name", "receiver_name", "recipient_name"], "")
    phone = _get(address, ["shipping_phone", "phone", "receiver_phone", "recipient_phone"], "")
    addr = _get(address, ["shipping_address", "address", "full_address", "address_full"], "")
    if isinstance(addr, dict):
        addr = _get(addr, ["full_address", "address"], "")
    shipping = _first_object(order, ["shipping", "shipment", "logistics"])
    tracking = _get(order, ["tracking_number", "tracking_no", "tracking", "tracking_id"], "")
    carrier = _detect_carrier_from_tracking(tracking) or _get(shipping, ["shipping_carrier", "carrier", "carrier_name"], "") or _get(order, ["shipping_carrier"], "")
    products = _get(order, ["product_info", "products", "items", "item_list"], [])
    if isinstance(products, dict):
        products = [products]
    if not isinstance(products, list):
        products = []

    parts: List[str] = []
    if idx > 1:
        parts.append(f"📦 <b>ĐƠN HÀNG {idx}</b>")
    parts.append("Tình trạng: " + h(status or "Chưa có dữ liệu"))
    parts.append("Mã đơn hàng: " + (f"<code>{h(order_id)}</code>" if order_id else "Chưa có dữ liệu"))
    parts.append("Thời gian đặt hàng: " + h(placed or "Chưa có dữ liệu"))
    parts.append("\n📦 <b>ĐỊA CHỈ NHẬN HÀNG</b>")
    for val in (person, phone, addr):
        if val:
            parts.append(h(_safe_trim(val, 400)))
    if not any((person, phone, addr)):
        parts.append("Chưa có thông tin người nhận")

    for pidx, product in enumerate(products[:max_products_per_order], start=1):
        if not isinstance(product, dict):
            continue
        parts.append(f"\n🛍 <b>SẢN PHẨM {pidx}</b>")
        name = _get(product, ["name", "product_name", "title", "item_name"], "")
        variation = _get(product, ["model_name", "variation", "classification", "model", "variation_name"], "")
        qty = _get(product, ["quantity", "qty", "amount", "item_count"], None)
        if name:
            parts.append("Tên sản phẩm: " + h(_safe_trim(name, 220)))
        if variation:
            parts.append("Phân loại: " + h(_safe_trim(variation, 120)))
        if qty not in (None, ""):
            parts.append("Số lượng: " + h(qty))
        link = _product_link(product, order)
        if link:
            # Plain visible URL: Telegram can produce a native product preview.
            parts.append("Liên kết: " + h(link))
    if len(products) > max_products_per_order:
        parts.append(f"… và {len(products) - max_products_per_order} sản phẩm khác")

    parts.append("\n🚚 <b>ĐƠN VỊ VẬN CHUYỂN</b>")
    method = _payment_description(order, shipping)
    if method:
        parts.append(h(method))
    if carrier:
        parts.append("Đơn vị vận chuyển: " + h(carrier))
    if tracking:
        parts.append("Mã vận đơn: <code>" + h(tracking) + "</code>")
    if delivery_status:
        parts.append("Thông tin: " + h(_safe_trim(delivery_status, 300)))
    cod = _cod_amount(order, shipping)
    if cod:
        parts.append("\n💵 Vui lòng thanh toán " + h(cod) + " khi nhận hàng")

    # Avoid invalid HTML resulting from arbitrarily slicing a message mid-tag.
    text = "\n".join(parts).strip()
    if len(text) > 3900:
        # Shorten the free-form fields by dropping longer sections (keep product URL).
        parts = [p for p in parts if len(p) <= 450]
        text = "\n".join(parts).strip()
    if len(text) > 3900:
        # This is exceptionally long; retain the link and the most important fields.
        core = [p for p in parts if len(p) < 240]
        text = "\n".join(core).strip()
    return text


def format_orders_for_telegram(
    data: Dict[str, Any],
    max_orders_per_cookie: int = 5,
    max_products_per_order: int = 5,
) -> List[str]:
    """Return one HTML-safe Telegram message per order.

    The existing telegram_bot.py sends the returned messages via reply_text with
    parse_mode='HTML', allowing Telegram to preview the first Shopee product URL.
    Product photos are NOT downloaded or sent via Telegram's photo API.
    """
    accounts = data.get("allOrderDetails") or []
    if not isinstance(accounts, list) or not accounts:
        return ["❌ Không có dữ liệu đơn hàng. (API trả rỗng)"]

    messages: List[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        orders = account.get("orderDetails") or []
        if not isinstance(orders, list):
            continue
        if not orders:
            messages.append("❌ Không có đơn hàng trong tài khoản này.")
            continue
        for idx, order in enumerate(orders[:max_orders_per_cookie], start=1):
            if not isinstance(order, dict):
                continue
            msg = _format_one_order(order, idx, max_products_per_order)
            if msg:
                messages.append(msg)
        omitted = len(orders) - max_orders_per_cookie
        if omitted > 0:
            messages.append(f"ℹ️ Còn {omitted} đơn chưa hiển thị (giới hạn mỗi cookie: {max_orders_per_cookie}).")
    return messages or ["❌ Không có dữ liệu đơn hàng hợp lệ."]
