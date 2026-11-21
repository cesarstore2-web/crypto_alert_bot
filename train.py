import json

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

from strategy import (FEATURES, TF_MIN, fetch_history, find_signals, label,
                      make_exchange)

CRYPTO = ["XLMUSDT", "ZECUSDT", "XRPUSDT", "HBARUSDT",
          "ADAUSDT", "LTCUSDT", "BTCUSDT", "ETHUSDT"]
GOLD = ["XAUUSDT", "PAXGUSDT"]
TRAIN_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]  # 1d للأسهم المغربية

# التوصية القوية: أقل حد احتمال (لا يقل عن MIN_THRESHOLD) تكون عنده
# نسبة النجاح في بيانات الاختبار >= TARGET_WIN وعدد الإشارات >= MIN_N
MIN_THRESHOLD = 0.60   # الحد الأدنى للتقييم الذي اخترته
TARGET_WIN = 0.50      # بنسبة 1:2 التعادل عند 0.333
MIN_N = 30


def main():
    ex = make_exchange()
    rows = []
    for sym in CRYPTO + GOLD:
        for tf in TRAIN_TFS:
            try:
                df = fetch_history(ex, sym, tf)
            except Exception as e:
                print("fetch error", sym, tf, e)
                continue
            if len(df) < 300:
                continue
            n_sig = 0
            for s in find_signals(df, TF_MIN[tf]):
                y = label(df, s)
                if y is None:
                    continue
                rows.append({**s["feat"], "y": y, "ts": s["ts"]})
                n_sig += 1
            print(f"{sym} {tf}: {len(df)} candles, {n_sig} labeled breakouts")

    data = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    print("total breakouts:", len(data))
    if len(data) < 300 or data["y"].nunique() < 2:
        raise SystemExit("Not enough data to train a model")

    params = dict(n_estimators=200, learning_rate=0.03, num_leaves=15,
                  min_child_samples=40, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, verbose=-1)

    # تحقق زمني: تدريب على أول 80% واختبار على آخر 20%
    cut = int(len(data) * 0.8)
    tr, va = data.iloc[:cut], data.iloc[cut:]
    model = LGBMClassifier(**params).fit(tr[FEATURES], tr["y"])
    p = model.predict_proba(va[FEATURES])[:, 1]

    table, threshold, valid_win, valid_n = {}, None, None, None
    for th in np.arange(0.30, 0.901, 0.025):
        sel = va[p >= th]
        n = len(sel)
        win = float(sel["y"].mean()) if n else None
        table[f"{th:.3f}"] = {"n": n,
                              "win": round(win, 3) if win is not None else None}
        if (threshold is None and th >= MIN_THRESHOLD - 1e-9
                and n >= MIN_N and win >= TARGET_WIN):
            threshold, valid_win, valid_n = round(float(th), 3), win, n

    report = {
        "breakouts": len(data),
        "base_win_rate": round(float(data["y"].mean()), 3),
        "valid_size": len(va),
        "strong_threshold": threshold,
        "valid_win_at_threshold": (round(valid_win, 3)
                                   if valid_win is not None else None),
        "valid_signals_at_threshold": valid_n,
        "by_threshold": table,
    }
    print(json.dumps(report, indent=2))

    # نحفظ النموذج الذي اختُبر (نفس النموذج الذي حُسب له الحد)
    joblib.dump({"model": model, "features": FEATURES,
                 "threshold": threshold, "valid_win": valid_win,
                 "valid_n": valid_n}, "model.pkl")
    with open("model_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("model.pkl saved")


if __name__ == "__main__":
    main()