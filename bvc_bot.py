import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import casased as cas
import pandas as pd

CANDIDATES = [
    "Attijariwafa", "BCP", "CIH", "BOA", "IAM", "Addoha",
    "Akdital", "COSUMAR", "Ciments Maroc", "CMGP Group",
    "CTM", "HPS", "CFG Bank", "Aradei Capital", "BMCI", "CDM",
    "TGC", "MNG",
]

ALIASES = {
    "TGC": ["TGCC", "TGC", "Travaux"],
    "MNG": ["Managem", "MNG"],
}
_NAMES = None

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
STATE_FILE = "state_bvc.json"


def send_telegram(message):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": message}).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            result = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram HTTP {e.code}") from e
    if not result.get("ok"):
        raise RuntimeError(f"Telegram failed: {result}")


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fmt(v):
    return f"{v:.2f}"


def resolve(symbol):
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = [str(n) for n in cas.notation()]
        except Exception as error:
            print("notation failed:", str(error)[:100])
            _NAMES = []

    if symbol not in ALIASES:
        return symbol
    if not _NAMES:
        return ALIASES[symbol][0]

    lower = {n.lower(): n for n in _NAMES}
    for opt in ALIASES[symbol]:
        if opt.lower() in lower:
            return lower[opt.lower()]
    for opt in ALIASES[symbol]:
        for low, real in lower.items():
            if opt.lower() in low:
                return real
    print("Name not found in notation:", symbol)
    return None


