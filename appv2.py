import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from datetime import datetime, timezone, timedelta


# ==============================
# Streamlit 設定
# ==============================

st.set_page_config(
    page_title="台股抄底觀測站",
    layout="wide"
)

st.title("🎯 台股五大關鍵底部觀測面板")
st.markdown("---")


# ==============================
# 格式化成交金額
# 單位：億元
# ==============================

def format_volume(yi):

    if yi is None:
        return "資料不足"

    if yi >= 10000:
        return f"{yi/10000:.3f} 兆元"

    return f"{yi:,.2f} 億元"



# ==============================
# TWSE 官方 API
#
# 取得：
# 1. 大盤指數
# 2. 漲跌點
# 3. 漲跌幅
# 4. 成交金額
# ==============================


@st.cache_data(ttl=300)
def fetch_twse_market_data():

    try:

        url = (
            "https://www.twse.com.tw/"
            "exchangeReport/MI_INDEX"
            "?response=json&type=MS"
        )


        headers = {
            "User-Agent":
            "Mozilla/5.0"
        }


        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )


        data = response.json()


        if data.get("stat") != "OK":

            return None


        result = {

            "index": None,
            "change": None,
            "change_pct": None,
            "turnover": None

        }


        for table in data["tables"]:


            title = table.get("title","")


            rows = table.get("data",[])


            # ----------------------
            # 大盤指數
            # ----------------------

            if "發行量加權股價指數" in title:


                row = rows[0]


                result["index"] = float(
                    row[1].replace(",","")
                )

                result["change"] = float(
                    row[2].replace(",","")
                )

                result["change_pct"] = float(
                    row[3].replace(",","")
                    .replace("%","")
                )



            # ----------------------
            # 成交金額
            # ----------------------

            if "成交統計" in title:


                for r in rows:

                    if "成交金額" in r[0]:

                        amount = (
                            r[1]
                            .replace(",","")
                        )


                        result["turnover"] = (
                            float(amount)
                            /
                            100000000
                        )



        return result



    except Exception as e:

        st.error(
            f"TWSE API錯誤:{e}"
        )

        return None




# ==============================
# Yahoo Finance 盤中成交量
#
# 注意：
# Yahoo給的是成交股數
# 需估算成交金額
# ==============================


@st.cache_data(ttl=60)
def fetch_yahoo_intraday_volume():


    try:


        df = yf.download(
            "^TWII",
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=False
        )


        if df.empty:

            return None



        volume = (
            df["Volume"]
            .sum()
        )


        price = (
            df["Close"]
            .mean()
        )


        # 成交金額
        # 股數 × 股價

        turnover = (
            volume
            *
            price
            /
            100000000
        )


        return round(turnover,2)



    except Exception as e:


        return None





# ==============================
# 盤中成交量預估
#
# 依照交易時間推估全天
# ==============================


def calculate_estimated_volume(
        current_volume
):


    if current_volume is None:

        return None


    tw_timezone = timezone(
        timedelta(hours=8)
    )


    now = datetime.now(
        tw_timezone
    )


    start = datetime.strptime(
        "09:00",
        "%H:%M"
    )


    end = datetime.strptime(
        "13:30",
        "%H:%M"
    )


    current = datetime.strptime(
        now.strftime("%H:%M"),
        "%H:%M"
    )



    if current < start:


        return current_volume



    if current > end:


        return current_volume



    elapsed = (
        current-start
    ).seconds / 60



    if elapsed <= 0:

        return current_volume



    estimated = (
        current_volume
        *
        270
        /
        elapsed
    )


    return round(
        estimated,
        2
    )




# ==============================
# Yahoo ETF歷史資料
# ==============================


@st.cache_data(ttl=3600)
def fetch_stock_history(symbol):


    try:


        df = yf.Ticker(
            symbol
        ).history(
            period="1y"
        )


        return df



    except:

        return pd.DataFrame()




# ==============================
# 技術指標計算
# ==============================


def calculate_indicators(df):


    df = df.copy()


    df["5MA"] = (
        df["Close"]
        .rolling(5)
        .mean()
    )


    df["10MA"] = (
        df["Close"]
        .rolling(10)
        .mean()
    )


    df["20MA"] = (
        df["Close"]
        .rolling(20)
        .mean()
    )


    df["120MA"] = (
        df["Close"]
        .rolling(120)
        .mean()
    )



    # KD

    low9 = (
        df["Low"]
        .rolling(9)
        .min()
    )


    high9 = (
        df["High"]
        .rolling(9)
        .max()
    )


    df["RSV"] = (
        (df["Close"]-low9)
        /
        (high9-low9)
        *
        100
    )


    df["K"] = (
        df["RSV"]
        .ewm(
            com=2,
            adjust=False
        )
        .mean()
    )


    df["D"] = (
        df["K"]
        .ewm(
            com=2,
            adjust=False
        )
        .mean()
    )


    return df
# ==============================
# K線圖繪製
# ==============================


