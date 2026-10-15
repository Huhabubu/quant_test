"""Inspect HTTP response of suspect public hourly dataset paths; no market data stored."""
import json
from urllib.parse import urlencode
from urllib.request import urlopen, Request
for d,h in [("2026-08-01","00"),("2026-08-01","12"),("2026-08-31","00"),("2026-09-01","00"),("2026-10-01","00")]:
    remote=f"binance_futures/{d}/{h}/SOLUSDT_orderbook.parquet"
    u="https://api.cryptohftdata.com/v1/download?"+urlencode({"file":remote})
    try:
        with urlopen(Request(u,headers={"User-Agent":"quant-test-hour-diagnostic/1.0"}),timeout=30) as response:
            prefix=response.read(300)
            info={
                "date":d, "hour":h, "status":response.status,
                "type":response.headers.get("Content-Type"),
                "length":response.headers.get("Content-Length"),
                "magic":prefix[:4].hex(),
                "body_if_text":prefix[:220].decode("utf-8",errors="replace")
                    if not prefix.startswith(b"PAR1") else None
            }
    except Exception as exc:
        info={"date":d,"hour":h,"error":repr(exc)}
    print("HTTP_DIAGNOSTIC "+json.dumps(info),flush=True)
