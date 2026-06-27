"""
GitHub Actions：每日收盘 Top20 监测脚本
"""
import asyncio
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

QQ_USER = os.environ.get("QQ_EMAIL_USER", "1303768055@qq.com")
QQ_PASS = os.environ.get("QQ_EMAIL_PASS", "xswhybtmqneehgii")
QQ_TO = os.environ.get("QQ_EMAIL_TO", "1303768055@qq.com")

TENCENT_BATCH = 50
CODES_FILE = "all_lof_codes.json"


def _load_codes() -> list[dict]:
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [{"code": d["code"].zfill(6), "market": d.get("market", "SZ")} for d in data]


def _clean_code(raw: str) -> str:
    import re
    m = re.search(r'\d+', raw or "")
    return m.group().zfill(6) if m else ""


async def fetch_quotes(client, codes: list[dict]) -> list[dict]:
    items = []
    for i in range(0, len(codes), TENCENT_BATCH):
        batch = codes[i:i + TENCENT_BATCH]
        qs = ",".join(f"{'sz' if f['market'] == 'SZ' else 'sh'}{f['code']}" for f in batch)
        url = f"https://qt.gtimg.cn/q={qs}"
        try:
            resp = await client.get(url, timeout=15)
            for line in resp.text.strip().split("\n"):
                if "=" not in line:
                    continue
                _, val = line.split("=", 1)
                fields = val.strip('"').split("~")
                if len(fields) < 82:
                    continue
                code = _clean_code(fields[2])
                if not code:
                    continue
                price = _safe_float(fields[3])
                nav = _safe_float(fields[81])
                high = _safe_float(fields[33])
                low = _safe_float(fields[34])
                prev_close = _safe_float(fields[4])
                amplitude = round((high - low) / prev_close * 100, 2) if high and low and prev_close and prev_close > 0 else 0.0
                premium = round((price - nav) / nav * 100, 4) if price and nav and nav > 0 else None
                total_shares = _safe_float(fields[79])
                items.append({
                    "code": code, "name": fields[1],
                    "price": price, "nav": nav, "premium_rate": premium,
                    "amplitude": amplitude,
                    "change_pct": _safe_float(fields[32]),
                    "volume": _safe_float(fields[6]),
                    "amount": (_safe_float(fields[37]) or 0) * 10000,
                    "total_shares": total_shares,
                })
        except Exception as e:
            print(f"[WARN] 批次 {i//TENCENT_BATCH} 失败: {e}")
    return items


async def fetch_est_nav(client, codes: list[str]) -> dict:
    """从 fundgz 获取实时估算净值"""
    import re
    est = {}
    for code in codes:
        try:
            resp = await client.get(f"http://fundgz.1234567.com.cn/js/{code}.js", timeout=10)
            match = re.search(r'({.*?})', resp.text)
            if match:
                d = json.loads(match.group(1))
                gsz = _safe_float(d.get("gsz"))
                gszzl = _safe_float(d.get("gszzl"))
                dwjz = _safe_float(d.get("dwjz"))
                est[code] = {"nav": dwjz, "est_nav": gsz, "est_change": gszzl}
        except Exception as e:
            print(f"[WARN] {code} 估算净值失败: {e}")
    return est


async def fetch_limits(client, codes: list[str]) -> dict:
    """批量爬取申购限额（仅限 TOP40 只基金）"""
    limits = {}
    for code in codes:
        try:
            resp = await client.get(
                f"https://fundf10.eastmoney.com/jjfl_{code}.html",
                headers={"Referer": "https://fundf10.eastmoney.com/"},
                timeout=10,
            )
            text = resp.text
            # 申购状态（使用 [^<]* 限制匹配纯文本，避免跨标签）
            status = "open"
            m = re.search(r'申购状态</td>\s*<td[^>]*>\s*([^<]+)\s*</td>', text)
            if m:
                raw = m.group(1).strip()
                if "暂停" in raw or "停止" in raw or "封闭" in raw:
                    status = "暂停申购"
                elif "限制" in raw:
                    status = "限制申购"

            # 限购金额
            limit_label = ""
            m = re.search(r'日累计申购限额</td>\s*<td[^>]*>\s*([^<]+)\s*</td>', text)
            if m:
                raw = m.group(1).strip()
                if raw in ("---", "--", ""):
                    limit_label = ""
                elif "无限额" in raw or "无限制" in raw:
                    limit_label = "无限额"
                else:
                    limit_label = raw.strip()

            limits[code] = {"status": status, "limit_label": limit_label}
        except Exception as e:
            print(f"[WARN] {code} 限额查询失败: {e}")
            limits[code] = {"status": "未知", "limit_label": ""}
        await asyncio.sleep(0.3)  # 适度延迟
    return limits


