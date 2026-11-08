#!/usr/bin/env python3
"""
VIX 期限倒挂报警

盘后运行：取最近30个交易日的 VIX 与 VIX3M 收盘价，
当最新一个交易日 VIX > VIX3M（期限结构倒挂）时发邮件报警；
未倒挂时只写日志，不发邮件。

邮件内容：VIX、VIX3M、连续倒挂天数（今天才开始倒挂为1，昨天起倒挂则为2，依此类推；
今天未倒挂则为0，此时不发邮件）。

环境变量:
  RESEND_API_KEY  - Resend API Key (必需，存于GitHub Secrets)
"""

import datetime
import logging
import os
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vix_alarm")

# ─── 参数 ──────────────────────────────────────────────────────────────────
VIX_SYMBOL   = "^VIX"
VIX3M_SYMBOL = "^VIX3M"
WINDOW       = 30          # 统计最近多少个交易日
FETCH_PERIOD = "3mo"       # 多拉一些，保证对齐后仍有30个交易日

RESEND_API_URL = "https://api.resend.com/emails"
EMAIL_TO   = ["garyfocus@hotmail.com"]
EMAIL_FROM = "VIX报警 <messenger@ceic.ca>"


# ─── 数据 ──────────────────────────────────────────────────────────────────
def fetch_close(symbol: str) -> pd.Series:
    """拉取日线收盘价，索引统一成无时区的日期，方便两个指数按日期对齐"""
    df = yf.Ticker(symbol).history(period=FETCH_PERIOD, interval="1d")
    if df is None or df.empty:
        raise RuntimeError(f"{symbol} 数据拉取失败(空数据)")
    s = df["Close"].astype(float).dropna()
    if s.empty:
        raise RuntimeError(f"{symbol} 没有有效收盘价")
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated(keep="last")]


def load_data() -> pd.DataFrame:
    """返回最近 WINDOW 个交易日、两个指数都有数据的 DataFrame，列: VIX, VIX3M"""
    vix = fetch_close(VIX_SYMBOL).rename("VIX")
    vix3m = fetch_close(VIX3M_SYMBOL).rename("VIX3M")
    df = pd.concat([vix, vix3m], axis=1, join="inner").sort_index().tail(WINDOW)
    if df.empty:
        raise RuntimeError("VIX 与 VIX3M 没有可对齐的交易日")
    return df


# ─── 分析 ──────────────────────────────────────────────────────────────────
def analyze(df: pd.DataFrame) -> dict:
    over = df["VIX"] > df["VIX3M"]

    streak = 0
    for flag in reversed(over.tolist()):
        if flag:
            streak += 1
        else:
            break

    last = df.iloc[-1]
    return {
        "date":      df.index[-1].strftime("%Y-%m-%d"),
        "vix":       float(last["VIX"]),
        "vix3m":     float(last["VIX3M"]),
        "spread":    float(last["VIX"] - last["VIX3M"]),
        "n_days":    int(len(df)),
        "streak":    streak,                    # 截至最新一天连续倒挂的交易日数
        "streak_capped": streak >= len(df),     # 撑满整个统计窗口，实际可能更长
        "alarm":     bool(over.iloc[-1]),
    }


# ─── 邮件 ──────────────────────────────────────────────────────────────────
def build_html(info: dict) -> str:
    style = """
    <style>
        body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background:#f5f5f7; margin:0; padding:16px; }
        .container { max-width:600px; margin:0 auto; background:#fff; border-radius:10px; overflow:hidden; }
        .header { background:#c0392b; color:#fff; padding:16px; }
        .header h2 { margin:0; font-size:18px; }
        .header p { margin:4px 0 0; font-size:12px; opacity:0.85; }
        .body { padding:16px; }
        table.stats { width:100%; border-collapse:collapse; }
        table.stats td { width:33.3%; text-align:center; padding:12px 4px; border:1px solid #eee; }
        .label { font-size:12px; color:#777; }
        .num { font-size:24px; font-weight:700; margin-top:4px; }
        .red { color:#c0392b; }
        .note { font-size:13px; color:#555; margin-top:14px; line-height:1.7; }
        .footer { padding:12px 16px; font-size:11px; color:#999; border-top:1px solid #eee; }
    </style>
    """
    streak_text = f"{info['streak']}+" if info["streak_capped"] else str(info["streak"])
    return f"""
    <html>
    <head><meta charset="utf-8">{style}</head>
    <body>
        <div class="container">
            <div class="header">
                <h2>VIX 期限结构倒挂报警</h2>
                <p>数据日期 {info['date']}　|　VIX &gt; VIX3M</p>
            </div>
            <div class="body">
                <table class="stats">
                    <tr>
                        <td><div class="label">VIX</div><div class="num red">{info['vix']:.2f}</div></td>
                        <td><div class="label">VIX3M</div><div class="num">{info['vix3m']:.2f}</div></td>
                        <td><div class="label">连续倒挂天数</div><div class="num red">{streak_text}<span style="font-size:13px;color:#777"> 天</span></div></td>
                    </tr>
                </table>
                <div class="note">
                    VIX 高出 VIX3M：{info['spread']:+.2f}
                </div>
            </div>
            <div class="footer">
                倒挂天数 = 截至 {info['date']} 连续 VIX &gt; VIX3M 的交易日数（当天未倒挂则为 0）　统计窗口：最近 {info['n_days']} 个交易日
            </div>
        </div>
    </body>
    </html>
    """


def send_email(html: str, info: dict) -> bool:
    resend_api_key = os.environ.get("RESEND_API_KEY")
    if not resend_api_key:
        logger.error("缺少必需的环境变量 RESEND_API_KEY，无法发送邮件")
        return False

    payload = {
        "from": EMAIL_FROM,
        "to": EMAIL_TO,
        "subject": f"VIX倒挂报警 {info['date']}　VIX {info['vix']:.2f} > VIX3M {info['vix3m']:.2f}",
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {resend_api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            logger.info("报警邮件发送成功")
            return True
        logger.error(f"邮件发送失败: HTTP {resp.status_code} - {resp.text}")
        return False
    except Exception as e:  # noqa: BLE001
        logger.error(f"邮件发送异常: {e}")
        return False


# ─── 主流程 ────────────────────────────────────────────────────────────────
def main():
    try:
        df = load_data()
    except Exception as e:  # noqa: BLE001
        # 报警程序取不到数据时必须让任务失败，否则会"静默地不报警"
        logger.error(f"数据获取失败: {e}")
        sys.exit(1)

    info = analyze(df)
    logger.info(
        f"{info['date']}  VIX={info['vix']:.2f}  VIX3M={info['vix3m']:.2f}  "
        f"连续倒挂 {info['streak']} 天"
    )

    today = datetime.datetime.now(ZoneInfo("America/Vancouver")).date()
    data_date = datetime.date.fromisoformat(info["date"])
    if (today - data_date).days > 4:
        logger.warning(f"最新数据日期 {info['date']} 距今超过4天，数据可能没有更新")

    if not info["alarm"]:
        logger.info("VIX <= VIX3M，未倒挂，不发送邮件")
        return

    ok = send_email(build_html(info), info)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
