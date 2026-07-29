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
# 🌟 Fugle 追蹤清單
# -------------------------
FUGLE_SYMBOL_MAP = {
    "大盤": "IX0001",
    "0052": "0052",
    "00662": "00662",
    "00830": "00830",
}

# WebSocket 資料儲存區
from threading import Lock
_fugle_store = {"data": {}, "lock": Lock()}

def fugle_store_set(key: str, value: Dict[str, Any]):
    with _fugle_store["lock"]:
        _fugle_store["data"][key] = value

def fugle_store_get_all() -> Dict[str, Dict[str, Any]]:
    with _fugle_store["lock"]:
        return dict(_fugle_store["data"])

# -------------------------
# 讀取 Token (Fugle & Gemini)
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
        gemini_tok = os.environ.get("GEMINI_API_KEY")

    return fugle_tok, gemini_tok

fugle_token, gemini_api_key = get_api_tokens()

# -------------------------
# 🤖 Gemini AI 盤後解析
# -------------------------
@st.cache_data(ttl=24*3600)
def analyze_kline_with_gemini(df_recent_json: str, api_key: str, date_str: str) -> str:
    if not api_key:
        return "⚠️ 請在 secrets.toml 設定 GEMINI_API_KEY，即可啟用 AI 自動判斷 K 線型態與盤勢解析。"
    try:
        genai.configure(api_key=api_key)
        prompt = f"""
        你是一位專業的台股技術分析師。現在的日期是 {date_str}。
        這是我提供的台股加權指數近 10 個交易日的 OHLCV 報價（JSON格式，日期為鍵值，包含開, 高, 低, 收, 量）：
        {df_recent_json}
        
        請根據最新一日的 K 線數據與近 10 日走勢，執行以下任務（請保持客觀、精練）：
        1. 判斷最新一個交易日的「K 線型態」（例如：實體黑K、實體紅K、長下影線、十字線、孕線等）。
        2. 判斷是否有打底跡象（例如 W 底、破底翻等）。
        3. 給出一段 100 字以內的精簡盤勢分析。
        
        請嚴格依照以下 Markdown 格式輸出：
        **📌 今日 K 線型態**：[填入型態]
        **📊 盤勢解析**：[填入精簡分析]
        """
        
        models_to_try = ['gemini-1.5-flash', 'gemini-1.5-flash-latest', 'gemini-pro']
        last_error = ""
        for m_name in models_to_try:
            try:
                model = genai.GenerativeModel(m_name)
                response = model.generate_content(prompt)
                return response.text
            except Exception as e:
                last_error = str(e)
                continue
        return f"❌ AI 解析發生錯誤：{last_error}"
    except Exception as e:
        return f"❌ AI 設定發生錯誤：{str(e)}"

# -------------------------
# 官方證交所 API 區 (確保指數與成交量絕對正確)
# -------------------------
@st.cache_data(ttl=10)
def fetch_twse_summary():
    """ 抓取證交所官方即時大盤摘要 (最正確的指數與漲跌) """
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        ts = int(datetime.now().timestamp() * 1000)
        url = f"https://www.twse.com.tw/res/data/zh/home/summary.json?_={ts}"
        res = requests.get(url, headers=headers, timeout=8)
        res.raise_for_status()
        data = res.json()
        return {
            "index": float(str(data.get("TSE_I", "0")).replace(",", "")),
            "diff": float(str(data.get("TSE_D", "0")).replace(",", "")),
            "pct": float(str(data.get("TSE_P", "0")).replace(",", "")),
            "turnover_yi": data.get("TSE_V"),
        }, "Success"
    except Exception as e:
        return {}, str(e)

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
# Fugle v1.0 REST API (個股報價主戰場)
# -------------------------
def fetch_fugle_intraday(symbol: str, token: str) -> Dict[str, Any]:
    if not token: return {"error": "No token"}
    clean_symbol = str(symbol).strip()
    url = f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{clean_symbol}"
    try:
        r = requests.get(url, headers={"X-API-KEY": token}, timeout=8)
        if r.status_code != 200: return {"error": f"HTTP {r.status_code}"}
        quote = r.json().get("data", r.json())
        # 同時抓取 closePrice, lastPrice, previousClose 避免抓空
        price = quote.get("closePrice") or quote.get("lastPrice") or quote.get("price") or quote.get("previousClose")
        vol = quote.get("totalAmount") if clean_symbol == "IX0001" else (quote.get("totalVolume") or quote.get("volume"))
        if price is not None:
            return {"price": float(price), "volume": float(vol) if vol is not None else None, "raw": quote}
        return {"error": "Parse error"}
    except Exception as e:
        return {"error": str(e)}

