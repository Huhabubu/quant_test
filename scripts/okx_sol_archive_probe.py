"""Probe OKX historical market data links for SOL-USDT-SWAP, read-only."""
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
import json, sys

API = "https://www.okx.com/api/v5/public/market-data-history"

def ms(day):
    return str(int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000))

def fetch(url, limit, ranged=False):
    headers = {"User-Agent": "okx-hftbacktest-data-audit/1.0"}
    if ranged:
        headers["Range"] = f"bytes=0-{limit-1}"
    with urlopen(Request(url, headers=headers), timeout=25) as r:
        return r.status, dict(r.headers), r.read(limit)

def check(label, module, start, end):
    params = dict(module=str(module), instType="SWAP", dateAggrType="daily",
                  begin=ms(start), end=ms(end), instFamilyList="SOL-USDT")
    print(f"\n### {label}: {start} to {end} | module={module}", flush=True)
    try:
        status, _, data = fetch(API + "?" + urlencode(params), 1024 * 1024)
        print("API HTTP:", status)
        response = json.loads(data)
        print("OKX code:", response.get("code"), "message:", response.get("msg"))
        if response.get("code") != "0":
            return False
        print("Response data count:", len(response.get("data", [])))
        for batch in response.get("data", [])[:2]:
            print("Batch fields:", list(batch), "totalSizeMB:", batch.get("totalSizeMB"))
            print("Group count:", len(batch.get("details", [])))
            for group in batch.get("details", [])[:4]:
                print("Group fields:", list(group), "instId:", group.get("instId"),
                      "instFamily:", group.get("instFamily"),
                      "file_count:", len(group.get("groupDetails", [])))
        files = [(group, item)
                 for batch in response.get("data", [])
                 for group in batch.get("details", [])
                 if group.get("instFamily") == "SOL-USDT"
                 for item in group.get("groupDetails", [])]
        print("Files found:", len(files))
        for group, item in files[:5]:
            print(" ", item.get("filename"), "sizeMB=", item.get("sizeMB"),
                  "dateTs=", item.get("dateTs"))
        if not files:
            return False
        url = files[0][1].get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            print("Invalid or missing file URL")
            return False
        print("Download host:", parsed.netloc, "(query string not logged)")
        status, headers, prefix = fetch(url, 131072, ranged=True)
        print("Download HTTP:", status, "range:", headers.get("Content-Range"),
              "bytes_read:", len(prefix), "signature:", prefix[:8].hex())
        return status in (200, 206) and bool(prefix)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        print("Request error:", type(exc).__name__, str(exc)[:240])
        return False

if __name__ == "__main__":
    book = check("L2_400", 4, "2026-09-02", "2026-09-02")
    trades = check("TRADES_UTC8", 1, "2026-09-02", "2026-09-03")
    print("\nSUMMARY L2:", book, "Trades:", trades)
    print("Scope: existence/download probe ONLY; sequence continuity not checked.")
    sys.exit(0 if book and trades else 2)
