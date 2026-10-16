"""Probe OKX historical market data links for SOL-USDT-SWAP, read-only."""
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
import json, sys, zlib, struct

API = "https://www.okx.com/api/v5/public/market-data-history"

def ms(day):
    return str(int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000))

def fetch(url, limit, ranged=False):
    headers = {"User-Agent": "okx-hftbacktest-data-audit/1.0"}
    if ranged:
        headers["Range"] = f"bytes=0-{limit-1}"
    with urlopen(Request(url, headers=headers), timeout=25) as r:
        return r.status, dict(r.headers), r.read(limit)


def preview(prefix):
    """Inspect decompressed prefix in memory; never extract an archive to disk."""
    try:
        if prefix.startswith(b"\x1f\x8b"):
            expanded = zlib.decompressobj(31).decompress(prefix, 262144)
            name = expanded[:100].split(b"\0")[0].decode("utf-8", "replace")
            sample = expanded[512:] if len(expanded) > 512 else b""
            print("Archive: gzip/tar; first tar member:", repr(name))
        elif prefix.startswith(b"PK\x03\x04") and len(prefix) > 30:
            method = struct.unpack_from("<H", prefix, 8)[0]
            nlen, elen = struct.unpack_from("<HH", prefix, 26)
            start = 30 + nlen + elen
            name = prefix[30:30+nlen].decode("utf-8", "replace")
            sample = zlib.decompressobj(-15).decompress(prefix[start:], 262144) if method == 8 else prefix[start:start+4096]
            print("Archive: ZIP; first member:", repr(name), "compression:", method)
        else:
            print("Unrecognized archive signature")
            return
        lines = sample.decode("utf-8", "replace").splitlines()[:3]
        for line in lines:
            print("Decoded sample:", line[:260])
        if prefix.startswith(b"\x1f\x8b"):
            for line in lines[:3]:
                try:
                    obj = json.loads(line)
                    print("L2 event fields:", sorted(obj.keys()), "action:", obj.get("action"), "ts:", obj.get("ts"))
                except json.JSONDecodeError:
                    print("L2 event line is partial or not JSON")
        print("Prefix has seqId/prevSeqId:", b"seqId" in sample, b"prevSeqId" in sample)
    except Exception as exc:
        print("Bounded prefix decode unavailable:", type(exc).__name__, str(exc)[:160])

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
        verified = 0
        for _, item in files[:2]:
            url = item.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc:
                print("Invalid or missing file URL")
                continue
            print("Download:", item.get("filename"), "host:", parsed.netloc, "(query hidden)")
            status, headers, prefix = fetch(url, 131072, ranged=True)
            print("Download HTTP:", status, "range:", headers.get("Content-Range"),
                  "bytes_read:", len(prefix), "signature:", prefix[:8].hex())
            if status in (200, 206) and prefix:
                verified += 1
                preview(prefix)
        return verified == min(2, len(files))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        print("Request error:", type(exc).__name__, str(exc)[:240])
        return False

if __name__ == "__main__":
    book = check("L2_400", 4, "2026-09-02", "2026-09-02")
    trades = check("TRADES_UTC8", 1, "2026-09-02", "2026-09-03")
    print("\nSUMMARY L2:", book, "Trades:", trades)
    print("Scope: existence/download probe ONLY; sequence continuity not checked.")
    sys.exit(0 if book and trades else 2)
