"""
GitHub Actions：QDII 限购监控（集思录数据源）
"""
import asyncio
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

QQ_USER = os.environ.get("QQ_EMAIL_USER", "")
QQ_PASS = os.environ.get("QQ_EMAIL_PASS", "")
QQ_TO = os.environ.get("QQ_EMAIL_TO", "")
CACHE_FILE = "data/limits_cache.json"

JISILU_URL = "https://www.jisilu.cn/data/qdii/qdii_list/"
JISILU_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.jisilu.cn/data/qdii/",
    "X-Requested-With": "XMLHttpRequest",
}


def _safe_float(val):
    if val is None:
        return None
    try:
        v = float(str(val).strip().replace("%", ""))
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    except (ValueError, TypeError):
        return None


async def fetch_qdii() -> list[dict]:
    """从集思录获取全部 QDII 基金数据"""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(JISILU_URL, headers=JISILU_HEADERS)
        data = resp.json()
    results = []
    for row in data.get("rows", []):
        c = row.get("cell", {})
        code = c.get("fund_id", "").zfill(6)
        if not code:
            continue
        price = _safe_float(c.get("price"))
        nav = _safe_float(c.get("fund_nav"))
        nav_discount = _safe_float(c.get("nav_discount_rt"))
        # discount_rt 是溢价率（正值=溢价，负值=折价）
        premium = nav_discount if nav_discount is not None else None
        results.append({
            "code": code,
            "name": c.get("fund_nm", ""),
            "price": price,
            "nav": nav,
            "premium_rate": round(premium, 2) if premium is not None else None,
            "apply_status": c.get("apply_status", "未知"),
            "apply_fee": c.get("apply_fee", ""),
            "redeem_status": c.get("redeem_status", "未知"),
            "min_amt": _safe_float(c.get("min_amt")),
            "increase_rt": c.get("increase_rt", ""),
            "amount": _safe_float(c.get("amount")),
        })
    return results


def _build_html(funds: list[dict], opened: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 限购基金表格
    limited = [f for f in funds if "暂停" in f.get("apply_status", "")]
    rows = ""
    for f in limited[:50]:
        premium = f.get("premium_rate")
        premium_str = f"{premium:+.2f}%" if premium is not None else "-"
        min_amt = f.get("min_amt")
        min_str = f"{min_amt:.0f}元" if min_amt else "-"
        rows += f"""<tr style="border-bottom:1px solid #f5f5f5">
<td style="padding:6px 8px;font-weight:600">{f['code']}</td>
<td style="padding:6px 8px">{f['name'][:16]}</td>
<td style="padding:6px 8px">{premium_str}</td>
<td style="padding:6px 8px;color:#f5222d">{f['apply_status']}</td>
<td style="padding:6px 8px">{min_str}</td>
<td style="padding:6px 8px">{f.get('increase_rt','')}</td>
</tr>"""

    # 开放提醒
    opening = ""
    if opened:
        opening = f"""<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;padding:12px;margin-bottom:16px">
<h3 style="margin:0 0 8px;font-size:15px;color:#52c41a">🔓 {len(opened)}只基金限购已开放</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="background:#f6ffed"><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">变化</th></tr>
{"".join(f'<tr><td style="padding:6px 8px;font-weight:600">{f["code"]}</td><td style="padding:6px 8px;color:#52c41a">{f.get("prev","")} → 已开放</td></tr>' for f in opened)}
</table></div>"""

    return f"""<div style="font-family:sans-serif;max-width:720px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#722ed1,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">🔒 QDII 申购状态监控 · {len(limited)}只暂停</h2></div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">
{opening}
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#fff1f0"><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">名称</th><th style="padding:6px 8px;text-align:right">溢价率</th><th style="padding:6px 8px">申购状态</th><th style="padding:6px 8px;text-align:right">最低申购</th><th style="padding:6px 8px;text-align:right">涨跌幅</th></tr></thead>
<tbody>{rows}</tbody></table>
<div style="margin-top:12px;padding:8px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
数据来源：集思录 jisilu.cn。仅展示暂停申购的基金。<br>触发时间：{now}
</div></div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置")
        sys.exit(1)

    # 获取 QDII 数据
    funds = await fetch_qdii()
    print(f"获取 QDII 基金: {len(funds)} 只")

    if not funds:
        print("无数据，跳过")
        return

    # 加载缓存
    prev_cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            prev_cache = json.load(f)

    # 构建当前状态
    current = {}
    for f in funds:
        current[f["code"]] = {
            "apply_status": f["apply_status"],
            "premium": f["premium_rate"],
            "min_amt": f["min_amt"],
        }

    # 检测限购开放
    opened = []
    for code, cur in current.items():
        prev = prev_cache.get(code, {})
        prev_status = prev.get("apply_status", "")
        if prev_status and "暂停" in prev_status and "暂停" not in (cur.get("apply_status") or ""):
            opened.append({"code": code, "prev": prev_status})

    # 保存缓存
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)

    if opened:
        print(f"检测到限购开放: {len(opened)} 只")
        for f in opened:
            print(f"  {f['code']}: {f['prev']} → 已开放")

    html = _build_html(funds, opened)
    limited_count = sum(1 for f in funds if "暂停" in f.get("apply_status", ""))
    subject = f"【QDII限购】{limited_count}只暂停 溢价TOP {funds[0]['premium_rate']:+.2f}%" if limited_count and funds[0].get("premium_rate") else "【QDII限购】无暂停基金"

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


if __name__ == "__main__":
    asyncio.run(main())