def get_df(symbol):
    name = resolve(symbol)
    if name is None:
        return None

    start = (date.today() - timedelta(days=400)).isoformat()
    df = None
    for kwargs in ({"start": start}, {}):
        try:
            df = cas.get_history(name, **kwargs)
        except Exception as error:
            print("get_history failed:", name, kwargs, str(error)[:100])
            df = None
        if df is not None and len(df) > 0:
            break

    if df is None or len(df) == 0:
        return None

    df = df.copy()
    for col in ["Value", "Min", "Max", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Value"])
    if "Min" not in df.columns:
        df["Min"] = df["Value"]
    if "Max" not in df.columns:
        df["Max"] = df["Value"]
    if "Volume" not in df.columns:
        df["Volume"] = float("nan")
    df["Min"] = df["Min"].where(df["Min"] > 0, df["Value"])
    df["Max"] = df["Max"].where(df["Max"] > 0, df["Value"])

    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def calc_stoch(df, n=14, smooth=3):
    low_n = df["Min"].rolling(n).min()
    high_n = df["Max"].rolling(n).max()
    raw = 100 * (df["Value"] - low_n) / (high_n - low_n).replace(0, float("nan"))
    k = raw.rolling(smooth).mean()
    d = k.rolling(smooth).mean()
    return k, d


def calc_atr(df, period=14):
    prev = df["Value"].shift(1)
    tr = pd.concat([
        df["Max"] - df["Min"],
        (df["Max"] - prev).abs(),
        (df["Min"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def evaluate(symbol, df):
    if len(df) < 40:
        return None

    close = df["Value"]
    rsi = calc_rsi(close)
    k, d = calc_stoch(df)
    atr = calc_atr(df)
    hh = df["Max"].rolling(20).max().shift(1)
    ll = df["Min"].rolling(20).min().shift(1)
    vmed = df["Volume"].rolling(20).median().shift(1)

    c, c1 = close.iloc[-1], close.iloc[-2]
    r, r1 = rsi.iloc[-1], rsi.iloc[-2]
    k0, k1 = k.iloc[-1], k.iloc[-2]
    d0, d1 = d.iloc[-1], d.iloc[-2]
    a = atr.iloc[-1]
    h20, l20 = hh.iloc[-1], ll.iloc[-1]

    if any(pd.isna(x) for x in [r, r1, k0, k1, d0, d1, a, h20, l20]) or a <= 0:
        return None

    score = 0
    why = []
    change = c / c1 - 1

    # RSI (25)
    if r > r1:
        if r < 30:
            score += 25
            why.append("RSI في تشبع بيعي ويرتد")
        elif r < 40:
            score += 20
            why.append("RSI منخفض ويرتفع")
        elif r < 50:
            score += 12
            why.append("RSI يرتفع من منطقة محايدة")
        elif r <= 65:
            score += 6
    if r > 70:
        score -= 15
        why.append("RSI مرتفع (خطر)")

    # Stochastic (25)
    cross_up = k0 > d0 and k1 <= d1
    if cross_up:
        if k0 < 30:
            score += 25
            why.append("تقاطع Stochastic صاعد في القاع")
        elif k0 < 50:
            score += 18
            why.append("تقاطع Stochastic صاعد")
        elif k0 < 80:
            score += 8
    elif k0 > d0 and k0 < 40:
        score += 8

    # Donchian 20 (25)
    if c > h20:
        score += 25
        why.append("اختراق أعلى قمة 20 يوماً")
    else:
        pos = (c - l20) / (h20 - l20) if h20 > l20 else 0.5
        if pos < 0.2 and c > c1:
            score += 15
            why.append("ارتداد من أسفل القناة")
        elif pos < 0.4 and c > c1:
            score += 8

    # الحجم (20)
    vol = df["Volume"].iloc[-1]
    vm = vmed.iloc[-1]
    if not pd.isna(vol) and not pd.isna(vm) and vm > 0:
        ratio = vol / vm
        if ratio >= 2:
            score += 20
            why.append("حجم تداول مرتفع جداً")
        elif ratio >= 1.5:
            score += 15
            why.append("حجم تداول مرتفع")
        elif ratio >= 1:
            score += 8

    # الشمعة (5)
    if c > c1 and change < 0.06:
        score += 5
    if change > 0.07:
        score -= 10
        why.append("صعد كثيراً اليوم (مطاردة سعر)")

    score = max(0, min(100, score))

    risk = 1.5 * a
    return {
        "symbol": symbol,
        "score": score,
        "why": why,
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "entry": c,
        "stop": c - risk,
        "tp1": c + risk,
        "tp2": c + 2 * risk,
        "rsi": r,
    }


def rating(score):
    if score >= 65:
        return "قوية 🟢"
    if score >= 45:
        return "متوسطة 🟡"
    return "ضعيفة 🔴 (الأفضل عدم الدخول اليوم)"


def build_message(top, others):
    text = (
        f"🇲🇦 أفضل فرصة اليوم - بورصة الدار البيضاء\n"
        f"السهم: {top['symbol']}\n"
        f"النقاط: {top['score']}/100 - {rating(top['score'])}\n"
        f"تاريخ الإغلاق: {top['date']}\n"
        f"سعر الإغلاق: {fmt(top['entry'])} MAD\n"
        f"وقف الخسارة: {fmt(top['stop'])}\n"
        f"الهدف الأول: {fmt(top['tp1'])}\n"
        f"الهدف الثاني: {fmt(top['tp2'])}\n"
        f"RSI: {top['rsi']:.0f}\n"
    )
    if top["why"]:
        text += "الأسباب: " + " + ".join(top["why"]) + "\n"
    if others:
        text += "بدائل: " + " | ".join(f"{o['symbol']} ({o['score']})" for o in others) + "\n"
    text += "ضع أمراً بسعر محدد في الجلسة القادمة، ولا تطارد السعر.\nتنبيه تجريبي وليس ضماناً للربح."
    return text


def main():
    state = load_state()
    results = []
    errors = []

    for symbol in CANDIDATES:
        try:
            df = get_df(symbol)
            if df is None:
                print("No data:", symbol)
                errors.append(f"{symbol}: no data")
                continue

            age = (pd.Timestamp.today().normalize() - df.index[-1]).days
            if age > 5:
                print("Stale data:", symbol, df.index[-1].date())
                errors.append(f"{symbol}: stale {df.index[-1].date()}")
                continue

            res = evaluate(symbol, df)
            if res:
                results.append(res)
                print("Score:", symbol, res["score"])
            else:
                errors.append(f"{symbol}: not enough data")
        except Exception as error:
            print("Error:", symbol, str(error))
            errors.append(f"{symbol}: {str(error)[:80]}")

    if not results:
        report = "⚠️ BVC bot: لا توجد نتائج\n" + "\n".join(errors[:6])
        print(report)
        send_telegram(report[:3500])
        return

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[0]

    if state.get("last_sent") == top["date"]:
        print("Already sent for", top["date"])
        return

    send_telegram(build_message(top, results[1:3]))
    state["last_sent"] = top["date"]
    save_state(state)
    print("Alert sent:", top["symbol"], top["score"])


if __name__ == "__main__":
    main()