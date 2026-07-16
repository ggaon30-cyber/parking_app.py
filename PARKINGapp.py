"""
서울시 공영주차장 안내 지도 앱
- CSV 업로드 / 기본 데이터 사용
- 주소 기반 지도 시각화 (마커 hover 시 주소·요금 표시)
- 자치구별 필터링, 최저요금 주차장 찾기
- 무료 여부(정기권 기준 추정) 표시
- 부가기능: 검색, 자치구별 통계, 결과 다운로드, 즐겨찾기, 주소 미매칭 지오코딩
"""

import io
import re
import time
import unicodedata

import pandas as pd
import streamlit as st
import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# ------------------------------------------------------------------
# 기본 설정
# ------------------------------------------------------------------
st.set_page_config(page_title="서울시 공영주차장 안내", page_icon="🅿️", layout="wide")

DEFAULT_DATA_PATH = "data/서울시_공영주차장_안내_정보.csv"
SEOUL_CENTER = (37.5665, 126.9780)

GU_LIST = [
    "종로구", "중구", "용산구", "성동구", "광진구", "동대문구", "중랑구", "성북구",
    "강북구", "도봉구", "노원구", "은평구", "서대문구", "마포구", "양천구", "강서구",
    "구로구", "금천구", "영등포구", "동작구", "관악구", "서초구", "강남구", "송파구", "강동구",
]

# ------------------------------------------------------------------
# 데이터 로딩 & 전처리
# ------------------------------------------------------------------
def read_csv_any_encoding(file_or_path) -> pd.DataFrame:
    """cp949(EUC-KR) / utf-8(-sig) 등 인코딩을 순서대로 시도해서 읽는다."""
    encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    last_err = None
    for enc in encodings:
        try:
            if hasattr(file_or_path, "seek"):
                file_or_path.seek(0)
            return pd.read_csv(file_or_path, encoding=enc)
        except (UnicodeDecodeError, UnicodeError) as e:
            last_err = e
            continue
    raise last_err


def extract_gu(address: str) -> str:
    if not isinstance(address, str):
        return "정보없음"
    m = re.search(r"([가-힣]+구)", address)
    return m.group(1) if m else "정보없음"


@st.cache_data(show_spinner=False)
def load_and_clean(file_bytes_or_path):
    df = read_csv_any_encoding(file_bytes_or_path)

    # 이름없는/빈 컬럼 제거
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]
    df.columns = [unicodedata.normalize("NFC", str(c)).strip() for c in df.columns]

    # 컬럼명 유연 매핑 (다른 버전의 서울 열린데이터광장 CSV 대비)
    col_map = {}
    for c in df.columns:
        if c in ("주차장명", "명칭", "주차장", "시설명"):
            col_map[c] = "주차장명"
        elif c in ("주소", "소재지", "소재지도로명주소", "도로명주소"):
            col_map[c] = "주소"
        elif "정기권" in c and ("금액" in c or "요금" in c):
            col_map[c] = "월정기권금액"
        elif c in ("위도", "lat", "Lat", "LAT", "latitude"):
            col_map[c] = "위도"
        elif c in ("경도", "lng", "lon", "Lon", "LON", "longitude"):
            col_map[c] = "경도"
        elif "무료" in c:
            col_map[c] = "무료여부_원본"
        elif "토요일" in c or "주말" in c:
            col_map[c] = "주말운영_원본"
        elif "공휴일" in c:
            col_map[c] = "공휴일운영_원본"
    df = df.rename(columns=col_map)

    required = ["주차장명", "주소"]
    for r in required:
        if r not in df.columns:
            raise ValueError(f"필수 컬럼 '{r}' 을(를) CSV에서 찾을 수 없습니다.")

    if "월정기권금액" not in df.columns:
        df["월정기권금액"] = pd.NA
    if "위도" not in df.columns:
        df["위도"] = pd.NA
    if "경도" not in df.columns:
        df["경도"] = pd.NA

    df["월정기권금액"] = pd.to_numeric(df["월정기권금액"], errors="coerce")
    df["위도"] = pd.to_numeric(df["위도"], errors="coerce")
    df["경도"] = pd.to_numeric(df["경도"], errors="coerce")

    df["자치구"] = df["주소"].apply(extract_gu)

    # 무료 여부: 실제 '무료' 컬럼이 있으면 그것을, 없으면 정기권 금액 0원을 '무료(추정)'으로 표시
    if "무료여부_원본" in df.columns:
        df["무료여부"] = df["무료여부_원본"].astype(str)
    else:
        df["무료여부"] = df["월정기권금액"].apply(
            lambda x: "무료(정기권 0원 기준 추정)" if pd.notna(x) and x == 0
            else ("유료" if pd.notna(x) else "정보없음")
        )

    for col, label in [("주말운영_원본", "주말운영"), ("공휴일운영_원본", "공휴일운영")]:
        if col in df.columns:
            df[label] = df[col].astype(str)

    df = df.reset_index(drop=True)
    df["_row_id"] = df.index
    return df


