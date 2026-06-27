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
    """读取最近 N 个**交易日**的溢价数据，返回 {code: [ (date, premium), ... ]}"""
    import glob
    files = sorted(glob.glob("data/daily/*.json"))
    # 排除今天（当天数据尚未写入）和空快照
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
                if code and premium is not None:
                    history.setdefault(code, []).append((date, premium))
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

    def _trend_cell(code: str) -> str:
        """生成5日溢价趋势HTML"""
        h = (history or {}).get(code, [])
        if not h:
            return '<span style="color:#999;font-size:11px">-</span>'
        # 按日期排序
        h.sort(key=lambda x: x[0])
        parts = []
        for date, premium in h:
            color = "#52c41a" if premium > 0 else "#999"
            parts.append(f'<span style="color:{color};font-size:11px">{premium:+.1f}</span>')
        # 方向箭头
        if len(h) >= 2:
            latest = h[-1][1]
            prev = h[-2][1]
            if latest > prev:
                arrow = "↑"
                arrow_color = "#f5222d"
            elif latest < prev:
                arrow = "↓"
                arrow_color = "#52c41a"
            else:
                arrow = "→"
                arrow_color = "#999"
            # 趋势百分比变化
            change = latest - prev
            change_str = f'<span style="color:{arrow_color};font-size:11px;font-weight:600"> {arrow} {change:+.1f}</span>'
        else:
            change_str = ""
        dates_str = "/".join(d[0][5:] for d in h)  # 只取 MM-DD
        return f'<div style="line-height:1.6">{dates_str}<br>{" ".join(parts)}{change_str}</div>'

    def _rows(items):
        color = "#f5222d"
        rows = ""
        for f in items:
            l = limits.get(f["code"], {})
            badge = _limit_badge(l.get("status", ""), l.get("limit_label", ""))
            url = f"https://fund.eastmoney.com/{f['code']}.html"
            code = f["code"]
            e = est_navs.get(code, {})
            gsz = e.get("est_nav")
            dwjz = e.get("nav")
            price = f.get("price")
            est_premium = None
            if gsz and price and gsz > 0:
                est_premium = round((price - gsz) / gsz * 100, 4)
            est_nav_str = f"{gsz:.4f}" if gsz else "-"
            est_str = f"{est_premium:+.2f}%" if est_premium is not None else "-"
            dwjz_str = f"{dwjz:.4f}" if dwjz else "-"
            trend = _trend_cell(code)
            rows += f"""<tr style="border-bottom:1px solid #f5f5f5">
<td style="padding:6px 8px"><a href="{url}" target="_blank" style="color:#333;text-decoration:none;font-weight:600">{f['code']}</a><br><span style="color:#666;font-size:11px">{f['name'][:14]}</span></td>
<td style="padding:6px 8px;color:{color};font-weight:700;text-align:right">{f['premium_rate']:+.2f}%</td>
<td style="padding:6px 8px;text-align:right;color:#999;font-size:12px">{dwjz_str}</td>
<td style="padding:6px 8px;text-align:right;color:#999;font-size:12px">{est_nav_str}</td>
<td style="padding:6px 8px;color:#fa8c16;text-align:right">{est_str}</td>
<td style="padding:6px 8px;text-align:right">{_format_amt(f.get('amount'))}</td>
<td style="padding:6px 8px;text-align:center">{badge}</td>
<td style="padding:6px 8px;text-align:center;font-size:11px">{trend}</td>
</tr>"""
        return rows

    return f"""<div style="font-family:sans-serif;max-width:720px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#722ed1,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">📊 LOF 溢价率 Top40 · 收盘报告</h2>
</div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">

<h3 style="margin:0 0 8px;font-size:15px;color:#f5222d">🔥 溢价 TOP40</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#fff1f0"><th style="padding:6px 8px;text-align:left">代码/名称</th><th style="padding:6px 8px;text-align:right">溢价率</th><th style="padding:6px 8px;text-align:right">T-1净值</th><th style="padding:6px 8px;text-align:right">IPOV</th><th style="padding:6px 8px;text-align:right">IPOV溢价率</th><th style="padding:6px 8px;text-align:right">成交额</th><th style="padding:6px 8px">限购</th><th style="padding:6px 8px;text-align:center">近5日溢价</th></tr></thead>
<tbody>{_rows(premium)}</tbody></table>

<div style="margin-top:16px;padding:10px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
红色=暂停申购 / 橙色=限制申购或有限额 / 灰色=开放申购<br>
T-1净值=最新确认净值 / IPOV=天天基金盘中实时估算 / IPOV溢价率=基于IPOV的修正溢价率<br>
近5日溢价：历史每日溢价率 + 当日较前日变化趋势（↑溢价扩大 / ↓溢价收窄）。触发时间：{now}
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
    if len(traded) < 5:
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
