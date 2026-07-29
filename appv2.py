# app.py
import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Any, Optional

# 嘗試用 curl_cffi 讓 yfinance 的 session 模擬 Chrome TLS 指紋 (可選)
try:
    from curl_cffi import requests as cffi_requests
    YF_SESSION = cffi_requests.Session(impersonate="chrome")
except Exception:
    YF_SESSION = None

st.set_page_config(page_title="台股進場數據觀測", layout="wide")
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

# 自動刷新 (盤中)
AUTOREFRESH_OK = True
try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
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
# TWSE 官方 OpenAPI：取得全市場當日/歷史 EOD 類資料 (備援/參考)
# Endpoint: https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
# 注意：OpenAPI 回傳欄位不一定固定，解析要做健壯處理
# -------------------------
OPENAPI_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
OPENAPI_UI_URL = "https://openapi.twse.com.tw/v1/ui/#/"

@st.cache_data(ttl=60*60)  # 1 小時快取
def fetch_twse_openapi_stock_day_all() -> Tuple[Any, str]:
    try:
        url = OPENAPI_STOCK_DAY_ALL_URL
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=12)
        res.raise_for_status()
        data = res.json()
        return data, "Success"
    except Exception as e:
        return {}, f"OpenAPI 連線錯誤: {e}"

def find_stock_in_openapi(all_data: Any, symbol: str) -> Tuple[Optional[float], Optional[Dict[str, Any]], Optional[str]]:
    """
    嘗試在 openapi 全市場資料中尋找股票 symbol（如 '0052'、'00830'、'00662'）。
    回傳 (close_price 或 None, 原始 row 或 None, message 或 None)
    message 用來說明解析過程中發生的情況（例如欄位名稱不符）。
    """
    if not all_data:
        return None, None, "OpenAPI 無資料"
    # openapi 端可能回傳 list 或 dict（需兼容）
    rows = all_data if isinstance(all_data, list) else all_data.get("data") or all_data.get("items") or []
    if not isinstance(rows, list):
        return None, None, "OpenAPI 回傳格式非預期（非 list）"

    # 標準化查詢代碼，ETFs 與股票在 openapi 上可能沒有前導零，先嘗試多種形式
    symbol_z4 = symbol.zfill(4)
    possible_codes = {symbol, symbol_z4}

    # 常見欄位名稱集合
    code_keys = ["Code", "code", "StockNo", "stockNo", "StockNo1", "stock_no", "股票代號", "證券代號"]
    close_keys = ["Close", "close", "ClosePrice", "closePrice", "ClosePriceNew", "收盤價", "成交價"]

    for row in rows:
        # 找出 row 中任何可能的 code 欄位
        row_code = None
        for k in code_keys:
            if k in row and row[k] is not None:
                row_code = str(row[k]).strip()
                break
        if row_code is None:
            # 有些 row 用其他 key，例如 'c' 或 'stock_id'
            for k, v in row.items():
                if isinstance(v, (str, int)) and str(v).isdigit():
                    # 若該值看起來像代碼則檢查
                    val = str(v).zfill(4)
                    if val in possible_codes or str(v) in possible_codes:
                        row_code = str(v)
                        break
        if row_code is None:
            continue

        # 對比
        if row_code in possible_codes or row_code.zfill(4) in possible_codes:
            # 找 close 價
            for ck in close_keys:
                if ck in row and row[ck] not in (None, "", "-"):
                    try:
                        return float(row[ck]), row, None
                    except Exception:
                        # 非數值形式，嘗試去除逗號再轉
                        try:
                            return float(str(row[ck]).replace(",", "")), row, None
                        except Exception:
                            return None, row, "找到對應代碼但收盤價欄位格式不可轉為數值"
            # 未找到常見的 close 欄位，回傳 row 以便人工檢查
            return None, row, "找到對應代碼但未找到已知的收盤價欄位"
    return None, None, "OpenAPI 中找不到對應股票代號"

