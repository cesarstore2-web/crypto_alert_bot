import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SYMBOLS = ["BERAUSDT", "XLMUSDT", "ZECUSDT", "BTCUSDT", "ETHUSDT"]
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = "state.json"

BASE ="https://api.gateio.ws/api/v4"

INTERVAL_MS = {
    "1h": 3600 * 1000,
    "15m": 15 * 60 * 1000,
    "5m": 5 * 60 * 1000,
}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from e


def get_klines(symbol, interval, limit):
    gate_symbol = symbol.replace("USDT", "_USDT") if "_" not in symbol else symbol
    params = urllib.parse.urlencode({
        "currency_pair": gate_symbol,
        "interval": interval,
        "limit": limit,
    })
    raw_data = get_json(f"{BASE}/spot/candlesticks?{params}")

    step = INTERVAL_MS.get(interval, 60 * 1000)

    # Gate.io: [time, quote_volume, close, high, low, open, ...]
    formatted = []
    for c in raw_data:
        t_str, vol, close_p, high_p, low_p, open_p = c[:6]
        open_time = int(t_str) * 1000
        close_time = open_time + step - 1
        formatted.append([open_time, open_p, high_p, low_p, close_p, vol, close_time])

    formatted.sort(key=lambda x: x[0])
    return formatted


def ema(values, period):
    alpha = 2 / (period + 1)
    result = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        result.append(current)
    return result


def send_telegram(message):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    request = urllib.request.Request(url, data=data)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Telegram HTTP {e.code} - check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
        ) from e

    if not result.get("ok"):
        raise RuntimeError(f"Telegram message failed: {result}")


def load_state():
    try:
        with open(STATE_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as file:
        json.dump(state, file, indent=2)


def fmt(value):
    return f"{value:.8f}".rstrip("0").rstrip(".")


def scan(symbol):
    h1 = get_klines(symbol, "1h", 220)
    m15 = get_klines(symbol, "15m", 100)
    m5 = get_klines(symbol, "5m", 100)

    now = int(time.time() * 1000)

    # الشموع المغلقة فقط
    h1 = [c for c in h1 if int(c[6]) < now]
    m15 = [c for c in m15 if int(c[6]) < now]
    m5 = [c for c in m5 if int(c[6]) < now]

    if len(h1) < 205 or len(m15) < 25 or len(m5) < 20:
        return None

    close1 = [float(c[4]) for c in h1]
    close15 = [float(c[4]) for c in m15]

    trend = "BUY" if close1[-1] > ema(close1, 200)[-1] else "SELL"
    ema15 = ema(close15, 20)[-1]

    if trend == "BUY" and close15[-1] <= ema15:
        return None
    if trend == "SELL" and close15[-1] >= ema15:
        return None

    # A = شمعة الـ Sweep ، C = شمعة التأكيد ، والـ FVG هو الفجوة بينهما
    a = m5[-3]
    c = m5[-1]
    prior = m5[-15:-3]  # 12 شمعة قبل A

    ref_low = min(float(x[3]) for x in prior)
    ref_high = max(float(x[2]) for x in prior)

    a_high, a_low, a_close = float(a[2]), float(a[3]), float(a[4])
    c_open, c_high, c_low, c_close = (
        float(c[1]), float(c[2]), float(c[3]), float(c[4])
    )

    side = None

    if trend == "BUY":
        if a_low < ref_low and a_close > ref_low and c_low > a_high and c_close > c_open:
            side = "BUY"

    if trend == "SELL":
        if a_high > ref_high and a_close < ref_high and c_high < a_low and c_close < c_open:
            side = "SELL"

    if side is None:
        return None

    ranges = [float(x[2]) - float(x[3]) for x in m5[-15:-1]]
    atr_proxy = sum(ranges) / len(ranges)

    entry = c_close

    if side == "BUY":
        stop = a_low - 0.25 * atr_proxy
        risk = entry - stop
        if risk <= 0:
            return None
        tp1 = entry + risk
        tp2 = entry + 2 * risk
    else:
        stop = a_high + 0.25 * atr_proxy
        risk = stop - entry
        if risk <= 0:
            return None
        tp1 = entry - risk
        tp2 = entry - 2 * risk

    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "candle": str(c[6]),
    }


def get_active_pairs():
    try:
        info = get_json(BASE + "/spot/currency_pairs")
        return {
            item["id"].replace("_", "")
            for item in info
            if item.get("trade_status") == "tradable"
        }
    except Exception as error:
        print("Could not load pairs list, scanning all symbols:", str(error))
        return set(SYMBOLS)


def main():
    active = get_active_pairs()
    state = load_state()
    send_telegram("اختبار: البوت يعمل ✅")

    for symbol in SYMBOLS:
        if symbol not in active:
            print("Unavailable Spot pair:", symbol)
            continue

        try:
            signal = scan(symbol)

            if not signal:
                print("No signal:", symbol)
                continue

            key = symbol + ":" + signal["side"]

            if state.get(key) == signal["candle"]:
                print("Already sent:", symbol)
                continue

            meaning = (
                "شراء محتمل"
                if signal["side"] == "BUY"
                else "تنبيه بيع أو تخفيف مركز Spot قائم، وليس فتح Short"
            )

            message = (
                f"🔔 CRYPTO ALERT\n"
                f"العملة: {symbol}\n"
                f"الإشارة: {signal['side']}\n"
                f"المعنى: {meaning}\n"
                f"الفريمات: 1H / 15M / 5M\n"
                f"الدخول المرجعي: {fmt(signal['entry'])}\n"
                f"وقف الخسارة: {fmt(signal['stop'])}\n"
                f"الهدف الأول: {fmt(signal['tp1'])}\n"
                f"الهدف الثاني: {fmt(signal['tp2'])}\n"
                f"الشروط: EMA + Liquidity Sweep + FVG\n"
                f"تنبيه تجريبي وليس ضماناً للربح."
            )

            send_telegram(message)
            state[key] = signal["candle"]
            save_state(state)
            print("Alert sent:", symbol, signal["side"])

        except Exception as error:
            print("Error:", symbol, str(error))

    save_state(state)


if __name__ == "__main__":
    main()