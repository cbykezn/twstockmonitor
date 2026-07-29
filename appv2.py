import os
import json
import time
import threading
import requests
import traceback
import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Any, Optional, List

# websocket-client may not be available in some envs; import guarded
try:
    import websocket
    WS_AVAILABLE = True
except Exception:
    websocket = None
    WS_AVAILABLE = False

# optional curl_cffi for yfinance session impersonation
try:
    from curl_cffi import requests as cffi_requests
    YF_SESSION = cffi_requests.Session(impersonate="chrome")
except Exception:
    YF_SESSION = None

st.set_page_config(page_title="台股抄底觀測站", layout="wide")
st.title("🎯 台股五大關鍵底部觀測面板")
st.markdown("---")

TW_TZ = timezone(timedelta(hours=8))

def is_market_open():
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return start <= now <= end

# autorefresh
try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_OK = True
except Exception:
    AUTOREFRESH_OK = False

if AUTOREFRESH_OK and is_market_open():
    st_autorefresh(interval=15_000, limit=None, key="market_autorefresh")
elif not AUTOREFRESH_OK:
    st.sidebar.warning("⚠️ 尚未安裝 streamlit-autorefresh，盤中不會自動更新。請執行：pip install streamlit-autorefresh")

def format_volume(yi):
    if yi is None:
        return "N/A"
    if yi >= 10000:
        zhao = yi / 10000
        return f"{zhao:.2f} 兆元"
    else:
        return f"{yi:,.2f} 億元"

# -------------------------
# Fugle symbol mapping (your monitored symbols)
# -------------------------
FUGLE_SYMBOL_MAP = {
    "0052": "TW.0052",
    "00662": "TW.00662",
    "00830": "TW.00830",
}

# thread-safe store for WS
from threading import Lock
_fugle_store = {"data": {}, "lock": Lock()}

def fugle_store_set(key: str, value: Dict[str, Any]):
    with _fugle_store["lock"]:
        _fugle_store["data"][key] = value

def fugle_store_get_all() -> Dict[str, Dict[str, Any]]:
    with _fugle_store["lock"]:
        return dict(_fugle_store["data"])

# -------------------------
# 🌟 無敵版 Token 讀取函數 (暴力掃描所有可能格式)
# -------------------------
def get_fugle_token_and_source():
    token = None
    source = None
    secrets_keys = None
    try:
        secrets = st.secrets
        secrets_keys = list(secrets.keys())
        if secrets_keys:
            st.sidebar.info(f"st.secrets 成功讀取，目前含有頂層鍵: {', '.join(secrets_keys)}")
        
        # 1. 暴力掃描所有鍵值
        for k, v in secrets.items():
            # 如果是純字串 (如 FUGLE_TOKEN = "xxx")
            if isinstance(v, str) and v.strip():
                token = v.strip()
                source = f"st.secrets['{k}']"
                break
            # 如果是區塊格式 (如 [FUGLE] token = "xxx")
            elif hasattr(v, "items"):
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, str) and sub_v.strip():
                        token = sub_v.strip()
                        source = f"st.secrets['{k}']['{sub_k}']"
                        break
            if token:
                break
    except Exception:
        secrets_keys = None

    # 2. 環境變數備援
    if not token:
        for env_key in ("FUGLE_TOKEN", "FUGLE__TOKEN", "FUGLE_API_KEY", "FUGLEKEY"):
            val = os.environ.get(env_key)
            if val:
                token = val.strip()
                source = f"env:{env_key}"
                break

    return token, source, secrets_keys

fugle_token, fugle_token_source, fugle_secrets_keys = get_fugle_token_and_source()
if fugle_token:
    masked = (fugle_token[:4] + "..." + fugle_token[-4:]) if len(fugle_token) > 8 else "****"
    st.sidebar.success(f"Fugle token 已成功載入！\n（來源: {fugle_token_source}，{masked}）")
else:
    st.sidebar.error("Fugle token 依然未載入，請確認 secrets.toml 格式是否正確。")

