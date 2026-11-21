import bisect

import ccxt
import numpy as np
import pandas as pd

# ---------------- إعدادات الاستراتيجية ----------------
K = 3              # عرض نافذة القمم/القيعان المتأرجحة
RSI_LEN = 14
OB, OS = 65, 35    # تشبع شرائي / بيعي
OB_LOOK = 10       # نافذة التحقق من وصول RSI لمنطقة التشبع
MIN_GAP = 5        # أقل مسافة (شموع) بين نقطتي خط الاتجاه
MAX_AGE = 120      # أقصى عمر لخط الاتجاه (شموع)
RR = 2.0           # عائد/مخاطرة 1:2
SL_BUF_ATR = 0.1   # هامش صغير خلف القمة/القاع
HORIZON = 60       # أقصى عدد شموع لتقييم الصفقة في التدريب

TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
FEATURES = ["side", "tf", "rsi", "rsi_chg3", "d_ema20", "d_ema50",
            "vol_ratio", "atr_pct", "risk_atr", "slope_atr"]

ALIASES = {"XAUUSDT": "XAUT/USDT", "PAXGUSDT": "PAXG/USDT"}


def make_exchange():
    return ccxt.gateio({"enableRateLimit": True})


def to_pair(sym):
    return ALIASES.get(sym) or sym.replace("USDT", "/USDT")


def rows_to_df(rows):
    df = pd.DataFrame(
        rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def fetch_recent(ex, sym, tf, limit=300):
    rows = ex.fetch_ohlcv(to_pair(sym), tf, limit=limit)
    df = rows_to_df(rows)
    return df.iloc[:-1].reset_index(drop=True)  # آخر شمعة قيد التكوّن


def fetch_history(ex, sym, tf, n=6000):
    pair = to_pair(sym)
    ms = ex.parse_timeframe(tf) * 1000
    since = ex.milliseconds() - n * ms
    rows = []
    while since < ex.milliseconds():
        batch = ex.fetch_ohlcv(pair, tf, since=since, limit=1000)
        if not batch:
            break
        rows += batch
        new_since = batch[-1][0] + ms
        if new_since <= since:
            break
        since = new_since
    df = rows_to_df(rows)
    return df.iloc[:-1].reset_index(drop=True)


def add_indicators(df):
    c, h, l = df["close"], df["high"], df["low"]
    d = c.diff()
    au = d.clip(lower=0).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df


def pivot_idx(arr, kind):
    out = []
    for i in range(K, len(arr) - K):
        w = arr[i - K:i + K + 1]
        v = arr[i]
        ext = w.min() if kind == "low" else w.max()
        if v == ext and (w == v).sum() == 1:
            out.append(i)
    return out


def last_two(piv, t):
    j = bisect.bisect_right(piv, t - K)  # القمة تُؤكَّد بعد K شموع
    return (piv[j - 2], piv[j - 1]) if j >= 2 else None


def line(x1, y1, x2, y2, x):
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def find_signals(df, tf_min, start=0):
    """كسر متزامن لخط اتجاه السعر وخط اتجاه RSI. SHORT / LONG."""
    df = add_indicators(df.reset_index(drop=True).copy())
    n = len(df)
    if n < 80:
        return []
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    rs, atr = df["rsi"].values, df["atr"].values
    e20, e50 = df["ema20"].values, df["ema50"].values
    vol, vma = df["volume"].values, df["vol_ma"].values

    pl, ph = pivot_idx(lo, "low"), pivot_idx(hi, "high")
    rl, rh = pivot_idx(rs, "low"), pivot_idx(rs, "high")

    def broken(short, pp, rp, i):
        ya = lo if short else hi
        lp = line(pp[0], ya[pp[0]], pp[1], ya[pp[1]], i)
        lr = line(rp[0], rs[rp[0]], rp[1], rs[rp[1]], i)
        if short:
            return cl[i] < lp and rs[i] < lr
        return cl[i] > lp and rs[i] > lr

    out = []
    for t in range(max(start, 60), n):
        if not (atr[t] > 0) or not (vma[t] > 0):
            continue
        for side in ("SHORT", "LONG"):
            short = side == "SHORT"
            if short:  # اتجاه صاعد: قيعان صاعدة في السعر وRSI
                pp, rp = last_two(pl, t), last_two(rl, t)
                if not pp or not rp:
                    continue
                if not (lo[pp[1]] > lo[pp[0]] and rs[rp[1]] > rs[rp[0]]):
                    continue
                if rs[max(0, t - OB_LOOK):t + 1].max() < OB:
                    continue
            else:      # اتجاه هابط: قمم هابطة في السعر وRSI
                pp, rp = last_two(ph, t), last_two(rh, t)
                if not pp or not rp:
                    continue
                if not (hi[pp[1]] < hi[pp[0]] and rs[rp[1]] < rs[rp[0]]):
                    continue
                if rs[max(0, t - OB_LOOK):t + 1].min() > OS:
                    continue
            if pp[1] - pp[0] < MIN_GAP or rp[1] - rp[0] < MIN_GAP:
                continue
            if t - pp[0] > MAX_AGE or t - rp[0] > MAX_AGE:
                continue
            # الكسر يجب أن يحدث على آخر شمعة (لا تكرار)
            if not broken(short, pp, rp, t) or broken(short, pp, rp, t - 1):
                continue

            buf = SL_BUF_ATR * atr[t]
            entry = cl[t]
            swings = ph if short else pl
            j = bisect.bisect_right(swings, t - K)
            if short:
                sl = (hi[swings[j - 1]] if j else hi[t - 10:t + 1].max()) + buf
                if sl <= entry:
                    sl = hi[t - 10:t + 1].max() + buf
                risk = sl - entry
            else:
                sl = (lo[swings[j - 1]] if j else lo[t - 10:t + 1].min()) - buf
                if sl >= entry:
                    sl = lo[t - 10:t + 1].min() - buf
                risk = entry - sl
            if risk <= 0:
                continue
            tp = entry - RR * risk if short else entry + RR * risk

            ya = lo if short else hi
            slope = (ya[pp[1]] - ya[pp[0]]) / (pp[1] - pp[0])
            feat = {
                "side": 0 if short else 1,
                "tf": tf_min,
                "rsi": rs[t],
                "rsi_chg3": rs[t] - rs[t - 3],
                "d_ema20": (entry - e20[t]) / atr[t],
                "d_ema50": (entry - e50[t]) / atr[t],
                "vol_ratio": vol[t] / vma[t],
                "atr_pct": atr[t] / entry,
                "risk_atr": risk / atr[t],
                "slope_atr": slope / atr[t],
            }
            out.append({"t": t, "ts": df["ts"].iloc[t], "side": side,
                        "entry": entry, "sl": sl, "tp": tp,
                        "rsi": rs[t], "feat": feat})
    return out


def label(df, s):
    """1 = وصل الهدف أولاً، 0 = ضرب الستوب أولاً، None = لم يُحسم."""
    hi, lo = df["high"].values, df["low"].values
    end = min(len(df), s["t"] + 1 + HORIZON)
    for i in range(s["t"] + 1, end):
        if s["side"] == "LONG":
            sl_hit, tp_hit = lo[i] <= s["sl"], hi[i] >= s["tp"]
        else:
            sl_hit, tp_hit = hi[i] >= s["sl"], lo[i] <= s["tp"]
        if sl_hit:  # إن حدث الاثنان في نفس الشمعة نفترض الستوب (تحفظاً)
            return 0
        if tp_hit:
            return 1
    return None