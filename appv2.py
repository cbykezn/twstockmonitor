# app.py
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

# websocket-client 載入保護
try:
    import websocket
    WS_AVAILABLE = True
except Exception:
    websocket = None
    WS_AVAILABLE = False

# curl_cffi 載入保護
try:
    from curl_cffi import requests as cffi_requests
    YF_SESSION = cffi_requests.Session(impersonate="chrome")
except Exception:
    YF_SESSION = None

# google-generativeai 載入保護
try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except Exception:
    GENAI_AVAILABLE = False

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

# 自動重新整理設定
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
# Fugle 追蹤清單（監控 key -> fugle symbol）
# -------------------------
FUGLE_SYMBOL_MAP = {
    "大盤": "IX0001",   # Fugle 大盤識別符（你使用的）
    "0052": "0052",
    "00662": "00662",
    "00830": "00830",
}

# WebSocket 資料儲存區（thread-safe）
from threading import Lock
_fugle_store = {"data": {}, "lock": Lock()}

def fugle_store_set(key: str, value: Dict[str, Any]):
    with _fugle_store["lock"]:
        _fugle_store["data"][key] = value

def fugle_store_get_all() -> Dict[str, Dict[str, Any]]:
    with _fugle_store["lock"]:
        return dict(_fugle_store["data"])

# -------------------------
# 讀取 Token (Fugle & Gemini) - 支援多種 secrets 格式與 env
# -------------------------
def get_api_tokens():
    fugle_tok = None
    gemini_tok = None
    try:
        secrets = st.secrets
        for k, v in secrets.items():
            k_upper = k.upper()
            if isinstance(v, str):
                if "FUGLE" in k_upper:
                    fugle_tok = v.strip()
                if "GEMINI" in k_upper:
                    gemini_tok = v.strip()
            elif hasattr(v, "items"):
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, str):
                        if "FUGLE" in k_upper or "FUGLE" in sub_k.upper():
                            fugle_tok = sub_v.strip()
                        if "GEMINI" in k_upper or "GEMINI" in sub_k.upper():
                            gemini_tok = sub_v.strip()
    except Exception:
        pass

    if not fugle_tok:
        fugle_tok = os.environ.get("FUGLE_TOKEN") or os.environ.get("FUGLE_API_KEY")
    if not gemini_tok:
        gemini_tok = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_TOKEN")
    return fugle_tok, gemini_tok

fugle_token, gemini_api_key = get_api_tokens()
if fugle_token:
    masked = fugle_token[:4] + "..." + fugle_token[-4:] if len(fugle_token) > 8 else "****"
    st.sidebar.success(f"Fugle token 已載入（已遮蔽: {masked}）")
else:
    st.sidebar.warning("Fugle token 未載入，請檢查 Streamlit Secrets 或環境變數 FUGLE_TOKEN")