# -------------------------
# Fugle WebSocket (background thread)
# -------------------------
def start_fugle_ws(symbols: List[str], token: str):
    if not WS_AVAILABLE:
        st.sidebar.error("websocket-client 未安裝，請在 requirements.txt 加上 websocket-client 並重新部署。")
        return None
    if not token:
        st.sidebar.warning("Fugle token 未設定，WebSocket 不會啟動。")
        return None

    ws_url = f"wss://realtime.fugle.tw/v0/streams/quote?token={token}"

    def on_open(ws):
        print("Fugle WS opened")
        for s in symbols:
            try:
                sub_msg = json.dumps({"type": "subscribe", "symbol": s})
                ws.send(sub_msg)
                time.sleep(0.05)
            except Exception as e:
                print("subscribe error", e)

    def _extract_value_from_msg(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        candidates_price = [
            lambda d: d.get("last"), lambda d: d.get("lastPrice"),
            lambda d: d.get("price"), lambda d: d.get("z"),
            lambda d: d.get("trade", {}).get("price") if isinstance(d.get("trade"), dict) else None
        ]
        candidates_vol = [
            lambda d: d.get("volume"), lambda d: d.get("v"),
            lambda d: d.get("trade", {}).get("volume") if isinstance(d.get("trade"), dict) else None
        ]
        candidates_time = [
            lambda d: d.get("time"), lambda d: d.get("t"), lambda d: d.get("timestamp")
        ]

        p = None
        for f in candidates_price:
            try:
                val = f(data)
                if val not in (None, "", "-"):
                    p = float(val)
                    break
            except Exception:
                continue

        vol = None
        for f in candidates_vol:
            try:
                val = f(data)
                if val not in (None, "", "-"):
                    vol = float(val)
                    break
            except Exception:
                continue

        tm = None
        for f in candidates_time:
            try:
                val = f(data)
                if val not in (None, "", "-"):
                    tm = str(val)
                    break
            except Exception:
                continue

        return p, vol, tm

    def on_message(ws, message):
        try:
            data = json.loads(message)
        except Exception:
            try:
                st.sidebar.warning("收到無法解析的 Fugle WS 訊息（raw），側欄顯示以供 debug。")
                st.sidebar.text(message)
            except Exception:
                pass
            return

        sym = data.get("symbol") or data.get("s") or data.get("instrumentId") or data.get("id")
        key = None
        if sym:
            for k, v in FUGLE_SYMBOL_MAP.items():
                if str(sym).upper() == v.upper() or str(sym).endswith(k):
                    key = k
                    break
        if not key:
            def _search_for_symbol(obj):
                if isinstance(obj, dict):
                    for kk, vv in obj.items():
                        if isinstance(vv, str) and any(vv.upper() == mapv.upper() or vv.endswith(k) for k, mapv in FUGLE_SYMBOL_MAP.items()):
                            return kk, vv
                        res = _search_for_symbol(vv)
                        if res:
                            return res
                if isinstance(obj, list):
                    for item in obj:
                        res = _search_for_symbol(item)
                        if res:
                            return res
                return None
            res = _search_for_symbol(data)
            if res:
                _, found_val = res
                for k, v in FUGLE_SYMBOL_MAP.items():
                    if str(found_val).upper() == v.upper() or str(found_val).endswith(k):
                        key = k
                        break

        price, volume, msg_time = _extract_value_from_msg(data)
        if not key:
            st.sidebar.warning("Fugle WS 訊息中未識別到監控標的代碼 (symbol)，原始 JSON 如下：")
            st.sidebar.json(data)
            return
        if price is None and volume is None:
            st.sidebar.warning(f"Fugle WS 訊息解析失敗（{key}），請檢查 raw JSON (側欄)。")
            st.sidebar.json(data)
            fugle_store_set(key, {"raw": data, "time": msg_time})
            return
        fugle_store_set(key, {"price": price, "volume": volume, "time": msg_time, "raw": data})

    def on_error(ws, error):
        print("Fugle WS error:", error)
        try:
            st.sidebar.error(f"Fugle WebSocket 發生錯誤: {error}")
        except Exception:
            pass

    def on_close(ws, close_status_code, close_msg):
        print("Fugle WS closed", close_status_code, close_msg)
        try:
            st.sidebar.warning("Fugle WebSocket 連線已關閉，將嘗試重連。")
        except Exception:
            pass

    def run_loop():
        while True:
            try:
                ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print("WS run_forever exception:", e)
                try:
                    st.sidebar.warning(f"Fugle WS run_forever 例外: {e}，3秒後重試。")
                except Exception:
                    pass
            time.sleep(3)

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return t

# -------------------------
# Fugle meta lookup: find numeric symbolId (cached)
# -------------------------
@st.cache_data(ttl=60 * 10)
def fetch_fugle_symbol_meta(code_or_symbol: str, token: str) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    if not token:
        return None, None
    headers = {"X-API-KEY": token}
    try:
        s = code_or_symbol.strip()
        if s.isdigit():
            url = "https://api.fugle.tw/marketdata/v1.0/meta/symbols"
            params = {"symbolId": s}
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code == 200:
                js = r.json()
                data = js.get("data") or js
                if isinstance(data, list) and data:
                    item = data[0]
                    try:
                        return int(item.get("symbolId")), item
                    except Exception:
                        sid = item.get("symbolId")
                        if isinstance(sid, str) and sid.isdigit():
                            return int(sid), item
                if isinstance(data, dict) and data.get("symbolId"):
                    return int(data.get("symbolId")), data

        cand = []
        if s.upper().startswith("TW."):
            cand.append(s)
            cand.append(s.split(".", 1)[1])
        else:
            cand.append(s)
            cand.append("TW." + s)
        url = "https://api.fugle.tw/marketdata/v1.0/meta/symbols"
        for q in cand:
            params = {"q": q}
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code != 200:
                continue
            js = r.json()
            arr = js.get("data") or js.get("result") or js
            if isinstance(arr, list) and arr:
                item = arr[0]
                sid = item.get("symbolId")
                if isinstance(sid, (int, float)):
                    return int(sid), item
                if isinstance(sid, str) and sid.isdigit():
                    return int(sid), item
        try:
            st.sidebar.warning(f"Fugle symbol meta 未找到: {code_or_symbol}（嘗試過：{cand}）")
        except Exception:
            pass
        return None, None
    except Exception as e:
        try:
            st.sidebar.error(f"fetch_fugle_symbol_meta 例外: {e}")
        except Exception:
            pass
        return None, None

# -------------------------
# Fugle intraday using numeric symbolId
# -------------------------
def fetch_fugle_intraday(symbol_or_code: str, token: str) -> Dict[str, Any]:
    if not token:
        return {"error": "Fugle token not set"}
    headers = {"X-API-KEY": token}
    try:
        s = str(symbol_or_code).strip()
        if s.isdigit():
            symbol_id_numeric = int(s)
        else:
            meta_id, meta_raw = fetch_fugle_symbol_meta(s, token)
            if meta_id:
                symbol_id_numeric = meta_id
            else:
                return {"error": f"Cannot find Fugle symbolId for {symbol_or_code}"}

        url = "https://api.fugle.tw/realtime/v0.3/intraday/quote"
        params = {"symbolId": symbol_id_numeric}
        r = requests.get(url, headers=headers, params=params, timeout=8)
        try:
            r.raise_for_status()
        except requests.HTTPError as he:
            try:
                st.sidebar.error(f"Fugle intraday HTTP {r.status_code} for symbolId={symbol_id_numeric}")
                try:
                    st.sidebar.json(r.json())
                except Exception:
                    st.sidebar.text(r.text[:2000])
            except Exception:
                pass
            return {"error": f"HTTP {r.status_code}: {he}"}

        data = r.json()
        container = data.get("data") or data.get("result") or data
        price = None
        volume = None
        if isinstance(container, dict):
            quote = container.get("quote") if isinstance(container.get("quote"), dict) else container
            price = quote.get("lastPrice") or quote.get("last") or container.get("lastPrice") or container.get("last")
            volume = quote.get("volume") or container.get("volume") or container.get("totalVolume")
            if price is None:
                def _deep_find_number(obj):
                    if isinstance(obj, dict):
                        for v in obj.values():
                            res = _deep_find_number(v)
                            if res is not None:
                                return res
                    if isinstance(obj, list):
                        for it in obj:
                            res = _deep_find_number(it)
                            if res is not None:
                                return res
                    if isinstance(obj, (int, float)):
                        return float(obj)
                    if isinstance(obj, str):
                        s = obj.replace(",", "")
                        if s.replace(".", "", 1).isdigit():
                            return float(s)
                    return None
                price = _deep_find_number(container)
        else:
            def _deep_find_number(obj):
                if isinstance(obj, dict):
                    for v in obj.values():
                        res = _deep_find_number(v)
                        if res is not None:
                            return res
                if isinstance(obj, list):
                    for it in obj:
                        res = _deep_find_number(it)
                        if res is not None:
                            return res
                if isinstance(obj, (int, float)):
                    return float(obj)
                if isinstance(obj, str):
                    s = obj.replace(",", "")
                    if s.replace(".", "", 1).isdigit():
                        return float(s)
                return None
            price = _deep_find_number(container)

        return {"price": float(price) if price is not None else None,
                "volume": float(volume) if volume is not None else None,
                "raw": data, "symbolId": symbol_id_numeric}
    except Exception as e:
        try:
            st.sidebar.error(f"fetch_fugle_intraday exception: {e}")
        except Exception:
            pass
        return {"error": str(e)}

# -------------------------
# TWSE OpenAPI and summary (kept as backup)
# -------------------------
OPENAPI_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

@st.cache_data(ttl=60*60)
def fetch_twse_openapi_stock_day_all() -> Tuple[Any, str]:
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(OPENAPI_STOCK_DAY_ALL_URL, headers=headers, timeout=12)
        res.raise_for_status()
        return res.json(), "Success"
    except Exception as e:
        return {}, f"OpenAPI 連線錯誤: {e}"

@st.cache_data(ttl=10)
def fetch_twse_summary():
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        ts = int(datetime.now().timestamp() * 1000)
        url = f"https://www.twse.com.tw/res/data/zh/home/summary.json?_={ts}"
        res = requests.get(url, headers=headers, timeout=8)
        res.raise_for_status()
        data = res.json()
        return {
            "index": data.get("TSE_I"),
            "diff": data.get("TSE_D"),
            "pct": data.get("TSE_P"),
            "turnover_yi": data.get("TSE_V"),
            "time": data.get("SHTIME"),
        }, "Success"
    except Exception as e:
        return {}, f"summary.json 連線錯誤: {e}"

# -------------------------
# K-line drawing (kept)
# -------------------------
def draw_professional_chart(df, title_name):
    df = df.copy()
    df['5MA'] = df['Close'].rolling(window=5).mean()
    df['10MA'] = df['Close'].rolling(window=10).mean()
    df['20MA'] = df['Close'].rolling(window=20).mean()
    df['120MA'] = df['Close'].rolling(window=120).mean()
    df['9VMin'] = df['Low'].rolling(window=9, min_periods=1).min()
    df['9VMax'] = df['High'].rolling(window=9, min_periods=1).max()
    denom = (df['9VMax'] - df['9VMin']).replace(0, 1)
    df['RSV'] = 100 * (df['Close'] - df['9VMin']) / denom
    df['K'] = df['RSV'].ewm(com=2, adjust=False).mean()
    df['D'] = df['K'].ewm(com=2, adjust=False).mean()

    latest = df.iloc[-1]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.6, 0.2, 0.2])
    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
                increasing_line_color='#FF3333', decreasing_line_color='#00AA00', name="K線"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['5MA'], line=dict(color='yellow', width=1), name=f'5MA: {latest["5MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['10MA'], line=dict(color='hotpink', width=1), name=f'10MA: {latest["10MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['20MA'], line=dict(color='deepskyblue', width=1), name=f'20MA: {latest["20MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['120MA'], line=dict(color='mediumaquamarine', width=1), name=f'120MA: {latest["120MA"]:.2f}'), row=1, col=1)
    colors = ['#FF3333' if row['Close'] >= row['Open'] else '#00AA00' for _, row in df.iterrows()]
    fig.add_trace(go.Bar(x=df.index, y=df['Volume'], marker_color=colors, name="成交股數"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['K'], line=dict(color='orange', width=1.2), name=f'K9: {latest["K"]:.2f}'), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['D'], line=dict(color='dodgerblue', width=1.2), name=f'D9: {latest["D"]:.2f}'), row=3, col=1)
    live_tag = " 🔴 盤中即時" if is_market_open() else " (收盤資料)"
    fig.update_layout(title=f"📊 {title_name} 專業技術分析圖表 (近一年){live_tag}", template="plotly_dark", xaxis_rangeslider_visible=False, height=750)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig

# -------------------------
# Main: start WS and perform data assembly
# -------------------------
tickers = {"大盤": "^TWII", "0052": "0052.TW", "00830": "00830.TW", "00662": "00662.TW"}

# start WS once
if "fugle_ws_started" not in st.session_state:
    st.session_state["fugle_ws_started"] = False

if not st.session_state["fugle_ws_started"]:
    if fugle_token and WS_AVAILABLE:
        symbols_to_sub = list(FUGLE_SYMBOL_MAP.values())
        start_fugle_ws(symbols_to_sub, fugle_token)
        st.session_state["fugle_ws_started"] = True
        st.sidebar.info("🔌 Fugle WebSocket 背景連線已啟動（若側欄無錯誤，表示連線正常）。")
    elif fugle_token and not WS_AVAILABLE:
        st.sidebar.warning("Fugle token 有設定，但 websocket-client 未安裝，無法啟動 WS。請安裝並重新部署。")
    else:
        st.sidebar.warning("Fugle token 未設定，請在 Secrets 或 environment 設定。")

with st.spinner('正在同步證交所官方數據、Fugle 即時數據與最新報價...'):
    openapi_all, openapi_msg = fetch_twse_openapi_stock_day_all()
    twse_summary, summary_msg = fetch_twse_summary()

    fugle_snapshot = fugle_store_get_all()

    realtime_quotes = {}
    for key, fugle_sym in FUGLE_SYMBOL_MAP.items():
        entry = fugle_snapshot.get(key)
        if entry and (entry.get("price") is not None):
            realtime_quotes[key] = {
                "price": entry.get("price"),
                "prev_close": entry.get("raw", {}).get("prevClose") or entry.get("raw", {}).get("y"),
                "volume_lots": entry.get("volume"),
                "time": entry.get("time"),
                "raw": entry.get("raw")
            }
        else:
            fallback = fetch_fugle_intraday(fugle_sym, fugle_token) if fugle_token else {"error": "no token"}
            if "error" in fallback:
                realtime_quotes[key] = {}
                st.sidebar.warning(f"Fugle fallback 失敗 ({key}): {fallback.get('error')}")
            else:
                price = fallback.get("price")
                vol = fallback.get("volume")
                realtime_quotes[key] = {"price": price, "prev_close": None, "volume_lots": vol, "time": None, "raw": fallback.get("raw")}
                fugle_store_set(key, {"price": price, "volume": vol, "time": None, "raw": fallback.get("raw")})

    @st.cache_data(ttl=6 * 60 * 60)
    def fetch_history(symbol, cache_date):
        last_err = None
        for attempt in range(3):
            try:
                if YF_SESSION is not None:
                    ticker = yf.Ticker(symbol, session=YF_SESSION)
                else:
                    ticker = yf.Ticker(symbol)
                df = ticker.history(period="1y")
                if not df.empty:
                    return df
            except Exception as e:
                last_err = e
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"yfinance 抓取 {symbol} 失敗（已重試3次）: {last_err}")

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")

    prices, changes, history_dfs, ma20_now, ma20_prev = {}, {}, {}, {}, {}
    for name, symbol in tickers.items():
        try:
            df = fetch_history(symbol, today_str)
        except Exception as e:
            cache_key = f"last_good_history_{name}"
            if cache_key in st.session_state:
                df = st.session_state[cache_key]
                st.warning(f"⚠️ {name} 歷史資料更新失敗（{e}），暫時沿用上次成功的快取資料。")
            else:
                st.error(f"❌ {name} 歷史資料抓取失敗，且無舊資料可用：{e}")
                continue

        if df.empty:
            continue

        st.session_state[f"last_good_history_{name}"] = df
        history_dfs[name] = df

        current_price, prev_close = None, None
        if name == "大盤" and twse_summary.get("index") is not None:
            current_price = twse_summary["index"]
            diff_amount = twse_summary["diff"]
            pct = twse_summary["pct"]
            prices[name] = round(current_price, 2)
            changes[name] = {"amount": diff_amount, "pct": pct}
            prev_close = current_price - diff_amount
        else:
            rt = realtime_quotes.get(name)
            if rt and rt.get("price") is not None:
                current_price = rt["price"]
                prev_close = rt.get("prev_close") if rt.get("prev_close") not in (None, 0) else None
                prices[name] = round(current_price, 2)
                if prev_close:
                    diff_amount = current_price - prev_close
                    changes[name] = {"amount": diff_amount, "pct": (diff_amount / prev_close) * 100}
                else:
                    try:
                        diff_amount = current_price - df['Close'].iloc[-1]
                        changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-1]) * 100}
                    except Exception:
                        changes[name] = {"amount": 0.0, "pct": 0.0}
            else:
                fallback_price = None
                try:
                    oa = openapi_all if isinstance(openapi_all, list) else openapi_all.get("data", [])
                    for row in oa:
                        if isinstance(row, (list, tuple)):
                            if any(str(cell).endswith(name) for cell in row if cell is not None):
                                for cell in reversed(row):
                                    try:
                                        cand = float(str(cell).replace(",", ""))
                                        fallback_price = cand
                                        break
                                    except Exception:
                                        continue
                        if fallback_price:
                            break
                        elif isinstance(row, dict):
                            if any(str(v).endswith(name) for v in row.values() if v):
                                for ck in ("Close", "close", "ClosePrice", "closePrice", "成交價", "收盤價"):
                                    if ck in row and row[ck] not in (None, "", "-"):
                                        try:
                                            fallback_price = float(str(row[ck]).replace(",", ""))
                                            break
                                        except Exception:
                                            continue
                        if fallback_price:
                            break
                    if fallback_price is not None:
                        current_price = fallback_price
                        prices[name] = round(current_price, 2)
                        changes[name] = {"amount": 0.0, "pct": 0.0}
                    else:
                        current_price = df['Close'].iloc[-1]
                        prices[name] = round(current_price, 2)
                        diff_amount = df['Close'].iloc[-1] - df['Close'].iloc[-2]
                        changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-2]) * 100}
                except Exception:
                    current_price = df['Close'].iloc[-1]
                    prices[name] = round(current_price, 2)
                    diff_amount = df['Close'].iloc[-1] - df['Close'].iloc[-2]
                    changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-2]) * 100}

        today = datetime.now(TW_TZ).date()
        if current_price is not None and df.index[-1].date() == today:
            df.loc[df.index[-1], "Close"] = current_price
            df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], current_price)
            df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], current_price)

        ma = df['Close'].rolling(window=20).mean()
        ma20_now[name], ma20_prev[name] = round(ma.iloc[-1], 2), round(ma.iloc[-2], 2)

    if len(prices) == 4:
        tw_df = history_dfs["大盤"]
        if summary_msg == "Success":
            st.sidebar.success(f"🟢 大盤即時資料已連線（更新時間：{twse_summary.get('time', '-')}）")
        else:
            st.sidebar.warning(f"⚠️ 大盤即時 API 狀態：{summary_msg}\n目前漲跌%為備援計算（可能不即時）")

        st.sidebar.header("⚙️ 參數設定與盤中觀察")
        cost_52 = st.sidebar.number_input("0052 成本價", value=180.0, step=1.0)
        cost_830 = st.sidebar.number_input("00830 成本價", value=45.0, step=0.5)
        cost_662 = st.sidebar.number_input("00662 成本價", value=115.0, step=0.5)

        loss_52 = round(((prices["0052"] - cost_52) / cost_52) * 100, 2)
        loss_830 = round(((prices["00830"] - cost_830) / cost_830) * 100, 2)
        loss_662 = round(((prices["00662"] - cost_662) / cost_662) * 100, 2)

        st.plotly_chart(draw_professional_chart(tw_df, "加權指數 (大盤)"), use_container_width=True)
        st.markdown("---")

        col1, col2, col3, col4 = st.columns(4)
        daily_volume = st.sidebar.number_input("手動輸入今日成交金額 (億)", value=10835.69, step=50.0, format="%.2f")
        vol_text = f"今日成交: {format_volume(daily_volume)}"

        col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.2f}", f"{changes['大盤']['amount']:+.2f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
        col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
        col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
        col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")

        st.caption(f"{'🔴 盤中即時更新中（以 Fugle WebSocket 為主）' if is_market_open() else '⚪ 目前非交易時間，顯示為最後收盤資料'}")
        st.markdown("---")

        worst_loss = min(loss_52, loss_830, loss_662)
        cond_volume_shrink = daily_volume <= 3500
        stage1_done = st.sidebar.checkbox("✅ 第一關：已完成巨量換手 (如見1兆以上)", value=True)
        stage2_no_new_low = st.sidebar.checkbox("⏳ 第二關條件A：指數近期沒有再創新低", value=False)
        stage3_breakout = st.sidebar.checkbox("⏳ 第三關：已站回 5/10MA 或放量長紅", value=False)
        weeks_passed = st.sidebar.slider("距離起跌已過幾週？", 0, 8, 0)
        candle_shape = st.sidebar.selectbox("今日大盤 K 線型態", ["實體黑K", "實體紅K", "長下影線", "W底成型", "放量長紅"])

        cond2 = cond_volume_shrink and stage2_no_new_low
        cond1 = (36000 <= prices["大盤"] <= 38000) or (worst_loss <= -15.0) or stage1_done
        cond3 = (3 <= weeks_passed <= 4)
        cond4 = (prices["大盤"] <= 41000) and (candle_shape in ["長下影線", "W底成型", "放量長紅"])
        cond5 = (all(prices[name] > ma20_now[name] for name in tickers) and all(ma20_now[name] > ma20_prev[name] for name in tickers)) or stage3_breakout

        st.subheader("🎯 底部三關卡與五筆資金進場監測")
        st.markdown(f"""
        > **📊 目前量能結構狀態解析**：
        > * **第一關（大量換手）**：{'✅ 已達成（見 1.08 兆巨量）' if stage1_done else '⏳ 觀察中'}
        > * **第二關（惜售量縮 / 窒息量）**：{'✅ 已達成' if cond2 else f'⏳ 評估中（目前成交量: {format_volume(daily_volume)}，待後續量縮至 3500 億以下且不破底）'}
        > * **第三關（均線與反攻）**：{'✅ 已確認反攻' if cond5 else '⏳ 等待站回 5/10MA 或放量長紅'}
        """)
        st.markdown("---")

        def render_card(col, title, condition, success_msg, fail_msg):
            with col:
                if condition: st.success(f"### 🟢 第 {title} 筆\n\n{success_msg}")
                else: st.error(f"### 🔴 鎖定中\n**第 {title} 筆**\n\n{fail_msg}")

        c1, c2, c3, c4, c5 = st.columns(5)
        render_card(c1, "1. 第一關-換手", cond1, f"巨量換手完成！\n成交: {format_volume(daily_volume)}", f"等待巨量換手確認\n大盤: {prices['大盤']:,.0f}")
        render_card(c2, "2. 第二關-窒息", cond2, f"量縮惜售，賣壓枯竭！\n成交量: {format_volume(daily_volume)}", f"目前成交量: {format_volume(daily_volume)}\n未創新低: {'是' if stage2_no_new_low else '否'}")
        render_card(c3, "3. 時間折磨", cond3, f"盤整期滿，時間滿足！\n已過 {weeks_passed} 週", f"已過 {weeks_passed} 週（目標 3-4 週）")
        render_card(c4, "4. 型態確認", cond4, f"第二隻腳打底完成！\n目前型態: {candle_shape}", f"目前型態: {candle_shape}")
        render_card(c5, "5. 第三關-反攻", cond5, f"均線共振 / 放量長紅，右側反攻！", f"等待站回 5/10MA 或放量")

        st.markdown("---")
        triggered_count = sum([cond1, cond2, cond3, cond4, cond5])
        if triggered_count > 0:
            st.info(f"🚨 **執行紀律：已有 {triggered_count} 筆資金達成觸發條件。請無視市場雜音，冷酷執行對應部位的進場！**")
        else:
            st.warning("☕ **空手觀望：目前尚無任何一筆資金觸發條件。保單借款利息是你買「從容」的成本，請耐心等待。**")
    else:
        st.error("資料讀取不完整，請稍後重新整理頁面。")