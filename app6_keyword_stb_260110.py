import streamlit as st
import pandas as pd
import altair as alt
import json
import os
import requests
import re
from collections import Counter
from datetime import datetime, timedelta
from pykrx import stock
from io import BytesIO

# ==============================================================================
# [1] 시스템 설정 및 전역 변수 초기화
# ==============================================================================
# API 인증 정보 및 로컬 저장 경로 설정
NAVER_CLIENT_ID = "UtJVnNmIIhf5KLF4Wssx"
NAVER_CLIENT_SECRET = "RWqDMr5avj"
FINNHUB_API_KEY = "d5ghto1r01ql4f48gcrgd5ghto1r01ql4f48gcs0"
HISTORY_DIR = "history"
os.makedirs(HISTORY_DIR, exist_ok=True)

def init_session_state():
    """Streamlit 세션 상태 초기화: 앱 리프레시 시에도 유지될 기본값 설정"""
    defaults = {
        "tickers_input": "005930, 000660, 005380, 000270, 012330, 035420, 035720, 017670, 207940, 008770, 041510, 122870, 035900, 352820",
        "max_per": 20, "min_up": 70, "min_drop": 30, "min_div": 4.0,
        "df": None, "market": "kr", "saved_portfolio": {}
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

init_session_state()

# ==============================================================================
# [2] 뉴스 수집 및 감성 분석 엔진
# ==============================================================================
def get_sentiment_score(text, market='kr'):
    """텍스트 내 키워드를 매칭하여 감성 점수 산출 (단순 키워드 카운팅 방식)"""
    # 긍정/부정 사전 정의 (한/영 통합)
    pos_kr = ['상승', '돌파', '수익', '호재', '성장', '매수', '긍정', '신고가', '최고', '증가', '성공', '반등', '실적개선', '우수']
    neg_kr = ['하락', '감소', '악재', '손실', '우려', '매도', '부정', '급락', '쇼크', '폭락', '실패', '약세', '부진']
    pos_en = ['up', 'rise', 'growth', 'gain', 'positive', 'buy', 'bullish', 'high', 'jump', 'surpass', 'beat', 'success', 'dividend']
    neg_en = ['down', 'fall', 'loss', 'drop', 'negative', 'sell', 'bearish', 'low', 'slump', 'miss', 'fail', 'concern', 'risk']
    
    score = 0
    text_lower = text.lower()
    pos_words = pos_kr + pos_en
    neg_words = neg_kr + neg_en
    
    for word in pos_words:
        if word in text_lower: score += 1
    for word in neg_words:
        if word in text_lower: score -= 1
    return score

def get_stock_news(query, market='us'):
    """시장에 따라 Finnhub(미국) 또는 Naver(한국/코인) API를 호출하여 뉴스 수집"""
    news_display, full_text_list, total_sentiment = [], [], 0
    try:
        if market == 'us':
            # Finnhub API: 최근 3일간의 기업 뉴스 수집
            url = f"https://finnhub.io/api/v1/company-news?symbol={query}&from={(datetime.now()-timedelta(days=3)).strftime('%Y-%m-%d')}&to={datetime.now().strftime('%Y-%m-%d')}&token={FINNHUB_API_KEY}"
            res = requests.get(url, timeout=5).json()[:3]
            for item in res:
                title, summary = item.get('headline', ''), item.get('summary', '')
                news_display.append(title)
                full_text_list.append(f"{title} {summary}")
                total_sentiment += get_sentiment_score(title + summary, 'us')
        else:
            # Naver 뉴스 검색 API: 관련도 높은 뉴스 3건 수집
            url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display=3&sort=sim"
            headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
            res = requests.get(url, headers=headers, timeout=5).json()
            for item in res.get('items', []):
                title = re.sub(r'<[^>]*>', '', item['title'])
                desc = re.sub(r'<[^>]*>', '', item['description'])
                news_display.append(title)
                full_text_list.append(f"{title} {desc}")
                total_sentiment += get_sentiment_score(title + desc, 'kr')
    except: pass
    
    label = "🙂 긍정" if total_sentiment > 0 else "😟 부정" if total_sentiment < 0 else "🧐 중립"
    return news_display, full_text_list, label, total_sentiment

# ==============================================================================
# [3] 유틸리티 및 데이터 래퍼 (API 통신 및 전처리)
# ==============================================================================
def get_save_file(): 
    """포트폴리오 파일명 생성 (시장별 별도 관리)"""
    return f"portfolio_{st.session_state.market}.json"

def get_safe_trading_day():
    """최근 개장일 확인: 주말/공휴일을 고려하여 데이터가 존재하는 가장 가까운 영업일 반환"""
    for i in range(10):
        target_day = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(target_day, target_day, "005930")
        if not df.empty: return target_day
    return datetime.now().strftime("%Y%m%d")

def get_kr_indicators(ticker):
    """
    네이버 금융에서 PER, PBR, 배당수익률을 추출합니다.
    데이터가 'N/A'이거나 부재할 경우 정확히 0.0을 반환하도록 예외 처리를 강화했습니다.
    """
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).text
        
        def parse_value(pattern, text):
            # 1. 지정된 패턴으로 텍스트 영역 탐색
            match = re.search(pattern, text, re.DOTALL)
            if match:
                raw_data = match.group(1).strip()
                # 2. 데이터가 'N/A'를 포함하거나 비어있으면 0.0 반환
                if 'N/A' in raw_data.upper() or not raw_data:
                    return 0.0
                
                # 3. 숫자, 소수점, 콤마만 남기고 정제
                val_str = re.sub(r'[^\d,.]', '', raw_data).replace(',', '')
                try:
                    return float(val_str) if val_str else 0.0
                except:
                    return 0.0
            return 0.0

        # PER: id="_per" 내부 텍스트 검사
        per = parse_value(r'id="_per">(.+?)<', res)
        # PBR: id="_pbr" 내부 텍스트 검사
        pbr = parse_value(r'id="_pbr">(.+?)<', res)
        # 배당수익률: em 태그 내부 텍스트 검사
        div = parse_value(r'배당수익률.*?<em.*?>(.+?)</em>', res)

        return per, pbr, div
    except:
        return 0.0, 0.0, 0.0

