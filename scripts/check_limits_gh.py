"""
GitHub Actions：QDII 限购监控
数据源优先：集思录 API → 缓存 → fundf10 逐只抓取（降级）
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

QQ_USER = os.environ.get("QQ_EMAIL_USER", "")
QQ_PASS = os.environ.get("QQ_EMAIL_PASS", "")
QQ_TO = os.environ.get("QQ_EMAIL_TO", "")
LIMITS_CACHE = "data/limits_cache.json"
JISILU_CACHE = "data/jisilu_qdii_cache.json"

JISILU_URL = "https://www.jisilu.cn/data/qdii/qdii_list/"
JISILU_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.jisilu.cn/data/qdii/",
    "X-Requested-With": "XMLHttpRequest",
}


def _safe_float(val):
    if val is None: return None
    try:
        v = float(str(val).strip().replace("%", ""))
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    except: return None


async def fetch_from_jisilu() -> list[dict] | None:
    """从集思录获取 QDII 数据，失败返回 None"""
    import httpx
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.jisilu.cn/data/qdii/",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = await httpx.get(JISILU_URL, headers=headers, timeout=15)
        data = resp.json()
        funds = []
        for row in data.get("rows", []):
            c = row.get("cell", {})
            code = c.get("fund_id", "").zfill(6)
            if not code: continue
            p = _safe_float(c.get("price"))
            n = _safe_float(c.get("fund_nav"))
            pr = _safe_float(c.get("nav_discount_rt"))
            funds.append({
                "code": code, "name": c.get("fund_nm", ""),
                "price": p, "nav": n, "premium_rate": round(pr, 2) if pr is not None else None,
                "apply_status": c.get("apply_status", "未知"),
                "apply_fee": c.get("apply_fee", ""),
                "redeem_status": c.get("redeem_status", "未知"),
                "min_amt": _safe_float(c.get("min_amt")),
                "increase_rt": c.get("increase_rt", ""),
                "amount": _safe_float(c.get("amount")),
            })
        if funds:
            # 缓存基金列表供降级使用
            os.makedirs("data", exist_ok=True)
            with open(JISILU_CACHE, "w", encoding="utf-8") as f:
                json.dump([{"code": f["code"], "name": f["name"]} for f in funds], f, ensure_ascii=False)
            return funds
    except Exception as e:
        print(f"[WARN] 集思录接口不可用: {e}")
    return None


async def fetch_from_cache_fallback() -> list[dict]:
    """降级：从缓存读取 QDII 列表，逐只抓 fundf10"""
    qdii_list = []
    if os.path.exists(JISILU_CACHE):
        with open(JISILU_CACHE, "r", encoding="utf-8") as f:
            qdii_list = json.load(f)
    else:
        print("[WARN] 无 QDII 缓存，回退到 all_lof_codes.json")
        with open("all_lof_codes.json", "r", encoding="utf-8") as f:
            for d in json.load(f):
                qdii_list.append({"code": d["code"].zfill(6), "name": d.get("name", "")})
        qdii_list = qdii_list[:80]  # 最多查80只

    import httpx
    funds = []
    async with httpx.AsyncClient(timeout=10) as client:
        for item in qdii_list[:80]:
            code = item["code"]
            try:
                resp = await client.get(
                    f"https://fundf10.eastmoney.com/jjfl_{code}.html",
                    headers={"Referer": "https://fundf10.eastmoney.com/"},
                    timeout=10,
                )
                text = resp.text
                status = "开放申购"
                m = re.search(r'申购状态</td>\s*<td[^>]*>\s*([^<]+)\s*</td>', text)
                if m:
                    r = m.group(1).strip()
                    if "暂停" in r or "停止" in r or "封闭" in r:
                        status = "暂停申购"
                    elif "限制" in r:
                        status = "限制申购"

                min_amt = None
                m = re.search(r'日累计申购限额</td>\s*<td[^>]*>\s*([^<]+)\s*</td>', text)
                if m:
                    raw = m.group(1).strip()
                    if raw not in ("---", "--", "") and "无限额" not in raw and "无限制" not in raw:
                        num = re.search(r'[\d.]+', raw)
                        if num:
                            val = float(num.group())
                            if "亿" in raw: val *= 1e8
                            elif "万" in raw: val *= 1e4
                            min_amt = val if val < 1e9 else None

                funds.append({
                    "code": code, "name": item.get("name", ""),
                    "price": None, "nav": None, "premium_rate": None,
                    "apply_status": status, "min_amt": min_amt,
                    "increase_rt": "", "amount": None,
                })
            except Exception as e:
                print(f"[WARN] {code} 抓取失败: {e}")
            await asyncio.sleep(0.3)
    return funds


def _build_html(funds: list[dict], opened: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    limited = [f for f in funds if "暂停" in f.get("apply_status", "")]
    limited.sort(key=lambda x: -(x.get("premium_rate") or 0))

    rows = ""
    for f in limited[:80]:
        pr = f.get("premium_rate")
        pr_str = f"{pr:+.2f}%" if pr is not None else "-"
        ma = f.get("min_amt")
        ma_str = f"{ma:.0f}元" if ma else "-"
        rows += f"""<tr style="border-bottom:1px solid #f5f5f5">