# -------------------------
# Gemini AI 盤後解析（保留）
# -------------------------
@st.cache_data(ttl=24*3600)
def analyze_kline_with_gemini(df_recent_json: str, api_key: str, date_str: str) -> str:
    if not api_key:
        return "⚠️ 請在 secrets.toml 設定 GEMINI_API_KEY，即可啟用 AI 自動判斷 K 線型態與盤勢解析。"
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-3.6-flash')
        prompt = f"""
        你是一位專業的台股技術分析師。現在的日期是 {date_str}。
        這是我提供的台股加權指數近 10 個交易日的 OHLCV 報價（JSON格式，日期為鍵值，包含開、高、低、收、量）：{df_recent_json}
        請根據最新一日的 K 線數據與近 10 日走勢，判斷型態並給出 100 字以內的精簡盤勢分析。
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"❌ AI 解析發生錯誤：{str(e)}"

# -------------------------
# Fugle v1.0 WebSocket（背景 thread）
# 保持你原本的 WS 行為（訂閱 aggregates）
# -------------------------
def start_fugle_ws(symbols: List[str], token: str):
    if not WS_AVAILABLE or not token:
        return None
    ws_url = "wss://api.fugle.tw/marketdata/v1.0/stock/streaming"
    def on_open(ws):
        try:
            ws.send(json.dumps({"event": "auth", "apikey": token}))
            time.sleep(0.5)
            for s in symbols:
                ws.send(json.dumps({"event": "subscribe", "channel": "aggregates", "symbol": s}))
                time.sleep(0.05)
        except Exception:
            pass

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("event") == "data":
                payload = data.get("data", {})
                sym_str = str(payload.get("symbol", ""))
                target_key = next((k for k, v in FUGLE_SYMBOL_MAP.items() if sym_str == v or str(sym_str).endswith(k)), None)
                if target_key:
                    price = payload.get("close") or payload.get("price")
                    # 大盤與個股欄位可能不同，保守取得
                    vol = payload.get("totalAmount") if target_key == "大盤" else (payload.get("totalVolume") or payload.get("volume"))
                    if price is not None:
                        current = fugle_store_get_all().get(target_key, {})
                        fugle_store_set(target_key, {
                            "price": float(price),
                            "volume": float(vol) if vol not in (None, "") else current.get("volume"),
                            "time": payload.get("time") or payload.get("timestamp"),
                            "raw": payload
                        })
        except Exception:
            pass

    def on_error(ws, error):
        try:
            st.sidebar.error(f"Fugle WS error: {error}")
        except Exception:
            pass

    def run_loop():
        while True:
            try:
                ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error)
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                time.sleep(3)

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return t

# -------------------------
# Fugle v1.0 REST API（盤中備援）
# 使用 path: /marketdata/v1.0/stock/intraday/quote/{symbol}
# -------------------------
def fetch_fugle_intraday(symbol: str, token: str) -> Dict[str, Any]:
    if not token:
        return {"error": "No token"}
    clean_symbol = str(symbol).strip()
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{clean_symbol}"
    try:
        r = requests.get(url, headers={"X-API-KEY": token}, timeout=8)
        if r.status_code != 200:
            # 在側欄顯示回應供 debug
            try:
                st.sidebar.warning(f"Fugle REST {r.status_code} for {clean_symbol}")
                try:
                    st.sidebar.json(r.json())
                except Exception:
                    st.sidebar.text(r.text[:1000])
            except Exception:
                pass
            return {"error": f"HTTP {r.status_code}"}
        quote = r.json().get("data", r.json())
        price = quote.get("closePrice") or quote.get("lastPrice") or quote.get("price")
        vol = quote.get("totalAmount") if clean_symbol == "IX0001" else (quote.get("totalVolume") or quote.get("volume"))
        if price is not None:
            return {"price": float(price), "volume": float(vol) if vol is not None else None, "raw": quote}
        return {"error": "Parse error"}
    except Exception as e:
        return {"error": str(e)}

# -------------------------
# TWSE 官方大盤 API (每日成交量與 20 日均量)
# -------------------------
@st.cache_data(ttl=60)
def fetch_twse_market_turnover():
    try:
        url = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = res.json()
        if data.get('stat') == 'OK':
            return round(float(data['data'][-1][2].replace(',', '')) / 100000000.0, 2), "Success"
        return None, "API Error"
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=3600)
def fetch_twse_historical_turnover_20d():
    try:
        today = datetime.now(TW_TZ)
        date_this = today.strftime("%Y%m%d")
        date_last = (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m%d")
        headers = {'User-Agent': 'Mozilla/5.0'}
        data_this = requests.get(f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={date_this}", headers=headers, timeout=10).json().get('data', [])
        combined = data_this
        if len(data_this) < 20:
            time.sleep(1)
            data_last = requests.get(f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={date_last}", headers=headers, timeout=10).json().get('data', [])
            combined = data_last + data_this
        if not combined: return 4500.0, "無資料"
        last_20 = combined[-20:]
        avg_yi = round((sum([float(row[2].replace(',', '')) for row in last_20]) / len(last_20)) / 100000000.0, 2)
        return avg_yi, "Success"
    except Exception as e:
        return 4500.0, f"Error: {e}"

# -------------------------
# 指標計算與圖表（不變）
# -------------------------
def compute_indicators(df):
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
    return df

def draw_professional_chart(df, title_name):
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
# 主程式執行區（整合 Fugle 主資料來源 + TWSE summary fallback + yfinance）
# -------------------------
tickers = {"大盤": "^TWII", "0052": "0052.TW", "00830": "00830.TW", "00662": "00662.TW"}

if "fugle_ws_started" not in st.session_state:
    st.session_state["fugle_ws_started"] = False

if not st.session_state["fugle_ws_started"]:
    if fugle_token and WS_AVAILABLE:
        start_fugle_ws(list(FUGLE_SYMBOL_MAP.values()), fugle_token)
        st.session_state["fugle_ws_started"] = True
        st.sidebar.info("🔌 Fugle WebSocket 背景連線已啟動（若側欄無錯誤，表示連線正常）。")
    elif fugle_token and not WS_AVAILABLE:
        st.sidebar.warning("Fugle token 已設定，但 websocket-client 未安裝，無法啟動 WS。")
    else:
        st.sidebar.info("Fugle token 未設定或不可用，系統會以 yfinance/OpenAPI 做備援。")

# 主要流程
with st.spinner('正在同步數據、計算指標與 AI 解析...'):
    # 取得 TWSE summary（大盤即時）
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

    openapi_all = {}
    try:
        openapi_all, _ = fetch_twse_market_turnover()  # we just want to ensure function exists (not used as earlier)
    except Exception:
        openapi_all = {}

    twse_summary, summary_msg = fetch_twse_summary()

    # 讀取 Fugle WS 快取 snapshot
    fugle_snapshot = fugle_store_get_all()

    realtime_quotes = {}
    # 先以 WS snapshot 為主，若缺再用 REST fallback（fetch_fugle_intraday）
    for key, fugle_sym in FUGLE_SYMBOL_MAP.items():
        entry = fugle_snapshot.get(key)
        if entry and (entry.get("price") is not None):
            realtime_quotes[key] = {"price": entry.get("price"), "volume": entry.get("volume"), "raw": entry.get("raw")}
        else:
            # REST fallback: 若為大盤嘗試用 IX0001（你設定的），個股用其代號
            fallback = fetch_fugle_intraday(fugle_sym, fugle_token) if fugle_token else {"error": "no token"}
            if "error" not in fallback:
                realtime_quotes[key] = {"price": fallback.get("price"), "volume": fallback.get("volume"), "raw": fallback.get("raw")}
                fugle_store_set(key, {"price": fallback.get("price"), "volume": fallback.get("volume"), "time": None, "raw": fallback.get("raw")})
            else:
                # 不成功時保持空字典（後續會退回 yfinance）
                realtime_quotes[key] = {}

    # 歷史資料
    @st.cache_data(ttl=6 * 60 * 60)
    def fetch_history(symbol, cache_date):
        ticker = yf.Ticker(symbol, session=YF_SESSION) if YF_SESSION else yf.Ticker(symbol)
        return ticker.history(period="1y")

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    prices, changes, history_dfs, ma20_now, ma20_prev, kd_data = {}, {}, {}, {}, {}, {}

    for name, symbol in tickers.items():
        try:
            df = fetch_history(symbol, today_str)
            if df.empty:
                st.sidebar.warning(f"{name} 的 yfinance 歷史資料為空")
                continue
        except Exception as e:
            st.sidebar.warning(f"{name} 歷史資料抓取失敗: {e}")
            continue

        # 先擷取即時價格（優先：Fugle WS -> twse_summary（大盤） -> Fugle REST -> yfinance）
        current_price = None
        # 1) Fugle WS/REST snapshot
        rt = realtime_quotes.get("大盤" if name == "大盤" else name)
        if rt and rt.get("price") is not None:
            current_price = rt.get("price")

        # 2) 若仍沒有，且為大盤，嘗試 twse_summary
        if current_price is None and name == "大盤":
            if twse_summary and twse_summary.get("index") not in (None, "", 0):
                try:
                    current_price = float(twse_summary.get("index"))
                except Exception:
                    current_price = None

        # 3) 若仍沒有，嘗試 Fugle REST fallback（再次）
        if current_price is None and fugle_token:
            fb = fetch_fugle_intraday(FUGLE_SYMBOL_MAP.get(name, name), fugle_token)
            if fb and "error" not in fb and fb.get("price") is not None:
                current_price = fb.get("price")
                # sync to store
                fugle_store_set(name, {"price": current_price, "volume": fb.get("volume"), "raw": fb.get("raw")})

        # 4) 若仍沒有，嘗試 TWSE OpenAPI（簡單嘗試）
        if current_price is None and openapi_all:
            try:
                rows = openapi_all if isinstance(openapi_all, list) else openapi_all.get("data", []) or openapi_all.get("items", [])
                for row in rows:
                    # support both list and dict rows
                    if isinstance(row, dict):
                        vals = row.values()
                        if any(str(v).endswith(str(name)) for v in vals if v):
                            for ck in ("Close", "close", "ClosePrice", "closePrice", "成交價", "收盤價"):
                                if ck in row and row[ck] not in (None, "", "-"):
                                    try:
                                        current_price = float(str(row[ck]).replace(",", ""))
                                        break
                                    except Exception:
                                        continue
                            if current_price:
                                break
                    elif isinstance(row, (list, tuple)):
                        if any(str(cell).endswith(str(name)) for cell in row if cell):
                            for cell in reversed(row):
                                try:
                                    cand = float(str(cell).replace(",", ""))
                                    current_price = cand
                                    break
                                except Exception:
                                    continue
                            if current_price:
                                break
            except Exception:
                pass

        # 5) 最後退回 yfinance 的最後收盤
        if current_price is None:
            try:
                current_price = float(df['Close'].iloc[-1])
            except Exception:
                st.sidebar.error(f"{name} 無法取得任何價格來源")
                continue

        # 更穩健的「昨日收盤」與漲跌計算：找最後一個交易日 < today
        today_date = datetime.now(TW_TZ).date()
        try:
            prev_close_candidates = df[df.index.date < today_date]['Close']
            if not prev_close_candidates.empty:
                yf_prev_close = float(prev_close_candidates.iloc[-1])
            else:
                # 若沒有早於今天的交易日，退回最後一筆
                yf_prev_close = float(df['Close'].iloc[-1])
        except Exception:
            yf_prev_close = float(df['Close'].iloc[-1])

        # 寫入價格、計算差額與百分比（避免除以零）
        prices[name] = round(float(current_price), 2)
        diff_amount = float(current_price) - float(yf_prev_close)
        pct = 0.0
        try:
            if float(yf_prev_close) != 0:
                pct = (diff_amount / float(yf_prev_close)) * 100
        except Exception:
            pct = 0.0
        changes[name] = {"amount": diff_amount, "pct": pct}

        # 更新當日 K 棒（盤中即時）
        if df.index[-1].date() == today_date:
            try:
                df.loc[df.index[-1], "Close"] = current_price
                df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], current_price)
                df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], current_price)
            except Exception:
                pass

        # 計算指標
        df = compute_indicators(df)
        history_dfs[name] = df
        try:
            ma20_now[name] = round(df['20MA'].iloc[-1], 2)
            ma20_prev[name] = round(df['20MA'].iloc[-2], 2)
        except Exception:
            ma20_now[name] = None
            ma20_prev[name] = None
        try:
            kd_data[name] = {"K": round(df['K'].iloc[-1], 2), "D": round(df['D'].iloc[-1], 2)}
        except Exception:
            kd_data[name] = {"K": 0.0, "D": 0.0}

    # === 成交量判斷 ===
    twse_vol, twse_msg = fetch_twse_market_turnover()
    fugle_twii_vol = realtime_quotes.get("大盤", {}).get("volume")

    if fugle_twii_vol and fugle_twii_vol > 1000000:
        final_daily_volume = round(fugle_twii_vol / 100000000.0, 2)
        vol_source = "Fugle盤中即時"
    elif twse_vol is not None:
        final_daily_volume = twse_vol
        vol_source = "TWSE盤後數據"
    else:
        final_daily_volume = 3200.0
        vol_source = "手動預設"

    twse_20d_avg, twse_20d_msg = fetch_twse_historical_turnover_20d()

    # UI render
    if len(prices) == 4:
        st.sidebar.header("⚙️ 參數設定與盤中觀察")
        cost_52 = st.sidebar.number_input("0052 成本價", value=180.0, step=1.0)
        cost_830 = st.sidebar.number_input("00830 成本價", value=45.0, step=0.5)
        cost_662 = st.sidebar.number_input("00662 成本價", value=115.0, step=0.5)

        loss_52 = round(((prices["0052"] - cost_52) / cost_52) * 100, 2)
        loss_830 = round(((prices["00830"] - cost_830) / cost_830) * 100, 2)
        loss_662 = round(((prices["00662"] - cost_662) / cost_662) * 100, 2)

        st.plotly_chart(draw_professional_chart(history_dfs["大盤"], "加權指數 (大盤)"), use_container_width=True)

        # Gemini AI 盤後解析（盤中封鎖）
        st.markdown("##### 🤖 Gemini 雙子星 AI 盤後解析")
        if GENAI_AVAILABLE and gemini_api_key:
            if is_market_open():
                st.info("⏳ 盤中不呼叫 AI 解析，將於收盤後執行。")
            else:
                recent_df = history_dfs["大盤"].tail(10)[['Open','High','Low','Close','Volume']].copy()
                recent_df = recent_df.round(2)
                recent_df.index = recent_df.index.strftime('%Y-%m-%d')
                json_payload = recent_df.to_json(orient="index")
                ai_analysis_result = analyze_kline_with_gemini(json_payload, gemini_api_key, today_str)
                st.info(ai_analysis_result)
        elif not GENAI_AVAILABLE:
            st.warning("⚠️ 尚未安裝 Google AI 套件，請 pip install google-generativeai")
        else:
            st.warning("⚠️ 請在 secrets 裡設定 GEMINI_API_KEY")

        st.markdown("---")

        col1, col2, col3, col4 = st.columns(4)
        st.sidebar.markdown("---")
        st.sidebar.write("📌 **大盤量能基準設定**")
        avg_vol_20 = st.sidebar.number_input("官方近 20 日均量 (億)", value=float(twse_20d_avg), step=100.0)
        daily_volume = st.sidebar.number_input(f"今日大盤成交量 ({vol_source}) 億", value=float(final_daily_volume), step=50.0, format="%.2f")

        vol_percentage = (daily_volume / avg_vol_20 * 100) if avg_vol_20 > 0 else 0
        if vol_percentage <= 70:
            st.sidebar.success(f"🔥 **今日量縮比：{vol_percentage:.1f}%**")
        else:
            st.sidebar.warning(f"📊 **今日量縮比：{vol_percentage:.1f}%**")

        twse_vol_display = format_volume(twse_vol) if twse_vol else "尚未公布或抓取失敗"
        st.sidebar.info(f"🏛️ **官方盤後結算量**：**{twse_vol_display}**")

        vol_text = f"今日成交: {format_volume(daily_volume)}"

        col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.2f}", f"{changes['大盤']['amount']:+.2f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
        col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
        col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
        col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")

        # KD 指標顯示
        st.markdown("##### 📉 最新 KD 指標與交叉訊號")
        k_col1, k_col2, k_col3, k_col4 = st.columns(4)
        def render_kd(col, name, kd_dict):
            k_val, d_val = kd_dict['K'], kd_dict['D']
            color = "orange" if k_val >= d_val else "mediumaquamarine"
            status = "黃金交叉 (K>D)" if k_val >= d_val else "死亡交叉 (K<D)"
            col.markdown(f"**{name}**<br/><span style='color:{color}; font-size:16px;'>K: {k_val} / D: {d_val} ({status})</span>", unsafe_allow_html=True)
        render_kd(k_col1, "大盤", kd_data["大盤"])
        render_kd(k_col2, "0052", kd_data["0052"])
        render_kd(k_col3, "00830", kd_data["00830"])
        render_kd(k_col4, "00662", kd_data["00662"])

        st.caption(f"{'🔴 盤中即時更新中（以 Fugle v1.0 數據為主）' if is_market_open() else '⚪ 目前非交易時間，顯示為最後收盤資料'}")
        st.markdown("---")

        # 五筆策略（保留你原本邏輯）
        st.sidebar.markdown("---")
        st.sidebar.write("📌 **主觀型態與防守判定**")
        stage2_no_new_low = st.sidebar.checkbox("✅ 指數近期沒有再創新低", value=False)
        break_39384 = st.sidebar.checkbox("⚠️ 大盤是否已跌破 39,384 點？", value=False)
        candle_shape = st.sidebar.selectbox("今日大盤 K 線型態", ["實體黑K", "實體紅K", "長下影線", "十字線", "W底成型", "放量長紅"])
        weeks_passed = st.sidebar.slider("距離 7/29 已過幾週？", 0, 8, 0)

        cond1 = (36000 <= prices["大盤"] <= 38000) or (loss_830 <= -15.0) or (loss_662 <= -15.0)
        cond_volume_shrink = (daily_volume <= 3500) or (vol_percentage <= 70)
        cond2 = cond_volume_shrink and stage2_no_new_low
        cond3 = (3 <= weeks_passed <= 4) and not break_39384
        cond4 = (prices["大盤"] <= 41000) and (candle_shape in ["長下影線", "十字線", "W底成型"])
        check_list = ["大盤", "00830", "00662"]
        cond5 = all(ma20_now.get(n) is not None and prices[n] > ma20_now.get(n) for n in check_list) and all(ma20_now.get(n) is not None and ma20_prev.get(n) is not None and ma20_now.get(n) > ma20_prev.get(n) for n in check_list)

        st.subheader("🎯 五筆資金進場監測")
        def render_card(col, title, condition, success_msg, fail_msg):
            with col:
                if condition: st.success(f"### 🟢 第 {title} 筆\n\n{success_msg}")
                else: st.error(f"### 🔴 鎖定中\n**第 {title} 筆**\n\n{fail_msg}")

        c1, c2, c3, c4, c5 = st.columns(5)
        render_card(c1, "1. 空間的極致", cond1, f"已達防禦區間！\n大盤: {prices['大盤']:,.0f}", f"未達防禦深度\n大盤: {prices['大盤']:,.0f}")
        render_card(c2, "2. 量能的窒息", cond2, f"賣壓枯竭，籌碼乾淨！\n量縮比: {vol_percentage:.1f}%", f"量縮未滿足\n量縮比: {vol_percentage:.1f}%")
        render_card(c3, "3. 時間的折磨", cond3, f"底部承接力道確認！\n已過 {weeks_passed} 週", f"時間未到或破底\n已過 {weeks_passed} 週")
        render_card(c4, "4. 型態的確認", cond4, f"第二隻腳打底完成！\n型態: {candle_shape}", f"打底型態未確認\n型態: {candle_shape}")
        render_card(c5, "5. 趨勢的反轉", cond5, f"消化完成，多頭啟動！\n三者站上且月線上揚", f"右側趨勢未確認\n均線未全面站上或上揚")

        st.markdown("---")
        triggered_count = sum([cond1, cond2, cond3, cond4, cond5])
        if triggered_count > 0:
            st.info(f"🚨 **執行紀律：已有 {triggered_count} 筆資金達成觸發條件。請冷酷執行對應部位的進場！**")
        else:
            st.warning("☕ **空手觀望：目前尚無任何一筆資金觸發條件。**")
    else:
        st.error("資料讀取不完整，請稍後重新整理頁面。")