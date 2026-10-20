# -*- coding: utf-8 -*-
"""数据抓取层：全部走公开接口，不需要登录、不需要截图。

数据源（按优先级，逐级兜底）
  · 场内实时价   腾讯行情 qt.gtimg.cn（一次请求可批量，含开盘/昨收/成交额）
  · 纳指100实时  新浪美股 gb_$ndx
  · 纳指期货NQ   新浪外盘 hf_NQ
  · 纳指100历史  东财 100.NDX100  ->  新浪 .NDX 日K
  · 基金净值     天天基金 pingzhongdata/{code}.js
  · 场内K线      腾讯 fqkline/mkline  ->  东财 push2his

所有请求带本地缓存，避免反复抓取被限流。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, ".cache")
CACHE_ENABLED = os.environ.get("NDX_NO_CACHE") != "1"


def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".bin")


def _read_cache(url: str, ttl: float) -> bytes | None:
    if not (CACHE_ENABLED and ttl > 0):
        return None
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) > ttl:
            return None
        with open(p, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_cache(url: str, body: bytes) -> None:
    if not CACHE_ENABLED:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "wb") as fh:
            fh.write(body)
    except OSError:
        pass


def _get(url: str, referer: str | None = None, timeout: int = 15,
         retries: int = 3, encoding: str | None = None,
         ttl: float = 0.0) -> str | bytes:
    """带缓存 + 重试的 GET。ttl 单位为秒，0 表示不缓存。"""
    cached = _read_cache(url, ttl)
    if cached is not None:
        return cached.decode(encoding, "replace") if encoding else cached

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer or "https://finance.sina.com.cn",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
                raw = resp.read()
            if len(raw) > 200:
                _write_cache(url, raw)
            return raw.decode(encoding, "replace") if encoding else raw
        except Exception as exc:          # noqa: BLE001 - 网络抖动统一重试
            last_err = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET 失败 {url} -> {last_err!r}")


# ---------------------------------------------------------------- 实时行情
def tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """批量取场内实时行情。codes 形如 ['sh513100', 'sz159509']。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        text = _get("https://qt.gtimg.cn/q=" + ",".join(chunk), encoding="gbk", ttl=20)
        for line in text.split(";"):
            if '"' not in line:
                continue
            f = line.split('"')[1].split("~")
            if len(f) < 40:
                continue
            try:
                price, prev = float(f[3]), float(f[4])
            except ValueError:
                continue
            if price <= 0:
                price = prev
            out[f[2]] = {
                "code": f[2], "name": f[1],
                "price": price, "prev_close": prev,
                "open": float(f[5] or 0), "high": float(f[33] or 0), "low": float(f[34] or 0),
                "quote_time": f[30],
                "volume": float(f[36] or 0), "amount": float(f[37] or 0) * 10000.0,
                "turnover": float(f[38]) if f[38] not in ("", "-") else None,
                "pct": (price / prev - 1.0) if prev else 0.0,
            }
    return out


def nx_index() -> dict:
    """纳斯达克100 指数实时（新浪美股）。"""
    text = _get("https://hq.sinajs.cn/list=gb_$ndx", encoding="gbk", ttl=20)
    f = text.split('"')[1].split(",")
    price, chg = float(f[1]), float(f[4])
    return {"price": price, "prev_close": price - chg, "chg": chg,
            "pct": float(f[2]) / 100.0, "time": f[3]}


def nq_futures() -> dict:
    """纳指期货连续合约（新浪外盘）。f[7] 为昨收/昨结算。"""
    text = _get("https://hq.sinajs.cn/list=hf_NQ", encoding="gbk", ttl=20)
    f = text.split('"')[1].split(",")
    price, prev = float(f[0]), float(f[7])
    return {"price": price, "prev_close": prev,
            "pct": (price / prev - 1.0) if prev else 0.0,
            "time": f[6], "date": f[12] if len(f) > 12 else ""}


# ---------------------------------------------------------------- K 线
EM_HOSTS = ["https://push2his1.eastmoney.com", "https://push2his2.eastmoney.com",
            "https://push2his.eastmoney.com"]


