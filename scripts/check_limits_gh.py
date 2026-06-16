"""
GitHub Actions：QDII 申购限购监控
从 data/qdii_codes.json 读取基金列表，逐只爬取 fundf10 获取申购状态
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


async def fetch_qdii_limits() -> list[dict]:
    """从 data/qdii_codes.json 读取 QDII 列表，逐只爬取 fundf10 获取申购状态"""
    qdii_file = "data/qdii_codes.json"
    if not os.path.exists(qdii_file):
        print(f"[ERROR] {qdii_file} 不存在")
        return []

    with open(qdii_file, "r", encoding="utf-8") as f:
        qdii_list = json.load(f)

    import httpx
    funds = []
    async with httpx.AsyncClient(timeout=10) as client:
        for item in qdii_list:
            code = item["code"].zfill(6)
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

                limit_label = ""
                m = re.search(r'日累计申购限额</td>\s*<td[^>]*>\s*([^<]+)\s*</td>', text)
                if m:
                    raw = m.group(1).strip()
                    limit_label = raw if raw not in ("---", "--", "") and "无限额" not in raw and "无限制" not in raw else ""

                funds.append({
                    "code": code, "name": item.get("name", ""),
                    "apply_status": status, "limit_label": limit_label,
                })
            except Exception as e:
                print(f"[WARN] {code} 抓取失败: {e}")
            await asyncio.sleep(0.3)
    return funds


def _build_html(funds: list[dict], opened: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    limited = [f for f in funds if "暂停" in f.get("apply_status", "")]

    rows = ""
    for f in limited:
        ll = f.get("limit_label", "")
        rows += f"""<tr style="border-bottom:1px solid #f5f5f5">
<td style="padding:6px 8px;font-weight:600">{f['code']}</td>
<td style="padding:6px 8px">{f['name'][:16]}</td>
<td style="padding:6px 8px;color:#f5222d">{f['apply_status']}</td>
<td style="padding:6px 8px">{ll}</td>
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
<thead><tr style="background:#fff1f0"><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">名称</th><th style="padding:6px 8px">申购状态</th><th style="padding:6px 8px">限额</th></tr></thead>
<tbody>{rows}</tbody></table>
<div style="margin-top:12px;padding:8px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
数据来源：fundf10.eastmoney.com<br>触发时间：{now}
</div></div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置"); sys.exit(1)

    funds = await fetch_qdii_limits()
    print(f"获取 QDII 基金: {len(funds)} 只")
    if not funds:
        print("无数据，跳过"); return

    # 加载缓存 + 检测变化
    prev = {}
    if os.path.exists(LIMITS_CACHE):
        with open(LIMITS_CACHE, "r", encoding="utf-8") as f:
            prev = json.load(f)

    cur = {f["code"]: {"apply_status": f["apply_status"]} for f in funds}
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
    subject = f"【QDII限购】{limited}只暂停" if limited else "【QDII限购】无暂停基金"

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
