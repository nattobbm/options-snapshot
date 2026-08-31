#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快照档案体检 — 检查抓到的数据到底属于哪个交易日、是不是收盘态。

判据说明:
  `last_trade_time` 是标的最后一笔成交时间(美东),它才是这行数据真正描述的时段;
  `session_date` 只是抓取脚本贴的标签。两者不一致 = 数据被错贴日期,
  下游按 session_date 做的任何时间序列分析都会错位。

四类问题:
  1. 缺日       —— 交易日完全没有记录
  2. 错贴日期   —— last_trade_time 的日期 ≠ session_date
  3. 非收盘态   —— last_trade_time 早于当日 15:55 ET,抓到的是盘中快照
  4. 重复       —— 相邻记录 OHLC 完全相同

用法:  python health_check.py [--days 45]
"""

import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
UNDERLYING = os.path.join(BASE, "data", "underlying.csv")
CHAINS = os.path.join(BASE, "data", "chains")

HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
CLOSE_CUTOFF = "15:55:00"   # 美东收盘 16:00,留 5 分钟容差


def trading_days(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in HOLIDAYS_2026:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def load_underlying():
    rows = defaultdict(dict)
    with open(UNDERLYING, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows[r["ticker"]][r["session_date"]] = r
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    args = ap.parse_args()

    under = load_underlying()
    tickers = sorted(under.keys())
    first = min(min(v.keys()) for v in under.values())      # 建档日,之前的不算缺
    end = max(max(v.keys()) for v in under.values())
    start_d = max(datetime.strptime(first, "%Y-%m-%d"),
                  datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days))
    days = trading_days(start_d, datetime.strptime(end, "%Y-%m-%d"))

    print(f"档案体检 | 建档日 {first} | 回看 {days[0]} ~ {days[-1]}({len(days)} 个交易日)\n")

    for t in tickers:
        recs = under[t]
        chain_dir = os.path.join(CHAINS, t)
        have_chain = set()
        if os.path.isdir(chain_dir):
            have_chain = {n[:-7] for n in os.listdir(chain_dir) if n.endswith(".csv.gz")}

        missing = [d for d in days if d not in recs]
        mislabeled, intraday, dup = [], [], []
        prev = None
        for d in days:
            r = recs.get(d)
            if not r:
                prev = None
                continue
            lt = (r.get("last_trade_time") or "").replace("T", " ")
            if lt:
                lt_date, lt_time = lt[:10], lt[11:19]
                if lt_date != d:
                    mislabeled.append((d, lt_date))
                elif lt_time and lt_time < CLOSE_CUTOFF:
                    intraday.append((d, lt_time))
            key = (r.get("open"), r.get("high"), r.get("low"), r.get("close"))
            if prev and prev[1] == key and all(key):
                dup.append((prev[0], d))
            prev = (d, key)

        good = len(days) - len(missing) - len(mislabeled) - len(intraday)
        flag = "OK " if good == len(days) else "⚠  "
        print(f"{flag}{t}: 真收盘态 {good}/{len(days)} 天  "
              f"(缺 {len(missing)} / 错贴 {len(mislabeled)} / 盘中 {len(intraday)})"
              f"  链文件 {len([d for d in days if d in have_chain])}/{len(days)}")
        if missing:
            print(f"     缺日: {' '.join(missing)}")
        if mislabeled:
            print(f"     错贴日期(标签 → 实际成交日): " +
                  ", ".join(f"{a}→{b}" for a, b in mislabeled))
        if intraday:
            print(f"     非收盘态(标签 → 最后成交时刻): " +
                  ", ".join(f"{a}@{b}" for a, b in intraday))
        if dup:
            print(f"     OHLC 与前一记录相同: " + ", ".join(f"{a}={b}" for a, b in dup))
        print()

    print("结论口径:'真收盘态' = 有记录、日期没贴错、且最后成交时刻在 15:55 ET 之后。")
    print("只有这类记录能直接用于按日的时间序列分析;其余需要按 last_trade_time 重贴日期后才可用。")


if __name__ == "__main__":
    main()