def _safe_float(val):
    if val is None:
        return None
    try:
        v = float(str(val).strip())
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    except (ValueError, TypeError):
        return None


HISTORY_CACHE = "data/premium_history_cache.json"
DELAYED_CODES_FILE = "data/delayed_nav_codes.json"
TZ_CN = timezone(timedelta(hours=8))


def _load_delayed_codes() -> set:
    """加载净值延迟基金列表"""
    if os.path.exists(DELAYED_CODES_FILE):
        try:
            with open(DELAYED_CODES_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _is_trade_day(d: datetime) -> bool:
    """基于交易日历判断是否为 A 股交易日（chinese_calendar 含法定节假日与调休安排）"""
    try:
        import chinese_calendar as cc
        return cc.is_workday(d.date())
    except (ImportError, NotImplementedError):
        # 库未安装或日期超出数据范围：退化为周末判断
        return d.weekday() < 5


def load_premium_history(days=5) -> dict:
    """从全量历史缓存读取最近 N 个**交易日**的溢价数据
    返回 {code: [ (date, premium, price, nav), ... ]}"""
    cache = {}
    if os.path.exists(HISTORY_CACHE):
        try:
            with open(HISTORY_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
    sorted_dates = sorted(cache.keys())
    # 排除今天
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    sorted_dates = [d for d in sorted_dates if d != today][-days:]

    history = {}
    for date in sorted_dates:
        day_data = cache.get(date, {})
        for code, item in day_data.items():
            premium = item.get("premium")
            price = item.get("price")
            nav = item.get("nav")
            amplitude = item.get("amplitude")
            nav_date = item.get("nav_date", date)
            if premium is not None:
                history.setdefault(code, []).append((date, premium, price, nav, amplitude, nav_date))
    print(f"历史交易日: {len(sorted_dates)} 天 ({sorted_dates})")
    return history


def _format_amt(amt):
    if not amt:
        return "-"
    if amt >= 1e8:
        return f"{amt/1e8:.2f}亿"
    if amt >= 1e4:
        return f"{amt/1e4:.0f}万"
    return f"{amt:.0f}"


def _limit_badge(status: str, label: str) -> str:
    """生成限购徽章 HTML"""
    if status == "暂停申购":
        return '<span style="background:#f5222d;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px">暂停</span>'
    if status == "限制申购" or label:
        return f'<span style="background:#fa8c16;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px">{label or "限购"}</span>'
    return '<span style="color:#999;font-size:11px">开放</span>'


def _build_html(premium: list, discount: list, est_navs: dict, limits: dict,
                history: dict = None, is_trade_day: bool = True,
                delayed_codes: set = None) -> str:
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M UTC+8")

    all_dates = set()
    if history:
        for v in history.values():
            for d, _, _, _, _, _ in v:
                all_dates.add(d)
    sorted_dates = sorted(all_dates)
    # 交易日只取最近4个历史日（第5列用今日实时）
    if is_trade_day and len(sorted_dates) > 4:
        sorted_dates = sorted_dates[-4:]
    today_label = datetime.now(TZ_CN).strftime("%m-%d")

    def _val(v, fmt=".4f"):
        return f"{v:{fmt}}" if v is not None else "-"

    NW = 'white-space:nowrap;'

    def _rows(items):
        rows = ""
        alt = False
        for f in items:
            code = f["code"]
            name = f["name"][:14]
            url = f"https://fund.eastmoney.com/{code}.html"
            bg = "#fafbfc" if alt else "#fff"

            l = limits.get(code, {})
            badge = _limit_badge(l.get("status", ""), l.get("limit_label", ""))
            shares = f.get("total_shares")
            shares_str = f"{shares:.2f}亿" if shares and shares >= 10000 else (f"{shares:.0f}万" if shares else "-")

            e = est_navs.get(code, {})
            dwjz = e.get("nav")
            gsz = e.get("est_nav")
            price = f.get("price")
            est_premium = None
            if gsz and price and gsz > 0:
                est_premium = round((price - gsz) / gsz * 100, 4)

            h = (history or {}).get(code, [])
            h_map = {d: (pr, pv, nv, amp, nd) for d, pr, pv, nv, amp, nd in h}

            # 延迟基金：净值日期 < 交易日的天数不展示数据
            is_delayed = code in (delayed_codes or set())
            latest_hist_date = sorted_dates[-1] if sorted_dates else None
            # 延迟基金最近1天历史净值滞后，趋势计算用倒数第2个有效值
            valid_dates = [d for d in sorted_dates
                           if d in h_map and (not is_delayed or h_map[d][4] >= d)]

            vals = [h_map[d] for d in valid_dates]
            if len(vals) >= 2:
                diff = vals[-1][0] - vals[-2][0]
                if diff > 0: arrow, ac = "↑", "#e74c3c"
                elif diff < 0: arrow, ac = "↓", "#27ae60"
                else: arrow, ac = "→", "#999"
                dir_chg = f'<span style="color:{ac};font-weight:600">{arrow} {diff:+.1f}</span>'
            else:
                dir_chg = '<span style="color:#999">-</span>'

            today_amp = f.get("amplitude")

            def _td(extra=""):
                return f'style="padding:2px 3px;font-size:11px;border:none;text-align:center;white-space:nowrap;{extra}"'

            def _rc(field, extractor, color_fn=None, fmt=".4f"):
                c = f'<td {_td(f"color:#7f8c8d;background:{bg}")}>{field}</td>'
                for d in sorted_dates:
                    item = h_map.get(d)
                    # 延迟基金：净值日期 < 交易日的天，显示灰色"-"
                    if is_delayed and item and item[4] < d:
                        c += f'<td {_td(f"color:#ddd;background:{bg}")}>-</td>'
                        continue
                    val = extractor(item) if item else None
                    if val is not None:
                        cl = color_fn(val) if color_fn else "#2c3e50"
                        c += f'<td {_td(f"color:{cl};background:{bg}")}><b>{_val(val, fmt)}</b></td>'
                    else:
                        c += f'<td {_td(f"color:#ddd;background:{bg}")}>-</td>'
                return c

            date_cells = f'<td {_td(f"color:#7f8c8d;background:{bg}")}><b>日期</b></td>'
            for d in sorted_dates:
                # 延迟基金最近1天标记为灰色
                item = h_map.get(d)
                if is_delayed and item and item[4] < d:
                    date_cells += f'<td {_td(f"color:#ccc;background:{bg}")}><b>{d[5:]}</b></td>'
                else:
                    date_cells += f'<td {_td(f"color:#2c3e50;background:{bg}")}><b>{d[5:]}</b></td>'

            prc_cells = _rc("收盘", lambda x: x[1], fmt=".4f")
            nav_cells = _rc("净值", lambda x: x[2], fmt=".4f")
            prem_cells = _rc("溢价", lambda x: x[0], lambda v: "#e74c3c" if v > 0 else "#7f8c8d", fmt="+.1f")
            amp_cells = _rc("振幅", lambda x: x[3], fmt=".2f")

            if is_trade_day:
                t = _td(f"color:#e67e22;background:{bg}")
                # 延迟基金今日列标注T-1
                today_hdr = f'{today_label}<br><span style="font-size:8px;color:#999">T-1</span>' if is_delayed else today_label
                date_cells += f'<td {t}><b>{today_hdr}</b></td>'
                prc_cells += f'<td {_td(f"color:#2c3e50;background:{bg}")}><b>{_val(price)}</b></td>'
                nav_cells += f'<td {_td(f"color:#2c3e50;background:{bg}")}><b>{_val(dwjz)}</b></td>'
                prem_cells += f'<td {_td(f"color:#e67e22;background:{bg}")}><b>{_val(est_premium, "+.2f")}</b></td>' if est_premium is not None else f'<td {_td(f"color:#ddd;background:{bg}")}>-</td>'
                amp_cells += f'<td {t}><b>{_val(today_amp, ".2f")}</b></td>'

            s = 'style="padding:2px 3px;font-size:11px;border:none;white-space:nowrap;'
            rows += f"""<tr style="background:{bg}">
<td {s}text-align:left;width:88px" rowspan="6" valign="middle">
<a href="{url}" target="_blank" style="color:#2980b9;font-weight:700;font-size:11px;text-decoration:none">{code}</a><br>
<span style="color:#95a5a6;font-size:9px">{name}</span>
</td>
<td {s}text-align:right;font-weight:700;color:#e74c3c">{f['premium_rate']:+.2f}%</td>
<td {s}text-align:center">{badge}</td>
<td {s}text-align:right;color:#7f8c8d">{_format_amt(f.get('amount'))}</td>
<td {s}text-align:center;color:#7f8c8d">{shares_str}</td>
<td {s}text-align:center">{dir_chg}</td>
</tr>
<tr style="background:{bg}">
{date_cells}
</tr>
<tr style="background:{bg}">
{prc_cells}
</tr>
<tr style="background:{bg}">
{nav_cells}
</tr>
<tr style="background:{bg}">
{prem_cells}
</tr>
<tr style="background:{bg};border-bottom:2px solid #ecf0f1">
{amp_cells}
</tr>"""
            alt = not alt
        return rows

    return f"""<div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:780px;margin:0 auto;padding:12px">
<div style="background:linear-gradient(135deg,#2c3e50,#3498db);border-radius:8px 8px 0 0;padding:12px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:15px;letter-spacing:1px">📊 LOF 溢价率 Top40 · 收盘报告</h2>
</div>
<div style="background:#fff;border:1px solid #e0e0e0;border-top:none;padding:10px">

<h3 style="margin:0 0 6px;font-size:12px;color:#2c3e50;border-left:3px solid #e74c3c;padding-left:8px">🔥 溢价 TOP40</h3>
<table style="width:100%;border-collapse:collapse;font-size:11px;border:none">
<thead><tr style="background:#f8f9fa;border-bottom:2px solid #dee2e6">
<th style="padding:2px 3px;text-align:left;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">代码</th>
<th style="padding:2px 3px;text-align:right;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">溢价率</th>
<th style="padding:2px 3px;text-align:center;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">限购</th>
<th style="padding:2px 3px;text-align:right;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">成交额</th>
<th style="padding:2px 3px;text-align:center;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">份额</th>
<th style="padding:2px 3px;text-align:center;font-size:10px;color:#495057;font-weight:600;white-space:nowrap">趋势</th>
</tr></thead>
<tbody>{_rows(premium)}</tbody></table>

<div style="margin-top:10px;padding:5px 8px;background:#f8f9fa;border-radius:4px;font-size:9px;color:#7f8c8d;line-height:1.5">
<span style="background:#e74c3c;color:#fff;padding:0 2px;border-radius:2px">暂停</span> 暂停&nbsp;
<span style="background:#e67e22;color:#fff;padding:0 2px;border-radius:2px">X元</span> 限制&nbsp;
<span style="color:#7f8c8d">开放</span> 开放&nbsp;|&nbsp;
↑扩大 ↓收窄&nbsp;|&nbsp;IPOV=实时估算&nbsp;|&nbsp;{now}
</div>
</div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置")
        sys.exit(1)

    codes = _load_codes()
    print(f"加载基金代码: {len(codes)} 只")

    # ── 交易日历判断（北京时间）──
    now_cn = datetime.now(TZ_CN)
    date_str = now_cn.strftime("%Y-%m-%d")
    skip_check = os.environ.get("SKIP_TRADE_CHECK", "").lower() in ("true", "1", "yes")
    calendar_trade_day = _is_trade_day(now_cn)
    print(f"当前: {now_cn.strftime('%Y-%m-%d %H:%M')} (UTC+8) | 交易日历: {'交易日' if calendar_trade_day else '非交易日'}")

    if not calendar_trade_day and not skip_check:
        print("[SKIP] 非交易日（交易日历判定），跳过执行，不更新历史缓存")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        items = await fetch_quotes(client, codes)

    print(f"获取行情: {len(items)} 条")

    # ── 成交量辅助校验（交易日历已判定为交易日或强制模式）──
    traded = [f for f in items if f.get("amount") and f["amount"] > 0]
    has_volume = len(traded) >= 5

    if skip_check:
        is_trade_day = False
        print(f"[FORCE] 强制运行模式（跳过交易日检测），成交 {len(traded)} 只")
    elif not has_volume:
        # 交易日但无成交：可能是盘中运行或数据异常
        is_trade_day = False
        print(f"[WARN] 交易日但成交异常（仅 {len(traded)} 只），不显示实时列")
    else:
        is_trade_day = True

    valid = [f for f in items if f["premium_rate"] is not None and f.get("amount") and f["amount"] > 0]
    valid.sort(key=lambda x: x["premium_rate"], reverse=True)
    top_premium = [f for f in valid if f["premium_rate"] > 0][:40]

    if not top_premium:
        print("无有效数据，跳过")
        return

    need_codes = [f["code"] for f in top_premium]
    async with httpx.AsyncClient(timeout=10) as client:
        est_navs, limits = await asyncio.gather(
            fetch_est_nav(client, need_codes),
            fetch_limits(client, need_codes),
        )
    print(f"估算净值: {len(est_navs)} 只, 限购: {len(limits)} 只")

    # 持久化限购到缓存
    os.makedirs("data", exist_ok=True)
    try:
        with open("data/limits_cache.json", "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    cache.update(limits)
    with open("data/limits_cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    # ── 加载历史溢价数据（统一取5天，交易日用4天历史+今日实时） ──
    history = load_premium_history(days=5)
    hist_dates = set(d for v in history.values() for d, *_ in v)
    print(f"加载历史: {len(hist_dates)} 天 ({sorted(hist_dates)})")

    delayed_codes = _load_delayed_codes()
    if delayed_codes:
        print(f"净值延迟基金: {len(delayed_codes)} 只")

    html = _build_html(top_premium, [], est_navs, limits, history=history,
                       is_trade_day=is_trade_day, delayed_codes=delayed_codes)
    subject = f"【LOF收盘】溢价TOP {top_premium[0]['premium_rate']:+.2f}%"

    recipients = [a.strip() for a in QQ_TO.split(",") if a.strip()]

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = f"lof_monitor <{QQ_USER}>"
    msg["To"] = ",".join(recipients)
    msg["Subject"] = subject

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=15) as s:
        s.login(QQ_USER, QQ_PASS)
        s.sendmail(QQ_USER, recipients, msg.as_string())

    print(f"邮件已发送: {subject}")
    if top_premium:
        l = limits.get(top_premium[0]["code"], {})
        print(f"溢价TOP1: {top_premium[0]['code']} {top_premium[0]['premium_rate']:+.2f}% 限购:{l.get('status','')} {l.get('limit_label','')}")

    # ── 持久化（仅交易日写入快照和历史缓存，非交易日强制运行只发邮件）──
    if not is_trade_day:
        print("[SKIP] 非交易日强制运行，不写入快照与历史缓存")
        return

    def _calc_est_premium(price, est_nav):
        if est_nav and est_nav > 0 and price:
            return round((price - est_nav) / est_nav * 100, 4)
        return None
    snapshot = {
        "date": date_str,
        "total_funds": len(valid),
        "top_premium": [
            {"code": f["code"], "name": f["name"], "premium": f["premium_rate"],
             "price": f["price"], "nav": f["nav"],
             "est_nav": est_navs.get(f["code"], {}).get("est_nav"),
             "est_premium": _calc_est_premium(f["price"], est_navs.get(f["code"], {}).get("est_nav")),
             "amount": f.get("amount"), "limit": limits.get(f["code"], {}),
             "history_5d": history.get(f["code"], [])}
            for f in top_premium
        ],
    }
    os.makedirs("data/daily", exist_ok=True)
    path = f"data/daily/{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"快照已保存: {path}")

    # ── 保存全量数据到历史缓存（所有有净值的基金，不限TOP40）──
    cache = {}
    if os.path.exists(HISTORY_CACHE):
        try:
            with open(HISTORY_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
    day_entry = {}
    for item in items:
        code = item.get("code", "")
        premium = item.get("premium_rate")
        price = item.get("price")
        nav = item.get("nav")
        amplitude = item.get("amplitude")
        if code and premium is not None:
            day_entry[code] = {"premium": premium, "price": price, "nav": nav,
                               "amplitude": amplitude, "nav_date": date_str}
    cache[date_str] = day_entry
    # 只保留最近30天
    dates = sorted(cache.keys())
    if len(dates) > 30:
        for d in dates[:-30]:
            del cache[d]
    with open(HISTORY_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"历史缓存: {len(day_entry)} 只基金")


if __name__ == "__main__":
    # httpx is imported here because it's only available in GitHub Actions
    import httpx
    asyncio.run(main())
