#!/usr/bin/env python3
r"""
intraday.py — SPX 盘中 GEX/DEX 快照采集器（GitHub Actions 版）

移植自本地 `BOT\量化学习\data\gex_intraday\collect.py`。
搬到 Actions 的原因：本地版依赖电脑开机，2026-08-21/24/28 三个交易日全天丢帧
（WakeToRun 只能唤醒睡眠、唤不醒关机）。Actions 不依赖任何本地设备。

与本地版的关系：**两者并存，互补**。
  本地版  每 5 分钟，粒度细，但依赖开机
  Actions 每 15 分钟（且有调度延迟），粒度粗，但保底不断
合并时按 snap_et 去重即可（格式完全一致）。

设计原则（与本地版相同，见 collect.py 文档）：
  1. 原始优先 —— 存瘦身后的完整链，衍生指标事后可重算
  2. 时间诚实 —— 同时记 抓取墙钟 与 链内最大 last_trade_time
  3. 质量校验 —— greeks 全零 / GEX 爆炸 / 墙位脱轨 一律隔离，不进时序表

输出：
  data/intraday/chains/YYYY-MM-DD/HHMM.csv.gz   瘦身链
  data/intraday/gex_timeseries.csv              每帧一行汇总
  data/intraday/rejected.log                    被隔离的坏帧记录
"""
import csv, gzip, json, os, re, sys, math, urllib.request
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                      # runner 缺 tzdata 时的回退
    ET = timezone(timedelta(hours=-4))

BASE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "intraday")
CHAIN_D  = os.path.join(BASE, "chains")
TS_CSV   = os.path.join(BASE, "gex_timeseries.csv")
REJ_LOG  = os.path.join(BASE, "rejected.log")
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"
UA       = os.environ.get("SNAP_UA", "intraday.py/1.0 (personal research)")

STRIKE_W = 0.15      # 保留 |K/S-1| < 15%
MAX_EXP  = 8         # 保留最近 8 个到期
FIELDS   = ["expiry", "right", "strike", "bid", "ask", "volume",
            "open_interest", "iv", "delta", "gamma", "theta", "vega",
            "last_trade_price", "last_trade_time"]
OPT_RE   = re.compile(r"^SPXW?(\d{6})([CP])(\d{8})$")


def in_session(now_et):
    """ET 交易时段判断。
    截止 16:05：SPX 期权 16:00 停止交易，之后 0DTE 剩余时间 T→0，
    BSM gamma ∝ 1/√T 数学爆炸（本地版 2026-08-13 16:20 帧算出 +197.68B）。"""
    if now_et.weekday() >= 5:
        return False, "周末"
    hm = now_et.hour * 60 + now_et.minute
    if hm < 9 * 60 + 25:
        return False, f"未开盘 ({now_et:%H:%M} ET)"
    if hm > 16 * 60 + 5:
        return False, f"已收盘 ({now_et:%H:%M} ET)"
    return True, ""


