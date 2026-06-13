# LOF Premium Tracker

全市场 LOF 基金折溢价监控 — 通过 GitHub Actions 自动运行，QQ 邮箱接收通知。

零基础设施，零部署成本，只需一个 GitHub 仓库。

## 工作流

| 时间 (UTC+8) | 工作流 | 功能 | 通知方式 |
|-------------|--------|------|---------|
| **09:15** | `limit_check` | 爬取申购限额 → 对比缓存 → 发现限购开放则提醒 | 邮件 🔓 |
| **14:30** | `daily_monitor` | 腾讯行情 → 计算溢价率 → TOP20 排名 | 邮件 📊 |

## 配置

### 1. 准备 QQ 邮箱

打开 QQ 邮箱 → **设置** → **账户** → **POP3/IMAP/SMTP 服务** → 开启 SMTP → 获取**授权码**（16 位字母）

### 2. 添加密钥

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 值 |
|--------|-----|
| `QQ_EMAIL_USER` | 你的QQ号@qq.com |
| `QQ_EMAIL_PASS` | QQ邮箱授权码 |
| `QQ_EMAIL_TO` | 接收通知的邮箱 |

### 3. 启用工作流

首次配置后，手动触发一次测试：

1. 打开仓库 **Actions** 页面
2. 点击 **Daily Monitor** → **Run workflow**
3. 查看运行日志，确认数据采集和邮件发送正常

之后每天自动按上述时间表执行。

## 数据持久化

所有数据通过 git commit 保存到仓库：

```
data/
├── daily/YYYY-MM-DD.json    # 每日溢价率快照（含 TOP20 详情）
└── limits_cache.json         # 申购限额缓存（用于对比变化）
```

历史数据完全可回溯，随时查看某一天的溢价排行。

## 手动触发

任何时候可在 GitHub Actions 页面手动运行工作流：

- **Daily Monitor** — 立即执行收盘监测并发送邮件
- **Limit Check** — 立即检查限购开放情况

## 项目结构

```
lof-monitor/
├── .github/workflows/
│   ├── daily_monitor.yml     # 14:30 收盘监测
│   └── limit_check.yml       # 09:15 限购检测
├── scripts/
│   ├── monitor_top20.py      # 腾讯行情 → 溢价率 → TOP20 邮件
│   └── check_limits_gh.py    # 申购限额爬取 → 对比 → 开放提醒
├── data/                     # 数据持久化（git commit）
├── all_lof_codes.json        # LOF 代码列表（540+ 只）
└── README.md
```

## 技术说明

- 行情来源：腾讯 QT 接口（`qt.gtimg.cn`），字段 81 直接返回基金单位净值
- 溢价率计算：(价格 - 净值) / 净值 × 100%
- 邮件发送：QQ 邮箱 SMTP-SSL（端口 465）
- 运行环境：GitHub Actions ubuntu-latest，仅需 httpx 依赖
