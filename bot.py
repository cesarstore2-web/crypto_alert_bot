import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = "state.json"
BASE = "https://api.gateio.ws/api/v4"

# ---------------- الأصول ----------------
CRYPTO = ["XLMUSDT", "ZECUSDT", "XRPUSDT", "HBARUSDT",
          "ADAUSDT", "LTCUSDT", "BTCUSDT", "ETHUSDT"]
GOLD = ["XAUUSDT", "PAXGUSDT"]
# Gate.io لا يملك XAUUSDT، البديل المقابل للذهب هو XAUT_USDT
GATE_ALIASES = {"XAUUSDT": "XAUT_USDT", "PAXGUSDT": "PAXG_USDT"}

CASA = ["ADH", "AKT", "JET", "TGC", "MNG", "ATW",
        "BCP", "MSA", "CMA", "LHM", "ATL", "DHO"]
CASA_EXCHANGE = "CSEMA"

# ---------------- الإعدادات ----------------
SIGNAL_TF = "30m"
TREND_TF = "1h"
EMA_LEN = 2
WMA_LEN = 20
ATR_LEN = 14
SL_ATR_MULT = 2    # ATR Multiplier (SL)
RR = 1.0           # Risk-to-Reward 1:X

# ---------------- التوقيت ----------------
TZ = ZoneInfo("Africa/Casablanca")
CASA_OPEN = (9, 30)
CASA_CLOSE = (15, 30)

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000,
}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from e


# كل شمعة: (open_time_ms, open, high, low, close) ـ مغلقة فقط
def gate_klines(symbol, interval, limit=100):
    pair = GATE_ALIASES.get(symbol) or symbol.replace("USDT", "_USDT")
    q = urllib.parse.urlencode(
        {"currency_pair": pair, "interval": interval, "limit": limit})
    raw = get_json(f"{BASE}/spot/candlesticks?{q}")
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    out = []
    for c in raw:
        # Gate: [time, quote_vol, close, high, low, open, ...]
        t = int(c[0]) * 1000
        if t + step > now:  # شمعة لم تُغلق بعد
            continue
        out.append((t, float(c[5]), float(c[3]), float(c[4]), float(c[2])))
    out.sort(key=lambda x: x[0])
    return out


_tv = None


def tv_klines(code, interval, limit=100):
    """أسهم الدار البيضاء عبر tvDatafeed (غير رسمي)."""
    global _tv
    from tvDatafeed import TvDatafeed, Interval
    if _tv is None:
        _tv = TvDatafeed()
    iv = {
        "1m": Interval.in_1_minute, "5m": Interval.in_5_minute,
        "15m": Interval.in_15_minute, "30m": Interval.in_30_minute,
        "1h": Interval.in_1_hour,
    }[interval]
    df = _tv.get_hist(code, CASA_EXCHANGE, interval=iv, n_bars=limit)
    if df is None or df.empty:
        return []
    df = df.iloc[:-1]  # آخر شمعة قيد التكوّن
    return [
        (int(ts.timestamp() * 1000), float(r.open), float(r.high),
         float(r.low), float(r.close))
        for ts, r in df.iterrows()
    ]


def ema(values, period):
    a = 2 / (period + 1)
    cur, res = values[0], []
    for v in values:
        cur = a * v + (1 - a) * cur
        res.append(cur)
    return res


def wma(values, period):
    w = list(range(1, period + 1))
    den = sum(w)
    res = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        win = values[i - period + 1:i + 1]
        res.append(sum(x * y for x, y in zip(win, w)) / den)
    return res


def atr(candles, period):
    trs = []
    for i in range(1, len(candles)):
        _, _, h, l, _ = candles[i]
        pc = candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return sum(trs) / len(trs)


def state_of(candles):
    """+1 فوق، -1 تحت، 0 غير متاح."""
    if len(candles) < WMA_LEN + 2:
        return 0
    closes = [c[4] for c in candles]
    return 1 if ema(closes, EMA_LEN)[-1] > wma(closes, WMA_LEN)[-1] else -1


def cross_of(candles):
    if len(candles) < WMA_LEN + 3:
        return None
    closes = [c[4] for c in candles]
    e, w = ema(closes, EMA_LEN), wma(closes, WMA_LEN)
    if e[-2] <= w[-2] and e[-1] > w[-1]:
        return "BUY"
    if e[-2] >= w[-2] and e[-1] < w[-1]:
        return "SELL"
    return None


def icon(s):
    return {1: "🟢 صاعد", -1: "🔴 هابط", 0: "⚪ —"}[s]


def fmt(v):
    return f"{v:.8f}".rstrip("0").rstrip(".")


def send_telegram(text):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram HTTP {e.code}") from e
    if not res.get("ok"):
        raise RuntimeError(f"Telegram failed: {res}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def casa_open_now():
    n = datetime.now(TZ)
    if n.weekday() >= 5:
        return False
    hm = (n.hour, n.minute)
    return CASA_OPEN <= hm <= CASA_CLOSE


def scan(name, fetch):
    c30 = fetch(name, SIGNAL_TF)
    side = cross_of(c30)
    if not side:
        return None

    # الدخول: إغلاق شمعة التقاطع | SL = ATR x 2 | الهدف RR 1:1
    entry = c30[-1][4]
    risk = SL_ATR_MULT * atr(c30, ATR_LEN)
    if risk <= 0:
        return None
    sign = 1 if side == "BUY" else -1
    stop = entry - sign * risk
    tp = entry + sign * risk * RR

    trend = state_of(fetch(name, TREND_TF))

    return {"side": side, "entry": entry, "stop": stop, "tp": tp,
            "candle": str(c30[-1][0]), "trend": trend}


def build_message(name, kind, s):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (توقيت المغرب)")
    if kind == "casa":
        meaning = "شراء محتمل" if s["side"] == "BUY" else "بيع / تخفيف المركز"
    else:
        meaning = ("شراء محتمل" if s["side"] == "BUY"
                   else "تنبيه بيع أو تخفيف مركز Spot قائم، وليس فتح Short")
    return (
        f"🔔 ALERT | {name}\n"
        f"الإشارة: {s['side']} ({meaning})\n"
        f"التقاطع: EMA{EMA_LEN} / WMA{WMA_LEN} على {SIGNAL_TF}\n"
        f"الدخول: {fmt(s['entry'])}\n"
        f"وقف الخسارة: {fmt(s['stop'])}\n"
        f"الهدف: {fmt(s['tp'])}\n"
        f"━━━━━━━━━━\n"
        f"الاتجاه العام (1H): {icon(s['trend'])}\n"
        f"🕒 {now}\n"
        f"تنبيه تجريبي وليس ضماناً للربح."
    )


def main():
    state = load_state()
    jobs = [(s, "gate") for s in CRYPTO + GOLD]
    if casa_open_now():
        jobs += [(c, "casa") for c in CASA]
    else:
        print("Casablanca market closed, skipping stocks")

    for name, kind in jobs:
        fetch = gate_klines if kind == "gate" else tv_klines
        try:
            sig = scan(name, fetch)
            if not sig:
                print("No signal:", name)
                continue
            key = f"{name}:{sig['side']}"
            if state.get(key) == sig["candle"]:
                print("Already sent:", name)
                continue
            send_telegram(build_message(name, kind, sig))
            state[key] = sig["candle"]
            save_state(state)
            print("Alert sent:", name, sig["side"])
        except Exception as e:
            print("Error:", name, str(e))

    save_state(state)


if __name__ == "__main__":
    main()