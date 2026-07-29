import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re

st.set_page_config(page_title="台股抄底觀測站", layout="wide")
st.title("🎯 台股五大關鍵底部觀測面板")
st.markdown("---")

# === 🌟 終極防護版：Yahoo 股市成交量爬蟲函數 ===
@st.cache_data(ttl=60)
def fetch_twii_turnover():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        # 使用安全的 URL 編碼 %5E 來代替 ^ 符號，避免產生 404 錯誤
        res = requests.get('https://tw.stock.yahoo.com/quote/%5ETWII', headers=headers, timeout=10)
        
        if res.status_code == 404:
             return None, "404找不到網頁 (網址已失效)"
        elif res.status_code != 200:
            return None, f"網站阻擋 (狀態碼: {res.status_code})"
            
        soup = BeautifulSoup(res.text, 'html.parser')
        
        # 暴力掃描法：直接掃描網頁所有文字，尋找「成交金額」
        elements = soup.find_all(['span', 'div', 'li'])
        for i, element in enumerate(elements):
            text = element.get_text(strip=True)
            if '成交金額' in text:
                # 往後找 10 個標籤內的第一個數字
                for j in range(1, 11):
                    if i + j < len(elements):
                        val_str = elements[i+j].get_text(strip=True)
                        # 確保裡面有數字
                        if any(char.isdigit() for char in val_str):
                            # 使用正則表達式，只提取數字與小數點 (自動過濾掉 '億'、',' 等符號)
                            clean_numbers = re.findall(r'[0-9]+(?:\.[0-9]+)?', val_str.replace(',', ''))
                            if clean_numbers:
                                try:
                                    return float(clean_numbers[0]), "Success"
                                except ValueError:
                                    continue
        return None, "找不到成交金額區塊 (Yahoo 網頁版型已更改)"
    except Exception as e:
        return None, f"連線錯誤: {str(e)}"

# 自動推算預估量
def calculate_estimated_volume(current_vol):
    now = datetime.now()
    market_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    market_end = now.replace(hour=13, minute=30, second=0, microsecond=0)
    
    if now < market_start or now > market_end:
        return current_vol
    else:
        elapsed_minutes = (now - market_start).total_seconds() / 60
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

