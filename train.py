import json

import joblib
import pandas as pd
from lightgbm import LGBMClassifier

from strategy import (FEATURES, TF_MIN, fetch_history, find_signals, label,
                      make_exchange)

CRYPTO = ["XLMUSDT", "ZECUSDT", "XRPUSDT", "HBARUSDT",
          "ADAUSDT", "LTCUSDT", "BTCUSDT", "ETHUSDT"]
GOLD = ["XAUUSDT", "PAXGUSDT"]
TRAIN_TFS = ["5m", "15m", "30m", "1h", "4h", "1d"]  # 1d للأسهم المغربية
THRESHOLD = 0.80


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

    params = dict(n_estimators=300, learning_rate=0.03, num_leaves=15,
                  min_child_samples=20, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, verbose=-1)

    # تحقق زمني: تدريب على أول 80% واختبار على آخر 20%
    cut = int(len(data) * 0.8)
    tr, va = data.iloc[:cut], data.iloc[cut:]
    m = LGBMClassifier(**params).fit(tr[FEATURES], tr["y"])
    p = m.predict_proba(va[FEATURES])[:, 1]
    sel = va[p >= THRESHOLD]
    report = {
        "breakouts": len(data),
        "base_win_rate": round(float(data["y"].mean()), 3),
        "valid_size": len(va),
        "valid_selected_80": len(sel),
        "valid_win_rate_selected": (round(float(sel["y"].mean()), 3)
                                    if len(sel) else None),
    }
    print(json.dumps(report, indent=2))

    final = LGBMClassifier(**params).fit(data[FEATURES], data["y"])
    joblib.dump({"model": final, "features": FEATURES}, "model.pkl")
    with open("model_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("model.pkl saved")


if __name__ == "__main__":
    main()