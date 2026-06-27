"""
全量重建 LOF 基金近5个交易日溢价历史缓存。

数据源：
- 收盘价：东方财富 push2his 日K接口（仅返回真实交易日，天然过滤非交易日）
- 单位净值：东方财富 pingzhongdata 的 Data_netWorthTrend 历史净值序列

输出：data/premium_history_cache.json
格式：{ "YYYY-MM-DD": { "<code>": {"premium": float, "price": float, "nav": float}, ... }, ... }

用法：
    python scripts/rebuild_history.py
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import httpx

CODES_FILE = "all_lof_codes.json"
HISTORY_CACHE = "data/premium_history_cache.json"
TARGET_DAYS = 5
CONCURRENCY = 15
TZ_CN = timezone(timedelta(hours=8))


def load_codes() -> list[dict]:
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [{"code": d["code"].zfill(6), "market": d.get("market", "SZ")} for d in data]


def _secid(code: str, market: str) -> str:
    # 东方财富 secid：SZ->0，SH->1
    return f"{'0' if market == 'SZ' else '1'}.{code}"


async def fetch_kline(client: httpx.AsyncClient, code: str, market: str) -> tuple[str, dict]:
    """获取日K数据序列 {date: (close, high, low)}"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _secid(code, market),
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "klt": 101,   # 日K
        "fqt": 0,     # 不复权（LOF 场内真实价格）
        "beg": "19900101",
        "end": "20500101",
    }
    try:
        r = await client.get(url, params=params, timeout=15)
        j = r.json()
        klines = (j.get("data") or {}).get("klines") or []
        out = {}
        for line in klines:
            parts = line.split(",")
            if len(parts) < 5:
                continue
            try:
                # f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低
                out[parts[0]] = (float(parts[2]), float(parts[3]), float(parts[4]))
            except ValueError:
                continue
        return code, out
    except Exception as e:
        print(f"[WARN] kline {code}: {e}")
        return code, {}


async def fetch_nav(client: httpx.AsyncClient, code: str) -> tuple[str, dict]:
    """获取历史单位净值序列 {date: nav}"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}
    try:
        r = await client.get(url, headers=headers, timeout=15)
        m = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", r.text, re.S)
        if not m:
            return code, {}
        arr = json.loads(m.group(1))
        out = {}
        for item in arr:
            ts = item.get("x")
            y = item.get("y")
            if ts and y is not None:
                dt = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d")
                out[dt] = y
        return code, out
    except Exception as e:
        print(f"[WARN] nav {code}: {e}")
        return code, {}


async def _run_with_sem(sem, fn, *args):
    async with sem:
        return await fn(*args)


async def main():
    codes = load_codes()
    print(f"加载基金代码: {len(codes)} 只")

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        kline_tasks = [_run_with_sem(sem, fetch_kline, client, c["code"], c["market"]) for c in codes]
        nav_tasks = [_run_with_sem(sem, fetch_nav, client, c["code"]) for c in codes]

        print("拉取日K收盘价 + 历史净值 ...")
        kline_results, nav_results = await asyncio.gather(
            asyncio.gather(*kline_tasks),
            asyncio.gather(*nav_tasks),
        )

    klines = dict(kline_results)
    navs = dict(nav_results)
    print(f"收盘价获取: {sum(1 for v in klines.values() if v)} 只成功 | "
          f"净值获取: {sum(1 for v in navs.values() if v)} 只成功")

    # 确定近 N 个交易日：从所有 K 线日期并集取最近 N 个
    all_dates = set()
    for v in klines.values():
        all_dates.update(v.keys())
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    all_dates.discard(today)  # 排除今天，避免盘中/非交易日数据
    target_dates = sorted(all_dates)[-TARGET_DAYS:]
    if len(target_dates) < TARGET_DAYS:
        print(f"[WARN] 仅找到 {len(target_dates)} 个交易日，不足 {TARGET_DAYS} 天")
    print(f"目标交易日 ({len(target_dates)}): {target_dates}")

    # 预排序净值日期，便于 ≤D 最新净值查找
    nav_sorted = {code: sorted(v.keys()) for code, v in navs.items()}
    # 预排序 K 线日期，便于停牌日用最近收盘价补齐
    kline_sorted = {code: sorted(v.keys()) for code, v in klines.items()}

    import bisect

    cache: dict = {}
    for date in target_dates:
        day_entry = {}
        for c in codes:
            code = c["code"]
            kl = klines.get(code, {})
            kdates = kline_sorted.get(code, [])

            # 取当日K线数据（停牌日补齐：沿用最近交易日的收盘价，振幅为0）
            kl_data = kl.get(date)
            is_suspended = False
            if not kl_data:
                is_suspended = True
                if kdates:
                    kidx = bisect.bisect_right(kdates, date) - 1
                    if kidx >= 0:
                        prev_date = kdates[kidx]
                        prev_close = kl[prev_date][0]
                        kl_data = (prev_close, prev_close, prev_close)  # 停牌：高=低=收盘
                if not kl_data:
                    continue

            close, high, low = kl_data
            price = close

            # 计算振幅 = (最高 - 最低) / 昨收 × 100%
            amplitude = 0.0
            if not is_suspended and kdates:
                kidx = bisect.bisect_right(kdates, date) - 1
                if kidx > 0:  # kidx 指向 date 自身，kidx-1 为前一日
                    prev_close = kl[kdates[kidx - 1]][0]
                    if prev_close and prev_close > 0:
                        amplitude = round((high - low) / prev_close * 100, 2)

            nav = None
            dates = nav_sorted.get(code, [])
            if dates:
                idx = bisect.bisect_right(dates, date) - 1
                if idx >= 0:
                    nav = navs[code][dates[idx]]
            if price and nav and nav > 0:
                premium = round((price - nav) / nav * 100, 4)
                day_entry[code] = {"premium": premium, "price": price, "nav": nav,
                                   "amplitude": amplitude}
        cache[date] = day_entry
        print(f"  {date}: {len(day_entry)} 只基金")

    os.makedirs("data", exist_ok=True)
    with open(HISTORY_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in cache.values())
    print(f"\n完成: {len(cache)} 个交易日, 共 {total} 条记录 -> {HISTORY_CACHE}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
