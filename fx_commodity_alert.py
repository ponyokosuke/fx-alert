#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
為替・コモディティ 急変動アラートシステム（クラウド版 / GitHub Actions）
========================================================================

GitHub Actionsの定期実行（cron）から呼び出されることを前提にした、
「1回起動 → 1回チェック → 終了」のスクリプトです。

  - 日中: LINEに通知（1通）
  - 夜間: LINEを連続送信（バースト送信）して気づきやすくする
  - 「今日はオフ」等の一時停止設定（state/control.json、GitHub上で直接編集可）
  - 直近の通知時刻（クールダウン管理）は state/state.json に保存し、
    GitHub Actionsの各実行間で引き継がれます

■ シークレット（環境変数）
  LINE_CHANNEL_ACCESS_TOKEN         必須
  TWILIO_ACCOUNT_SID/AUTH_TOKEN 等  電話機能を使う場合のみ（デフォルトOFF）

■ ローカルでのテスト実行
  export LINE_CHANNEL_ACCESS_TOKEN="xxxx"
  python fx_commodity_alert.py --test-line
  python fx_commodity_alert.py --once

■ 一時停止・夜間設定の変更（ローカルCLIから。GitHub上ならUIからも可）
  python fx_commodity_alert.py --off-today
  python fx_commodity_alert.py --off-until 2026-09-20
  python fx_commodity_alert.py --on
  python fx_commodity_alert.py --set-night-count 8
  python fx_commodity_alert.py --set-night-interval 10
  python fx_commodity_alert.py --status
