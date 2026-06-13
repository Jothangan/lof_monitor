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

QQ_USER = os.environ.get("QQ_EMAIL_USER", "")
QQ_PASS = os.environ.get("QQ_EMAIL_PASS", "")
QQ_TO = os.environ.get("QQ_EMAIL_TO", "")

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

            # 申购状态
            status = "open"
            m = re.search(r'申购状态</td>\s*<td[^>]*>(.*?)</td>', text, re.DOTALL)
            if m:
                raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                if "暂停" in raw or "停止" in raw or "封闭" in raw:
                    status = "暂停申购"
                elif "限制" in raw:
                    status = "限制申购"

            # 限购金额
            limit_label = ""
            m = re.search(r'日累计申购限额</td>\s*<td[^>]*>(.*?)</td>', text, re.DOTALL)
            if m:
                raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
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


def _build_html(premium: list, discount: list, limits: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _rows(items, is_premium=True):
        color = "#f5222d" if is_premium else "#1890ff"
        rows = ""
        for i, f in enumerate(items[:20], 1):
            l = limits.get(f["code"], {})
            badge = _limit_badge(l.get("status", ""), l.get("limit_label", ""))
            rows += f"""<tr style="border-bottom:1px solid #f5f5f5">
<td style="padding:6px 8px;color:#999;width:24px">{i}</td>
<td style="padding:6px 8px;font-weight:600">{f['code']}</td>
<td style="padding:6px 8px">{f['name'][:12]}</td>
<td style="padding:6px 8px;color:{color};font-weight:700;text-align:right">{f['premium_rate']:+.2f}%</td>
<td style="padding:6px 8px;text-align:right">{_format_amt(f.get('amount'))}</td>
<td style="padding:6px 8px;text-align:center">{badge}</td>
</tr>"""
        return rows

    return f"""<div style="font-family:sans-serif;max-width:680px;margin:0 auto;padding:20px">
<div style="background:linear-gradient(135deg,#722ed1,#1890ff);border-radius:12px 12px 0 0;padding:20px;text-align:center">
<h2 style="color:#fff;margin:0;font-size:18px">📊 LOF 溢价率 Top20 · 收盘报告</h2>
</div>
<div style="background:#fff;border:1px solid #f0f0f0;padding:16px">

<h3 style="margin:0 0 8px;font-size:15px;color:#f5222d">🔥 溢价 TOP20</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#fff1f0"><th style="padding:6px 8px;width:24px">#</th><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">名称</th><th style="padding:6px 8px;text-align:right">溢价率</th><th style="padding:6px 8px;text-align:right">成交额</th><th style="padding:6px 8px">限购</th></tr></thead>
<tbody>{_rows(premium)}</tbody></table>

<div style="height:20px"></div>

<h3 style="margin:0 0 8px;font-size:15px;color:#1890ff">💧 折价 TOP20</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead><tr style="background:#e6f7ff"><th style="padding:6px 8px;width:24px">#</th><th style="padding:6px 8px;text-align:left">代码</th><th style="padding:6px 8px;text-align:left">名称</th><th style="padding:6px 8px;text-align:right">溢价率</th><th style="padding:6px 8px;text-align:right">成交额</th><th style="padding:6px 8px">限购</th></tr></thead>
<tbody>{_rows(discount, False)}</tbody></table>

<div style="margin-top:16px;padding:10px;background:#fffbe6;border:1px solid #ffe58f;border-radius:6px;font-size:12px;color:#666">
红色=暂停申购 / 橙色=限制申购或有限额 / 灰色=开放申购<br>
净值来源：腾讯行情接口字段81（单位净值）。触发时间：{now}
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

    valid = [f for f in items if f["premium_rate"] is not None and f.get("amount") and f["amount"] > 0]
    valid.sort(key=lambda x: x["premium_rate"], reverse=True)
    top_premium = [f for f in valid if f["premium_rate"] > 0][:20]
    top_discount = [f for f in valid if f["premium_rate"] < 0]
    top_discount.sort(key=lambda x: x["premium_rate"])
    top_discount = top_discount[:20]

    if not top_premium and not top_discount:
        print("无有效数据，跳过")
        return

    # 获取 TOP40 的申购限额
    need_limits = set()
    for f in top_premium + top_discount:
        need_limits.add(f["code"])
    async with httpx.AsyncClient(timeout=10) as client:
        limits = await fetch_limits(client, list(need_limits))
    print(f"获取限购信息: {len(limits)} 只")

    # 持久化限购到缓存
    os.makedirs("data", exist_ok=True)
    try:
        with open("data/limits_cache.json", "r") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    cache.update(limits)
    with open("data/limits_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    html = _build_html(top_premium, top_discount, limits)
    subject = f"【LOF收盘】溢价TOP {top_premium[0]['premium_rate']:+.2f}%" if top_premium else "【LOF收盘】无溢价"

    recipients = [a.strip() for a in QQ_TO.split(",") if a.strip()]

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = QQ_USER
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
    snapshot = {
        "date": date_str,
        "total_funds": len(valid),
        "top20_premium": [
            {"code": f["code"], "name": f["name"], "premium": f["premium_rate"],
             "price": f["price"], "nav": f["nav"], "amount": f.get("amount"),
             "limit": limits.get(f["code"], {})}
            for f in top_premium
        ],
        "top20_discount": [
            {"code": f["code"], "name": f["name"], "premium": f["premium_rate"],
             "price": f["price"], "nav": f["nav"], "amount": f.get("amount"),
             "limit": limits.get(f["code"], {})}
            for f in top_discount
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
