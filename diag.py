import contextlib
import io
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log_stream = io.StringIO()
logging.basicConfig(level=logging.DEBUG, stream=log_stream)

import casased as cas

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()


def send(msg):
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": msg[:3500]}).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20)


lines = ["🔎 BVC diagnostic"]
lines.append("casased version: " + str(getattr(cas, "__version__", "unknown")))

for url in ["https://medias24.com/bourse", "https://www.casablanca-bourse.com"]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            lines.append(f"{url} -> {r.status}")
    except urllib.error.HTTPError as e:
        lines.append(f"{url} -> HTTP {e.code}")
    except Exception as e:
        lines.append(f"{url} -> {type(e).__name__}: {str(e)[:60]}")

try:
    lines.append("isin BCP -> " + str(cas.get_isin_by_name("BCP")))
except Exception as e:
    lines.append(f"isin failed: {type(e).__name__}: {str(e)[:80]}")

out = io.StringIO()
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        df = cas.get_history("BCP")
    n = 0 if df is None else len(df)
    lines.append(f"get_history BCP -> {type(df).__name__}, rows={n}")
    if n:
        lines.append(df.tail(2).to_string()[:300])
except Exception as e:
    lines.append(f"get_history raised {type(e).__name__}: {str(e)[:200]}")

printed = out.getvalue().strip()
if printed:
    lines.append("library output: " + printed[-400:])

debug = log_stream.getvalue().strip()
if debug:
    lines.append("log: " + debug[-600:])

report = "\n".join(lines)
print(report)
send(report)