def em_kline(secid: str, klt: int = 101, limit: int = 250, fqt: int = 0,
             ttl: float = 900) -> list[dict]:
    """东财K线。klt: 101=日线 60=60分钟。fqt: 0不复权 1前复权。"""
    path = ("/api/qt/stock/kline/get"
            f"?secid={secid}&klt={klt}&fqt={fqt}&lmt={limit}&end=20500101"
            "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    data, last_err = None, None
    for host in EM_HOSTS:
        try:
            data = json.loads(_get(host + path, referer="https://quote.eastmoney.com/",
                                   retries=1, ttl=ttl))
            if (data or {}).get("data"):
                break
            data = None
        except Exception as exc:                   # noqa: BLE001
            last_err = exc
    if not data:
        raise RuntimeError(f"东财K线失败 {secid}: {last_err!r}")
    rows = data["data"].get("klines") or []
    return [{"date": p[0][:16], "open": float(p[1]), "close": float(p[2]),
             "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
             "amount": float(p[6]) if len(p) > 6 else 0.0}
            for p in (r.split(",") for r in rows)]


def tencent_kline(pref_code: str, period: str = "day", limit: int = 250,
                  fq: bool = True, ttl: float = 900) -> list[dict]:
    """腾讯K线。pref_code 形如 sh513100；period: day / m15 / m60。"""
    if period.startswith("m"):
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={pref_code},{period},,{limit}"
    else:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={pref_code},day,,,{limit},{'qfq' if fq else ''}")
    data = json.loads(_get(url, referer="https://gu.qq.com/", ttl=ttl))
    node = data["data"][pref_code]
    keys = ([period + "_qfq", "qfqday"] if period == "day" and fq else [period])
    keys += ["day", "qfqday", "m15", "m60"]
    key = next((k for k in keys if k in node), None)
    rows = node.get(key) or []
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        d = str(r[0])
        if len(d) == 12 and d.isdigit():           # 202610191445 -> 2026-10-19 14:45
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:]}"
        out.append({"date": d, "open": float(r[1]), "close": float(r[2]),
                    "high": float(r[3]), "low": float(r[4]),
                    "volume": float(r[5]) if len(r) > 5 else 0.0, "amount": 0.0})
    return out


def a_kline(pref_code: str, secid: str, period: str = "day", limit: int = 250,
            fqt: int = 0) -> list[dict]:
    """场内K线：腾讯优先，东财兜底。"""
    try:
        return tencent_kline(pref_code, period, limit, fq=bool(fqt))
    except Exception:                              # noqa: BLE001
        klt = {"day": 101, "m60": 60, "m15": 15}.get(period, 101)
        return em_kline(secid, klt, limit, fqt=fqt)


# ---------------------------------------------------------------- 纳指历史
def ndx_history(limit: int = 420) -> list[dict]:
    """纳指100 日线：东财 -> 新浪。"""
    try:
        rows = em_kline("100.NDX100", 101, limit, ttl=3600)
        if len(rows) > 60:
            return rows
    except Exception:                              # noqa: BLE001
        pass
    return sina_ndx_daily(limit)


def sina_ndx_daily(limit: int = 420) -> list[dict]:
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20x=/"
           "US_MinKService.getDailyK?symbol=.NDX&___qn=3")
    text = _get(url, referer="https://finance.sina.com.cn", ttl=3600,
                encoding="utf-8")
    m = re.search(r"\((\[.*?\])\)", text, re.S)
    if not m:
        raise RuntimeError("新浪纳指历史解析失败")
    rows = json.loads(m.group(1))
    out = [{"date": r["d"], "open": float(r["o"]), "close": float(r["c"]),
            "high": float(r["h"]), "low": float(r["l"]), "volume": 0.0, "amount": 0.0}
           for r in rows]
    out.sort(key=lambda x: x["date"])
    return out[-limit:]


# ---------------------------------------------------------------- 净值
_NAV_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", re.S)


def fund_nav(code: str) -> list[dict]:
    """单位净值历史，返回 [{'date': 'YYYY-MM-DD', 'nav': float}, ...] 升序。"""
    text = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                referer="https://fund.eastmoney.com/", ttl=1800,
                encoding="utf-8")
    m = _NAV_RE.search(text)
    if not m:
        raise RuntimeError(f"{code} 净值解析失败")
    out = []
    for r in json.loads(m.group(1)):
        try:
            d = datetime(1970, 1, 1) + timedelta(hours=8, seconds=r["x"] / 1000.0)
            out.append({"date": d.strftime("%Y-%m-%d"), "nav": float(r["y"])})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------- 批量
def fetch_bundle(peers: list[dict], history_days: int = 120) -> dict:
    """并发抓取所有基金的净值 + 日线（不复权/前复权）+ 60分钟线。"""
    result: dict[str, dict] = {}

    def one(p: dict) -> tuple[str, dict]:
        code, market = p["code"], p["market"]
        pref = f"{market}{code}"
        secid = f"{'1' if market == 'sh' else '0'}.{code}"
        row: dict = {"nav": [], "kline": [], "kline_adj": [], "m15": [], "errors": []}
        try:
            row["nav"] = fund_nav(code)
        except Exception as exc:                       # noqa: BLE001
            row["errors"].append(f"nav:{exc}")
        try:
            row["kline"] = a_kline(pref, secid, "day", history_days + 40, fqt=0)
        except Exception as exc:                       # noqa: BLE001
            row["errors"].append(f"kline:{exc}")
        try:
            row["kline_adj"] = a_kline(pref, secid, "day", 260, fqt=1)
        except Exception as exc:                       # noqa: BLE001
            row["errors"].append(f"kline_adj:{exc}")
        try:
            row["m15"] = a_kline(pref, secid, "m15", 48, fqt=0)
        except Exception as exc:                       # noqa: BLE001
            row["errors"].append(f"m15:{exc}")
        return code, row

    with ThreadPoolExecutor(max_workers=4) as pool:
        for code, row in pool.map(one, peers):
            result[code] = row
    return result
