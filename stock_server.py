#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Real-time stock quote HTTP server.

Markets: US (with pre-market / after-hours / overnight sessions),
         HK (Hong Kong), CN (A-shares: Shanghai / Shenzhen / Beijing).

No paid / no-registration APIs.  Data is scraped directly from public web
endpoints used by their own websites:

  * Yahoo Finance   (v7 quote w/ crumb  +  v8 chart)  -> marketState, pre/post
  * Robinhood       (public /quotes/ endpoint)        -> regular + extended + overnight
  * Tencent gtimg   (qt.gtimg.cn)                      -> US / HK / CN
  * Sina hq         (hq.sinajs.cn)                     -> US / HK / CN
  * Eastmoney push2 (push2.eastmoney.com)             -> US / HK / CN

When Yahoo is rate-limited / risk-controlled (HTTP 429) the request is
retried through a public "reader" proxy (r.jina.ai / markdown.new) which
fetches the page server-side and returns its body, bypassing the limit.

Pure stdlib -- nothing to install.  Python 3.8+.

Usage:
    python3 stock_server.py [--port 8080] [--host 0.0.0.0]

Endpoints:
    GET /                      -> usage / help (JSON)
    GET /health                -> {"ok": true}
    GET /quote?symbol=AAPL     -> normalized quote JSON
    GET /quote/AAPL            -> same (path style)
    Optional params:
        market=us|hk|cn        force market (otherwise auto-detected)
        source=yahoo|robinhood|tencent|sina|eastmoney   force one source
        raw=1                  include raw upstream payloads
"""

import os
import json
import gzip
import zlib
import io
import re
import sys
import time
import threading
import argparse
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_TIMEOUT = 12


# --------------------------------------------------------------------------- #
#  Low level HTTP helper                                                      #
# --------------------------------------------------------------------------- #
def http_get(url, headers=None, timeout=DEFAULT_TIMEOUT, opener=None, encoding=None):
    """GET a URL, transparently handling gzip. Returns decoded text."""
    h = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    fn = opener.open if opener else urllib.request.urlopen
    with fn(req, timeout=timeout) as resp:
        data = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        elif enc == "deflate":
            # we advertise deflate, so decode it too (raw or zlib-wrapped)
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        if encoding:
            return data.decode(encoding, errors="replace")
        # try utf-8 then gbk (chinese sources)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gbk", errors="replace")


def _strip_proxy_wrapper(txt):
    """Reader proxies (markdown.new / r.jina.ai) prepend a header block and
    sometimes wrap the body in a code fence.  Return the raw upstream body."""
    marker = "Markdown Content:"
    if marker in txt:
        txt = txt.split(marker, 1)[1]
    txt = txt.strip()
    # drop a leading/trailing ``` fence if present
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt[3:]
        if txt.rstrip().endswith("```"):
            txt = txt.rstrip()[:-3]
    return txt.strip()


def http_get_via_proxy(url, timeout=20):
    """Fetch a URL through a public reader proxy to bypass rate limiting /
    risk control.  The proxy fetches server-side from its own IP.

    markdown.new is preferred (per request: it rarely gets frequency-capped)
    but it drops the query string, so for URLs that carry one we put the
    query-preserving r.jina.ai first."""
    has_query = "?" in url
    md = "https://markdown.new/" + url
    jina = "https://r.jina.ai/" + url
    proxies = [jina, md] if has_query else [md, jina]
    last_err = None
    phdrs = {"User-Agent": "Mozilla/5.0", "Accept": "text/plain",
             "Accept-Encoding": "identity"}
    for p in proxies:
        try:
            txt = http_get(p, headers=phdrs, timeout=timeout)
            body = _strip_proxy_wrapper(txt)
            if body and '"data":null' not in body:
                return body
        except Exception as e:  # noqa
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("proxy failed")


def fetch_resilient(url, headers=None, encoding=None, timeout=DEFAULT_TIMEOUT,
                    opener=None, proxy_fallback=True):
    """GET with automatic reader-proxy fallback on rate-limit / connection
    reset (HTTP 429/403/503 or a dropped connection)."""
    try:
        return http_get(url, headers=headers, timeout=timeout,
                        opener=opener, encoding=encoding)
    except urllib.error.HTTPError as e:
        if proxy_fallback and e.code in (429, 403, 503):
            return http_get_via_proxy(url)
        raise
    except (urllib.error.URLError, ConnectionError, OSError):
        # URLError / a dropped or reset connection (RemoteDisconnected is an
        # OSError subclass) -> retry server-side through the reader proxy.
        if proxy_fallback:
            return http_get_via_proxy(url)
        raise


def num(v):
    """Best-effort float; returns None for blanks/invalid."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def round2(v, nd=4):
    return None if v is None else round(v, nd)


def pct(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100.0, 4)


# --------------------------------------------------------------------------- #
#  Symbol / market detection                                                  #
# --------------------------------------------------------------------------- #
def detect_market(symbol):
    """Return (market, code) where market in {us, hk, cn}."""
    s = symbol.strip()
    low = s.lower()

    # explicit prefixes / suffixes
    if low.startswith("hk") and low[2:].isdigit():
        return "hk", low[2:]
    if low.endswith(".hk"):
        return "hk", re.sub(r"\.hk$", "", low)
    if low.startswith("us") and s[2:].replace(".", "").isalpha():
        return "us", s[2:].upper()
    if low.endswith(".us"):
        return "us", s[:-3].upper()
    if low.endswith(".ss") or low.endswith(".sh"):
        return "cn", re.sub(r"\.(ss|sh)$", "", low)
    if low.endswith(".sz"):
        return "cn", re.sub(r"\.sz$", "", low)
    if (low.startswith(("sh", "sz", "bj")) and low[2:].isdigit()
            and len(low[2:]) == 6):
        return "cn", low[2:]

    digits = re.sub(r"\D", "", s)
    # pure numeric
    if s.isdigit():
        if len(s) == 6:
            return "cn", s
        if len(s) in (4, 5):
            return "hk", s.zfill(5)
        # 1-3 digit -> assume HK
        return "hk", s.zfill(5)

    # contains letters -> US (allow . or - for class shares)
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]*", s):
        return "us", s.upper()

    # fallback: digits len 6 -> cn else hk
    if len(digits) == 6:
        return "cn", digits
    return "hk", digits.zfill(5)