def get_crypto_data(ticker):
    """업비트 Public API를 사용하여 암호화폐 시세 및 52주 고/저점 데이터 수집"""
    try:
        url = f"https://api.upbit.com/v1/ticker?markets=KRW-{ticker}"
        res = requests.get(url, timeout=5).json()
        if not res: return None
        data = res[0]
        return {
            '현재가': data['trade_price'],
            '52주 고점': data['highest_52_week_price'],
            '52주 저점': data['lowest_52_week_price'],
            '24시간 변동률 (%)': round(data['signed_change_rate'] * 100, 2)
        }
    except: return None

# ==============================================================================
# [4] 핵심 키워드 추출 로직
# ==============================================================================
def extract_keywords(full_texts, ticker_name, market='kr'):
    """뉴스 텍스트에서 불용어를 제외하고 가장 빈도가 높은 주요 단어 3개 추출"""
    # 필터링할 무의미한 단어 집합 (Stopwords)
    stop_words = {
        '이번엔', '달라', '스토리', '이슈들', '최대', '올해', '때문', '통해', '대해', '위해',
        '관련', '진행', '이후', '이상', '이하', '기대', '전망', '분석', '기사', '뉴스', '오늘',
        '등', '및', '위한', '기존', '확인', '중', '것', '이', '가', '에', '의', '를', '은', '는',
        '로', '으로', '과', '와', '도', '까지', '부터', '에서', '이다', '입니다', '하고',
        '종목', '주식', '코인', '시장', '투자', '투자자', '거래', '분석', '상승', '하락', 
        '전망', '분기', '실적', '주가', '가격', '비중', '목표', '추천', '매수', '매도', 
        '상황', '이유', '속보', '특징주', '전문가', '전략', '포인트',
        'the', 'and', 'for', 'with', 'from', 'into', 'during', 'including', 'until',
        'against', 'among', 'throughout', 'despite', 'towards', 'upon', 'concerning',
        'about', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
        'could', 'would', 'will', 'also', 'their', 'this', 'that', 'its', 'it', 'to',
        'what', 'which', 'who', 'whom', 'whose', 'when', 'where', 'why', 'how', 'than',
        'stock', 'stocks', 'market', 'markets', 'share', 'shares', 'price', 'prices', 
        'investing', 'investor', 'investors', 'trading', 'coin', 'coins', 'crypto', 
        'cryptocurrency', 'bitcoin', 'ethereum', 'daily', 'report', 'analysis', 
        'forecast', 'update', 'today', 'says', 'said', 'expected', 'likely', 'potential',
        'announced', 'latest', 'breaking', 'news', 'brief', 'summary', 'outlook'
    }

    combined_text = " ".join(full_texts).lower()
    clean_text = re.sub(r'&[a-z]+;', ' ', combined_text)
    clean_text = re.sub(r'[^\w\s]', ' ', clean_text)
    words = clean_text.split()
    
    filtered_words = []
    ticker_parts = set(ticker_name.lower().split())
    
    for w in words:
        # 필터링 조건: 3글자 이상, 숫자가 아님, 불용어 아님, 티커명 미포함
        if len(w) >= 3 and not w.isdigit() and w not in stop_words:
            if not any(part in w for part in ticker_parts if len(part) >= 2):
                filtered_words.append(w)
    
    counts = Counter(filtered_words)
    most_common = counts.most_common(3)
    
    if market == 'us':
        return [word.capitalize() for word, count in most_common]
    return [word for word, count in most_common]

