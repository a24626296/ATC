#!/usr/bin/env python3
"""
台灣周邊空域緊急代碼 (Squawk 7500/7600/7700) 監控腳本
------------------------------------------------------
資料來源：OpenSky Network REST API (https://opensky-network.org)
通知方式：Telegram Bot API 推播
互動查詢：在 Telegram 對話框直接輸入航班呼號（例如 CAL781），排程下次執行時
          會回覆該航班目前在監控空域內的即時狀態（非即時聊天，會有排程間隔的延遲）

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
EMERGENCY_CODES = {"7500", "7600", "7700"}
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

def send_telegram_push(text, chat_id=None):
    target = chat_id or TELEGRAM_CHAT_ID
    if not (TELEGRAM_BOT_TOKEN and target):
        print("[提醒] 未設定 TELEGRAM_BOT_TOKEN / chat id，略過推播")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": target,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print(f"[Telegram] 已推播給 {target}")
    except urllib.error.HTTPError as e:
        print(f"[錯誤] Telegram 推播失敗：{e.code} {e.read()}")


def get_telegram_updates(offset):
    """抓取自 offset 之後的新訊息（getUpdates 是輪詢式 API，不需要 webhook / 常駐伺服器）"""
    if not TELEGRAM_BOT_TOKEN:
        return []
    params = urllib.parse.urlencode({"offset": offset, "timeout": 0})
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("result", [])
    except Exception as e:
        print(f"[錯誤] 讀取 Telegram getUpdates 失敗：{e}")
        return []


def format_flight_status(a):
    is_emergency = a["squawk"] in EMERGENCY_CODES
    lines = [
        f"✈️ {a['callsign'] or a['icao24']}",
        f"Squawk: {a['squawk'] or '—'}" + (f" ⚠️ {SQUAWK_MEANING.get(a['squawk'], '')}" if is_emergency else ""),
        f"狀態: {'地面' if a['on_ground'] else '飛行中'}",
        f"位置: {a['latitude']:.3f}, {a['longitude']:.3f}" if a["latitude"] is not None else "位置: —",
        f"高度: {a['altitude_m']} m" if a["altitude_m"] is not None else "高度: —",
        f"地速: {a['velocity']} m/s" if a["velocity"] is not None else "地速: —",
        f"航向: {a['heading']}°" if a["heading"] is not None else "航向: —",
        f"國籍: {a['origin_country'] or '—'}",
    ]
    return "\n".join(lines)


def handle_telegram_commands(state, aircraft):
    """查詢當前這批空域資料裡有沒有使用者輸入的航班編號，並回覆查詢者。
    只在排程執行的當下處理一次，所以查詢後最多要等到下一輪排程（例如 5 分鐘）才會收到回覆，
    不是即時聊天機器人。"""
    if not TELEGRAM_BOT_TOKEN:
        return state

    offset = state.get("telegram_last_update_id", 0) + 1
    updates = get_telegram_updates(offset)

    for update in updates:
        state["telegram_last_update_id"] = update["update_id"]
        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            continue

        chat_id = message["chat"]["id"]
        text = message["text"].strip()

        if text.startswith("/start") or text.startswith("/help"):
            send_telegram_push(
                "👋 台灣空域監控 Bot\n"
                "直接輸入航班呼號（例如 CAL781、EVA067、SJX800），\n"
                "我會回覆該航班目前在監控空域內的即時狀態。\n"
                "（排程每隔幾分鐘跑一次，查詢後可能要等一下才會收到回覆）",
                chat_id,
            )
            continue

        query = text.upper().replace(" ", "")
        if not query:
            continue

        # 先找完全相符，找不到再找呼號開頭相符（例如只打航空公司代碼）
        match = next((a for a in aircraft if a["callsign"].upper() == query), None)
        if not match:
            candidates = [a for a in aircraft if a["callsign"].upper().startswith(query)]
            if len(candidates) == 1:
                match = candidates[0]
            elif len(candidates) > 1:
                names = ", ".join(a["callsign"] for a in candidates[:10])
                send_telegram_push(f"🔍 找到多個符合 \"{text}\" 的航班：{names}\n請輸入完整呼號查詢", chat_id)
                continue

        if match:
            send_telegram_push(format_flight_status(match), chat_id)
        else:
            send_telegram_push(
                f"❓ 目前監控空域內查無航班 \"{text}\"\n"
                f"（可能已降落、不在台灣周邊空域內，或呼號打錯）",
                chat_id,
            )

    return state


# ---------- 狀態存取 ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {
        "active_alerts": {},
        "history": [],
        "all_aircraft": [],
        "last_update": None,
        "bbox": BBOX,
        "telegram_last_update_id": 0,
    }


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

    # 處理使用者在 Telegram 上輸入的航班查詢（用這批剛抓到的即時資料回覆）
    state = handle_telegram_commands(state, aircraft)

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