# -------------------------
# 證交所 summary.json（盤中即時大盤指數、當下累積成交金額）
# -------------------------
@st.cache_data(ttl=10)
def fetch_twse_summary():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
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
# MIS API：盤中即時個股/ETF 報價（主要）
# Endpoint: https://mis.twse.com.tw/stock/api/getStockInfo.jsp
# 呼叫時先打首頁拿 cookie，再合併多支在 ex_ch 一次取得以減少請求次數
# 若回傳格式改變或缺少 msgArray，會回傳警示訊息與原始 JSON（供檢查）
# -------------------------
@st.cache_data(ttl=5)
def fetch_realtime_quotes_mis(code_map: Dict[str, str]) -> Tuple[Dict[str, Any], str]:
    """
    code_map 範例: {"大盤": "t00", "0052": "0052", "00830": "00830", "00662": "00662"}
    回傳: (result_dict, msg)
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        session = requests.Session()
        session.get("https://mis.twse.com.tw/stock/index.jsp", headers=headers, timeout=5)

        # 建構 ex_ch：使用 tse_{code}.tw 形式（包含 index 的 t00）
        ex_ch_items = []
        for code in code_map.values():
            # 若 code 為 t00 (index)，保持 t00 的形式；community practice 是 tse_t00.tw 可用
            ex_ch_items.append(f"tse_{code}.tw")
        ex_ch = "|".join(ex_ch_items)

        ts = int(datetime.now().timestamp() * 1000)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0&_={ts}"
        res = session.get(url, headers=headers, timeout=8)
        res.raise_for_status()
        data = res.json()

        # 偵測結構是否異常
        if "msgArray" not in data or not isinstance(data["msgArray"], list):
            # 結構有變動，回傳原始 JSON 並提示使用者檢查
            msg = "MIS API 回傳結構異常：缺少 msgArray 或型態改變。可能需要檢查 API 節點（https://mis.twse.com.tw/stock/index.jsp）或調整解析邏輯。"
            st.sidebar.warning(msg)
            st.sidebar.text("若需要，請檢查以下原始回應（僅供 debug）：")
            st.sidebar.json(data)
            return {}, msg

        code_to_name = {v: k for k, v in code_map.items()}
        result = {}
        for item in data.get("msgArray", []):
            code = item.get("c")
            # item 可能包含 'c' = code (like '0052'), 有時候為整數字串
            name = code_to_name.get(code) or code_to_name.get(str(code).zfill(4))
            if not name:
                # 有時回傳的 code 欄跟我們的 mapping 不一致，試著以 'n' (name) 去 match
                n = item.get("n")
                for k, v in code_map.items():
                    if n and (str(n).find(k) != -1 or str(k).find(str(n)) != -1):
                        name = k
                        break
            if not name:
                # 跳過不在我們監控清單內的項目
                continue

            def _to_float(s):
                try:
                    return float(s) if s not in ("-", "", None) else None
                except (ValueError, TypeError):
                    return None

            price = _to_float(item.get("z"))
            prev_close = _to_float(item.get("y"))

            if price is None:
                bid = item.get("b", "").split("_")[0] if item.get("b") else ""
                ask = item.get("a", "").split("_")[0] if item.get("a") else ""
                price = _to_float(bid) or _to_float(ask) or prev_close

            volume_lots = _to_float(item.get("v")) or 0

            result[name] = {
                "price": price,
                "prev_close": prev_close,
                "volume_lots": volume_lots,
                "time": item.get("t", "-"),
                "date": item.get("d", "-"),
                "raw": item  # 保留原始 item 以供 debug
            }

        if not result:
            return {}, "MIS API 無回傳預期的監控標的資料（可能為非交易時間或被限流）"
        return result, "Success"
    except Exception as e:
        return {}, f"即時報價連線錯誤: {e}"

# -------------------------
# 估算今日成交量（當 summary.json 不可得時，用昨日 EOD x 時間比例估算全天）
# -------------------------
def calculate_estimated_volume(current_vol):
    now = datetime.now(TW_TZ)
    current_time = now.time()
    market_start = datetime.strptime("09:00:00", "%H:%M:%S").time()
    market_end = datetime.strptime("13:30:00", "%H:%M:%S").time()

    if current_time < market_start or current_time > market_end:
        return current_vol
    else:
        market_start_dt = datetime.combine(now.date(), market_start)
        now_dt = datetime.combine(now.date(), current_time)
        elapsed_minutes = (now_dt - market_start_dt).total_seconds() / 60
        if elapsed_minutes > 0:
            est_vol = current_vol * (270.0 / elapsed_minutes)
            return round(est_vol, 2)
        return current_vol

# -------------------------
# 畫 K 線圖（專業）
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
            increasing_line_color='#FF3333', increasing_fillcolor='#FF3333', decreasing_line_color='#00AA00', decreasing_fillcolor='#00AA00', name="K線"), row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=df['5MA'], line=dict(color='yellow', width=1), name=f'5MA: {latest["5MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['10MA'], line=dict(color='hotpink', width=1), name=f'10MA: {latest["10MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['20MA'], line=dict(color='deepskyblue', width=1), name=f'20MA: {latest["20MA"]:.2f}'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['120MA'], line=dict(color='mediumaquamarine', width=1), name=f'120MA: {latest["120MA"]:.2f}'), row=1, col=1)

    colors = ['#FF3333' if row['Close'] >= row['Open'] else '#00AA00' for _, row in df.iterrows()]
    fig.add_trace(go.Bar(x=df.index, y=df['Volume'], marker_color=colors, name="成交股數"), row=2, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=df['K'], line=dict(color='orange', width=1.2), name=f'K9: {latest["K"]:.2f}'), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['D'], line=dict(color='dodgerblue', width=1.2), name=f'D9: {latest["D"]:.2f}'), row=3, col=1)

    fig.add_annotation(xref="x domain", yref="y domain", x=0.01, y=0.98,
        text=f"<span style='color:yellow'>5MA: {latest['5MA']:.2f}</span>  <span style='color:hotpink'>10MA: {latest['10MA']:.2f}</span>  <span style='color:deepskyblue'>20MA: {latest['20MA']:.2f}</span>  <span style='color:mediumaquamarine'>120MA: {latest['120MA']:.2f}</span>", showarrow=False, font=dict(size=12), row=1, col=1)
    fig.add_annotation(xref="x domain", yref="y domain", x=0.01, y=0.95,
        text=f"<span style='color:orange'>K9: {latest['K']:.2f}</span>  <span style='color:dodgerblue'>D9: {latest['D']:.2f}</span>", showarrow=False, font=dict(size=13, weight="bold"), row=3, col=1)

    live_tag = " 🔴 盤中即時" if is_market_open() else " (收盤資料)"
    fig.update_layout(title=f"📊 {title_name} 專業技術分析圖表 (近一年){live_tag}", template="plotly_dark", xaxis_rangeslider_visible=False, height=750, margin=dict(l=20, r=20, t=50, b=20), legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig

# -------------------------
# 主程式
# -------------------------
tickers = {"大盤": "^TWII", "0052": "0052.TW", "00830": "00830.TW", "00662": "00662.TW"}
# MIS 的 code mapping（給 fetch_realtime_quotes_mis 使用）
mis_codes = {"大盤": "t00", "0052": "0052", "00830": "00830", "00662": "00662"}

prices, changes, history_dfs, ma20_now, ma20_prev = {}, {}, {}, {}, {}

with st.spinner('正在同步證交所官方數據與最新報價...'):
    # 1) 取得 OpenAPI 全市場資料（備援）
    openapi_all, openapi_msg = fetch_twse_openapi_stock_day_all()

    # 2) 取得 summary.json（大盤當下即時指數與成交金額）
    twse_summary, summary_msg = fetch_twse_summary()

    # 3) 用 MIS API 取得個股/ETF 即時報價（主要來源）
    realtime_quotes, rt_msg = fetch_realtime_quotes_mis(mis_codes)

    # 4) 歷史日線資料 (yfinance)，用今天的日期當 key 做快取
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
            if rt and rt.get("price") is not None and rt.get("prev_close") is not None:
                current_price = rt["price"]
                prev_close = rt["prev_close"]
                prices[name] = round(current_price, 2)
                diff_amount = current_price - prev_close
                changes[name] = {"amount": diff_amount, "pct": (diff_amount / prev_close) * 100 if prev_close else 0.0}
            else:
                # 若 MIS 沒資料，改用 OpenAPI 嘗試找最近的收盤價（備援）
                fallback_price, matched_row, msg = find_stock_in_openapi(openapi_all, name)
                if fallback_price is not None:
                    current_price = fallback_price
                    prices[name] = round(current_price, 2)
                    # 若 yfinance 有昨天收盤價可用來算 delta
                    try:
                        diff_amount = current_price - df['Close'].iloc[-1]
                        changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-1]) * 100}
                    except Exception:
                        changes[name] = {"amount": 0.0, "pct": 0.0}
                else:
                    # 若 openapi 也無，則使用 yfinance 的最後收盤價
                    if msg:
                        # 顯示較詳細的診斷訊息在側欄，並提供 openapi 的檢視連結
                        st.sidebar.warning(f"⚠️ OpenAPI 解析提示: {msg}\n若欄位有調整，請至 {OPENAPI_UI_URL} 或 {OPENAPI_STOCK_DAY_ALL_URL} 查看原始欄位並更新解析器。")
                        if matched_row is not None:
                            st.sidebar.text("找到的 row（供 debug）：")
                            st.sidebar.json(matched_row)
                    current_price = df['Close'].iloc[-1]
                    prices[name] = round(current_price, 2)
                    diff_amount = df['Close'].iloc[-1] - df['Close'].iloc[-2]
                    changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-2]) * 100}

        # 盤中同步今天的 K 棒 (若 history 最後一日為今天)
        today = datetime.now(TW_TZ).date()
        if current_price is not None and df.index[-1].date() == today:
            df.loc[df.index[-1], "Close"] = current_price
            df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], current_price)
            df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], current_price)

        ma = df['Close'].rolling(window=20).mean()
        ma20_now[name], ma20_prev[name] = round(ma.iloc[-1], 2), round(ma.iloc[-2], 2)

# 若所有資料齊全則畫面
if len(prices) == 4:
    tw_df = history_dfs["大盤"]

    if summary_msg == "Success":
        st.sidebar.success(f"🟢 大盤即時資料已連線（更新時間：{twse_summary.get('time', '-')}）")
    else:
        st.sidebar.warning(f"⚠️ 大盤即時 API 狀態：{summary_msg}\n目前漲跌%為備援計算（可能不即時）")

    if rt_msg != "Success":
        st.sidebar.warning(f"⚠️ 個股/ETF 即時報價 API 狀態：{rt_msg}\n目前漲跌%為備援計算（可能不即時）")
        st.sidebar.info("若 MIS API 結構有變動，請檢查：https://mis.twse.com.tw/stock/index.jsp")

    # 側邊參數
    st.sidebar.header("⚙️ 參數設定與盤中觀察")
    cost_52 = st.sidebar.number_input("0052 成本價", value=180.0, step=1.0)
    cost_830 = st.sidebar.number_input("00830 成本價", value=45.0, step=0.5)
    cost_662 = st.sidebar.number_input("00662 成本價", value=115.0, step=0.5)

    loss_52 = round(((prices["0052"] - cost_52) / cost_52) * 100, 2)
    loss_830 = round(((prices["00830"] - cost_830) / cost_830) * 100, 2)
    loss_662 = round(((prices["00662"] - cost_662) / cost_662) * 100, 2)

    st.sidebar.markdown("---")
    st.sidebar.write("📌 **大盤成交金額 (億)**")

    use_auto_vol = st.sidebar.checkbox("自動抓取證交所官方成交量", value=True)
    if use_auto_vol:
        if twse_summary.get("turnover_yi") is not None:
            daily_volume = twse_summary["turnover_yi"]
            st.sidebar.success(f"🟢 盤中即時成交金額: **{format_volume(daily_volume)}**（更新時間 {twse_summary.get('time', '-')})")
        else:
            st.sidebar.warning(f"⚠️ 即時成交金額 API 失敗（{summary_msg}），若 OpenAPI 有前一日 EOD 可用以估算，系統會採用估算值，否則請手動輸入。")
            # 嘗試從 openapi_all 找到前一日整體成交金額 (視 endpoint 是否含此統計)
            daily_volume = st.sidebar.number_input("手動輸入今日成交金額 (億)", value=10835.69, step=50.0, format="%.2f")
    else:
        daily_volume = st.sidebar.number_input("手動輸入今日成交金額 (億)", value=10835.69, step=50.0, format="%.2f")

    # 個股/ETF 即時累積成交量（張）－ 來自 MIS API
    st.sidebar.markdown("---")
    st.sidebar.write("📌 **個股/ETF 盤中累積成交量（張）**")
    for name in ["0052", "00830", "00662"]:
        rt = realtime_quotes.get(name)
        if rt and rt.get("volume_lots") is not None:
            st.sidebar.write(f"{name}: {rt['volume_lots']:,.0f} 張")

    st.sidebar.markdown("---")
    st.sidebar.write("📌 **底部三關卡客觀條件判定**")
    stage1_done = st.sidebar.checkbox("✅ 第一關：已完成巨量換手 (如見1兆以上)", value=True)
    stage2_no_new_low = st.sidebar.checkbox("⏳ 第二關條件A：指數近期沒有再創新低", value=False)
    stage3_breakout = st.sidebar.checkbox("⏳ 第三關：已站回 5/10MA 或放量長紅", value=False)

    weeks_passed = st.sidebar.slider("距離起跌已過幾週？", 0, 8, 0)
    candle_shape = st.sidebar.selectbox("今日大盤 K 線型態", ["實體黑K", "實體紅K", "長下影線", "W底成型", "放量長紅"])

    # 圖表
    st.plotly_chart(draw_professional_chart(tw_df, "加權指數 (大盤)"), use_container_width=True)

    st.markdown("---")
    col1, col2, col3, col4 = st.columns(4)
    vol_text = f"今日成交: {format_volume(daily_volume)}"

    col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.2f}", f"{changes['大盤']['amount']:+.2f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
    col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
    col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
    col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")
    st.caption(f"{'🔴 盤中即時更新中（每 15 秒自動刷新）' if is_market_open() else '⚪ 目前非交易時間，顯示為最後收盤資料'}")
    st.markdown("---")

    # 底部關卡邏輯
    worst_loss = min(loss_52, loss_830, loss_662)
    cond_volume_shrink = daily_volume <= 3500
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