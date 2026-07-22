#!/usr/bin/env python3
"""
snap.py — 每日期权链快照落盘器

只做一件事: 抓 CBOE 免费延迟链, 原样存下来。
不判别、不打分、不推送、不进框架。

用法:
    python3 snap.py                          # 读同目录 tickers.txt
    python3 snap.py --tickers SPXC,MOD,_SPX  # 直接指定
    python3 snap.py --raw                    # 同时保留原始 JSON
    python3 snap.py --force                  # 覆盖已存在的当日文件

输出:
    data/chains/{TICKER}/{SESSION_DATE}.csv.gz   一天一票一条链
    data/underlying.csv                          标的层每日一行 (append-only)
    data/raw/{TICKER}/{SESSION_DATE}.json.gz     --raw 时才写

依赖: 无。Python 3.9+ 标准库。
"""

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = os.environ.get("CBOE_BASE", "https://cdn.cboe.com/api/global/delayed_quotes/options")
UA = os.environ.get("SNAP_UA", "snap.py/1.0 (personal research; contact: you@example.com)")

# CBOE 把指数类符号加下划线前缀
INDEX_SYMBOLS = {"SPX", "SPXW", "VIX", "NDX", "RUT", "XSP", "DJX", "OEX", "XEO", "MRUT"}

OSI = re.compile(r"^(?P<root>[A-Z0-9\.\^_]+?)(?P<exp>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")

# 每份合约保留的字段。CBOE 字段名有过变动, 缺失的写空而不是崩。
OPTION_FIELDS = [
    "option", "bid", "bid_size", "ask", "ask_size", "last_trade_price",
    "volume", "open_interest", "iv", "delta", "gamma", "theta", "vega", "rho",
    "theo", "open", "high", "low", "prev_day_close", "change", "percent_change",
    "last_trade_time",
]
UNDERLYING_FIELDS = [
    "current_price", "close", "prev_day_close", "open", "high", "low",
    "volume", "iv30", "iv30_change", "last_trade_time", "seqno",
]
CSV_HEADER = ["session_date", "ticker", "expiry", "right", "strike"] + OPTION_FIELDS
UNDERLYING_HEADER = ["session_date", "ticker", "fetched_at_utc", "payload_timestamp"] + UNDERLYING_FIELDS


def normalize(ticker: str) -> str:
    t = ticker.strip().upper().lstrip("^")
    if t.startswith("_"):
        return t
    return "_" + t if t in INDEX_SYMBOLS else t


def fetch(symbol: str, retries: int = 3, timeout: int = 30) -> dict:
    url = f"{BASE}/{symbol}.json"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":          # 有时候直接给 gzip
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except Exception as e:                  # noqa: BLE001 — 网络层什么都可能抛
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{symbol}: 抓取失败 ({last})")


def unwrap(payload: dict) -> tuple[list, dict, str]:
    """CBOE 的外层包装换过一次: 有时 options 在顶层, 有时在 data 里。两种都吃。"""
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    options = node.get("options") or payload.get("options") or []
    quote = {k: node.get(k) for k in UNDERLYING_FIELDS}
    ts = payload.get("timestamp") or node.get("timestamp") or ""
    return options, quote, str(ts)


def session_date_of(ts: str) -> str:
    """用报文自带的时间戳定 session, 不用本地日期。假期自然会命中已存在的文件而跳过。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", ts or "")
    if m:
        return m.group(0)
    return datetime.now(ET).strftime("%Y-%m-%d")


def parse_osi(sym: str) -> tuple[str, str, str]:
    """AMZN260619C00250000 -> ('2026-06-19', 'C', '250.000')"""
    m = OSI.match((sym or "").replace(" ", "").upper())
    if not m:
        return "", "", ""
    e = m.group("exp")
    expiry = f"20{e[0:2]}-{e[2:4]}-{e[4:6]}"
    strike = f"{int(m.group('strike')) / 1000:.3f}"
    return expiry, m.group("right"), strike


def write_chain(path: str, ticker: str, sdate: str, options: list) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    n = 0
    with gzip.open(tmp, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for o in options:
            if not isinstance(o, dict):
                continue
            expiry, right, strike = parse_osi(o.get("option", ""))
            w.writerow([sdate, ticker, expiry, right, strike] + [o.get(k, "") for k in OPTION_FIELDS])
            n += 1
    os.replace(tmp, path)                        # 原子落盘, 中断不留半个文件
    return n


def append_underlying(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    seen = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                seen.add((r.get("session_date"), r.get("ticker")))
    if (row["session_date"], row["ticker"]) in seen:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=UNDERLYING_HEADER, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def load_tickers(args) -> list:
    if args.tickers:
        return [t for t in args.tickers.split(",") if t.strip()]
    path = args.tickers_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickers.txt")
    if not os.path.exists(path):
        sys.exit(f"没有 --tickers, 也找不到 {path}")
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="CBOE 延迟期权链每日快照")
    p.add_argument("--tickers", help="逗号分隔, 如 SPXC,MOD,_SPX")
    p.add_argument("--tickers-file", help="每行一个 ticker 的文件, 默认 ./tickers.txt")
    p.add_argument("--out", default="data", help="输出目录, 默认 ./data")
    p.add_argument("--raw", action="store_true", help="同时保留原始 JSON")
    p.add_argument("--force", action="store_true", help="覆盖当日已存在的文件")
    p.add_argument("--sleep", type=float, default=1.0, help="每个 ticker 之间的间隔秒数")
    p.add_argument("--skip-weekend", action="store_true", help="周末直接退出")
    args = p.parse_args()

    now_et = datetime.now(ET)
    if args.skip_weekend and now_et.weekday() >= 5:
        print(f"[skip] {now_et:%Y-%m-%d} 是周末")
        return 0

    tickers = load_tickers(args)
    ok, skipped, failed = [], [], []

    for raw_t in tickers:
        sym = normalize(raw_t)
        try:
            payload = fetch(sym)
            options, quote, ts = unwrap(payload)
            sdate = session_date_of(ts)
            chain_path = os.path.join(args.out, "chains", sym, f"{sdate}.csv.gz")

            if os.path.exists(chain_path) and not args.force:
                print(f"[skip] {sym} {sdate} 已存在")
                skipped.append(sym)
                continue
            if not options:
                raise RuntimeError("返回体里没有 options, 字段名可能变了")

            n = write_chain(chain_path, sym, sdate, options)

            row = {"session_date": sdate, "ticker": sym,
                   "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "payload_timestamp": ts, **quote}
            append_underlying(os.path.join(args.out, "underlying.csv"), row)

            if args.raw:
                rp = os.path.join(args.out, "raw", sym, f"{sdate}.json.gz")
                os.makedirs(os.path.dirname(rp), exist_ok=True)
                with gzip.open(rp, "wt", encoding="utf-8") as fh:
                    json.dump(payload, fh)

            size = os.path.getsize(chain_path) / 1024
            print(f"[ok]   {sym} {sdate}  {n} 份合约  {size:.0f}KB")
            ok.append(sym)
        except Exception as e:                   # noqa: BLE001
            print(f"[FAIL] {sym}: {e}", file=sys.stderr)
            failed.append(sym)
        time.sleep(args.sleep)

    print(f"\n成功 {len(ok)} / 跳过 {len(skipped)} / 失败 {len(failed)}")
    if failed:
        print("失败清单: " + ", ".join(failed), file=sys.stderr)
    # 只有全军覆没才算这次运行失败, 免得单票退市天天报警
    return 1 if (failed and not ok and not skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