def cn_prefix(code):
    """Shanghai/Shenzhen/Beijing prefix for a 6-digit A-share code."""
    c = code[0]
    if c in ("5", "6", "9"):          # SH main / STAR(688) / B / funds
        return "sh"
    if c in ("0", "2", "3"):          # SZ main / SME / ChiNext
        return "sz"
    if c in ("4", "8"):               # Beijing Stock Exchange
        return "bj"
    return "sh"


def eastmoney_secid(market, code):
    if market == "us":
        return "105." + code           # 105/106/107 NASDAQ/NYSE/AMEX; 105 works for most
    if market == "hk":
        return "116." + code.zfill(5)
    pre = cn_prefix(code)
    return ("1." if pre == "sh" else "0.") + code


# --------------------------------------------------------------------------- #
#  Yahoo Finance client (crumb-aware, proxy fallback)                         #
# --------------------------------------------------------------------------- #
class YahooClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._crumb = None
        self._opener = None
        self._crumb_ts = 0

    def _ensure_crumb(self, force=False):
        with self._lock:
            if (not force and self._crumb and self._opener
                    and time.time() - self._crumb_ts < 1800):
                return
            cj = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cj))
            # prime cookies (404 is fine, it still sets A1 cookie)
            for prime in ("https://fc.yahoo.com",
                          "https://finance.yahoo.com/quote/AAPL"):
                try:
                    http_get(prime, opener=opener, timeout=8)
                except Exception:
                    pass
            crumb = http_get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                opener=opener, timeout=8).strip()
            self._crumb = crumb
            self._opener = opener
            self._crumb_ts = time.time()

    def yahoo_symbol(self, market, code):
        if market == "us":
            return code.replace(".", "-")        # BRK.B -> BRK-B
        if market == "hk":
            return code.lstrip("0").zfill(4) + ".HK"
        pre = cn_prefix(code)
        return code + (".SS" if pre == "sh" else ".SZ")

    def quote(self, market, code):
        """v7 quote: rich pre/post/marketState (US). Needs crumb."""
        sym = self.yahoo_symbol(market, code)
        self._ensure_crumb()
        url = ("https://query1.finance.yahoo.com/v7/finance/quote?symbols="
               + urllib.parse.quote(sym) + "&crumb="
               + urllib.parse.quote(self._crumb))
        try:
            txt = http_get(url, opener=self._opener, timeout=DEFAULT_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._ensure_crumb(force=True)
                url = ("https://query1.finance.yahoo.com/v7/finance/quote?symbols="
                       + urllib.parse.quote(sym) + "&crumb="
                       + urllib.parse.quote(self._crumb))
                txt = http_get(url, opener=self._opener, timeout=DEFAULT_TIMEOUT)
            else:
                raise
        d = json.loads(txt)
        res = d.get("quoteResponse", {}).get("result", [])
        if not res:
            raise RuntimeError("yahoo quote empty for " + sym)
        return res[0]

    def chart(self, market, code):
        """v8 chart: no crumb needed; proxy fallback on 429."""
        sym = self.yahoo_symbol(market, code)
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               + urllib.parse.quote(sym)
               + "?includePrePost=true&interval=1m&range=1d")
        try:
            txt = http_get(url, timeout=DEFAULT_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                txt = http_get_via_proxy(url)
            else:
                raise
        d = json.loads(txt)
        return d["chart"]["result"][0]


YAHOO = YahooClient()


def normalize_yahoo_quote(market, code, q, raw=False):
    state = q.get("marketState")          # PRE/PREPRE/REGULAR/POST/POSTPOST/CLOSED
    reg = num(q.get("regularMarketPrice"))
    prev = num(q.get("regularMarketPreviousClose"))
    if prev is None:
        prev = num(q.get("previousClose"))
    pre_p = num(q.get("preMarketPrice"))
    post_p = num(q.get("postMarketPrice"))

    session = {
        "regular": {
            "price": reg,
            "change": round2(num(q.get("regularMarketChange"))),
            "changePercent": round2(num(q.get("regularMarketChangePercent"))),
        },
        "pre": None,
        "post": None,
        "overnight": None,
    }
    if pre_p is not None:
        session["pre"] = {
            "price": pre_p,
            "change": round2(num(q.get("preMarketChange"))),
            "changePercent": round2(num(q.get("preMarketChangePercent"))),
        }
    if post_p is not None:
        session["post"] = {
            "price": post_p,
            "change": round2(num(q.get("postMarketChange"))),
            "changePercent": round2(num(q.get("postMarketChangePercent"))),
        }

    # choose "active" price by session
    price = reg
    if state in ("PRE",) and pre_p is not None:
        price = pre_p
    elif state in ("POST", "POSTPOST") and post_p is not None:
        price = post_p
    elif state in ("PREPRE",) and post_p is not None:
        # overnight gap before pre-open: last known extended price
        price = post_p

    ts = q.get("regularMarketTime")
    out = {
        "ok": True,
        "symbol": q.get("symbol"),
        "market": market.upper(),
        "name": q.get("longName") or q.get("shortName"),
        "currency": q.get("currency"),
        "price": price,
        "previousClose": prev,
        "open": num(q.get("regularMarketOpen")),
        "dayHigh": num(q.get("regularMarketDayHigh")),
        "dayLow": num(q.get("regularMarketDayLow")),
        "volume": num(q.get("regularMarketVolume")),
        "change": round2(price - prev) if (price is not None and prev) else None,
        "changePercent": pct(price, prev),
        "marketState": map_us_state(state),
        "session": session,
        "timestamp": iso_from_epoch(ts),
        "source": "yahoo",
    }
    if raw:
        out["raw"] = q
    return out


def map_us_state(s):
    return {
        "PRE": "PRE",
        "PREPRE": "CLOSED",       # overnight gap before pre-market
        "REGULAR": "REGULAR",
        "POST": "POST",
        "POSTPOST": "CLOSED",
        "CLOSED": "CLOSED",
    }.get(s, s or "UNKNOWN")


def iso_from_epoch(ts, tz_offset_hours=None):
    if not ts:
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    if tz_offset_hours is None:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    tz = timezone(timedelta(hours=tz_offset_hours))
    return datetime.fromtimestamp(ts, tz=tz).isoformat()


def _parse_iso_epoch(s):
    """Parse an ISO-8601 timestamp (handles a trailing 'Z' and sub-second
    precision beyond microseconds, e.g. Robinhood's nanoseconds) into a float
    epoch.  Returns None on failure."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    # datetime accepts at most 6 fractional digits -> truncate nanoseconds
    m = re.match(r"(.*\.\d{6})\d*([+-]\d{2}:?\d{2})?$", t)
    if m:
        t = m.group(1) + (m.group(2) or "")
    try:
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Robinhood (US, adds overnight / 24h trade price)                           #
# --------------------------------------------------------------------------- #
_RH_HDRS = {"Accept": "application/json",
            "Origin": "https://robinhood.com",
            "Referer": "https://robinhood.com/"}


def robinhood_quote(code):
    url = "https://api.robinhood.com/quotes/?symbols=" + urllib.parse.quote(code)
    txt = http_get(url, headers=_RH_HDRS)
    d = json.loads(txt)
    res = [r for r in d.get("results", []) if r]
    if not res:
        raise RuntimeError("robinhood empty for " + code)
    return res[0]


def robinhood_overnight(code):
    """Live overnight(夜盘) data from Robinhood's 24/5 historicals.

    The public /quotes/ endpoint *freezes* at the 8pm-ET after-hours close, so
    `last_non_reg_trade_price` there just echoes the 盘后 close.  The
    `bounds=24_5` historicals instead keep printing real `session:"overnight"`
    bars for the Blue Ocean ATS overnight session (8pm-4am ET), which is the
    data that flickers live on robinhood.com.  We use 15-second bars over the
    trailing hour so the latest price is near-real-time (~15-30s old); high/low/
    volume therefore cover the trailing hour of overnight, not the whole night.
    Returns the latest overnight print (price/time/high/low/volume) or None."""
    url = ("https://api.robinhood.com/marketdata/historicals/"
           + urllib.parse.quote(code.upper())
           + "/?interval=15second&span=hour&bounds=24_5")
    txt = http_get(url, headers=_RH_HDRS)
    d = json.loads(txt)
    bars = d.get("historicals") or []
    ov = [b for b in bars
          if b.get("session") == "overnight"
          and not b.get("interpolated")
          and (num(b.get("volume")) or 0) > 0]
    if not ov:
        return None
    last = ov[-1]
    highs = [num(b.get("high_price")) for b in ov if num(b.get("high_price")) is not None]
    lows = [num(b.get("low_price")) for b in ov if num(b.get("low_price")) is not None]
    return {
        "price": num(last.get("close_price")),
        "time": last.get("begins_at"),
        "open": num(ov[0].get("open_price")),
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "volume": sum((num(b.get("volume")) or 0) for b in ov),
    }


def normalize_robinhood(code, r, raw=False):
    reg = num(r.get("last_trade_price"))
    ext = num(r.get("last_extended_hours_trade_price"))
    prev = num(r.get("previous_close")) or num(r.get("adjusted_previous_close"))

    # `last_non_reg_trade_price` from /quotes/ freezes at the 8pm 盘后 close, so
    # the real live overnight(夜盘) price comes from the 24/5 historicals.
    try:
        ov = robinhood_overnight(code)
    except Exception:
        ov = None
    overnight = ov.get("price") if ov else None

    # the freshest non-regular price is the "active" one off-hours
    price = overnight or ext or reg
    session = {
        "regular": {"price": reg, "change": round2(reg - prev) if (reg and prev) else None,
                    "changePercent": pct(reg, prev)},
        "pre": None,
        "post": ({"price": ext, "changePercent": pct(ext, prev)} if ext else None),
        "overnight": ({"price": overnight,
                       "changePercent": pct(overnight, reg or prev),
                       "high": ov.get("high"), "low": ov.get("low"),
                       "volume": ov.get("volume"),
                       "time": ov.get("time")} if overnight else None),
    }
    out = {
        "ok": True,
        "symbol": code.upper(),
        "market": "US",
        "name": code.upper(),
        "currency": "USD",
        "price": price,
        "previousClose": prev,
        "open": None,
        "dayHigh": None,
        "dayLow": None,
        "volume": None,
        "bid": num(r.get("bid_price")),
        "ask": num(r.get("ask_price")),
        "change": round2(price - prev) if (price and prev) else None,
        "changePercent": pct(price, prev),
        "marketState": None,
        "session": session,
        "timestamp": r.get("venue_last_non_reg_trade_time") or r.get("updated_at"),
        "source": "robinhood",
    }
    if raw:
        out["raw"] = r
    return out


def us_yahoo_plus_robinhood(market, code, raw=False):
    """Primary US path: Yahoo (state + OHLC + pre/post) enriched with the live
    Robinhood overnight(夜盘) print.  Both upstreams are fetched concurrently so
    the combined call costs ~max(yahoo, robinhood), not their sum."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_yahoo = ex.submit(YAHOO.quote, market, code)
        f_ov = ex.submit(robinhood_overnight, code)
        yq = f_yahoo.result()
        base = normalize_yahoo_quote(market, code, yq, raw=raw)
        try:
            ov = f_ov.result()
        except Exception as e:  # robinhood overnight optional
            base.setdefault("notes", []).append("overnight fetch failed: %s" % e)
            ov = None
    try:
        prev = base.get("previousClose")
        # session.* changes are quoted vs the regular-session close (the Yahoo
        # convention for pre/post); the top-level change stays vs previousClose.
        reg_close = (base["session"].get("regular") or {}).get("price")
        ref = reg_close if reg_close is not None else prev

        if ov and ov.get("price") is not None:
            ovp = ov["price"]
            ov_epoch = _parse_iso_epoch(ov.get("time"))
            # the last overnight bar is a live print when it is recent (5-min
            # bars; allow a little slack).  A stale overnight bar means the
            # session has ended -> report it but don't treat it as active.
            live = ov_epoch is not None and (time.time() - ov_epoch) < 20 * 60
            base["session"]["overnight"] = {
                "price": ovp,
                "change": round2(ovp - ref) if ref else None,
                "changePercent": pct(ovp, ref),
                "high": ov.get("high"),
                "low": ov.get("low"),
                "volume": ov.get("volume"),
                "time": ov.get("time"),
                "live": bool(live),
            }
            # once regular + after-hours are over, the live overnight print is
            # the active price.
            if live and base["marketState"] in ("CLOSED", "POST"):
                base["price"] = ovp
                base["change"] = round2(ovp - prev) if prev else None
                base["changePercent"] = pct(ovp, prev)
                base["marketState"] = "OVERNIGHT"
                base["timestamp"] = ov.get("time")  # active print -> freshest ts
        else:
            base["session"]["overnight"] = None
            if base["marketState"] == "CLOSED" and base["session"].get("post"):
                base.setdefault("notes", []).append(
                    "no overnight(夜盘) trades yet; showing last after-hours(盘后) price")
        base["source"] = "yahoo+robinhood"
    except Exception as e:  # enrichment optional
        base.setdefault("notes", []).append("overnight enrich failed: %s" % e)
    return base


# --------------------------------------------------------------------------- #
#  Tencent gtimg  (US / HK / CN)                                              #
# --------------------------------------------------------------------------- #
def tencent_quote(market, code, raw=False):
    if market == "us":
        key = "us" + code.upper()
    elif market == "hk":
        key = "hk" + code.zfill(5)
    else:
        key = cn_prefix(code) + code
    url = "https://qt.gtimg.cn/q=" + key
    txt = fetch_resilient(url, headers={"Referer": "https://gu.qq.com"}, encoding="gbk")
    m = re.search(r'="([^"]*)"', txt)
    if not m or not m.group(1):
        raise RuntimeError("tencent empty for " + key)
    f = m.group(1).split("~")
    # common layout: 0 mkt,1 name,2 code,3 price,4 prevclose,5 open,...
    name = f[1]
    price = num(f[3])
    prev = num(f[4])
    open_ = num(f[5])
    # fields differ a bit between markets; high/low live at known offsets
    high = low = vol = None
    change = changep = None
    ts = None
    try:
        if market == "cn":
            change, changep = num(f[31]), num(f[32])
            high, low = num(f[33]), num(f[34])
            vol = num(f[36])
            ts = f[30]
        elif market == "hk":
            high, low = num(f[33]), num(f[34])
            change, changep = num(f[31]), num(f[32])
            vol = num(f[36])
            ts = f[30]
        else:  # us
            high, low = num(f[33]), num(f[34])
            change, changep = num(f[31]), num(f[32])
            vol = num(f[36])
            ts = f[30]
    except (IndexError, ValueError):
        pass
    if change is None and price is not None and prev:
        change, changep = round2(price - prev), pct(price, prev)
    cur = {"us": "USD", "hk": "HKD", "cn": "CNY"}[market]
    out = {
        "ok": True,
        "symbol": code.upper() if market == "us" else key,
        "market": market.upper(),
        "name": name,
        "currency": cur,
        "price": price,
        "previousClose": prev,
        "open": open_,
        "dayHigh": high,
        "dayLow": low,
        "volume": vol,
        "change": round2(change),
        "changePercent": round2(changep),
        "marketState": None,
        "session": {"regular": {"price": price, "change": round2(change),
                                "changePercent": round2(changep)}},
        "timestamp": ts,
        "source": "tencent",
    }
    if raw:
        out["raw"] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
#  Sina hq  (US / HK / CN)  -- US feed carries after-hours fields             #
# --------------------------------------------------------------------------- #
def sina_quote(market, code, raw=False):
    if market == "us":
        key = "gb_" + code.lower()
    elif market == "hk":
        key = "hk" + code.zfill(5)
    else:
        key = cn_prefix(code) + code
    url = "https://hq.sinajs.cn/list=" + key
    txt = fetch_resilient(url, headers={"Referer": "https://finance.sina.com.cn"},
                          encoding="gbk")
    m = re.search(r'="([^"]*)"', txt)
    if not m or not m.group(1):
        raise RuntimeError("sina empty for " + key)
    f = m.group(1).split(",")
    out = {"ok": True, "market": market.upper(), "source": "sina",
           "session": {}, "marketState": None}
    if market == "cn":
        # name,open,prevclose,price,high,low,...,date,time
        out.update({
            "symbol": key, "name": f[0], "currency": "CNY",
            "open": num(f[1]), "previousClose": num(f[2]),
            "price": num(f[3]), "dayHigh": num(f[4]), "dayLow": num(f[5]),
            "volume": num(f[8]),
            "timestamp": (f[30] + " " + f[31]) if len(f) > 31 else None,
        })
    elif market == "hk":
        # engname,cnname,open,prevclose,high,low,price,change,pct,...,date,time
        out.update({
            "symbol": key, "name": f[1], "currency": "HKD",
            "open": num(f[2]), "previousClose": num(f[3]),
            "dayHigh": num(f[4]), "dayLow": num(f[5]), "price": num(f[6]),
            "change": num(f[7]), "changePercent": num(f[8]),
            "timestamp": (f[17] + " " + f[18]) if len(f) > 18 else None,
        })
    else:  # us:  name,price,pct,time(beijing),change,prevclose?,open,high,low,...
        price = num(f[1])
        prev = num(f[26]) if len(f) > 26 else None   # f[26]=昨收
        out.update({
            "symbol": code.upper(), "name": f[0], "currency": "USD",
            "price": price, "changePercent": num(f[2]),
            "change": num(f[4]),
            "open": num(f[5]), "dayHigh": num(f[6]), "dayLow": num(f[7]),
            "previousClose": prev,
            "volume": num(f[10]),
            "timestamp": f[3] if len(f) > 3 else None,
        })
        # after-hours fields (盘后) live near the tail of the sina US feed:
        # f[21]=post price, f[22]=post %chg, f[23]=post chg amount, f[24]=post time
        if len(f) > 24:
            post_price = num(f[21])
            if post_price:
                out["session"]["post"] = {
                    "price": post_price,
                    "change": num(f[23]),
                    "changePercent": num(f[22]),
                    "time": f[24],
                }
                out["marketState"] = "POST"
    if out.get("change") is None and out.get("price") is not None and out.get("previousClose"):
        out["change"] = round2(out["price"] - out["previousClose"])
        out["changePercent"] = pct(out["price"], out["previousClose"])
    out["session"]["regular"] = {"price": out.get("price"),
                                 "change": out.get("change"),
                                 "changePercent": out.get("changePercent")}
    if raw:
        out["raw"] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
#  Eastmoney push2  (US / HK / CN)                                            #
# --------------------------------------------------------------------------- #
def eastmoney_quote(market, code, raw=False):
    secid = eastmoney_secid(market, code)
    fields = "f43,f44,f45,f46,f47,f57,f58,f59,f60,f86,f168,f169,f170,f171"
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid="
           + secid + "&fields=" + fields)
    txt = fetch_resilient(url, headers={"Referer": "https://quote.eastmoney.com"})
    d = json.loads(txt)
    data = d.get("data")
    if not data:
        raise RuntimeError("eastmoney empty for " + secid)
    dec = data.get("f59")
    scale = 10 ** dec if isinstance(dec, int) and dec >= 0 else 100

    def sc(x):
        v = num(x)
        return None if v is None else round(v / scale, 6)

    price = sc(data.get("f43"))
    prev = sc(data.get("f60"))
    cur = {"us": "USD", "hk": "HKD", "cn": "CNY"}[market]
    change = num(data.get("f169"))
    changep = num(data.get("f170"))
    out = {
        "ok": True,
        "symbol": str(data.get("f57") or code),
        "market": market.upper(),
        "name": data.get("f58"),
        "currency": cur,
        "price": price,
        "previousClose": prev,
        "open": sc(data.get("f46")),
        "dayHigh": sc(data.get("f44")),
        "dayLow": sc(data.get("f45")),
        "volume": num(data.get("f47")),
        "change": round2(change / scale) if change is not None else (
            round2(price - prev) if (price and prev) else None),
        "changePercent": round2(changep / 100.0) if changep is not None else pct(price, prev),
        "marketState": None,
        "session": {"regular": {"price": price}},
        "timestamp": iso_from_epoch(data.get("f86")),
        "source": "eastmoney",
    }
    if raw:
        out["raw"] = data
    return out