def draw_professional_chart(df, title_name):


    df = calculate_indicators(df)


    latest = df.iloc[-1]


    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[
            0.6,
            0.2,
            0.2
        ]
    )


    # K線

    fig.add_trace(

        go.Candlestick(

            x=df.index,

            open=df["Open"],

            high=df["High"],

            low=df["Low"],

            close=df["Close"],

            name="K線"

        ),

        row=1,
        col=1
    )


    # 均線

    for ma,color in [
        ("5MA","yellow"),
        ("10MA","hotpink"),
        ("20MA","deepskyblue"),
        ("120MA","mediumaquamarine")
    ]:


        fig.add_trace(

            go.Scatter(

                x=df.index,

                y=df[ma],

                name=f"{ma}:{latest[ma]:.2f}",

                line=dict(
                    width=1,
                    color=color
                )

            ),

            row=1,
            col=1
        )



    # 成交量

    fig.add_trace(

        go.Bar(

            x=df.index,

            y=df["Volume"],

            name="成交量"

        ),

        row=2,
        col=1
    )



    # KD

    fig.add_trace(

        go.Scatter(

            x=df.index,

            y=df["K"],

            name=f"K:{latest['K']:.2f}"

        ),

        row=3,
        col=1
    )


    fig.add_trace(

        go.Scatter(

            x=df.index,

            y=df["D"],

            name=f"D:{latest['D']:.2f}"

        ),

        row=3,
        col=1
    )



    fig.update_layout(

        title=f"📊 {title_name} 技術分析",

        template="plotly_dark",

        height=750,

        xaxis_rangeslider_visible=False

    )


    return fig





# ==============================
# 主程式
# ==============================


tickers = {

    "0052":"0052.TW",

    "00830":"00830.TW",

    "00662":"00662.TW"

}



# ==============================
# 讀取資料
# ==============================


with st.spinner(
    "正在同步 TWSE + Yahoo 資料..."
):


    market = fetch_twse_market_data()


    intraday_volume = (
        fetch_yahoo_intraday_volume()
    )


    history = {}


    for name,symbol in tickers.items():

        df = fetch_stock_history(symbol)


        if not df.empty:

            history[name] = df





# ==============================
# 檢查資料
# ==============================


if market is None:


    st.error(
        "❌ 無法取得證交所資料"
    )

    st.stop()





# ==============================
# 成交量判斷
# ==============================


close_volume = market["turnover"]


estimated_volume = None


if intraday_volume:


    estimated_volume = (
        calculate_estimated_volume(
            intraday_volume
        )
    )





# ==============================
# 側邊欄
# ==============================


st.sidebar.header(
    "⚙️ 參數設定"
)



st.sidebar.markdown(
"""
### 成交金額

"""
)


st.sidebar.write(

f"""
🏛️ TWSE 收盤正式成交：

**{format_volume(close_volume)}**

"""
)



if estimated_volume:


    st.sidebar.info(

        f"""
📡 Yahoo盤中估算：

{format_volume(estimated_volume)}

"""

    )



else:


    st.sidebar.warning(
        "目前無盤中資料"
    )






# ==============================
# 顯示大盤
# ==============================


st.subheader(
    "📈 大盤即時狀態"
)



col1,col2,col3 = st.columns(3)



col1.metric(

    "加權指數",

    f"{market['index']:,.2f}"

)



col2.metric(

    "今日漲跌",

    f"{market['change']:+.2f}"

)



col3.metric(

    "漲跌幅",

    f"{market['change_pct']:+.2f}%"

)





st.markdown("---")





# ==============================
# K線
# ==============================


twii = yf.Ticker(
    "^TWII"
).history(
    period="1y"
)


if not twii.empty:


    st.plotly_chart(

        draw_professional_chart(
            twii,
            "加權指數"
        ),

        use_container_width=True

    )





# ==============================
# ETF 面板
# ==============================


st.subheader(
    "📦 ETF監控"
)


cols = st.columns(3)



for col,(name,df) in zip(
    cols,
    history.items()
):


    latest = df.iloc[-1]


    change = (
        latest["Close"]
        -
        df["Close"].iloc[-2]
    )


    pct = (
        change
        /
        df["Close"].iloc[-2]
        *
        100
    )


    col.metric(

        name,

        f"{latest['Close']:.2f}",

        f"{change:+.2f} ({pct:+.2f}%)"

    )







# ==============================
# 五筆資金策略
# ==============================


st.markdown("---")

st.subheader(
    "🎯 底部五筆資金進場監測"
)



# 使用目前大盤

index = market["index"]



# 第一關：
# 巨量換手

cond1 = (
    close_volume >= 10000
)



# 第二關：
# 窒息量

cond2 = (
    close_volume <=3500
)



# 第三關：
# 時間

weeks = st.slider(

    "距離起跌週數",

    0,

    8,

    0

)



cond3 = (
    weeks >=3
)





# 第四關：
# 型態

shape = st.selectbox(

    "今日型態",

    [

        "黑K",

        "紅K",

        "長下影線",

        "W底",

        "放量長紅"

    ]

)



cond4 = shape in [

    "長下影線",

    "W底",

    "放量長紅"

]





# 第五關：
# 均線

ma20 = (
    twii["Close"]
    .rolling(20)
    .mean()
)


cond5 = (

    twii["Close"].iloc[-1]

    >

    ma20.iloc[-1]

)





conditions = [

    cond1,

    cond2,

    cond3,

    cond4,

    cond5

]




names=[

"第一筆：巨量換手",

"第二筆：窒息量",

"第三筆：時間折磨",

"第四筆：型態確認",

"第五筆：右側突破"

]




cols = st.columns(5)



for col,name,cond in zip(
    cols,
    names,
    conditions
):


    with col:

        if cond:

            st.success(
                f"🟢\n{name}"
            )

        else:

            st.error(
                f"🔴\n{name}"
            )





# ==============================
# 最終提示
# ==============================


count=sum(conditions)


st.markdown("---")



if count:

    st.info(

        f"""
🚨 目前已有 {count}/5 個條件成立

請依照原定資金規劃執行

"""

    )

else:

    st.warning(

        """
☕ 尚未達成進場條件

等待市場給出訊號

"""

    )