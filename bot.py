import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import joblib
import pandas as pd

from strategy import TF_MIN, fetch_recent, find_signals, make_exchange

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = "state.json"
MODEL_FILE = "model.pkl"
MIN_PROB = 0.80

CRYPTO = ["XLMUSDT", "ZECUSDT", "XRPUSDT", "HBARUSDT",
          "ADAUSDT", "LTCUSDT", "BTCUSDT", "ETHUSDT"]
GOLD = ["XAUUSDT", "PAXGUSDT"]
CASA = ["ADH", "AKT", "JET", "TGC", "MNG", "ATW",
        "BCP", "MSA", "CMA", "LHM", "ATL", "DHO"]
CASA_EXCHANGE = "CSEMA"
SCAN_TFS = ["5m", "15m", "30m", "1h", "4h"]

TZ = ZoneInfo("Africa/Casablanca")
CASA_CLOSE = (15, 30)
CASA_SCAN_FROM, CASA_SCAN_TO = (15, 35), (17, 30)  # بعد إغلاق الشمعة اليومية


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


def fmt(v):
    return f"{v:.8f}".rstrip("0").rstrip(".")


def casa_scan_time():
    n = datetime.now(TZ)
    return n.weekday() < 5 and CASA_SCAN_FROM <= (n.hour, n.minute) <= CASA_SCAN_TO


_tv = None


def fetch_casa(code):
    """شموع يومية لأسهم الدار البيضاء عبر tvDatafeed (غير رسمي)."""
    global _tv
    from tvDatafeed import TvDatafeed, Interval
    if _tv is None:
        _tv = TvDatafeed()
    df = _tv.get_hist(code, CASA_EXCHANGE, interval=Interval.in_daily,
                      n_bars=300)
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        "ts": [int(t.timestamp() * 1000) for t in df.index],
        "open": df["open"].values, "high": df["high"].values,
        "low": df["low"].values, "close": df["close"].values,
        "volume": df["volume"].values,
    })
    n = datetime.now(TZ)
    if df.index[-1].date() == n.date() and (n.hour, n.minute) < CASA_CLOSE:
        out = out.iloc[:-1]  # شمعة اليوم لم تُغلق بعد
    return out.reset_index(drop=True)


def build_message(name, tf, s, prob):
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M (توقيت المغرب)")
    if s["side"] == "LONG":
        head = "LONG (شراء)"
        note = ""
    else:
        head = "SHORT (بيع)"
        note = "\nSpot: اعتبرها تنبيه بيع/تخفيف مركز قائم، وليس فتح Short."
    return (
        f"🔔 {name} | {tf}\n"
        f"الإشارة: {head}\n"
        f"الاستراتيجية: RSI Trendline Breakout\n"
        f"تقييم الذكاء الاصطناعي: {prob * 100:.0f}%\n"
        f"الدخول: {fmt(s['entry'])}\n"
        f"وقف الخسارة: {fmt(s['sl'])}\n"
        f"الهدف (1:2): {fmt(s['tp'])}\n"
        f"RSI: {s['rsi']:.1f}\n"
        f"🕒 {now}{note}\n"
        f"تنبيه تجريبي وليس ضماناً للربح."
    )


def main():
    try:
        bundle = joblib.load(MODEL_FILE)
    except FileNotFoundError:
        print("model.pkl not found: run the 'Train AI model' workflow first")
        return
    model, feats = bundle["model"], bundle["features"]

    state = load_state()
    ex = make_exchange()
    jobs = [(s, tf, "gate") for s in CRYPTO + GOLD for tf in SCAN_TFS]
    if casa_scan_time():
        jobs += [(c, "1d", "casa") for c in CASA]
    else:
        print("Casablanca daily scan skipped (outside window)")

    for name, tf, kind in jobs:
        try:
            df = (fetch_recent(ex, name, tf) if kind == "gate"
                  else fetch_casa(name))
            if df is None or len(df) < 80:
                continue
            sigs = find_signals(df, TF_MIN[tf], start=len(df) - 1)
            if not sigs:
                print("No breakout:", name, tf)
            for s in sigs:
                X = pd.DataFrame([s["feat"]])[feats]
                prob = float(model.predict_proba(X)[0][1])
                if prob < MIN_PROB:
                    print(f"Filtered {name} {tf} {s['side']} ({prob:.2f})")
                    continue
                key = f"{name}:{tf}:{s['side']}"
                candle = str(s["ts"])
                if state.get(key) == candle:
                    print("Already sent:", key)
                    continue
                send_telegram(build_message(name, tf, s, prob))
                state[key] = candle
                save_state(state)
                print("Alert sent:", key, f"{prob:.2f}")
        except Exception as e:
            print("Error:", name, tf, str(e))

    save_state(state)


if __name__ == "__main__":
    main()