# -------------------------
# 指標與圖表
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

with st.spinner('正在同步證交所官方數據、計算指標與 AI 解析...'):
    twse_summary, _ = fetch_twse_summary()
    twse_vol, _ = fetch_twse_market_turnover()
    twse_20d_avg, _ = fetch_twse_historical_turnover_20d()

    realtime_quotes = {}
    for key, fugle_sym in FUGLE_SYMBOL_MAP.items():
        if key == "大盤": continue # 大盤優先使用證交所 summary
        fallback = fetch_fugle_intraday(fugle_sym, fugle_token) if fugle_token else {"error": "no token"}
        if "error" not in fallback and fallback.get("price") is not None:
            realtime_quotes[key] = {"price": fallback.get("price"), "volume": fallback.get("volume")}

    @st.cache_data(ttl=6 * 60 * 60)
    def fetch_history(symbol, cache_date):
        ticker = yf.Ticker(symbol, session=YF_SESSION) if YF_SESSION else yf.Ticker(symbol)
        return ticker.history(period="1y")

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    prices, changes, history_dfs, ma20_now, ma20_prev, kd_data = {}, {}, {}, {}, {}, {}

    for name, symbol in tickers.items():
        try:
            df = fetch_history(symbol, today_str)
            if df.empty: continue
        except Exception:
            continue

        today_date = datetime.now(TW_TZ).date()
        if len(df) >= 2:
            yf_prev_close = df['Close'].iloc[-2]
        else:
            yf_prev_close = df['Close'].iloc[-1]

        # === 價格指派 (大盤優先用證交所官方，個股優先用 Fugle，yfinance 作為最終防線) ===
        if name == "大盤" and twse_summary.get("index") is not None:
            current_price = twse_summary["index"]
            diff_amount = twse_summary["diff"]
            pct = twse_summary["pct"]
            prices[name] = round(current_price, 2)
            changes[name] = {"amount": diff_amount, "pct": pct}
        else:
            rt_price = realtime_quotes.get(name, {}).get("price")
            if rt_price is not None:
                current_price = rt_price
                prices[name] = round(current_price, 2)
                diff_amount = current_price - yf_prev_close
                changes[name] = {"amount": diff_amount, "pct": (diff_amount / yf_prev_close) * 100}
            else:
                current_price = df['Close'].iloc[-1]
                prices[name] = round(current_price, 2)
                diff_amount = current_price - yf_prev_close
                changes[name] = {"amount": diff_amount, "pct": (diff_amount / yf_prev_close) * 100}

        # 更新今日 K 棒
        if current_price is not None and df.index[-1].date() == today_date and is_market_open():
            df.loc[df.index[-1], "Close"] = current_price
            df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], current_price)
            df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], current_price)

        df = compute_indicators(df)
        history_dfs[name] = df
        ma20_now[name] = round(df['20MA'].iloc[-1], 2)
        ma20_prev[name] = round(df['20MA'].iloc[-2], 2)
        kd_data[name] = {"K": round(df['K'].iloc[-1], 2), "D": round(df['D'].iloc[-1], 2)}

    # 成交量決定
    final_daily_volume = twse_vol if twse_vol is not None else 3200.0
    vol_source = "TWSE盤後數據"

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
        
        # 🤖 觸發 Gemini AI 盤後解析 (盤中封印 / 盤後解鎖)
        st.markdown("##### 🤖 Gemini 雙子星 AI 盤後解析")
        if GENAI_AVAILABLE and gemini_api_key:
            if is_market_open():
                st.info("⏳ **盤中不耗費資源**：AI 盤勢解析將於今日 13:30 收盤、價格鎖定後自動啟動。")
            else:
                recent_df = history_dfs["大盤"].tail(10)[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
                recent_df = recent_df.round(2)
                recent_df.index = recent_df.index.strftime('%Y-%m-%d')
                json_payload = recent_df.to_json(orient="index")
                
                ai_analysis_result = analyze_kline_with_gemini(json_payload, gemini_api_key, today_str)
                st.info(ai_analysis_result)
        elif not GENAI_AVAILABLE:
            st.warning("⚠️ 尚未安裝 Google AI 套件，請執行 `pip install google-generativeai`。")
        else:
            st.warning("⚠️ 請在 secrets.toml 設定 `GEMINI_API_KEY`。")

        st.markdown("---")

        col1, col2, col3, col4 = st.columns(4)
        
        st.sidebar.markdown("---")
        st.sidebar.write("📌 **大盤量能基準設定**")
        avg_vol_20 = st.sidebar.number_input("官方近 20 日均量 (億)", value=float(twse_20d_avg), step=100.0)
        daily_volume = st.sidebar.number_input(f"今日大盤成交量 ({vol_source}) 億", value=float(final_daily_volume), step=50.0, format="%.2f")
        
        vol_percentage = (daily_volume / avg_vol_20 * 100) if avg_vol_20 > 0 else 0
        if vol_percentage <= 70:
            st.sidebar.success(f"🔥 **今日量縮比：{vol_percentage:.1f}%**\n\n(小於 70%，已達窒息量標準)")
        else:
            st.sidebar.warning(f"📊 **今日量縮比：{vol_percentage:.1f}%**\n\n(大於 70%，尚有賣壓未枯竭)")

        twse_vol_display = format_volume(twse_vol) if twse_vol else "尚未公布或抓取失敗"
        st.sidebar.info(f"🏛️ **官方盤後結算量**：**{twse_vol_display}**")

        vol_text = f"今日成交: {format_volume(daily_volume)}"

        col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.2f}", f"{changes['大盤']['amount']:+.2f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
        col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
        col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
        col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")

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
        cond5 = all(prices[n] > ma20_now[n] for n in check_list) and all(ma20_now[n] > ma20_prev[n] for n in check_list)

        st.subheader("🎯 五筆資金進場監測")
        def render_card(col, title, condition, success_msg, fail_msg):
            with col:
                if condition: st.success(f"### 🟢 第 {title} 筆\n\n{success_msg}")
                else: st.error(f"### 🔴 鎖定中\n**第 {title} 筆**\n\n{fail_msg}")

        c1, c2, c3, c4, c5 = st.columns(5)
        render_card(c1, "1. 空間的極致", cond1, 
                    f"已達防禦區間！\n大盤: {prices['大盤']:,.0f}", 
                    f"未達防禦深度\n大盤: {prices['大盤']:,.0f}")
        render_card(c2, "2. 量能的窒息", cond2, 
                    f"賣壓枯竭，籌碼乾淨！\n量縮比: {vol_percentage:.1f}%", 
                    f"量縮未滿足\n量縮比: {vol_percentage:.1f}%")
        render_card(c3, "3. 時間的折磨", cond3, 
                    f"底部承接力道確認！\n已過 {weeks_passed} 週", 
                    f"時間未到或破底\n已過 {weeks_passed} 週")
        render_card(c4, "4. 型態的確認", cond4, 
                    f"第二隻腳打底完成！\n型態: {candle_shape}", 
                    f"打底型態未確認\n型態: {candle_shape}")
        render_card(c5, "5. 趨勢的反轉", cond5, 
                    f"消化完成，多頭啟動！\n三者站上且月線上揚", 
                    f"右側趨勢未確認\n均線未全面站上或上揚")

        st.markdown("---")
        triggered_count = sum([cond1, cond2, cond3, cond4, cond5])
        if triggered_count > 0:
            st.info(f"🚨 **執行紀律：已有 {triggered_count} 筆資金達成觸發條件。請冷酷執行對應部位的進場！**")
        else:
            st.warning("☕ **空手觀望：目前尚無任何一筆資金觸發條件。保單借款利息是你買「從容」的成本，請耐心等待。**")