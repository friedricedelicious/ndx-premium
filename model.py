# -*- coding: utf-8 -*-
"""计算层：估算净值 -> 溢价率 -> 溢价分位 -> 相对强弱排名。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 基础工具
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _pctile(xs: list[float], value: float) -> float:
    if not xs:
        return float("nan")
    return sum(1 for x in xs if x <= value) / len(xs)


def _zscore(xs: list[float], value: float) -> float:
    s = _std(xs)
    return (value - _mean(xs)) / s if s > 1e-9 else 0.0


def _pct_change(a: float, b: float) -> float:
    return a / b - 1.0 if b else 0.0


def _year_start_ret(kline_adj: list[dict], year: int) -> float | None:
    """今年以来的涨幅（用前复权收盘价，剔除分红影响）。"""
    if not kline_adj:
        return None
    rows = [r for r in kline_adj if r["date"] >= f"{year}-01-01"]
    if len(rows) < 2:
        return None
    return _pct_change(rows[-1]["close"], rows[0]["close"])


def _n_day_ret(kline_adj: list[dict], n: int) -> float | None:
    if len(kline_adj) <= n:
        return None
    return _pct_change(kline_adj[-1]["close"], kline_adj[-1 - n]["close"])


# ---------------------------------------------------------------- 指数
def build_index_state(nx: dict, nq: dict, hist: list[dict]) -> dict:
    """纳指最新收盘 + 期货 -> 当前的“指数等效点位”。"""
    closes = [r for r in hist if r["close"] > 0]
    last = closes[-1] if closes else None
    last_close = last["close"] if last else nx["price"]
    last_date = last["date"] if last else ""

    # 美股盘中（行情时间在 15 分钟内）直接用现价；否则用“最后收盘 × (1+期货涨跌)”
    live = False
    try:
        qtime = datetime.strptime(nx["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        live = (datetime.now(CST) - qtime) < timedelta(minutes=20)
    except Exception:                       # noqa: BLE001
        pass

    if live:
        equiv, source = nx["price"], "美股盘中现价"
    else:
        equiv, source = last_close * (1 + nq["pct"]), "最后收盘+期货"

    return {
        "last_close": last_close,
        "last_date": last_date,
        "live": live,
        "equiv": equiv,
        "source": source,
        "ndx_pct": nx["pct"],
        "nq_pct": nq["pct"],
        "nq_price": nq["price"],
        "quote_time": nx["time"],
        "ndx_price": nx["price"],
    }


# ---------------------------------------------------------------- 单只基金
def analyse_fund(peer: dict, bundle: dict, index_state: dict, cfg: dict) -> dict:
    code = peer["code"]
    navs = bundle.get("nav") or []
    kline = bundle.get("kline") or []
    kline_adj = bundle.get("kline_adj") or []
    pos = peer.get("pos", 0.95)
    errors = list(bundle.get("errors") or [])

    nav_by_date = {r["date"]: r["nav"] for r in navs}
    close_by_date = {r["date"]: r["close"] for r in kline}
    ndx_hist = cfg["_ndx_map"]

    # ---- 净值基准日：最新一期净值，及其对应的美股收盘日（同日）
    nav_last = navs[-1] if navs else None
    if not nav_last:
        return {"code": code, "name": peer["name"], "ok": False,
                "error": "无净值数据", "notes": errors}

    base_date = nav_last["date"]
    base_close = None
    for back in range(0, 10):
        d = (datetime.strptime(base_date, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y-%m-%d")
        if d in ndx_hist:
            base_close = ndx_hist[d]
            base_ndx_date = d
            break
    if base_close is None:
        base_close, base_ndx_date = index_state["last_close"], index_state["last_date"]

    # ---- 估算净值 = 最新净值 × (1 + 仓位 × 指数自基准日以来的总变动)
    gross_move = index_state["equiv"] / base_close - 1.0
    est_nav = nav_last["nav"] * (1.0 + pos * gross_move)

    quote = cfg["_quotes"].get(code, {})
    price = quote.get("price") or (kline[-1]["close"] if kline else 0.0)
    prev_close = quote.get("prev_close") or (kline[-2]["close"] if len(kline) > 1 else price)

    premium = price / est_nav - 1.0 if est_nav else float("nan")
    premium_raw = price / nav_last["nav"] - 1.0 if nav_last["nav"] else float("nan")

    # ---- 当日 15 分钟走势（相对昨收），用于看“谁被不合理抛售”
    bars = bundle.get("m15") or []
    intraday = []
    if bars and prev_close:
        last_day = bars[-1]["date"][:10]
        intraday = [{"t": b["date"][11:16], "v": b["close"] / prev_close - 1.0}
                    for b in bars if b["date"][:10] == last_day][-16:]

    # ---- 历史溢价序列（收盘价 / 同日净值 - 1）
    hist = []
    for date in sorted(close_by_date):
        if date in nav_by_date and nav_by_date[date] > 0:
            hist.append({"d": date,
                         "premium": close_by_date[date] / nav_by_date[date] - 1.0,
                         "price": close_by_date[date],
                         "nav": nav_by_date[date]})
    hist = hist[-cfg["window"]:]
    today = datetime.now(CST).strftime("%Y-%m-%d")
    if not hist or hist[-1]["d"] != today:
        hist = hist + [{"d": today, "premium": premium, "price": price,
                        "nav": est_nav, "live": True}]

    hist_premiums = [h["premium"] for h in hist]
    mean20 = _mean(hist_premiums[-20:])
    mean60 = _mean(hist_premiums[-60:])
    z60 = _zscore(hist_premiums[-60:], premium)
    pct60 = _pctile(hist_premiums[-60:], premium)

    # ---- 收益与相对强弱
    year = datetime.now(CST).year
    ret_ytd = _year_start_ret(kline_adj, year)
    ret_20d = _n_day_ret(kline_adj, 20)
    ret_5d = _n_day_ret(kline_adj, 5)
    if ret_ytd is None:            # 上市不足一年时退回全部可得区间
        ret_ytd = _pct_change(kline_adj[-1]["close"], kline_adj[0]["close"]) if len(kline_adj) > 1 else 0.0

    return {
        "code": code,
        "name": peer["name"],
        "type": peer.get("type", "ETF"),
        "ok": True,
        "pos": pos,
        "price": price,
        "prev_close": prev_close,
        "pct": _pct_change(price, prev_close),
        "amount": quote.get("amount", 0.0),
        "turnover": quote.get("turnover"),
        "est_nav": est_nav,
        "nav": nav_last["nav"],
        "nav_date": nav_last["date"],
        "ndx_base_date": base_ndx_date,
        "premium": premium,
        "premium_raw": premium_raw,
        "mean20": mean20,
        "mean60": mean60,
        "spread20": premium - mean20,
        "z60": z60,
        "pct60": pct60,
        "ret_ytd": ret_ytd or 0.0,
        "ret_20d": ret_20d or 0.0,
        "ret_5d": ret_5d or 0.0,
        "history": hist,
        "intraday": intraday,
        "limit": peer.get("limit"),
        "note": peer.get("note", ""),
        "enabled": peer.get("enabled", True),
        "notes": errors,
    }


# ---------------------------------------------------------------- 汇总
def build_payload(peers: list[dict], bundle: dict, index_state: dict,
                  quotes: dict, ndx_map: dict, cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg["_ndx_map"] = ndx_map
    cfg["_quotes"] = quotes

    funds = [analyse_fund(p, bundle.get(p["code"], {}), index_state, cfg) for p in peers]
    ok = [f for f in funds if f.get("ok")]
    core = [f for f in ok if f["code"] in cfg["core_codes"]] or ok

    avg_ret_ytd = _mean([f["ret_ytd"] for f in core])
    avg_ret_20d = _mean([f["ret_20d"] for f in core])
    avg_ret_5d = _mean([f["ret_5d"] for f in core])
    avg_today = _mean([f["pct"] for f in core])
    avg_premium = _mean([f["premium"] for f in core])
    avg_mean20 = _mean([f["mean20"] for f in core])

    for f in funds:
        if not f.get("ok"):
            continue
        f["rs_ytd"] = f["ret_ytd"] - avg_ret_ytd
        f["rs_20d"] = f["ret_20d"] - avg_ret_20d
        f["rs_5d"] = f["ret_5d"] - avg_ret_5d
        f["rs_today"] = f["pct"] - avg_today
        f["dev"] = f["premium"] - avg_premium

    # 横向 z 值（用来把“溢价低 + 走势弱”合成一个可排序的分数）
    # 分组计算：核心 12 只互相比较，其余品种（不同标的）内部比较
    def zmap(group: list[dict], key: str) -> dict:
        vals = [f[key] for f in group]
        return {f["code"]: _zscore(vals, f[key]) for f in group}

    rest = [f for f in ok if f["code"] not in cfg["core_codes"]]
    z_prem = {**zmap(core, "premium"), **zmap(rest, "premium")}
    z_rs20 = {**zmap(core, "rs_20d"), **zmap(rest, "rs_20d")}
    z_rsytd = {**zmap(core, "rs_ytd"), **zmap(rest, "rs_ytd")}

    for f in funds:
        if not f.get("ok"):
            continue
        z1 = z_prem.get(f["code"], 0.0)
        z2 = z_rs20.get(f["code"], 0.0)
        z3 = z_rsytd.get(f["code"], 0.0)
        # 分数越高 = 越值得买入（溢价相对低、近期相对弱、年内相对弱）
        f["score"] = -0.45 * z1 - 0.35 * z2 - 0.20 * z3
        f["z_premium"] = z1
        f["core"] = f["code"] in cfg["core_codes"]
        if f["spread20"] <= -0.010 and f["rs_20d"] < 0:
            f["signal"] = "买入候选"
        elif f["spread20"] >= 0.015 and f["rs_20d"] > 0:
            f["signal"] = "偏贵/可换出"
        else:
            f["signal"] = "观察"

    # 对冲提示：整体平均溢价高，且比自身均值还有 2% 以上空间
    hedge = None
    spread_avg = avg_premium - avg_mean20
    if avg_premium >= cfg["hedge_premium_trigger"] and spread_avg >= cfg["hedge_spread_trigger"]:
        hot = sorted([f for f in core], key=lambda f: -f["premium"])[:3]
        hedge = {"avg_premium": avg_premium, "avg_mean20": avg_mean20,
                 "spread": spread_avg,
                 "top": [{"code": f["code"], "name": f["name"], "premium": f["premium"]}
                         for f in hot]}

    now = datetime.now(CST)
    funds.sort(key=lambda f: (-(f.get("score", -9)) if f.get("ok") else 9))

    return {
        "meta": {
            "updated": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_cn": now.strftime("%Y年%m月%d日 %H:%M"),
            "weekday": "一二三四五六日"[now.weekday()],
            "avg_premium": avg_premium,
            "avg_mean20": avg_mean20,
            "spread_avg": avg_premium - avg_mean20,
            "avg_ret_ytd": avg_ret_ytd,
            "hedge": hedge,
            "count": len(ok),
            "index": index_state,
        },
        "funds": funds,
    }