# ==============================================================================
# [5] 메인 사용자 인터페이스 (UI) 및 분석 컨트롤러
# ==============================================================================
st.set_page_config(page_title="주식 투자 판단 대시보드 v13.1", layout="wide")
st.title("📊 주식 투자 판단 대시보드 (v13.1)")

# 사이드바: 시장 선택 및 필터 파라미터 설정
market_choice = st.radio("📌 대상 선택", ["한국주식", "미국주식", "암호화폐"], horizontal=True)
st.session_state.market = 'kr' if market_choice == "한국주식" else 'us' if market_choice == "미국주식" else 'crypto'

if st.session_state.market == 'crypto' and st.session_state.tickers_input.startswith("005930"):
    st.session_state.tickers_input = "BTC, ETH, SOL, XRP, DOGE, ADA"

st.sidebar.header("🎯 필터 기준")
max_per = st.sidebar.slider("PER 최대값", 0, 50, st.session_state.max_per)
min_up = st.sidebar.slider("최소 상승여력 (%)", 0, 100, st.session_state.min_up)
min_drop = st.sidebar.slider("최소 하락률 (%)", 0, 100, st.session_state.min_drop)
min_div = st.sidebar.slider("최소 배당률 (%)", 0.0, 10.0, st.session_state.min_div)
enable_div = st.sidebar.checkbox("배당률로 크기 표현", value=True)

# 설정 저장 및 불러오기 버튼 로직
if st.sidebar.button("💾 포트폴리오 저장"):
    data = {"tickers": st.session_state.tickers_input, "max_per": max_per, "min_up": min_up, "min_drop": min_drop, "min_div": min_div}
    with open(get_save_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    st.sidebar.success("✅ 저장 완료")

if st.sidebar.button("📂 포트폴리오 불러오기"):
    if os.path.exists(get_save_file()):
        with open(get_save_file(), "r", encoding="utf-8") as f:
            p = json.load(f)
            st.session_state.tickers_input = p["tickers"]
            st.session_state.max_per, st.session_state.min_up = p["max_per"], p["min_up"]
            st.session_state.min_drop, st.session_state.min_div = p["min_drop"], p["min_div"]
            st.rerun()

tickers_input = st.text_input("✅ 종목 코드를 입력하세요", st.session_state.tickers_input)
st.session_state.tickers_input = tickers_input
tickers = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]