# --------------------------------------------------------------------------- #
#  Fuzzy search / symbol resolution                                           #
#  (name / pinyin / english / code  ->  primary listing)                      #
# --------------------------------------------------------------------------- #
# market preference when a company is listed in several places.  US listings
# of Chinese companies are usually ADRs (secondary), so HK / mainland win.
_MARKET_RANK = {"hk": 1, "cn": 2, "us": 3, "other": 9}
_US_EXCH = {"NMS", "NYQ", "NGM", "NCM", "PCX", "ASE", "BTS", "NYS", "OPR",
            "PNK", "OTC", "OOTC"}
_RESOLVE_CACHE = {}
_RESOLVE_TTL = 600


def _has_cjk(s):
    return any("一" <= ch <= "鿿" for ch in s)


_CORP_SUFFIX = re.compile(
    r"(集团|控股|股份|有限公司|公司|holdings?|group|limited|ltd|"
    r"incorporated|inc|corporation|corp|co)\.?$", re.I)


def norm_company(name):
    """Canonical key so the same company across markets collides, while
    distinct companies stay apart.  Strips depositary suffixes and trailing
    corporate-form words (集团/控股/Group/Holdings/Inc...) so e.g.
    京东 == 京东集团 and Alibaba == 'Alibaba Group Holding Limited',
    but 京东 != 京东方 and 平安银行 != 中国平安."""
    if not name:
        return ""
    s = re.sub(r"\s*\(adr\)\s*", "", name, flags=re.I).strip()
    # HK listing tag right after a Chinese name: -S/-W/-R/-SW (secondary /
    # weighted-voting / depositary).  A lone trailing latin letter on a CJK
    # name is always such a tag, never part of the real name.
    s = re.sub(r"([一-鿿])(swr|sw|sr|wr|s|w|r)$", r"\1", s, flags=re.I)
    s = re.sub(r"[\s,\.\-_/]+", "", s)
    for _ in range(4):                     # peel nested corporate suffixes
        s2 = _CORP_SUFFIX.sub("", s)
        if s2 == s or not s2:
            break
        s = s2
    return s.lower()


