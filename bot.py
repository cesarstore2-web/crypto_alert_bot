import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SYMBOLS = ["BERAUSDT", "XLMUSDT", "ZECUSDT", "BTCUSDT", "ETHUSDT"]
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

BASE = "https://gateio.ws"


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


def get_klines(symbol, interval, limit):
    gate_symbol = symbol.replace("USDT", "_USDT") if "_" not in symbol else symbol
    params = urllib.parse.urlencode({
        "currency_pair": gate_symbol,
        "interval": interval,
        "limit": limit
    })
    raw_data = get_json(f"{BASE}/spot/candlesticks?{params}")

    interval_ms = {
        "1h": 3600 * 1000,
        "15m": 15 * 60 * 1000,
        "5m": 5 * 60 * 1000
    }.get(interval, 60 * 1000)

    formatted = []
    for c in raw_data:
        open_time = int(c[0]) * 1000
        close_time = open_time + interval_ms - 1
        formatted.append([
            open_time,
            c[5],
            c[3],
            c[4],
            c[2],
            c[1],
            close_time
        ])
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
        "text": message
    }).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
        if not result.get("ok"):
            raise RuntimeError("Telegram message failed")


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

    recent = m5[-14:]
    setup = recent[-1]
    previous = recent[:-1]

    old_highs = [float(c[2]) for c in previous[-12:]]
    old_lows = [float(c[3]) for c in previous[-12:]]

    opening = float(setup[1])
    high = float(setup[2])
    low = float(setup[3])
    close = float(setup[4])

    side = None

    if trend == "BUY":
        if low < min(old_lows) and close > min(old_lows) and close > opening:
            if low > float(recent[-3][2]):
                side = "BUY"

    if trend == "SELL":
        if high > max(old_highs) and close < max(old_highs) and close < opening:
            if high < float(recent[-3][3]):
                side = "SELL"

    if side is None:
        return None

    ranges = [
        float(c[2]) - float(c[3])
        for c in m5[-15:-1]
    ]
    atr_proxy = sum(ranges) / len(ranges)

    entry = close

    if side == "BUY":
        stop = low - 0.25 * atr_proxy
        risk = entry - stop
        if risk <= 0:
            return None
        tp1 = entry + risk
        tp2 = entry + 2 * risk
    else:
        stop = high + 0.25 * atr_proxy
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
        "candle": str(setup[6])
    }


def main():
    info = get_json(BASE + "/spot/currency_pairs")

    active = {
        item["id"].replace("_", "")
        for item in info
        if item.get("trade_status") == "tradable"
    }

    state = load_state()

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
            print("Alert sent:", symbol, signal["side"])

        except Exception as error:
            print("Error:", symbol, str(error))

    save_state(state)


if __name__ == "__main__":
    main()
