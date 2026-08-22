#!/usr/bin/env python3
"""
台灣周邊空域緊急代碼 (Squawk 7500/7600/7700) 監控腳本
------------------------------------------------------
資料來源：OpenSky Network REST API (https://opensky-network.org)
通知方式：Telegram Bot API 推播

執行模式：單次執行（poll 一次就結束），設計給排程器（cron / GitHub Actions）
          每隔幾分鐘呼叫一次，本地端不需要開著機器常駐執行。
          new/resolved 事件的判斷是靠讀寫同目錄下的 state.json 來記憶上次狀態，
          所以只要 state.json 在每次執行之間有被保留（本地檔案，或是像
          GitHub Actions 那樣執行完 commit 回 repo），跨次執行就能正確比對。

使用方式：
    1. 設定環境變數：
       TELEGRAM_BOT_TOKEN          跟 @BotFather 申請的 Bot Token
       TELEGRAM_CHAT_ID            要推播的目標 chat id（個人或群組）
       OPENSKY_CLIENT_ID           (可選) OpenSky 帳號 OAuth2 client id，可提高查詢額度
       OPENSKY_CLIENT_SECRET       (可選) OpenSky 帳號 OAuth2 client secret
    2. python3 taiwan_squawk_monitor.py          # 跑一次
       python3 taiwan_squawk_monitor.py --loop   # 本機測試用：每 POLL_INTERVAL_SEC 跑一次，不結束

    輸出的 state.json 搭配 dashboard.html 一起用一個簡單的
    web server 開起來看（例如同資料夾下執行 `python3 -m http.server`）。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------- 設定區 ----------

# 台灣周邊空域範圍（概略涵蓋本島、澎湖、金門、周邊海域及進場空域，可自行調整）
BBOX = {
    "lamin": 20.5,
    "lamax": 26.5,
    "lomin": 117.5,
    "lomax": 123.5,
}

POLL_INTERVAL_SEC = 300           # --loop 模式下的輪詢間隔（5 分鐘）。
                                   # 若用 GitHub Actions 排程，實際間隔由 workflow 的 cron 決定，
                                   # 這個常數對排程模式沒有作用，只是保留給本機常駐測試用。
EMERGENCY_CODES = {"7500", "7600", "7700", "2612"}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET")

SQUAWK_MEANING = {
    "7500": "劫機 Hijack",
    "7600": "通訊/無線電失效 Radio Failure",
    "7700": "一般緊急 General Emergency",
}


# ---------- OpenSky ----------

def get_opensky_token():
    """若有設定 OAuth2 client credentials，換取 access token 以提高查詢額度"""
    if not (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET):
        return None
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": OPENSKY_CLIENT_ID,
        "client_secret": OPENSKY_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(OPENSKY_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["access_token"]
    except Exception as e:
        print(f"[警告] 無法取得 OpenSky token，改用匿名查詢：{e}")
        return None


def fetch_states(token=None):
    params = urllib.parse.urlencode(BBOX)
    url = f"{OPENSKY_URL}?{params}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def parse_aircraft(raw):
    """把 OpenSky state vector 陣列轉成好讀的 dict 清單"""
    aircraft = []
    for s in raw.get("states") or []:
        aircraft.append({
            "icao24": s[0],
            "callsign": (s[1] or "").strip(),
            "origin_country": s[2],
            "longitude": s[5],
            "latitude": s[6],
            "altitude_m": s[7],
            "on_ground": s[8],
            "velocity": s[9],
            "heading": s[10],
            "squawk": s[14],
        })
    return aircraft


# ---------- Telegram 推播 ----------

def send_telegram_push(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[提醒] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，略過推播")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print("[Telegram] 已推播")
    except urllib.error.HTTPError as e:
        print(f"[錯誤] Telegram 推播失敗：{e.code} {e.read()}")


# ---------- 狀態存取 ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"active_alerts": {}, "history": [], "all_aircraft": [], "last_update": None, "bbox": BBOX}


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)  # 原子寫入，避免 dashboard 讀到寫一半的檔案


# ---------- 單次輪詢 ----------

def poll_once(token, state):
    """跑一次查詢 + 比對 + 推播 + 存檔。回傳更新後的 state。"""
    raw = fetch_states(token)
    aircraft = parse_aircraft(raw)
    now_iso = datetime.now(timezone.utc).isoformat()

    current_emergency = {
        a["icao24"]: a for a in aircraft if a["squawk"] in EMERGENCY_CODES
    }

    # 新出現的緊急事件 -> 推播通知
    for icao24, a in current_emergency.items():
        if icao24 not in state["active_alerts"]:
            msg = (
                "⚠️ 台灣空域緊急代碼偵測\n"
                f"班機: {a['callsign'] or '未知'}\n"
                f"代碼: {a['squawk']} ({SQUAWK_MEANING.get(a['squawk'], '未知')})\n"
                f"位置: {a['latitude']:.3f}, {a['longitude']:.3f}\n"
                f"高度: {a['altitude_m']} m\n"
                f"國籍: {a['origin_country']}\n"
                f"時間 (UTC): {now_iso}"
            )
            print(msg)
            send_telegram_push(msg)
            state["history"].append({**a, "detected_at": now_iso})

    # 已解除的事件 -> 從 active 移除（仍保留在 history）
    for icao24 in list(state["active_alerts"].keys()):
        if icao24 not in current_emergency:
            resolved = state["active_alerts"][icao24]
            send_telegram_push(
                f"✅ 解除警報: {resolved['callsign'] or icao24} "
                f"(squawk {resolved['squawk']} 已消失/降落)"
            )

    state["active_alerts"] = current_emergency
    state["all_aircraft"] = aircraft
    state["last_update"] = now_iso
    state["history"] = state["history"][-200:]  # 只保留最近 200 筆
    state["bbox"] = BBOX

    save_state(state)
    print(f"[{now_iso}] 空域內共 {len(aircraft)} 架航機，緊急 {len(current_emergency)} 架")
    return state


def main():
    loop = "--loop" in sys.argv
    token = get_opensky_token()
    state = load_state()

    if not loop:
        # 單次執行模式：給 cron / GitHub Actions 排程用
        try:
            poll_once(token, state)
        except urllib.error.HTTPError as e:
            print(f"[錯誤] OpenSky API 回應 {e.code}：{e.read()}")
            sys.exit(1)
        except Exception as e:
            print(f"[錯誤] {e}")
            sys.exit(1)
        return

    # 本機測試用的常駐迴圈模式
    print(f"[--loop 模式] 開始監控台灣周邊空域 bbox={BBOX}，每 {POLL_INTERVAL_SEC} 秒輪詢一次")
    while True:
        try:
            state = poll_once(token, state)
        except urllib.error.HTTPError as e:
            print(f"[錯誤] OpenSky API 回應 {e.code}：{e.read()}")
        except Exception as e:
            print(f"[錯誤] {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