def fetch_chain():
    req = urllib.request.Request(CBOE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(raw, today):
    spot = raw["data"]["current_price"]
    rows = []
    for o in raw["data"]["options"]:
        m = OPT_RE.match(o["option"])
        if not m:
            continue
        exp = datetime.strptime(m.group(1), "%y%m%d").date()
        k   = int(m.group(3)) / 1000.0
        oi  = o.get("open_interest") or 0
        if exp < today or oi <= 0 or abs(k / spot - 1) > STRIKE_W:
            continue
        rows.append({"expiry": exp.isoformat(), "right": m.group(2), "strike": k,
                     "bid": o.get("bid"), "ask": o.get("ask"),
                     "volume": o.get("volume"), "open_interest": oi,
                     "iv": o.get("iv"), "delta": o.get("delta"),
                     "gamma": o.get("gamma"), "theta": o.get("theta"),
                     "vega": o.get("vega"),
                     "last_trade_price": o.get("last_trade_price"),
                     "last_trade_time": o.get("last_trade_time")})
    exps = sorted({r["expiry"] for r in rows})[:MAX_EXP]
    return spot, [r for r in rows if r["expiry"] in exps], exps


def agg(rows, spot, exps):
    """符号约定：call 记正、put 记负（做市商对手方假设）"""
    def block(sub):
        if not sub:
            return {}
        cg = pg = cd = pd_ = 0.0
        cwall, pwall = {}, {}
        for r in sub:
            g = (r["gamma"] or 0) * r["open_interest"] * 100 * spot * spot * 0.01
            d = (r["delta"] or 0) * r["open_interest"] * 100 * spot * 0.01
            if r["right"] == "C":
                cg += g; cd += d
                cwall[r["strike"]] = cwall.get(r["strike"], 0) + g
            else:
                pg -= g; pd_ += d
                pwall[r["strike"]] = pwall.get(r["strike"], 0) - g
        return {"call_gex": cg, "put_gex": pg, "net_gex": cg + pg,
                "call_dex": cd, "put_dex": pd_, "net_dex": cd + pd_,
                "call_wall": max(cwall, key=cwall.get) if cwall else None,
                "put_wall":  min(pwall, key=pwall.get) if pwall else None}
    exp0 = exps[0] if exps else None
    return block([r for r in rows if r["expiry"] == exp0]), block(rows), exp0


def validate(rows, spot):
    """链层校验。CBOE 偶发 greeks 全零快照（本地版记录 08-07/08-10/08-18 各一次）"""
    n = len(rows)
    if n < 500:
        return False, f"行数过少 {n}"
    if sum(1 for r in rows if (r["gamma"] or 0) != 0) < n * 0.5:
        return False, f"gamma非零行 <50% ({n}行) — CBOE greeks 未计算"
    if sum(1 for r in rows if (r["iv"] or 0) > 0) < n * 0.5:
        return False, f"iv非零行 <50% ({n}行)"
    if not (spot and spot > 0):
        return False, f"spot 异常 {spot}"
    return True, ""


def validate_agg(a0, spot):
    """汇总层校验：拦 gamma 数值爆炸（T→0）与墙位脱轨"""
    g = a0.get("net_gex", 0) / 1e9
    if abs(g) > 100:
        return False, f"净GEX {g:+.1f}B 超范围(|G|>100B) — 疑似 T→0 爆炸"
    for k in ("call_wall", "put_wall"):
        w = a0.get(k)
        if w and abs(w / spot - 1) > 0.10:
            return False, f"{k} {w:.0f} 偏离现价 {spot:.0f} 超 10%"
    return True, ""


def write_chain(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def reject(now_et, today, rows, spot, why):
    p = os.path.join(CHAIN_D, "_rejected", today.isoformat(), f"{now_et:%H%M}.csv.gz")
    write_chain(p, rows)
    os.makedirs(BASE, exist_ok=True)
    with open(REJ_LOG, "a", encoding="utf-8") as f:
        f.write(f"{now_et:%Y-%m-%d %H:%M}\t{why}\tspot={spot}\t-> {os.path.relpath(p, BASE)}\n")
    print(f"⚠ 坏帧已隔离 {now_et:%H:%M ET}: {why}")


def main():
    now_et = datetime.now(ET)
    force  = "--force" in sys.argv
    ok, why = in_session(now_et)
    if not ok and not force:
        print(f"跳过: {why}")
        return 0

    raw = fetch_chain()
    today = now_et.date()
    spot, rows, exps = parse(raw, today)
    if not rows:
        print("链为空，放弃")
        return 0

    ok, why = validate(rows, spot)
    if not ok:
        reject(now_et, today, rows, spot, why)
        return 0

    a0, aall, exp0 = agg(rows, spot, exps)
    ok2, why2 = validate_agg(a0, spot)
    if not ok2:
        reject(now_et, today, rows, spot, why2)
        return 0

    path = os.path.join(CHAIN_D, today.isoformat(), f"{now_et:%H%M}.csv.gz")
    write_chain(path, rows)

    os.makedirs(BASE, exist_ok=True)
    new = not os.path.exists(TS_CSV)
    lt = max((r["last_trade_time"] or "") for r in rows)
    with open(TS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["snap_et", "data_last_trade", "spot", "exp0",
                        "d0_net_gex", "d0_call_gex", "d0_put_gex", "d0_net_dex",
                        "d0_call_wall", "d0_put_wall",
                        "all_net_gex", "all_call_gex", "all_put_gex", "all_net_dex",
                        "n_rows", "chain_file"])
        w.writerow([now_et.strftime("%Y-%m-%d %H:%M"), lt, f"{spot:.2f}", exp0,
                    f"{a0.get('net_gex',0)/1e9:.3f}", f"{a0.get('call_gex',0)/1e9:.3f}",
                    f"{a0.get('put_gex',0)/1e9:.3f}", f"{a0.get('net_dex',0)/1e9:.3f}",
                    a0.get("call_wall"), a0.get("put_wall"),
                    f"{aall.get('net_gex',0)/1e9:.3f}", f"{aall.get('call_gex',0)/1e9:.3f}",
                    f"{aall.get('put_gex',0)/1e9:.3f}", f"{aall.get('net_dex',0)/1e9:.3f}",
                    len(rows), os.path.relpath(path, BASE).replace("\\", "/")])
    print(f"存档 {now_et:%H:%M ET} spot {spot:.2f} | 0DTE netGEX "
          f"{a0.get('net_gex',0)/1e9:+.2f}B netDEX {a0.get('net_dex',0)/1e9:+.2f}B | "
          f"{len(rows)}行 {os.path.getsize(path)/1024:.0f}KB")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