def smartbox_search(query):
    """Tencent smartbox: covers CJK / pinyin / english / code, all markets."""
    url = "https://smartbox.gtimg.cn/s3/?t=all&q=" + urllib.parse.quote(query)
    txt = fetch_resilient(url, headers={"Referer": "https://stockapp.finance.qq.com"},
                          encoding="gbk")
    m = re.search(r'="([^"]*)"', txt)
    if not m or not m.group(1):
        return []
    out = []
    for ent in m.group(1).split("^"):
        f = ent.split("~")
        if len(f) < 5:
            continue
        mk, code, name, kw, typ = f[0], f[1], f[2], f[3], f[4]
        if not typ.startswith("GP"):       # stocks only (skip index/warrant/fund)
            continue
        try:
            name = json.loads('"' + name.replace('"', '\\"') + '"')
        except Exception:
            pass
        if mk == "us":
            ticker = code.split(".")[0].upper()
            out.append({"market": "us", "code": ticker, "symbol": ticker,
                        "name": name, "exchange": "US"})
        elif mk == "hk":
            out.append({"market": "hk", "code": code.zfill(5), "symbol": code.zfill(5),
                        "name": name, "exchange": "HKG"})
        elif mk in ("sh", "sz"):
            out.append({"market": "cn", "code": code, "symbol": code,
                        "name": name, "exchange": mk.upper()})
    return out