# ------------------------------------------------------------------
# 지오코딩 (주소 -> 위경도), 결측치 보완용
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def geocode_addresses(addresses: tuple) -> dict:
    """Nominatim(OpenStreetMap)으로 주소를 위경도로 변환. 배포 환경 인터넷 연결 필요."""
    from geopy.geocoders import Nominatim
    from geopy.extra.rate_limiter import RateLimiter

    geolocator = Nominatim(user_agent="seoul_parking_app")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.0)

    results = {}
    for addr in addresses:
        query = f"서울특별시 {addr}"
        try:
            loc = geocode(query)
            if loc:
                results[addr] = (loc.latitude, loc.longitude)
            else:
                results[addr] = (None, None)
        except Exception:
            results[addr] = (None, None)
    return results


# ------------------------------------------------------------------
# 사이드바 - 데이터 입력
# ------------------------------------------------------------------
st.sidebar.title("🅿️ 데이터")
uploaded = st.sidebar.file_uploader("주차장 정보 CSV 업로드", type=["csv"])

try:
    if uploaded is not None:
        raw_bytes = uploaded.read()
        df = load_and_clean(io.BytesIO(raw_bytes))
        st.sidebar.success(f"업로드한 파일을 사용 중입니다. ({len(df):,}건)")
    else:
        df = load_and_clean(DEFAULT_DATA_PATH)
        st.sidebar.info(f"기본 제공 데이터를 사용 중입니다. ({len(df):,}건)")
except Exception as e:
    st.sidebar.error(f"CSV를 읽는 중 오류가 발생했습니다: {e}")
    st.stop()

missing_coord = df["위도"].isna() | df["경도"].isna()
n_missing = int(missing_coord.sum())

st.sidebar.markdown("---")
st.sidebar.caption(
    f"위·경도 보유: {len(df) - n_missing:,}건 / 결측: {n_missing:,}건"
)

do_geocode = st.sidebar.checkbox(
    "주소로 결측 위치 보완하기 (느림, 인터넷 필요)", value=False,
    help="체크하면 위·경도가 없는 주소를 OpenStreetMap(Nominatim)으로 변환합니다. 건수가 많으면 시간이 오래 걸릴 수 있어 필터링 후 사용을 권장합니다."
)

# ------------------------------------------------------------------
# 사이드바 - 필터
# ------------------------------------------------------------------
st.sidebar.title("🔍 필터")
gu_options = ["전체"] + sorted(df["자치구"].unique().tolist())
selected_gu = st.sidebar.selectbox("자치구 선택", gu_options)

only_free = st.sidebar.checkbox("무료 주차장만 보기", value=False)

max_price = int(df["월정기권금액"].max(skipna=True) or 0)
price_range = st.sidebar.slider(
    "월 정기권 금액 범위 (원)", 0, max_price if max_price > 0 else 100000,
    (0, max_price if max_price > 0 else 100000), step=10000
)

search_kw = st.sidebar.text_input("주차장명 검색")

# ------------------------------------------------------------------
# 필터 적용
# ------------------------------------------------------------------
filtered = df.copy()
if selected_gu != "전체":
    filtered = filtered[filtered["자치구"] == selected_gu]
if only_free:
    filtered = filtered[filtered["무료여부"].str.contains("무료", na=False)]
