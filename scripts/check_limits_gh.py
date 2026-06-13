"""
GitHub Actions：开盘前限购开放检测
依赖：httpx（内置）
"""
import asyncio
import json
import os
import re
import smtplib
import ssl
import sys
from email.mime.text import MIMEText

QQ_USER = os.environ.get("QQ_EMAIL_USER", "")
QQ_PASS = os.environ.get("QQ_EMAIL_PASS", "")
QQ_TO = os.environ.get("QQ_EMAIL_TO", "")

# 缓存文件（git 持久化到 data/ 目录）
CACHE_FILE = "data/limits_cache.json"


async def fetch_limit(code: str, client) -> dict:
    """抓取单只基金的申购限额"""
    url = f"https://fundf10.eastmoney.com/jjfl_{code}.html"
    headers = {"Referer": "https://fundf10.eastmoney.com/"}
    try:
        resp = await client.get(url, headers=headers, timeout=10)
        text = resp.text

        # 申购状态
        status = "unknown"
        m = re.search(r'申购状态</td>\s*<td[^>]*>(.*?)</td>', text, re.DOTALL)
        if m:
            raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if "暂停" in raw or "停止" in raw or "封闭" in raw:
                status = "suspended"
            else:
                status = "open"

        # 限购金额
        limit = None
        m = re.search(r'日累计申购限额</td>\s*<td[^>]*>(.*?)</td>', text, re.DOTALL)
        if m:
            raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if "无限额" in raw or "无限制" in raw or raw in ("---", "--", ""):
                limit = None
            else:
                num = re.search(r'[\d.]+', raw)
                if num:
                    val = float(num.group())
                    if "亿" in raw:
                        val *= 1e8
                    elif "万" in raw:
                        val *= 1e4
                    limit = val if val < 1e9 else None  # >=10亿视为无限额

        return {"code": code, "status": status, "limit": limit, "raw_text": raw if 'raw' in dir() else ""}
    except Exception as e:
        print(f"[WARN] {code} 获取失败: {e}")
        return None


def _build_html(newly_opened: list) -> str:
    items = "".join(
        f"<tr><td style='padding:8px;font-weight:600'>{f['code']}</td>"
        f"<td style='padding:8px'>{f.get('prev','-')} → 已开放</td></tr>"
        for f in newly_opened
    )
    return f"""<div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#52c41a,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">🔓 申购额度开放 · {len(newly_opened)}只</h2></div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">
<p>以下限购基金现已开放申购，可能存在套利机会：</p>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="background:#f6ffed"><th style="padding:8px;text-align:left">代码</th><th style="padding:8px;text-align:left">状态变化</th></tr>
{items}</table></div></div>"""


async def main():
    if not all([QQ_USER, QQ_PASS, QQ_TO]):
        print("ERROR: QQ_EMAIL_USER/PASS/TO 未配置")
        sys.exit(1)

    # 加载代码列表
    codes_file = "all_lof_codes.json"
    with open(codes_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    all_codes = [d["code"].zfill(6) for d in data]

    # 只检查 QDII/小规模基金（真正的限购标的主要是 QDII）
    # 简单策略：所有代码都检查，通过 cache 对比
    import httpx

    prev_cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            prev_cache = json.load(f)

    current = {}
    newly_opened = []

    async with httpx.AsyncClient(timeout=10) as client:
        # 每次检查 50 只（避免太多请求）
        check_codes = all_codes[:50]
        for code in check_codes:
            result = await fetch_limit(code, client)
            if result:
                current[code] = {"status": result["status"], "limit": result["limit"]}
                prev = prev_cache.get(code, {})
                prev_status = prev.get("status", "")
                if prev_status and prev_status != "open" and result["status"] == "open":
                    newly_opened.append({
                        "code": code,
                        "prev": prev_status,
                        "cur": "open",
                    })

    # 保存当前缓存
    os.makedirs(os.path.dirname(CACHE_FILE) or "/tmp", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(current, f)

    if not newly_opened:
        print("未检测到限购开放变化")
        return

    html = _build_html(newly_opened)
    subject = f"【申购开放】{len(newly_opened)}只基金限购已放开"

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
    for f in newly_opened:
        print(f"  {f['code']}: {f['prev']} → 开放")


if __name__ == "__main__":
    asyncio.run(main())