def yahoo_search(query):
    """Yahoo search: clean english longname (groups dual-listings) + codes."""
    url = ("https://query2.finance.yahoo.com/v1/finance/search?quotesCount=10"
           "&newsCount=0&q=" + urllib.parse.quote(query))
    try:
        txt = http_get(url, timeout=8)
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            txt = http_get_via_proxy(url)
        else:
            raise
    d = json.loads(txt)
    out = []
    for q in d.get("quotes", []):
        if q.get("quoteType") != "EQUITY":   # stocks only (drop ETF/index noise)
            continue
        sym = q.get("symbol") or ""
        name = q.get("longname") or q.get("shortname") or sym
        ex = q.get("exchange") or ""
        low = sym.lower()
        if low.endswith(".hk"):
            mk, code = "hk", sym[:-3].zfill(5)
        elif low.endswith(".ss") or low.endswith(".sh"):
            mk, code = "cn", re.sub(r"\.(ss|sh)$", "", low)
        elif low.endswith(".sz"):
            mk, code = "cn", low[:-3]
        elif "." not in sym and ex in _US_EXCH:
            mk, code = "us", sym.upper()
        else:
            continue                       # foreign listing (FRA/TAI/...) -> skip
        out.append({"market": mk, "code": code, "symbol": sym,
                    "name": name, "exchange": ex})
    return out


