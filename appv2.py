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

# curl_cffi 載入保護 (yfinance 備用)
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
# 🌟 Fugle 追蹤清單 (加入 IX0001 作為加權指數)
# -------------------------
FUGLE_SYMBOL_MAP = {
    "大盤": "IX0001",
    "0052": "0052",
    "00662": "00662",
    "00830": "00830",
}

# WebSocket 資料儲存區 (執行緒安全)
from threading import Lock
_fugle_store = {"data": {}, "lock": Lock()}

def fugle_store_set(key: str, value: Dict[str, Any]):
    with _fugle_store["lock"]:
        _fugle_store["data"][key] = value

def fugle_store_get_all() -> Dict[str, Dict[str, Any]]:
    with _fugle_store["lock"]:
        return dict(_fugle_store["data"])

# -------------------------
# 讀取 Token
# -------------------------
def get_fugle_token_and_source():
    token = None
    source = None
    secrets_keys = None
    try:
        secrets = st.secrets
        secrets_keys = list(secrets.keys())
        for k, v in secrets.items():
            if isinstance(v, str) and v.strip():
                token = v.strip()
                source = f"st.secrets['{k}']"
                break
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

    if not token:
        for env_key in ("FUGLE_TOKEN", "FUGLE__TOKEN", "FUGLE_API_KEY", "FUGLEKEY"):
            val = os.environ.get(env_key)
            if val:
                token = val.strip()
                source = f"env:{env_key}"
                break
    return token, source, secrets_keys

fugle_token, fugle_token_source, fugle_secrets_keys = get_fugle_token_and_source()

# -------------------------
# Fugle v1.0 WebSocket (無限迴圈背景執行)
# -------------------------
def start_fugle_ws(symbols: List[str], token: str):
    if not WS_AVAILABLE or not token:
        return None
    ws_url = "wss://api.fugle.tw/marketdata/v1.0/stock/streaming"
    
    def on_open(ws):
        auth_msg = json.dumps({"event": "auth", "apikey": token})
        ws.send(auth_msg)
        time.sleep(0.5)
        
        for s in symbols:
            try:
                sub_msg = json.dumps({
                    "event": "subscribe",
                    "channel": "aggregates",
                    "symbol": s
                })
                ws.send(sub_msg)
                time.sleep(0.05)
            except Exception:
                pass

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("event") == "data":
                payload = data.get("data", {})
                sym_str = str(payload.get("symbol", ""))
                
                target_key = None
                for k, v in FUGLE_SYMBOL_MAP.items():
                    if sym_str == v or sym_str.endswith(k):
                        target_key = k
                        break
                
                if target_key:
                    price = payload.get("close") or payload.get("price")
                    # 指數(IX0001)抓取真實成交金額(totalAmount)，個股抓取成交張數
                    vol = payload.get("totalAmount") if target_key == "大盤" else payload.get("totalVolume")
                    tm = payload.get("time") or payload.get("timestamp")
                    
                    if price:
                        current_data = fugle_store_get_all().get(target_key, {})
                        new_data = {
                            "price": float(price),
                            "volume": float(vol) if vol else current_data.get("volume"),
                            "time": tm,
                            "raw": payload
                        }
                        fugle_store_set(target_key, new_data)
        except Exception:
            pass

    def run_loop():
        while True:
            try:
                ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message)
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                time.sleep(3)

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()
    return t

# -------------------------
# Fugle v1.0 REST API (避免開盤瞬間WS沒資料的備援)
# -------------------------
def fetch_fugle_intraday(symbol: str, token: str) -> Dict[str, Any]:
    if not token:
        return {"error": "Fugle token not set"}
    
    clean_symbol = str(symbol).strip()
    headers = {"X-API-KEY": token}
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{clean_symbol}"
    
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        
        data = r.json()
        quote = data.get("data", {}) if "data" in data else data
        
        price = quote.get("closePrice") or quote.get("lastPrice") or quote.get("price")
        if clean_symbol == "IX0001":
             vol = quote.get("totalAmount")
        else:
             vol = quote.get("totalVolume") or quote.get("total") or quote.get("volume")
        
        if price is not None:
            return {"price": float(price), "volume": float(vol) if vol is not None else None, "raw": quote}
        
        return {"error": "Cannot parse price from v1.0 response"}
    except Exception as e:
        return {"error": str(e)}

# -------------------------
# TWSE 官方大盤 API (證交所盤後結算成交量備援)
# -------------------------
@st.cache_data(ttl=60)
def fetch_twse_market_turnover():
    try:
        url = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        
        if data.get('stat') == 'OK':
            latest_row = data['data'][-1]
            raw_amount_str = latest_row[2].replace(',', '')
            turnover_yi = round(float(raw_amount_str) / 100000000.0, 2)
            return turnover_yi, "Success"
        return None, "API Error"
    except Exception as e:
        return None, str(e)

# -------------------------
# 運算指標與 K 線圖繪製
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
# 主程式執行區
# -------------------------
tickers = {"大盤": "^TWII", "0052": "0052.TW", "00830": "00830.TW", "00662": "00662.TW"}