"""

import sys
import os
import json
import logging
import argparse
from datetime import datetime, date
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ============================================================
# CONFIG
# ============================================================
CONFIG = {
    # ---------------- 監視銘柄 ----------------
    "targets": {
        "USD/JPY": "JPY=X",
        "EUR/USD": "EURUSD=X",
        "金 Gold": "GC=F",
        "銀 Silver": "SI=F",
        "プラチナ Platinum": "PL=F",
        "原油 WTI Crude Oil": "CL=F",
    },

    # ---------------- しきい値 ----------------
    "default_threshold_pct": 1.0,
    "threshold_overrides_pct": {
        # "USD/JPY": 0.5,
        # "金 Gold": 1.5,
    },
    "lookback_minutes": 60,

    # ---------------- 日中／夜間の時間帯（JST） ----------------
    "day_start_hour": 7,
    "day_end_hour": 21,

    # ---------------- 夜間LINEバースト送信のデフォルト値 ----------------
    # （state/control.json に上書きがあればそちらが優先されます）
    "night_line_burst_count": 5,
    "night_line_burst_interval_sec": 15,

    # ---------------- 再通知クールダウン ----------------
    "line_cooldown_minutes": 60,
    "call_cooldown_minutes": 90,

    # ---------------- LINE設定 ----------------
    # 環境変数 LINE_CHANNEL_ACCESS_TOKEN（GitHub Secrets経由）を優先。
    # ローカルで直接テストしたいだけの場合に限り、下の文字列を書き換えてもOKですが、
    # その場合このファイルは絶対にGitHubへpushしないでください。
    "line_channel_access_token": os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),

    # ---------------- 電話（Twilio）設定 ----------------
    "call_enabled": False,
    "twilio_account_sid": os.environ.get("TWILIO_ACCOUNT_SID", ""),
    "twilio_auth_token": os.environ.get("TWILIO_AUTH_TOKEN", ""),
    "twilio_from_number": os.environ.get("TWILIO_FROM_NUMBER", ""),
    "twilio_to_number": os.environ.get("TWILIO_TO_NUMBER", ""),
    "call_message_repeat": 3,

    # ---------------- 状態ファイル（リポジトリにコミットされて引き継がれる） ----------------
    "control_file": "state/control.json",   # 一時停止・夜間設定の上書き
    "state_file": "state/state.json",       # 直近の通知時刻（クールダウン管理）

    "log_file": None,  # GitHub Actionsのログにそのまま出すのでファイルは作らない
}
# ============================================================


JST = ZoneInfo("Asia/Tokyo")


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("fx_alert")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ------------------------------------------------------------
# JSONファイル共通ユーティリティ
# ------------------------------------------------------------
def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------
# 一時停止・夜間設定（state/control.json）
# ------------------------------------------------------------
def load_control(cfg: dict) -> dict:
    return _load_json(cfg["control_file"])


def update_control(cfg: dict, updates: dict):
    control = load_control(cfg)
    control.update(updates)
    _save_json(cfg["control_file"], control)


def set_off_today(cfg: dict):
    today = datetime.now(JST).date().isoformat()
    update_control(cfg, {"off_until": today})


def set_off_until(cfg: dict, until_str: str):
    date.fromisoformat(until_str)
    update_control(cfg, {"off_until": until_str})


def clear_pause(cfg: dict):
    control = load_control(cfg)
    control.pop("off_until", None)
    _save_json(cfg["control_file"], control)


def get_night_settings(cfg: dict):
    control = load_control(cfg)
    count = control.get("night_burst_count", cfg["night_line_burst_count"])
    interval = control.get("night_burst_interval_sec", cfg["night_line_burst_interval_sec"])
    return count, interval


def set_night_settings(cfg: dict, count: int = None, interval: int = None):
    updates = {}
    if count is not None:
        if count < 1:
            raise ValueError("通数は1以上を指定してください")
        updates["night_burst_count"] = count
    if interval is not None:
        if interval < 0:
            raise ValueError("間隔は0以上を指定してください")
        updates["night_burst_interval_sec"] = interval
    if updates:
        update_control(cfg, updates)


def is_paused_today(cfg: dict) -> bool:
    control = load_control(cfg)
    off_until = control.get("off_until")
    if not off_until:
        return False
    try:
        off_until_date = date.fromisoformat(off_until)
    except ValueError:
        return False
    return datetime.now(JST).date() <= off_until_date


def print_status(cfg: dict):
    control = load_control(cfg)
    off_until = control.get("off_until")
    now = datetime.now(JST)
    print(f"現在時刻(JST): {now.strftime('%Y-%m-%d %H:%M:%S')}")
    if off_until:
        try:
            d = date.fromisoformat(off_until)
            print(f"状態: 監視オフ中（{off_until} まで）" if now.date() <= d else "状態: 監視ON（一時停止は期限切れ）")
        except ValueError:
            print("状態: 監視ON（control.jsonの内容が不正です）")
    else:
        print("状態: 監視ON")
    mode = "日中モード（LINE通知1通）" if is_daytime(cfg) else "夜間モード（LINEバースト送信）"
    print(f"現在の通知モード: {mode}")
    count, interval = get_night_settings(cfg)
    print(f"夜間LINEバースト設定: {count}通 / {interval}秒間隔")


# ------------------------------------------------------------
# 直近の通知時刻（state/state.json）＝クールダウン管理
# ------------------------------------------------------------
def load_state(cfg: dict):
    raw = _load_json(cfg["state_file"])
    last_line = {k: datetime.fromisoformat(v) for k, v in raw.get("last_line_alert", {}).items()}
    last_call = {k: datetime.fromisoformat(v) for k, v in raw.get("last_call_alert", {}).items()}
    return last_line, last_call


def save_state(cfg: dict, last_line: dict, last_call: dict):
    raw = {
        "last_line_alert": {k: v.isoformat() for k, v in last_line.items()},
        "last_call_alert": {k: v.isoformat() for k, v in last_call.items()},
    }
    _save_json(cfg["state_file"], raw)


# ------------------------------------------------------------
# 時間帯判定
# ------------------------------------------------------------
def is_daytime(cfg: dict, now: datetime = None) -> bool:
    now = now or datetime.now(JST)
    start, end = cfg["day_start_hour"], cfg["day_end_hour"]
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


# ------------------------------------------------------------
# LINE通知
# ------------------------------------------------------------
def send_line_broadcast(token: str, text: str) -> bool:
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    body = {"messages": [{"type": "text", "text": text[:4900]}]}
    try:
        res = requests.post(url, headers=headers, json=body, timeout=10)
        if res.status_code == 200:
            return True
        logging.getLogger("fx_alert").error(f"LINE送信失敗: status={res.status_code} body={res.text}")
        return False
    except requests.RequestException as e:
        logging.getLogger("fx_alert").error(f"LINE送信エラー: {e}")
        return False


# ------------------------------------------------------------
# 電話発信（Twilio、デフォルトOFF）
# ------------------------------------------------------------
def build_call_twiml(message: str, repeat: int) -> str:
    say = f'<Say language="ja-JP">{xml_escape(message)}</Say><Pause length="1"/>'
    return f"<Response>{say * max(1, repeat)}</Response>"


def make_phone_call(cfg: dict, message: str) -> bool:
    sid = cfg["twilio_account_sid"]
    token = cfg["twilio_auth_token"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    data = {
        "To": cfg["twilio_to_number"],
        "From": cfg["twilio_from_number"],
        "Twiml": build_call_twiml(message, cfg["call_message_repeat"]),
    }
    try:
        res = requests.post(url, data=data, auth=(sid, token), timeout=10)
        if res.status_code in (200, 201):
            return True
        logging.getLogger("fx_alert").error(f"電話発信失敗: status={res.status_code} body={res.text}")
        return False
    except requests.RequestException as e:
        logging.getLogger("fx_alert").error(f"電話発信エラー: {e}")
        return False


# ------------------------------------------------------------
# 価格取得
# ------------------------------------------------------------
def fetch_prices(tickers: list):
    return yf.download(
        tickers=tickers, period="1d", interval="5m",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )


def get_close_series(data, ticker: str, n_tickers: int):
    try:
        series = data["Close"] if n_tickers == 1 else data[ticker]["Close"]
        return series.dropna()
    except Exception:
        return None


# ------------------------------------------------------------
# メインのチェック処理（1回分）
# ------------------------------------------------------------
def check_and_alert(cfg: dict, logger: logging.Logger, last_line_alert: dict, last_call_alert: dict):
    targets = cfg["targets"]
    tickers = list(targets.values())
    n = len(tickers)

    try:
        data = fetch_prices(tickers)
    except Exception as e:
        logger.error(f"価格取得に失敗しました: {e}")
        return

    lookback = cfg["lookback_minutes"]
    lookback_bars = max(1, lookback // 5)
    daytime = is_daytime(cfg)

    for name, ticker in targets.items():
        series = get_close_series(data, ticker, n)
        if series is None or len(series) < lookback_bars + 1:
            logger.info(f"[{name}] データ不足のためスキップ（市場クローズの可能性）")
            continue

        latest_price = float(series.iloc[-1])
        past_price = float(series.iloc[-(lookback_bars + 1)])
        if past_price == 0:
            continue

        change_pct = (latest_price - past_price) / past_price * 100
        threshold = cfg["threshold_overrides_pct"].get(name, cfg["default_threshold_pct"])

        logger.info(
            f"[{name}] 現在値={latest_price:.4f} ({lookback}分前比 {change_pct:+.2f}% / "
            f"しきい値 ±{threshold}%) [{'日中' if daytime else '夜間'}]"
        )

        if abs(change_pct) < threshold:
            continue

        now = datetime.now(JST)
        direction = "急騰" if change_pct > 0 else "急落"
        text_msg = (
            f"⚠ {name} {direction}\n現在値: {latest_price:.4f}\n"
            f"{lookback}分前比: {change_pct:+.2f}%\n時刻: {now.strftime('%Y-%m-%d %H:%M JST')}"
        )
        call_msg = f"{name}が{direction}しています。{lookback}分前比、{abs(change_pct):.1f}パーセントの変動です。"

        if daytime:
            last = last_line_alert.get(name)
            cooldown = cfg["line_cooldown_minutes"]
            if last and (now - last).total_seconds() < cooldown * 60:
                logger.info(f"[{name}] クールダウン中のためLINE通知をスキップ")
                continue
            if send_line_broadcast(cfg["line_channel_access_token"], text_msg):
                logger.info(f"[{name}] LINE通知を送信しました")
                last_line_alert[name] = now
        else:
            last_l = last_line_alert.get(name)
            line_cooldown = cfg["line_cooldown_minutes"]
            if last_l and (now - last_l).total_seconds() < line_cooldown * 60:
                logger.info(f"[{name}] クールダウン中のため夜間LINEバーストをスキップ")
            else:
                count, interval = get_night_settings(cfg)
                sent = 0
                # GitHub Actions上は1回の実行が短時間で終わる必要があるため、
                # 連続送信の間隔は入れず、番号付きで一気に送信する
                for i in range(count):
                    burst_msg = f"🚨【夜間アラート {i+1}/{count}】\n{text_msg}"
                    if send_line_broadcast(cfg["line_channel_access_token"], burst_msg):
                        sent += 1
                logger.info(f"[{name}] 夜間LINEバースト送信: {sent}/{count}通 送信完了")
                last_line_alert[name] = now

            if cfg["call_enabled"]:
                last_c = last_call_alert.get(name)
                call_cooldown = cfg["call_cooldown_minutes"]
                if not last_c or (now - last_c).total_seconds() >= call_cooldown * 60:
                    if make_phone_call(cfg, call_msg):
                        logger.info(f"[{name}] 電話発信しました")
                        last_call_alert[name] = now
                else:
                    logger.info(f"[{name}] 電話クールダウン中のためスキップ")


def run_test_line(cfg: dict, logger: logging.Logger):
    ok = send_line_broadcast(cfg["line_channel_access_token"], "✅ テスト通知：為替・コモディティ監視システム（クラウド版）の接続確認です。")
    logger.info("LINEテスト送信: " + ("成功" if ok else "失敗。トークン・友だち追加状況を確認してください。"))


def run_test_call(cfg: dict, logger: logging.Logger):
    ok = make_phone_call(cfg, "これは、為替・コモディティ監視システムのテスト発信です。設定は正常です。")
    logger.info("電話テスト発信: " + ("成功" if ok else "失敗。Twilioの設定を確認してください。"))


def run_daily_report(cfg: dict, logger: logging.Logger):
    """しきい値に関係なく、全銘柄の現在値を1通のLINEにまとめて送る生存確認レポート"""
    targets = cfg["targets"]
    tickers = list(targets.values())
    n = len(tickers)
    now = datetime.now(JST)

    try:
        data = fetch_prices(tickers)
    except Exception as e:
        logger.error(f"価格取得に失敗しました: {e}")
        send_line_broadcast(
            cfg["line_channel_access_token"],
            f"⚠ 定期確認（{now.strftime('%Y-%m-%d %H:%M')} JST）\n"
            f"価格取得に失敗しました。システムに問題がある可能性があります。",
        )
        return

    lookback = cfg["lookback_minutes"]
    lookback_bars = max(1, lookback // 5)

    lines = [f"📊 定期確認（{now.strftime('%Y-%m-%d(%a) %H:%M')} JST）", "システムは正常に稼働しています。", ""]
    for name, ticker in targets.items():
        series = get_close_series(data, ticker, n)
        if series is None or len(series) < 1:
            lines.append(f"・{name}: データ取得不可（市場クローズの可能性）")
            continue
        latest_price = float(series.iloc[-1])
        if len(series) >= lookback_bars + 1:
            past_price = float(series.iloc[-(lookback_bars + 1)])
            change_pct = (latest_price - past_price) / past_price * 100 if past_price else 0.0
            lines.append(f"・{name}: {latest_price:.4f}（{lookback}分前比 {change_pct:+.2f}%）")
        else:
            lines.append(f"・{name}: {latest_price:.4f}")

    text = "\n".join(lines)
    ok = send_line_broadcast(cfg["line_channel_access_token"], text)
    logger.info("定期確認レポート送信: " + ("成功" if ok else "失敗"))


def main():
    parser = argparse.ArgumentParser(description="為替・コモディティ急変動アラート（クラウド版）")
    parser.add_argument("--test-line", action="store_true")
    parser.add_argument("--test-call", action="store_true")
    parser.add_argument("--daily-report", action="store_true", help="しきい値に関係なく全銘柄の現在値をLINEに送る（生存確認用）")
    parser.add_argument("--once", action="store_true", help="互換性のためのフラグ（クラウド版は常に1回だけ実行）")
    parser.add_argument("--off-today", action="store_true")
    parser.add_argument("--off-until", metavar="YYYY-MM-DD")
    parser.add_argument("--on", action="store_true")
    parser.add_argument("--set-night-count", type=int, metavar="N")
    parser.add_argument("--set-night-interval", type=int, metavar="SEC")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    cfg = CONFIG
    logger = setup_logger()

    if args.status:
        print_status(cfg)
        return
    if args.off_today:
        set_off_today(cfg)
        print("本日いっぱい監視をオフにしました。")
        return
    if args.off_until:
        set_off_until(cfg, args.off_until)
        print(f"{args.off_until} まで監視をオフにしました。")
        return
    if args.on:
        clear_pause(cfg)
        print("監視をONに戻しました。")
        return
    if args.set_night_count is not None or args.set_night_interval is not None:
        try:
            set_night_settings(cfg, count=args.set_night_count, interval=args.set_night_interval)
        except ValueError as e:
            print(f"エラー: {e}")
            sys.exit(1)
        count, interval = get_night_settings(cfg)
        print(f"夜間LINEバースト送信の設定を更新しました: {count}通 / {interval}秒間隔")
        return

    if not cfg["line_channel_access_token"]:
        logger.error(
            "LINEトークンが未設定です。環境変数 LINE_CHANNEL_ACCESS_TOKEN "
            "（GitHub Actionsの場合はSecrets経由）を設定してください。"
        )
        sys.exit(1)

    if args.test_line:
        run_test_line(cfg, logger)
        return
    if args.test_call:
        if not cfg["twilio_account_sid"]:
            logger.error("Twilioの設定が未完了です。")
            sys.exit(1)
        run_test_call(cfg, logger)
        return
    if args.daily_report:
        run_daily_report(cfg, logger)
        return

    # ---- ここから通常の1回チェック ----
    if is_paused_today(cfg):
        logger.info("本日は監視オフ設定のためスキップします")
        return

    last_line_alert, last_call_alert = load_state(cfg)
    try:
        check_and_alert(cfg, logger, last_line_alert, last_call_alert)
    finally:
        save_state(cfg, last_line_alert, last_call_alert)


if __name__ == "__main__":
    main()
