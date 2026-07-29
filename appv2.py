import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="台股抄底觀測站", layout="wide")
st.title("🎯 台股五大關鍵底部觀測面板")
st.markdown("---")

# 格式化成交量顯示（大於1兆自動換算）
def format_volume(yi):
    if yi >= 10000:
        zhao = yi / 10000
        return f"{zhao:.2f} 兆元"
    else:
        return f"{yi:,.2f} 億元"

# === 證交所官方 API 自動抓取大盤成交金額 ===
@st.cache_data(ttl=60)
def fetch_twse_market_turnover():
    try:
        url = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        
        if data.get('stat') == 'OK':
            latest_row = data['data'][-1]
            raw_amount_str = latest_row[2].replace(',', '')
            turnover_yi = round(float(raw_amount_str) / 100000000.0, 2)
            return turnover_yi, "Success"
        else:
            return None, "證交所 API 回傳狀態異常"
    except Exception as e:
        return None, f"連線錯誤: {str(e)}"

# 自動推算預估量
def calculate_estimated_volume(current_vol):
    tw_timezone = timezone(timedelta(hours=8))
    now = datetime.now(tw_timezone)
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

# 畫專業 K 線圖的專用函數
def draw_professional_chart(df, title_name):
    df['5MA'] = df['Close'].rolling(window=5).mean()
    df['10MA'] = df['Close'].rolling(window=10).mean()
    df['20MA'] = df['Close'].rolling(window=20).mean()
    df['120MA'] = df['Close'].rolling(window=120).mean()
    df['9VMin'] = df['Low'].rolling(window=9, min_periods=1).min()
    df['9VMax'] = df['High'].rolling(window=9, min_periods=1).max()
    df['RSV'] = 100 * (df['Close'] - df['9VMin']) / (df['9VMax'] - df['9VMin'])
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

    fig.update_layout(title=f"📊 {title_name} 專業技術分析圖表 (近一年)", template="plotly_dark", xaxis_rangeslider_visible=False, height=750, margin=dict(l=20, r=20, t=50, b=20), legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig

# === 主程式執行區 ===
tickers = {"大盤": "^TWII", "0052": "0052.TW", "00830": "00830.TW", "00662": "00662.TW"}
prices, changes, history_dfs, ma20_now, ma20_prev = {}, {}, {}, {}, {}

with st.spinner('正在同步證交所官方數據與最新報價...'):
    real_twse_vol, api_msg = fetch_twse_market_turnover()
    
    for name, symbol in tickers.items():
        df = yf.Ticker(symbol).history(period="1y")
        if not df.empty:
            history_dfs[name] = df
            prices[name] = round(df['Close'].iloc[-1], 2)
            diff_amount = df['Close'].iloc[-1] - df['Close'].iloc[-2]
            changes[name] = {"amount": diff_amount, "pct": (diff_amount / df['Close'].iloc[-2]) * 100}
            ma = df['Close'].rolling(window=20).mean()
            ma20_now[name], ma20_prev[name] = round(ma.iloc[-1], 2), round(ma.iloc[-2], 2)

if len(prices) == 4:
    tw_df = history_dfs["大盤"]

    # === 側邊欄設定 ===
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
        if real_twse_vol is not None:
            daily_volume = calculate_estimated_volume(real_twse_vol)
            st.sidebar.info(f"🏛️ 證交所官方實際量: **{format_volume(real_twse_vol)}**\n⏱️ 系統計算成交量: **{format_volume(daily_volume)}**")
        else:
            st.sidebar.error(f"API 讀取失敗: {api_msg}\n請改用手動輸入。")
            daily_volume = st.sidebar.number_input("手動輸入今日成交金額 (億)", value=10835.69, step=50.0, format="%.2f")
    else:
        daily_volume = st.sidebar.number_input("手動輸入今日成交金額 (億)", value=10835.69, step=50.0, format="%.2f")

    st.sidebar.markdown("---")
    st.sidebar.write("📌 **底部三關卡客觀條件判定**")
    stage1_done = st.sidebar.checkbox("✅ 第一關：已完成巨量換手 (如見1兆以上)", value=True)
    stage2_no_new_low = st.sidebar.checkbox("⏳ 第二關條件A：指數近期沒有再創新低", value=False)
    stage3_breakout = st.sidebar.checkbox("⏳ 第三關：已站回 5/10MA 或放量長紅", value=False)
    
    weeks_passed = st.sidebar.slider("距離起跌已過幾週？", 0, 8, 0)
    candle_shape = st.sidebar.selectbox("今日大盤 K 線型態", ["實體黑K", "實體紅K", "長下影線", "W底成型", "放量長紅"])

    # === 圖表與數據面板 ===
    st.plotly_chart(draw_professional_chart(tw_df, "加權指數 (大盤)"), use_container_width=True)
    
    st.markdown("---")
    col1, col2, col3, col4 = st.columns(4)
    vol_text = f"今日成交: {format_volume(daily_volume)}"
    
    col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.0f}", f"{changes['大盤']['amount']:+.0f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
    col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
    col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
    col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")
    st.markdown("---")

    # === 底部三關卡與 5 筆資金進場策略邏輯 ===
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