<td style="padding:6px 8px;font-weight:600">{f['code']}</td>
<td style="padding:6px 8px">{f['name'][:16]}</td>
<td style="padding:6px 8px;text-align:right">{pr_str}</td>
<td style="padding:6px 8px;color:#f5222d">{f['apply_status']}</td>
<td style="padding:6px 8px;text-align:right">{ma_str}</td>
</tr>"""

    opened_html = ""
    if opened:
        opened_html = f"""<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;padding:12px;margin-bottom:16px">
<h3 style="margin:0 0 8px;font-size:15px;color:#52c41a">🔓 {len(opened)}只基金限购已开放</h3><table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="background:#f6ffed"><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">变化</th></tr>
{"".join(f'<tr><td style="padding:6px 8px;font-weight:600">{f["code"]}</td><td style="padding:6px 8px;color:#52c41a">{f.get("prev","")} → 开放</td></tr>' for f in opened)}
</table></div>"""

    return f"""<div style="font-family:sans-serif;max-width:720px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#722ed1,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">🔒 QDII 申购状态 · {len(limited)}只暂停</h2></div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">
{opened_html}<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#fff1f0"><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">名称</th><th style="padding:6px 8px;text-align:right">溢价率</th><th style="padding:6px 8px">申购状态</th><th style="padding:6px 8px;text-align:right">最低申购</th></tr></thead>
<tbody>{rows}</tbody></table>
<div style="margin-top:12px;padding:8px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
数据来源：集思录 jisilu.cn（不可用时降级为 fundf10 逐只爬取）<br>触发时间：{now}
</div></div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置"); sys.exit(1)

    # 优先集思录，失败降级
    funds = await fetch_from_jisilu()
    source = "jisilu"
    if funds is None:
        print("集思录不可用，降级为 fundf10 逐只抓取")
        funds = await fetch_from_cache_fallback()
        source = "fundf10"

    print(f"获取 QDII 基金: {len(funds)} 只 (来源: {source})")
    if not funds:
        print("无数据，跳过"); return

    # 加载缓存 + 检测变化
    prev = {}
    if os.path.exists(LIMITS_CACHE):
        with open(LIMITS_CACHE, "r", encoding="utf-8") as f:
            prev = json.load(f)

    cur = {f["code"]: {"apply_status": f["apply_status"], "premium": f["premium_rate"], "min_amt": f["min_amt"]} for f in funds}
    opened = []
    for code, c in cur.items():
        p = prev.get(code, {})
        ps = p.get("apply_status", "")
        if ps and "暂停" in ps and "暂停" not in (c.get("apply_status") or ""):
            opened.append({"code": code, "prev": ps})

    os.makedirs("data", exist_ok=True)
    with open(LIMITS_CACHE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)

    html = _build_html(funds, opened)
    limited = sum(1 for f in funds if "暂停" in f.get("apply_status", ""))
    top = next((f for f in funds if f.get("premium_rate") is not None), None)
    top_pr = f"{top['premium_rate']:+.2f}%" if top and top.get("premium_rate") else ""
    subject = f"【QDII限购】{limited}只暂停 {top_pr}" if limited else "【QDII限购】无暂停基金"

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
