#!/usr/bin/env python3
# tiny CLI client: python3 q.py <query> [query2 ...]   (one line per result)
import os, sys, json, time, urllib.parse, urllib.request

PORT = int(os.environ.get("PORT", "8849"))      # override: PORT=9000 python3 q.py ...
BASE = "http://127.0.0.1:%d" % PORT


def one(q):
    url = BASE + "/quote?q=" + urllib.parse.quote(q)
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.loads(r.read())
    except Exception as e:
        print("  %-12s -> ERR %s" % (q, e))
        return
    ms = int((time.time() - t0) * 1000)
    name = (d.get("name") or "")[:18]
    sess = d.get("session") or {}
    extra = ""
    if d.get("market") == "US":
        post = (sess.get("post") or {}).get("price")
        ov = (sess.get("overnight") or {}).get("price")
        reg = (sess.get("regular") or {}).get("price")
        extra = " reg=%s post=%s overnight=%s" % (reg, post, ov)
    print("  %-12s -> %-3s:%-8s %-18s price=%-9s %s state=%-8s src=%-14s %dms%s" % (
        q, d.get("market", "?"), str(d.get("symbol", "?")), name,
        d.get("price"), d.get("currency", ""), d.get("marketState"),
        (d.get("source") or "")[:14], ms, extra))


for q in sys.argv[1:]:
    one(q)
