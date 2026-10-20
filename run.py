# -*- coding: utf-8 -*-
"""把 16 只纳指基金的价格 / 净值 / 溢价率 / 相对强弱算出来，生成手机端页面数据。

用法：
    python run.py            # 抓数据 -> 更新 docs/data.js
    python run.py --offline  # 不联网，用上次的 data.json 重算页面（调试用）
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import fetchers
import model

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
CONFIG_PATH = os.path.join(ROOT, "config.json")
CST = timezone(timedelta(hours=8))


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    settings = cfg.get("settings", {})
    cfg["window"] = int(settings.get("history_days", 120))
    cfg["core_codes"] = settings.get("core_codes") or [p["code"] for p in cfg["peers"]]
    cfg["hedge_premium_trigger"] = settings.get("hedge_premium_trigger", 0.09)
    cfg["hedge_spread_trigger"] = settings.get("hedge_spread_trigger", 0.02)
    return cfg


def collect(cfg: dict) -> dict:
    peers = cfg["peers"]
    t0 = time.time()

    quotes = fetchers.tencent_quotes([p["market"] + p["code"] for p in peers])
    print(f"  行情 {len(quotes)}/{len(peers)} 只  用时 {time.time() - t0:.1f}s")

    nx = fetchers.nx_index()
    nq = fetchers.nq_futures()
    ndx_hist = fetchers.ndx_history()
    ndx_map = {r["date"]: r["close"] for r in ndx_hist}
    idx = model.build_index_state(nx, nq, ndx_hist)
    print(f"  纳指 {nx['price']:.2f} ({nx['pct'] * 100:+.2f}%)  "
          f"期货 {nq['price']:.2f} ({nq['pct'] * 100:+.2f}%)  -> 等效 {idx['equiv']:.2f}")

    bundle = fetchers.fetch_bundle(peers, cfg["window"])
    nav_ok = sum(1 for v in bundle.values() if v["nav"])
    print(f"  净值 {nav_ok}/{len(peers)} 只  K线 {sum(1 for v in bundle.values() if v['kline'])} 只  "
          f"总用时 {time.time() - t0:.1f}s")

    payload = model.build_payload(peers, bundle, idx, quotes, ndx_map, cfg)
    payload["meta"]["elapsed"] = round(time.time() - t0, 1)
    return payload


def print_table(payload: dict) -> None:
    meta = payload["meta"]
    print("\n" + "=" * 88)
    print(f"  {meta['updated_cn']}   平均溢价 {meta['avg_premium'] * 100:.2f}%   "
          f"对照基准年内 {meta['avg_ret_ytd'] * 100:+.2f}%")
    print("=" * 88)
    print(f"{'代码':<7}{'名称':<16}{'现价':>7}{'今日':>8}{'估算净值':>9}{'溢价率':>8}"
          f"{'20日均':>8}{'分位':>7}{'年内%':>8}{'相对强弱':>9}  信号")
    print("-" * 88)
    for f in payload["funds"]:
        if not f.get("ok"):
            print(f"{f['code']:<7}{f['name']:<16}  数据缺失 {f.get('error', '')}")
            continue
        print(f"{f['code']:<7}{f['name']:<14}{f['price']:>8.3f}{f['pct'] * 100:>7.2f}%"
              f"{f['est_nav']:>9.4f}{f['premium'] * 100:>7.2f}%{f['mean20'] * 100:>7.2f}%"
              f"{f['pct60'] * 100:>6.0f}%{f['ret_ytd'] * 100:>7.1f}%{f['rs_20d'] * 100:>8.2f}%  {f['signal']}")
    if meta.get("hedge"):
        h = meta["hedge"]
        names = "、".join(f"{x['name']} {x['premium'] * 100:.1f}%" for x in h["top"])
        print(f"\n  ⚠ 对冲提示：平均溢价 {h['avg_premium'] * 100:.2f}%，"
              f"高于20日均值 {h['spread'] * 100:.2f}%（阈值 2%）  最高：{names}")
    print()


def write_outputs(payload: dict) -> None:
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    js = "window.__PREMIUM_DATA__ = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";"
    with open(os.path.join(DOCS, "data.js"), "w", encoding="utf-8") as fh:
        fh.write(js)
    print(f"  已写入 docs/data.js ({len(js) / 1024:.0f} KB)")


def push_notify(payload: dict) -> None:
    """可选：把当天排名推到手机（Bark / Server酱 / 企业微信机器人）。"""
    import urllib.request

    meta = payload["meta"]
    top = [f for f in payload["funds"] if f.get("ok")][:3]
    lines = [f"{meta['updated_cn']} 平均溢价 {meta['avg_premium'] * 100:.1f}%"]
    for f in top:
        lines.append(f"{f['name']} {f['premium'] * 100:.1f}% (分位{f['pct60'] * 100:.0f}%)")
    text = "\n".join(lines)

    bark = os.environ.get("BARK_URL")
    if bark:
        url = bark.rstrip("/") + "/" + urllib.request.quote("纳指溢价", safe="") + "/" + \
            urllib.request.quote(text, safe="") + "?group=ndx"
        try:
            urllib.request.urlopen(url, timeout=10)
            print("  Bark 推送成功")
        except Exception as exc:                    # noqa: BLE001
            print(f"  Bark 推送失败 {exc!r}")

    sc = os.environ.get("SERVERCHAN_KEY")
    if sc:
        body = json.dumps({"title": "纳指溢价日报", "desp": text}).encode()
        req = urllib.request.Request(f"https://sctapi.ftqq.com/{sc}.send", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            print("  Server酱 推送成功")
        except Exception as exc:                    # noqa: BLE001
            print(f"  Server酱 推送失败 {exc!r}")


def main() -> int:
    print("纳指溢价工作台 启动 ...")
    cfg = load_config()
    payload = collect(cfg)
    print_table(payload)
    write_outputs(payload)
    push_notify(payload)

    ok = sum(1 for f in payload["funds"] if f.get("ok"))
    if ok < len(cfg["peers"]) * 0.6:
        print(f"  ✗ 只有 {ok} 只取到完整数据，检查网络或数据源是否被限制")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