def pick_primary(cands):
    """From candidate listings choose the primary one (anchor = most relevant
    first hit; among its sibling listings prefer HK > CN > US, demoting
    depositary/secondary variants)."""
    anchor = cands[0]
    akey = norm_company(anchor["name"])
    group = [c for c in cands if norm_company(c["name"]) == akey] or [anchor]

    def score(c):
        mr = _MARKET_RANK.get(c["market"], 9)
        variant = 0
        nm = (c.get("name") or "")
        if re.search(r"(wr|r|sw)$", nm, flags=re.I) or "(adr)" in nm.lower():
            variant += 2
        if c["market"] == "hk" and str(c.get("code", "")).startswith("8"):
            variant += 1                   # 8xxxx = southbound/CDR variant
        if c["market"] == "us" and c.get("exchange") in ("PNK", "OTC", "OOTC"):
            variant += 1                   # OTC ADR
        return (mr, variant)

    group.sort(key=score)
    chosen = group[0]
    alts = [{"market": c["market"], "code": c["code"], "symbol": c.get("symbol"),
             "name": c.get("name")} for c in cands if c is not chosen][:6]
    return chosen, alts


def resolve_symbol(query):
    """query (name/pinyin/english/code) -> {market, code, name, ...}."""
    q = query.strip()
    key = q.lower()
    hit = _RESOLVE_CACHE.get(key)
    if hit and time.time() - hit[1] < _RESOLVE_TTL:
        return hit[0]

    def _safe(eng):
        try:
            return eng(q) or []
        except Exception:
            return []

    cjk = _has_cjk(q)
    if cjk:
        # smartbox understands Chinese; yahoo doesn't -> smartbox only
        cands = _safe(smartbox_search) or _safe(yahoo_search)
    else:
        # run both concurrently: yahoo groups dual-listings cleanly (correct
        # primary), smartbox is the fast fallback if yahoo is slow / empty.
        with ThreadPoolExecutor(max_workers=2) as ex:
            fy = ex.submit(_safe, yahoo_search)
            fs = ex.submit(_safe, smartbox_search)
            done, _ = wait([fy], timeout=2.5)
            cands = (fy.result() if fy in done else []) or fs.result()
    if not cands:
        raise RuntimeError("no match for '%s'" % query)

    chosen, alts = pick_primary(cands)
    result = {"market": chosen["market"], "code": chosen["code"],
              "name": chosen.get("name"), "matchedSymbol": chosen.get("symbol"),
              "alternatives": alts}
    _RESOLVE_CACHE[key] = (result, time.time())
    return result


# --------------------------------------------------------------------------- #
#  Orchestration: parallel source racing (first good result wins)             #
# --------------------------------------------------------------------------- #
SOURCE_FUNCS = {
    "tencent": tencent_quote,
    "sina": sina_quote,
    "eastmoney": eastmoney_quote,
}


