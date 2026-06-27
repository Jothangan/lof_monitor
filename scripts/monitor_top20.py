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
from datetime import datetime, timezone
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
                premium = round((price - nav) / nav * 100, 4) if price and nav and nav > 0 else None
                items.append({
                    "code": code, "name": fields[1],
                    "price": price, "nav": nav, "premium_rate": premium,
                    "change_pct": _safe_float(fields[32]),
                    "volume": _safe_float(fields[6]),
                    "amount": (_safe_float(fields[37]) or 0) * 10000,
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


def load_premium_history(days=5) -> dict:
    """读取最近 N 个**交易日**的溢价数据，返回 {code: [ (date, premium, price, nav), ... ]}"""
    import glob
    files = sorted(glob.glob("data/daily/*.json"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trade_files = []
    for fp in files:
        fname = os.path.basename(fp).replace(".json", "")
        if fname == today:
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("top_premium") and len(data["top_premium"]) > 0:
                trade_files.append(fp)
        except Exception:
            pass
    trade_files = trade_files[-days:]

    history = {}
    for fp in trade_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            date = data.get("date", os.path.basename(fp).replace(".json", ""))
            for item in data.get("top_premium", []):
                code = item.get("code", "")
                premium = item.get("premium")
                price = item.get("price")
                nav = item.get("nav")
                if code and premium is not None:
                    history.setdefault(code, []).append((date, premium, price, nav))
        except Exception as e:
            print(f"[WARN] 读取历史 {fp} 失败: {e}")
    print(f"历史交易日: {len(trade_files)} 天 ({[os.path.basename(f)[:10] for f in trade_files]})")
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
                history: dict = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    all_dates = set()
    if history:
        for v in history.values():
            for d, _, _, _ in v:
                all_dates.add(d)
    sorted_dates = sorted(all_dates)
    today_label = datetime.now(timezone.utc).strftime("%m-%d")

    def _val(v, fmt=".4f"):
        return f"{v:{fmt}}" if v is not None else "-"

    def _rows(items):
        rows = ""
        for f in items:
            code = f["code"]
            name = f["name"][:14]
            url = f"https://fund.eastmoney.com/{code}.html"

            l = limits.get(code, {})
            badge = _limit_badge(l.get("status", ""), l.get("limit_label", ""))

            e = est_navs.get(code, {})
            dwjz = e.get("nav")
            gsz = e.get("est_nav")
            price = f.get("price")
            est_premium = None
            if gsz and price and gsz > 0:
                est_premium = round((price - gsz) / gsz * 100, 4)

            h = (history or {}).get(code, [])
            h_map = {d: (pr, pv, nv) for d, pr, pv, nv in h}  # {date: (premium, price, nav)}

            # 历史趋势
            vals = [h_map[d] for d in sorted_dates if d in h_map]
            if len(vals) >= 2:
                diff = vals[-1][0] - vals[-2][0]
                if diff > 0: arrow, ac = "↑", "#f5222d"
                elif diff < 0: arrow, ac = "↓", "#52c41a"
                else: arrow, ac = "→", "#999"
                dir_chg = f'<span style="color:{ac};font-weight:600">{arrow} {diff:+.1f}</span>'
            else:
                dir_chg = '<span style="color:#999">-</span>'

            # 构建7列: 标签 | d1 | d2 | d3 | d4 | d5 | 今日
            def _row_cells(field, extractor, color_fn=None, fmt=".4f"):
                cells = f'<td style="padding:2px 6px;font-size:10px;color:#888;text-align:center"><b>{field}</b></td>'
                for d in sorted_dates:
                    item = h_map.get(d)
                    val = extractor(item) if item else None
                    if val is not None:
                        color = color_fn(val) if color_fn else "#666"
                        cells += f'<td style="padding:2px 6px;font-size:11px;color:{color};text-align:center"><b>{_val(val, fmt)}</b></td>'
                    else:
                        cells += '<td style="padding:2px 6px;font-size:11px;color:#ddd;text-align:center">-</td>'
                return cells

            # 日期行特殊处理：直接用sorted_dates的日期，不从h_map取
            date_row = '<td style="padding:2px 6px;font-size:10px;color:#888;text-align:center"><b>日期</b></td>'
            for d in sorted_dates:
                date_row += f'<td style="padding:2px 6px;font-size:11px;color:#999;text-align:center"><b>{d[5:]}</b></td>'

            price_row = _row_cells("收盘价", lambda x: x[2], lambda v: "#666")
            nav_row = _row_cells("净值", lambda x: x[1], lambda v: "#666")
            premium_row_cells = _row_cells("溢价率", lambda x: x[0], lambda v: "#52c41a" if v > 0 else "#999", fmt="+.1f")

            date_row += f'<td style="padding:2px 6px;font-size:11px;color:#999;text-align:center"><b>{today_label}(IPOV)</b></td>'
            price_row += f'<td style="padding:2px 6px;font-size:11px;color:#666;text-align:center"><b>{_val(price)}</b></td>'
            nav_row += f'<td style="padding:2px 6px;font-size:11px;color:#666;text-align:center"><b>{_val(dwjz)}</b></td>'
            premium_row_cells += f'<td style="padding:2px 6px;font-size:11px;color:#fa8c16;text-align:center"><b>{_val(est_premium, "+.2f")}</b></td>' if est_premium is not None else '<td style="padding:2px 6px;font-size:11px;color:#ddd;text-align:center">-</td>'

            rows += f"""<tr style="background:#fff;border-bottom:1px solid #eee">
<td style="padding:4px 6px" rowspan="6" valign="middle">
<a href="{url}" target="_blank" style="color:#333;font-weight:600;font-size:12px;text-decoration:none">{code}</a>
</td>
<td style="padding:4px 6px;text-align:right;font-weight:700;color:#f5222d;font-size:12px">{f['premium_rate']:+.2f}%</td>
<td style="padding:4px 6px;text-align:center;font-size:11px">{badge}</td>
<td style="padding:4px 6px;text-align:right;font-size:11px;color:#666">{_format_amt(f.get('amount'))}</td>
<td style="padding:4px 6px;text-align:right;font-size:11px">{dir_chg}</td>
</tr>
<tr style="background:#fff;border-bottom:1px solid #eee">
<td style="padding:2px 6px;font-size:11px;color:#999" colspan="4">{name}</td>
</tr>
<tr style="background:#fafafa">
{date_row}
</tr>
<tr style="background:#fafafa">
{price_row}
</tr>
<tr style="background:#fafafa">
{nav_row}
</tr>
<tr style="background:#fafafa;border-bottom:3px solid #e8e8e8">
{premium_row_cells}
</tr>"""
        return rows

    return f"""<div style="font-family:sans-serif;max-width:720px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#722ed1,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">📊 LOF 溢价率 Top40 · 收盘报告</h2>
</div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">

<h3 style="margin:0 0 8px;font-size:15px;color:#f5222d">🔥 溢价 TOP40</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#fff1f0">
<th style="padding:4px 6px;text-align:left">代码</th>
<th style="padding:4px 6px;text-align:right">溢价率</th>
<th style="padding:4px 6px;text-align:center">限购</th>
<th style="padding:4px 6px;text-align:right">成交额</th>
<th style="padding:4px 6px;text-align:center">趋势</th>
</tr></thead>
<tbody>{_rows(premium)}</tbody></table>

<div style="margin-top:16px;padding:10px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
红色=暂停申购 / 橙色=限制申购或有限额 / 灰色=开放申购<br>
趋势列↑溢价扩大↓溢价收窄。触发时间：{now}
</div>
</div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置")
        sys.exit(1)

    codes = _load_codes()
    print(f"加载基金代码: {len(codes)} 只")

    async with httpx.AsyncClient(timeout=15) as client:
        items = await fetch_quotes(client, codes)

    print(f"获取行情: {len(items)} 条")

    # ── 非交易日检测：有交易量的基金数极少说明当天休市 ──
    traded = [f for f in items if f.get("amount") and f["amount"] > 0]
    skip_check = os.environ.get("SKIP_TRADE_CHECK", "").lower() in ("true", "1", "yes")
    if len(traded) < 5:
        if skip_check:
            print(f"[FORCE] 非交易日检测已跳过 ({len(traded)} 只成交)")
        else:
            print(f"[SKIP] 非交易日或数据异常：仅有 {len(traded)} 只基金有成交，跳过")
            return

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

    # ── 加载近5日溢价历史 ──
    history = load_premium_history(days=5)
    print(f"加载历史天数: {len(set(d for v in history.values() for d, _ in v))} 天")

    html = _build_html(top_premium, [], est_navs, limits, history=history)
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

    # ── 持久化快照 ──
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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


if __name__ == "__main__":
    # httpx is imported here because it's only available in GitHub Actions
    import httpx
    asyncio.run(main())