# ==============================================================================
# [6] 데이터 분석 엔진 (본체)
# ==============================================================================
if st.button("📊 분석 시작"):
    data = []
    latest_day = get_safe_trading_day()
    one_year_ago = (datetime.strptime(latest_day, "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
    
    for ticker in tickers:
        with st.spinner(f'{ticker} 분석 중...'):
            try:
                per, pbr, div, change_24h = 0, 0, 0, 0
                name = ""

                # [시장별 데이터 분기 처리]
                if st.session_state.market == 'crypto':
                    c_data = get_crypto_data(ticker)
                    if not c_data: continue
                    name, price = ticker, c_data['현재가']
                    high, low = c_data['52주 고점'], c_data['52주 저점']
                    change_24h = c_data['24시간 변동률 (%)']
                    query = ticker

                elif st.session_state.market == 'us':
                    params = {'token': FINNHUB_API_KEY, 'symbol': ticker}
                    q = requests.get("https://finnhub.io/api/v1/quote", params=params).json()
                    p = requests.get("https://finnhub.io/api/v1/stock/profile2", params=params).json()
                    f = requests.get("https://finnhub.io/api/v1/stock/metric", params={**params, 'metric': 'all'}).json()
                    
                    if 'c' not in q or q['c'] == 0: continue
                    name, price = p.get('name', ticker), q['c']
                    high = f['metric'].get('52WeekHigh', price) or price
                    low = f['metric'].get('52WeekLow', price) or price
                    per = f['metric'].get('peBasicExclExtraTTM', 0) or 0
                    pbr = f['metric'].get('pbAnnual', 0) or 0
                    div = f['metric'].get('dividendYieldIndicatedAnnual', 0) or 0
                    query = ticker
                    
                else: # 한국 주식 (KRX 라이브러리 활용)
                    name = stock.get_market_ticker_name(ticker)
                    if not name: continue
                    df_p = stock.get_market_ohlcv_by_date(latest_day, latest_day, ticker)
                    price = int(df_p['종가'].iloc[0])
                    hist = stock.get_market_ohlcv_by_date(one_year_ago, latest_day, ticker)
                    high, low = hist['고가'].max(), hist['저가'].min()
                    per, pbr, div = get_kr_indicators(ticker)
                    query = name

                # 뉴스 및 핵심 키워드 수집
                display_titles, analysis_texts, sentiment_label, s_score = get_stock_news(query, st.session_state.market)
                keywords = extract_keywords(analysis_texts, name, st.session_state.market)

                # 데이터 취합 (계산 필드 포함)
                data.append({
                    '종목': ticker, '기업명': name, '현재가': price, '52주 고점': float(high),
                    'PER': round(float(per), 2), 'PBR': round(float(pbr), 2), '배당률 (%)': round(float(div), 2),
                    '24시간 변동률 (%)': round(float(change_24h), 2),
                    '고점대비 (%)': round(((price / high) - 1) * 100, 2) if high != 0 else 0, 
                    '상승여력 (%)': round(((high - price) / (high - low) * 100) if high != low else 0, 2),
                    '뉴스감성': sentiment_label, '감성점수': s_score, 
                    '최근뉴스': display_titles[0] if display_titles else "최근 뉴스 없음",
                    '핵심키워드': ", ".join(keywords) if keywords else "데이터 없음"
                })
            except Exception as e: st.error(f"{ticker} 실패: {e}")
            
    if data:
        df = pd.DataFrame(data)
        
        def classify(row):
            """정량적 지표와 감성 점수를 합산하여 투자 등급 산출 (0~4점)"""
            score = 0
            if row['고점대비 (%)'] <= -min_drop: score += 1
            if row['상승여력 (%)'] >= min_up: score += 1
            if row['감성점수'] > 0: score += 0.5
            
            if st.session_state.market != 'crypto':
                if 0 < row['PER'] <= max_per: score += 1
                if row['배당률 (%)'] >= min_div: score += 1
            
            mapping = {4:'🔥🔥🔥🔥 초초적극 매수', 3:'🔥🔥🔥 초적극 매수', 2:'🔥🔥 적극 매수', 1:'🔥 매수', 0:'👀 관망'}
            return mapping.get(int(score), '👀 관망')

        df['투자등급'] = df.apply(classify, axis=1)
        st.session_state.df = df

# ==============================================================================
# [7] 시각화 및 결과 리포트
# ==============================================================================
df = st.session_state.df
if df is not None:
    # 데이터프레임 열 순서 조정
    cols = list(df.columns)
    if '투자등급' in cols: cols.remove('투자등급')
    if '뉴스감성' in cols: cols.remove('뉴스감성')
    target_idx = cols.index('기업명') + 1
    cols.insert(target_idx, '투자등급')
    cols.insert(target_idx + 1, '뉴스감성')
    
    display_cols = [c for c in cols if c not in ['감성점수', '최근뉴스']]
    display_df = df[display_cols]

    # 스타일링 함수 정의
    def get_color_code(val):
        if '🔥🔥🔥🔥' in val: return 'darkred', 'white'
        if '🔥🔥🔥' in val: return '#ff4b4b', 'white'
        if '🔥🔥' in val: return 'green', 'white'
        if '🔥' in val: return '#DAA520', 'black'
        return '#f0f2f6', 'black'

    def get_sentiment_color(val):
        if '긍정' in val: return 'background-color: #e6f4ea; color: #137333'
        if '부정' in val: return 'background-color: #fce8e6; color: #c5221f'
        return 'background-color: #f1f3f4; color: #3c4043'

    st.subheader("📋 종합 투자 분석 표")

    # 한국 시장 여부 확인 (현재가 포맷팅용)
    is_kr = st.session_state.market == 'kr'

    # 1. 소수점 및 N/A 포맷팅 적용
    styled_df = display_df.style.format({
        'PER': lambda x: "N/A" if x == 0 else f"{x:.2f}",
        'PBR': lambda x: "N/A" if x == 0 else f"{x:.2f}",
        '배당률 (%)': lambda x: "N/A" if x == 0 else f"{x:.2f}",
        '24시간 변동률 (%)': "{:.2f}",
        '고점대비 (%)': "{:.2f}",
        '상승여력 (%)': "{:.2f}",
        '52주 고점': "{:,.0f}" if is_kr else "{:,.2f}", # 국장은 정수, 외장/코인은 소수점 2자리
        '현재가': "{:,.0f}" if is_kr else "{:,.2f}" # 국장은 정수, 외장/코인은 소수점 2자리
    })
    
    # 2. 배경색 및 조건부 서식 추가 적용
    styled_df = styled_df.apply(lambda x: [f"background-color: {get_color_code(v)[0]}; color: {get_color_code(v)[1]}" for v in x], subset=['투자등급'])\
        .applymap(get_sentiment_color, subset=['뉴스감성'])\
        .apply(lambda s: ['background-color: #d1f7d6' if 0 < v <= max_per else '' for v in s], subset=['PER'])\
        .apply(lambda s: ['background-color: #d1e0f7' if v <= -min_drop else '' for v in s], subset=['고점대비 (%)'])\
        .apply(lambda s: ['background-color: #fff0b3' if v >= min_up else '' for v in s], subset=['상승여력 (%)'])\
        .apply(lambda s: ['background-color: #fde2e2' if v >= min_div else '' for v in s], subset=['배당률 (%)'])\
        .apply(lambda s: ['background-color: #e8f0fe' if abs(v) > 5 else '' for v in s], subset=['24시간 변동률 (%)'])
    
    # 3. 화면 출력
    st.dataframe(styled_df, use_container_width=True)


    # 종목별 상세 요약 카드 출력
    st.subheader("🧠 AI 투자 요약")
    for _, row in df.sort_values(by='투자등급', ascending=False).iterrows():
        bg, txt = get_color_code(row['투자등급'])
        st.markdown(f"""
        <div style="background-color: {bg}; color: {txt}; padding: 15px; border-radius: 10px; margin-bottom: 12px; border: 1px solid #ddd;">
            📌 <b>{row['기업명']}</b> ({row['종목']}) | {row['뉴스감성']}<br>
            <div style="margin: 5px 0;">🏷️ <b>주요 키워드:</b> {row['핵심키워드']}</div>
            <div style="margin: 5px 0; font-size: 0.85em; opacity: 0.8;">📰 {row['최근뉴스']}</div>
            <b>현재가:</b> {row['현재가']} | <b>상승여력:</b> {row['상승여력 (%)']}% | <b>등급:</b> {row['투자등급']}
        </div>
        """, unsafe_allow_html=True)

    # Altair 버블 차트 생성
    st.subheader("📈 투자 지표 대시보드")
    
    if st.session_state.market == 'crypto':
        x_title, chart_main_title = '24시간 변동률 (%)', "암호화폐 변동성 대비 상승여력 분석"
        bubble_size_title = "고정 크기" if not enable_div else "배당률(0%)"
    else:
        x_title, chart_main_title = 'PER (주가수익비율)', "PER 대비 상승여력 분석 (버블 크기: 배당률)"
        bubble_size_title = "배당률 크기"

    x_col = '24시간 변동률 (%)' if st.session_state.market == 'crypto' else 'PER'
    per_min, per_max = df[x_col].min(), df[x_col].max()
    up_min, up_max = df['상승여력 (%)'].min(), df['상승여력 (%)'].max()
    per_margin = (per_max - per_min) * 0.15 if per_max != per_min else 5
    up_margin = (up_max - up_min) * 0.15 if up_max != up_min else 5

    size_encoding = alt.Size('배당률 (%)', scale=alt.Scale(range=[120, 700]), legend=alt.Legend(title=bubble_size_title)) if enable_div else alt.value(120)
    domain = ['🔥🔥🔥🔥 초초적극 매수', '🔥🔥🔥 초적극 매수', '🔥🔥 적극 매수', '🔥 매수', '👀 관망']
    range_ = ['darkred', '#ff4b4b', 'green', '#DAA520', "#666769"]

    bubble = alt.Chart(df).mark_circle(opacity=0.7, stroke='white', strokeWidth=1).encode(
        x=alt.X(x_col, title=x_title, scale=alt.Scale(domain=[per_min - per_margin, per_max + per_margin])),
        y=alt.Y('상승여력 (%)', title='상승여력 (%)', scale=alt.Scale(domain=[up_min - up_margin, up_max + up_margin])),
        color=alt.Color('투자등급', scale=alt.Scale(domain=domain, range=range_), legend=alt.Legend(title="투자 등급")),
        size=size_encoding,
        tooltip=['기업명', '종목', x_col, '상승여력 (%)', '배당률 (%)', '뉴스감성', '투자등급']
    ).properties(height=500, title=chart_main_title).interactive()
    
    st.altair_chart(bubble, use_container_width=True)

    # 하단 바 차트 및 데이터 내보내기
    bar = alt.Chart(df).mark_bar().encode(
        x=alt.X('고점대비 (%)', title='고점 대비 하락률 (%)'),
        y=alt.Y('기업명', sort='x', title='종목명'),
        color=alt.Color('고점대비 (%)', scale=alt.Scale(scheme='redblue'), legend=None),
        tooltip=['기업명', '고점대비 (%)']
    ).properties(height=400, title="종목별 고점 대비 하락폭")
    st.altair_chart(bar, use_container_width=True)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Result')
    st.download_button("📥 엑셀 다운로드", data=output.getvalue(), file_name="stock_analysis.xlsx")