filtered = filtered[
    filtered["월정기권금액"].isna()
    | filtered["월정기권금액"].between(price_range[0], price_range[1])
]
if search_kw:
    filtered = filtered[filtered["주차장명"].str.contains(search_kw, case=False, na=False)]

# 선택적 지오코딩 (필터링된 결과 대상으로만 수행 → 호출 수 최소화)
if do_geocode:
    need = filtered[filtered["위도"].isna() | filtered["경도"].isna()]
    if len(need) > 0:
        if len(need) > 200:
            st.sidebar.warning(f"보완 대상이 {len(need)}건으로 많습니다. 자치구 등으로 좁혀서 사용해 주세요.")
        else:
            with st.spinner(f"주소 {len(need)}건의 위치 정보를 찾는 중..."):
                addr_tuple = tuple(need["주소"].fillna("").unique())
                geo_result = geocode_addresses(addr_tuple)
                for idx, row in need.iterrows():
                    lat, lng = geo_result.get(row["주소"], (None, None))
                    if lat is not None:
                        filtered.loc[idx, "위도"] = lat
                        filtered.loc[idx, "경도"] = lng

# ------------------------------------------------------------------
# 헤더 & 요약 지표
# ------------------------------------------------------------------
st.title("🅿️ 서울시 공영주차장 안내 지도")
st.caption("자치구를 선택하고 지도에서 마커에 마우스를 올려 주소와 요금을 확인하세요.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("검색된 주차장", f"{len(filtered):,}곳")
c2.metric("무료(추정) 주차장", f"{int(filtered['무료여부'].str.contains('무료', na=False).sum()):,}곳")
valid_price = filtered["월정기권금액"].dropna()
c3.metric("평균 월 정기권 금액", f"{int(valid_price.mean()):,}원" if len(valid_price) else "정보없음")
mappable = filtered.dropna(subset=["위도", "경도"])
c4.metric("지도에 표시 가능", f"{len(mappable):,}곳")

if n_missing > 0 and not do_geocode:
    st.info(
        f"전체 데이터 중 {n_missing:,}건은 CSV에 위·경도 정보가 없어 지도에 표시되지 않습니다. "
        "사이드바의 '주소로 결측 위치 보완하기'를 체크하면 보완을 시도합니다."
    )

st.markdown("---")

# ------------------------------------------------------------------
# 탭 구성
# ------------------------------------------------------------------
tab_map, tab_cheapest, tab_stats, tab_table = st.tabs(
    ["🗺️ 지도", "💰 최저요금 찾기", "📊 자치구 통계", "📋 데이터 표"]
)

# --- 지도 탭 ---
with tab_map:
    if len(mappable) == 0:
        st.warning("지도에 표시할 위치 정보가 있는 데이터가 없습니다.")
    else:
        if selected_gu != "전체" and len(mappable) > 0:
            center = [mappable["위도"].mean(), mappable["경도"].mean()]
            zoom = 13
        else:
            center = list(SEOUL_CENTER)
            zoom = 11

        fmap = folium.Map(location=center, zoom_start=zoom, tiles="cartodbpositron")
        cluster = MarkerCluster().add_to(fmap)

        for _, row in mappable.iterrows():
            price = row["월정기권금액"]
            price_txt = f"{int(price):,}원" if pd.notna(price) else "정보없음"
            free_txt = row["무료여부"]

            extra_lines = []
            if "주말운영" in row and pd.notna(row.get("주말운영")):
                extra_lines.append(f"주말운영: {row['주말운영']}")
            if "공휴일운영" in row and pd.notna(row.get("공휴일운영")):
                extra_lines.append(f"공휴일운영: {row['공휴일운영']}")
            extra_html = "<br>".join(extra_lines)

            tooltip_html = (
                f"<b>{row['주차장명']}</b><br>"
                f"{row['주소']}<br>"
                f"월정기권: {price_txt} ({free_txt})"
                + (f"<br>{extra_html}" if extra_html else "")
            )
            color = "green" if "무료" in str(free_txt) else "blue"
            folium.Marker(
                location=[row["위도"], row["경도"]],
                tooltip=folium.Tooltip(tooltip_html),
                popup=folium.Popup(tooltip_html, max_width=280),
                icon=folium.Icon(color=color, icon="car", prefix="fa"),
            ).add_to(cluster)

        st_folium(fmap, width=None, height=560, key="parking_map")
        st.caption("🟢 무료(추정)  🔵 유료 — 마커를 클릭하거나 마우스를 올리면 상세정보가 보입니다.")

# --- 최저요금 탭 ---
with tab_cheapest:
    st.subheader("자치구 내 최저요금 주차장")
    if selected_gu == "전체":
        st.info("사이드바에서 자치구를 선택하면 해당 구의 최저요금 주차장을 보여드립니다. 아래는 전체 기준 상위 결과입니다.")
    priced = filtered.dropna(subset=["월정기권금액"]).sort_values("월정기권금액")
    top_n = st.slider("표시할 개수", 3, 30, 10)
    show_cols = ["주차장명", "주소", "자치구", "월정기권금액", "무료여부"]
    st.dataframe(
        priced[show_cols].head(top_n).rename(columns={"월정기권금액": "월 정기권 금액(원)"}),
        use_container_width=True, hide_index=True,
    )
    if len(priced) > 0:
        cheapest = priced.iloc[0]
        st.success(
            f"💡 가장 저렴한 곳: **{cheapest['주차장명']}** "
            f"({cheapest['주소']}) — {int(cheapest['월정기권금액']):,}원"
        )

# --- 통계 탭 ---
with tab_stats:
    st.subheader("자치구별 요약")
    gu_stats = (
        df.groupby("자치구")
        .agg(주차장수=("주차장명", "count"), 평균정기권금액=("월정기권금액", "mean"),
             무료개수=("무료여부", lambda s: s.str.contains("무료", na=False).sum()))
        .sort_values("주차장수", ascending=False)
    )
    gu_stats["평균정기권금액"] = gu_stats["평균정기권금액"].round(0)
    col_a, col_b = st.columns(2)
    with col_a:
        st.caption("자치구별 주차장 수")
        st.bar_chart(gu_stats["주차장수"])
    with col_b:
        st.caption("자치구별 평균 월 정기권 금액(원)")
        st.bar_chart(gu_stats["평균정기권금액"])
    st.dataframe(gu_stats, use_container_width=True)

# --- 데이터 표 탭 ---
with tab_table:
    st.subheader("필터링된 데이터")
    st.dataframe(filtered.drop(columns=["_row_id"], errors="ignore"), use_container_width=True, hide_index=True)
    csv_bytes = filtered.drop(columns=["_row_id"], errors="ignore").to_csv(index=False).encode("utf-8-sig")
    st.download_button("⬇️ 현재 필터 결과 CSV 다운로드", data=csv_bytes,
                        file_name="필터링된_공영주차장.csv", mime="text/csv")

# ------------------------------------------------------------------
# 관심 주차장(즐겨찾기) - 세션 상태 기반
# ------------------------------------------------------------------
st.markdown("---")
st.subheader("⭐ 관심 주차장")

if "favorites" not in st.session_state:
    st.session_state.favorites = set()

fav_candidates = filtered["주차장명"].tolist()
if fav_candidates:
    pick = st.multiselect("관심 주차장으로 등록", fav_candidates,
                           default=[f for f in st.session_state.favorites if f in fav_candidates])
    st.session_state.favorites = set(pick)

if st.session_state.favorites:
    fav_df = df[df["주차장명"].isin(st.session_state.favorites)]
    st.dataframe(
        fav_df[["주차장명", "주소", "자치구", "월정기권금액", "무료여부"]],
        use_container_width=True, hide_index=True,
    )
else:
    st.caption("아직 등록된 관심 주차장이 없습니다.")

st.markdown("---")
st.caption(
    "데이터 출처: 사용자 업로드 CSV (서울시 공영주차장 안내 정보). "
    "위치 정보가 없는 주소는 지오코딩(OpenStreetMap Nominatim) 결과로 보완될 수 있으며 실제 위치와 다소 차이가 있을 수 있습니다."
)