if "fugle_ws_started" not in st.session_state:
    st.session_state["fugle_ws_started"] = False

if not st.session_state["fugle_ws_started"]:
    if fugle_token and WS_AVAILABLE:
        symbols_to_sub = list(FUGLE_SYMBOL_MAP.values())
        start_fugle_ws(symbols_to_sub, fugle_token)
        st.session_state["fugle_ws_started"] = True
        st.sidebar.success("🔌 Fugle API v1.0 即時串流連線已啟動。")

with st.spinner('正在同步數據、計算 KD 指標與最新報價...'):
    fugle_snapshot = fugle_store_get_all()
    realtime_quotes = {}
    
    for key, fugle_sym in FUGLE_SYMBOL_MAP.items():
        entry = fugle_snapshot.get(key)
        if entry and (entry.get("price") is not None):
            realtime_quotes[key] = {
                "price": entry.get("price"),
                "volume": entry.get("volume")
            }
        else:
            fallback = fetch_fugle_intraday(fugle_sym, fugle_token) if fugle_token else {"error": "no token"}
            if "error" not in fallback:
                realtime_quotes[key] = {
                    "price": fallback.get("price"),
                    "volume": fallback.get("volume")
                }
                fugle_store_set(key, fallback)

    @st.cache_data(ttl=6 * 60 * 60)
    def fetch_history(symbol, cache_date):
        ticker = yf.Ticker(symbol, session=YF_SESSION) if YF_SESSION else yf.Ticker(symbol)
        df = ticker.history(period="1y")
        return df

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    prices, changes, history_dfs, ma20_now, ma20_prev, kd_data = {}, {}, {}, {}, {}, {}

    for name, symbol in tickers.items():
        try:
            df = fetch_history(symbol, today_str)
            if df.empty: continue
        except Exception:
            continue

        current_price = None
        rt = realtime_quotes.get(name, {})
        
        if rt.get("price") is not None:
            current_price = rt["price"]
            prices[name] = round(current_price, 2)
            try:
                diff_amount = current_price - df['Close'].iloc[-1]
                changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-1]) * 100}
            except:
                changes[name] = {"amount": 0.0, "pct": 0.0}
        else:
            current_price = df['Close'].iloc[-1]
            prices[name] = round(current_price, 2)
            diff_amount = df['Close'].iloc[-1] - df['Close'].iloc[-2]
            changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-2]) * 100}

        # 將今日即時價格寫入歷史K線，確保當天日K與均線準確
        today = datetime.now(TW_TZ).date()
        if current_price is not None and df.index[-1].date() == today:
            df.loc[df.index[-1], "Close"] = current_price
            df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], current_price)
            df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], current_price)

        # 計算指標
        df = compute_indicators(df)
        history_dfs[name] = df
        ma20_now[name] = round(df['20MA'].iloc[-1], 2)
        ma20_prev[name] = round(df['20MA'].iloc[-2], 2)
        kd_data[name] = {"K": round(df['K'].iloc[-1], 2), "D": round(df['D'].iloc[-1], 2)}

    # === 動態判定大盤成交量 ===
    fugle_twii_vol = realtime_quotes.get("大盤", {}).get("volume")
    if fugle_twii_vol and fugle_twii_vol > 0:
        final_daily_volume = round(fugle_twii_vol / 100000000.0, 2)
        vol_source = "Fugle盤中即時"
    else:
        twse_vol, twse_msg = fetch_twse_market_turnover()
        if twse_vol is not None:
            final_daily_volume = twse_vol
            vol_source = "TWSE盤後數據"
        else:
            final_daily_volume = 3200.0
            vol_source = "手動預設"

    if len(prices) == 4:
        st.sidebar.header("⚙️ 參數設定與盤中觀察")
        cost_52 = st.sidebar.number_input("0052 成本價", value=180.0, step=1.0)
        cost_830 = st.sidebar.number_input("00830 成本價", value=45.0, step=0.5)
        cost_662 = st.sidebar.number_input("00662 成本價", value=115.0, step=0.5)

        loss_52 = round(((prices["0052"] - cost_52) / cost_52) * 100, 2)
        loss_830 = round(((prices["00830"] - cost_830) / cost_830) * 100, 2)
        loss_662 = round(((prices["00662"] - cost_662) / cost_662) * 100, 2)

        # 繪製圖表
        st.plotly_chart(draw_professional_chart(history_dfs["大盤"], "加權指數 (大盤)"), use_container_width=True)
        st.markdown("---")

        # 顯示主要數據
        col1, col2, col3, col4 = st.columns(4)
        daily_volume = st.sidebar.number_input(f"今日大盤成交量 ({vol_source}) 億", value=float(final_daily_volume), step=50.0, format="%.2f")
        vol_text = f"今日成交: {format_volume(daily_volume)}"

        col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.2f}", f"{changes['大盤']['amount']:+.2f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
        col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
        col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
        col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")

        # === 🌟 專屬 KD 技術指標面板 ===
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

        # 進場條件判定
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