def race(tasks, overall_timeout=8.0, prefer_grace=0.0):
    """Run tasks concurrently, return the first acceptable result.

    tasks: list of (rank, name, fn).  Higher rank = richer/preferred source.
    As soon as a max-rank source returns, ship it.  If a lower-rank source
    returns first, wait up to `prefer_grace` seconds for a richer one, then
    settle for the best we have.  This makes the common case fast while still
    favouring the source with pre/post/overnight detail when it's quick."""
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    fmap = {ex.submit(fn): rank for rank, _name, fn in tasks}
    max_rank = max(r for r, _n, _f in tasks)
    best = None
    settle = None
    pending = set(fmap)
    try:
        t_end = time.time() + overall_timeout
        while pending:
            limit = t_end if settle is None else min(t_end, settle)
            timeout = limit - time.time()
            if timeout <= 0:
                break
            done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                pending.discard(fut)
                rank = fmap[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if not res or res.get("price") is None:
                    continue
                if best is None or rank > best[0]:
                    best = (rank, res)
                if rank >= max_rank:
                    return res
                if settle is None and prefer_grace > 0:
                    settle = time.time() + prefer_grace
        return best[1] if best else None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def race_market(market, code, raw=False):
    if market == "us":
        # yahoo+robinhood is the only US source with pre/post/overnight, so it
        # gets the top rank and a wide grace window; sina carries after-hours
        # (盘后) and outranks tencent (regular session only) as the fallback.
        tasks = [
            (3, "yahoo+robinhood", lambda: us_yahoo_plus_robinhood("us", code, raw=raw)),
            (2, "sina", lambda: sina_quote("us", code, raw=raw)),
            (1, "tencent", lambda: tencent_quote("us", code, raw=raw)),
        ]
        # yahoo+robinhood is the only source carrying live overnight(夜盘); give it
        # a generous grace so it wins whenever it answers within ~2.5s, falling
        # back to sina (盘后) / tencent only when it is genuinely slow.
        return race(tasks, overall_timeout=8.0, prefer_grace=2.5)
    tasks = [
        (2, "tencent", lambda: tencent_quote(market, code, raw=raw)),
        (2, "sina", lambda: sina_quote(market, code, raw=raw)),
        (1, "eastmoney", lambda: eastmoney_quote(market, code, raw=raw)),
    ]
    return race(tasks, overall_timeout=8.0, prefer_grace=0.0)


def _looks_explicit(s):
    """True when the input is clearly a ticker/code (skip fuzzy search)."""
    low = s.lower()
    if re.fullmatch(r"\d{1,6}", s):
        return True
    if re.search(r"\.(hk|ss|sz|sh|us)$", low):
        return True
    if re.match(r"^(hk|sh|sz|bj)\d", low) or re.match(r"^US[A-Z]+$", s):
        return True
    # bare letters count as an explicit ticker only when UPPERCASE (AAPL, BRK.B);
    # lowercase words (apple, tencent, alibaba) are treated as names -> fuzzy.
    if re.fullmatch(r"[A-Z]{1,6}([.\-][A-Z])?", s):
        return True
    return False


def get_quote(query, market=None, source=None, raw=False, fuzzy=True):
    query = query.strip()
    resolved = None

    # ---- 1. determine (market, code) ----
    if market:
        market = market.lower()
        m2, code = detect_market(query)
        if m2 != market:
            code = re.sub(r"[^0-9A-Za-z]", "", query)
            code = re.sub(r"^(us|hk|sh|sz|bj)", "", code, flags=re.I) \
                if not code.isdigit() or len(code) > 6 else code
            if market == "hk" and code.isdigit():
                code = code.zfill(5)
            if market == "us":
                code = code.upper()
    elif fuzzy and (_has_cjk(query) or not _looks_explicit(query)):
        try:
            resolved = resolve_symbol(query)
            market, code = resolved["market"], resolved["code"]
        except Exception:
            market, code = detect_market(query)
    else:
        market, code = detect_market(query)

    # ---- 2. forced single source (no race) ----
    if source:
        source = source.lower()
        if source == "yahoo":
            res = normalize_yahoo_quote(market, code, YAHOO.quote(market, code), raw=raw)
        elif source == "robinhood":
            res = normalize_robinhood(code, robinhood_quote(code), raw=raw)
        elif source in SOURCE_FUNCS:
            res = SOURCE_FUNCS[source](market, code, raw=raw)
        else:
            raise RuntimeError("unknown source: " + source)
        res.setdefault("market", market.upper())
        res["code"] = code
        if resolved:
            res["query"], res["resolved"] = query, resolved
        return res

    # ---- 3. race the per-market sources ----
    res = race_market(market, code, raw=raw)

    # explicit guess wrong / not found? fall back to fuzzy search once.
    if (not res or res.get("price") is None) and resolved is None and fuzzy:
        try:
            resolved = resolve_symbol(query)
            if (resolved["market"], resolved["code"]) != (market, code):
                market, code = resolved["market"], resolved["code"]
                res = race_market(market, code, raw=raw)
        except Exception:
            pass

    if res and res.get("price") is not None:
        res.setdefault("market", market.upper())
        res["code"] = code
        if resolved:
            res["query"], res["resolved"] = query, resolved
        return res

    return {"ok": False, "query": query, "market": (market or "?").upper(),
            "error": "all sources failed / no match", "resolved": resolved}


# --------------------------------------------------------------------------- #
#  Markdown rendering (display-ready summary of the market-data fields)        #
# --------------------------------------------------------------------------- #
_STATE_CN = {"PRE": "盘前", "REGULAR": "盘中", "POST": "盘后",
             "OVERNIGHT": "夜盘", "CLOSED": "已收盘"}
_SESSION_ROWS = [("pre", "盘前"), ("regular", "盘中"), ("post", "盘后"),
                 ("overnight", "夜盘")]
_STATE_KEY = {"PRE": "pre", "REGULAR": "regular", "POST": "post",
              "OVERNIGHT": "overnight"}


def _fmt_price(v):
    if v is None:
        return "-"
    return ("%.4f" if abs(v) < 1 else "%.2f") % v


def _fmt_pct(v):
    return "-" if v is None else "%+.2f%%" % v


def _fmt_chg(v):
    return "-" if v is None else "%+.2f" % v


def _fmt_vol(v):
    if v is None:
        return "-"
    v = float(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return ("%.2f" % (v / div)).rstrip("0").rstrip(".") + unit
    return str(int(v))


BJ_TZ = timezone(timedelta(hours=8))


def _norm_ts(ts):
    """Normalize any upstream timestamp into a clean Beijing-time
    'YYYY-MM-DD HH:MM:SS' string.  Naive times from the Chinese sources are
    already Beijing; tz-aware / UTC / epoch values are converted.  Returns None
    when there is nothing usable."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    s = str(ts).strip()
    if re.fullmatch(r"\d{14}", s):                      # 20260624114913
        return "%s-%s-%s %s:%s:%s" % (s[0:4], s[4:6], s[6:8],
                                      s[8:10], s[10:12], s[12:14])
    if re.fullmatch(r"\d{8}", s):                       # 20260624
        return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8])
    if re.fullmatch(r"\d{13}", s):                      # epoch millis as string
        return datetime.fromtimestamp(int(s) / 1000, BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if re.fullmatch(r"\d{10}", s):                      # epoch seconds as string
        return datetime.fromtimestamp(int(s), BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    s2 = s.replace("/", "-")                            # 2026/06/24 -> 2026-06-24
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", s2):
        return s2
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", s2):
        return s2 + ":00"
    ep = _parse_iso_epoch(s)                            # ISO w/ Z or offset
    if ep is not None:
        return datetime.fromtimestamp(ep, BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return s


def _arrow(v):
    return "🔺" if (v or 0) > 0 else ("🔻" if (v or 0) < 0 else "➖")


def _row(cells, bold=False):
    if bold:
        cells = ["**%s**" % c for c in cells]
    return "| " + " | ".join(cells) + " |"


def build_markdown(r):
    """Render the market-data fields of a normalized quote into one
    display-ready markdown block.  The current price is the focal point
    (big heading); below it sit the per-session breakdown, day range and a
    Beijing-time stamp."""
    cur = r.get("currency") or ""
    name = r.get("name") or r.get("symbol") or r.get("code") or "?"
    sym = r.get("symbol") or r.get("code") or ""
    mkt = r.get("market") or ""
    sess = r.get("session") or {}
    state = r.get("marketState")
    akey = _STATE_KEY.get(state or "", "")
    act = sess.get(akey) or {}

    # headline change: the active session's own move (e.g. overnight vs today's
    # close), falling back to the top-level change (vs previous close).
    chg = act.get("change") if act.get("change") is not None else r.get("change")
    chgp = (act.get("changePercent") if act.get("changePercent") is not None
            else r.get("changePercent"))
    reg_close = (sess.get("regular") or {}).get("price")
    if akey in ("pre", "post", "overnight") and reg_close is not None:
        ref_label, ref_val = "今收", reg_close
    else:
        ref_label, ref_val = "昨收", r.get("previousClose")

    # ---- title + prominent current price ----
    state_cn = _STATE_CN.get(state or "", "")
    live = "🟢实时" if (akey == "overnight" and act.get("live")) else ""
    title = "### %s　`%s`・%s%s" % (
        name, sym, mkt, ("・" + state_cn + live) if state_cn else "")
    price_line = "# %s %s　%s %s（%s）" % (
        _fmt_price(r.get("price")), cur, _arrow(chgp), _fmt_chg(chg), _fmt_pct(chgp))
    lines = [title, "", price_line]

    ts = _norm_ts(act.get("time")) or _norm_ts(r.get("timestamp"))
    sub = []
    if ref_val is not None:
        sub.append("较%s %s" % (ref_label, _fmt_price(ref_val)))
    if ts:
        sub.append("更新于 %s（北京时间）" % ts)
    if sub:
        lines += ["", "> " + "　·　".join(sub)]

    # ---- per-session breakdown (US extended hours) ----
    if sess.get("pre") or sess.get("post") or sess.get("overnight"):
        rows = ["| 时段 | 现价 | 涨跌 | 涨跌幅 |", "| :-- | --: | --: | --: |"]
        for key, label in _SESSION_ROWS:
            s = sess.get(key)
            if not s or s.get("price") is None:
                continue
            tag = label + (" 🟢" if (key == "overnight" and s.get("live")) else "")
            rows.append(_row([tag, _fmt_price(s.get("price")),
                              _fmt_chg(s.get("change")), _fmt_pct(s.get("changePercent"))],
                             bold=(key == akey)))
        if len(rows) > 2:
            lines += [""] + rows

    # ---- day range / fundamentals ----
    facts = []
    for key, label in (("previousClose", "昨收"), ("open", "今开"),
                       ("dayHigh", "最高"), ("dayLow", "最低")):
        if r.get(key) is not None:
            facts.append("**%s** %s" % (label, _fmt_price(r[key])))
    if r.get("volume") is not None:
        facts.append("**成交量** %s" % _fmt_vol(r["volume"]))
    if facts:
        lines += ["", "　·　".join(facts)]

    # ---- overnight own range ----
    ov = sess.get("overnight") or {}
    if ov.get("price") is not None and (ov.get("high") is not None
                                        or ov.get("low") is not None):
        od = []
        if ov.get("low") is not None and ov.get("high") is not None:
            od.append("**夜盘区间** %s – %s" % (_fmt_price(ov["low"]), _fmt_price(ov["high"])))
        if ov.get("volume") is not None:
            od.append("**夜盘量** %s" % _fmt_vol(ov["volume"]))
        if od:
            lines.append("　·　".join(od))

    if r.get("source"):
        lines += ["", "<sub>数据来源 %s</sub>" % r["source"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  HTTP server                                                                #
# --------------------------------------------------------------------------- #
USAGE = {
    "service": "realtime-stock-quote",
    "markets": ["US (pre/regular/post/overnight)", "HK", "CN A-share"],
    "features": [
        "fuzzy search: name / pinyin / english / code (腾讯, tencent, 700, 0700.HK ...)",
        "multi-listed company resolves to its primary venue (HK > CN > US ADR)",
        "parallel source racing: returns as soon as one source answers",
    ],
    "endpoints": {
        "/quote?q=腾讯": "fuzzy query (name/pinyin/code), auto market + primary listing",
        "/quote?q=tencent": "english name",
        "/quote?symbol=AAPL": "exact ticker",
        "/quote?q=阿里巴巴": "multi-listed -> HK 09988 (primary)",
        "/quote?q=AAPL&market=us": "force market",
        "/quote?q=AAPL&source=robinhood": "force one source",
        "/quote?q=AAPL&fuzzy=0": "disable fuzzy resolution",
        "/quote?q=AAPL&raw=1": "include upstream raw payload",
        "/quote/0700.HK": "path style",
        "/search?q=腾讯": "resolve only: show matched symbol + alternatives",
        "/health": "liveness",
    },
    "examples": [
        "/quote?q=腾讯", "/quote?q=tencent", "/quote?q=700",
        "/quote?q=阿里巴巴", "/quote?q=茅台", "/quote?q=苹果",
        "/quote?symbol=AAPL", "/quote?symbol=600519",
    ],
    "sources": ["yahoo", "robinhood", "tencent", "sina", "eastmoney"],
}


class Handler(BaseHTTPRequestHandler):
    server_version = "StockQuote/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # http.server decodes the request line as latin-1, so a client that
        # sends raw (non-percent-encoded) UTF-8 in the URL arrives here as
        # mojibake.  Recover the original bytes and re-decode as UTF-8.
        # Percent-encoded / pure-ASCII paths round-trip through latin-1
        # unchanged, so they are unaffected.
        try:
            self.path = self.path.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/help"):
            return self._send(200, USAGE)
        if path == "/health":
            return self._send(200, {"ok": True, "time": datetime.now(timezone.utc).isoformat()})
        if path == "/favicon.ico":
            return self._send(404, {"ok": False})

        # ---- /search : resolve only ----
        if path == "/search":
            q = (qs.get("q") or qs.get("symbol") or qs.get("s") or [None])[0]
            if not q:
                return self._send(400, {"ok": False, "error": "missing q"})
            t0 = time.time()
            try:
                r = resolve_symbol(q)
                r = dict(r, ok=True, query=q,
                         latencyMs=int((time.time() - t0) * 1000))
                return self._send(200, r)
            except Exception as e:  # noqa
                return self._send(404, {"ok": False, "query": q, "error": str(e)})

        symbol = None
        if path.startswith("/quote/"):
            symbol = urllib.parse.unquote(path[len("/quote/"):])
        elif path == "/quote":
            symbol = (qs.get("q") or qs.get("symbol") or qs.get("s") or [None])[0]
        else:
            return self._send(404, {"ok": False, "error": "not found",
                                    "hint": "use /quote?q=腾讯 or /quote?symbol=AAPL"})

        if not symbol:
            return self._send(400, {"ok": False, "error": "missing symbol/q"})

        market = (qs.get("market") or [None])[0]
        source = (qs.get("source") or [None])[0]
        raw = (qs.get("raw") or ["0"])[0] in ("1", "true", "yes")
        fuzzy = (qs.get("fuzzy") or ["1"])[0] not in ("0", "false", "no")

        t0 = time.time()
        try:
            res = get_quote(symbol, market=market, source=source, raw=raw, fuzzy=fuzzy)
        except Exception as e:  # noqa
            return self._send(502, {"ok": False, "symbol": symbol, "error": str(e)})
        res["latencyMs"] = int((time.time() - t0) * 1000)
        res["fetchedAt"] = datetime.now(timezone.utc).isoformat()
        if res.get("ok"):
            # clean up upstream timestamps -> uniform Beijing-time strings
            res["timestamp"] = _norm_ts(res.get("timestamp"))
            for _s in (res.get("session") or {}).values():
                if isinstance(_s, dict) and _s.get("time"):
                    _s["time"] = _norm_ts(_s["time"])
            try:
                res["markdown"] = build_markdown(res)
            except Exception as e:  # markdown is best-effort, never fatal
                res["markdown"] = None
                res.setdefault("notes", []).append("markdown render failed: %s" % e)
            status = 200
        elif "all sources failed" in str(res.get("error", "")):
            status = 404          # symbol not found / unavailable everywhere
        else:
            status = 502
        return self._send(status, res)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (
            datetime.now().strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Stock quote server on http://%s:%d" % (args.host, args.port))
    print("Try: curl 'http://127.0.0.1:%d/quote?symbol=AAPL'" % args.port)
    # warm the Yahoo crumb/cookie in the background so the first US query is
    # fast enough to win the race (and thus carry live overnight/夜盘 data).
    threading.Thread(target=lambda: YAHOO._ensure_crumb(), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