with st.spinner('正在抓取報價與執行爬蟲中...'):
    real_twii_vol, spider_msg = fetch_twii_turnover() 
    
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
    # === 側邊欄設定 ===
    st.sidebar.header("⚙️ 參數設定與盤中觀察")
    cost_52 = st.sidebar.number_input("0052 成本價", value=180.0, step=1.0)
    cost_830 = st.sidebar.number_input("00830 成本價", value=45.0, step=0.5)
    cost_662 = st.sidebar.number_input("00662 成本價", value=115.0, step=0.5)
    
    loss_52 = round(((prices["0052"] - cost_52) / cost_52) * 100, 2)
    loss_830 = round(((prices["00830"] - cost_830) / cost_830) * 100, 2)
    loss_662 = round(((prices["00662"] - cost_662) / cost_662) * 100, 2)
    
    st.sidebar.markdown("---")
    st.sidebar.write("📌 **大盤成交量 (億)**")
    
    use_auto_vol = st.sidebar.checkbox("自動抓取/推算今日成交量", value=True)
    if use_auto_vol:
        if real_twii_vol is not None:
            daily_volume = calculate_estimated_volume(real_twii_vol)
            st.sidebar.info(f"🕷️ 爬蟲抓取實際量: **{real_twii_vol}** 億\n⏱️ 系統推算預估量: **{daily_volume}** 億")
        else:
            st.sidebar.error(f"連線失敗: {spider_msg}\n請暫時取消打勾，改用手動輸入。")
            daily_volume = st.sidebar.number_input("手動輸入預估量", value=3200.0, step=50.0)
    else:
        daily_volume = st.sidebar.number_input("手動輸入預估量", value=3200.0, step=50.0)

    st.sidebar.markdown("---")
    st.sidebar.write("📌 **主觀型態判定**")
    weeks_passed = st.sidebar.slider("距離 7/29 已經過幾週？", 0, 8, 0)
    break_39384 = st.sidebar.checkbox("大盤是否已跌破 39,384 點？", value=False)
    candle_shape = st.sidebar.selectbox("今日大盤 K 線型態", ["實體黑K", "實體紅K", "長下影線", "W底成型"])

    # === 圖表與數據面板 ===
    st.plotly_chart(draw_professional_chart(history_dfs["大盤"], "加權指數 (大盤)"), use_container_width=True)
    
    st.markdown("---")
    col1, col2, col3, col4 = st.columns(4)
    vol_text = f"今日成交: {real_twii_vol} 億" if real_twii_vol else "成交金額: 自動抓取失敗"
    
    col1.metric(f"📈 大盤指數 ({vol_text})", f"{prices['大盤']:,.0f}", f"{changes['大盤']['amount']:+.0f} 點 ({changes['大盤']['pct']:+.2f}%)", delta_color="inverse")
    col2.metric(f"📦 0052 (損益: {loss_52}%)", f"{prices['0052']}", f"{changes['0052']['amount']:+.2f} ({changes['0052']['pct']:+.2f}%)", delta_color="inverse")
    col3.metric(f"📦 00830 (損益: {loss_830}%)", f"{prices['00830']}", f"{changes['00830']['amount']:+.2f} ({changes['00830']['pct']:+.2f}%)", delta_color="inverse")
    col4.metric(f"📦 00662 (損益: {loss_662}%)", f"{prices['00662']}", f"{changes['00662']['amount']:+.2f} ({changes['00662']['pct']:+.2f}%)", delta_color="inverse")
    st.markdown("---")

    # === 判斷邏輯與燈號 ===
    worst_loss = min(loss_52, loss_830, loss_662)
    cond1 = (36000 <= prices["大盤"] <= 38000) or (worst_loss <= -15.0)
    cond2 = (daily_volume <= 3500) and not break_39384
    cond3 = (3 <= weeks_passed <= 4) and not break_39384
    cond4 = (prices["大盤"] <= 41000) and (candle_shape in ["長下影線", "W底成型"])
    cond5 = all(prices[name] > ma20_now[name] for name in tickers) and all(ma20_now[name] > ma20_prev[name] for name in tickers)

    st.subheader("🎯 五筆資金進場訊號監測")
    def render_card(col, title, condition, success_msg, fail_msg):
        with col:
            if condition: st.success(f"### 🟢 第 {title} 筆\n\n{success_msg}")
            else: st.error(f"### 🔴 鎖定中\n**第 {title} 筆**\n\n{fail_msg}")

    c1, c2, c3, c4, c5 = st.columns(5)
    render_card(c1, "1. 空間極致", cond1, f"已達極端防禦位！\n最深損益: {worst_loss}%", f"未達恐慌區間\n大盤: {prices['大盤']:,.0f}\n最深損益: {worst_loss}%")
    render_card(c2, "2. 量能窒息", cond2, f"量縮見底，賣壓枯竭！\n系統計算預估量: {daily_volume}億\n跌破 39384: {'是' if break_39384 else '否'}", f"預估量: {daily_volume}億\n跌破 39384: {'是' if break_39384 else '否'}")
    render_card(c3, "3. 時間折磨", cond3, f"盤整期滿，底部確認！\n已過 {weeks_passed} 週\n破防守線: {'是' if break_39384 else '否'}", f"已過 {weeks_passed} 週\n破防守線: {'是' if break_39384 else '否'}")
    render_card(c4, "4. 型態確認", cond4, f"第二隻腳打底完成！\n目前型態: {candle_shape}\n指數: {prices['大盤']:,.0f}", f"目前型態: {candle_shape}\n指數: {prices['大盤']:,.0f}")
    render_card(c5, "5. 趨勢反轉", cond5, f"均線共振，右側趨勢啟動！\n站上且均線上揚", f"尚未全面站回或均線下彎")

    st.markdown("---")
    triggered_count = sum([cond1, cond2, cond3, cond4, cond5])
    if triggered_count > 0:
        st.info(f"🚨 **執行紀律：已有 {triggered_count} 筆資金達成觸發條件。請無視市場雜音，冷酷執行對應部位的進場！**")
    else:
        st.warning("☕ **空手觀望：目前尚無任何一筆資金觸發條件。保單借款利息是你買「從容」的成本，請耐心等待。**")