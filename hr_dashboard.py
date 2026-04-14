import os
import logging
from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st

# ──────────────────────────────────────────────────────────────────────────────
# Логгирование
# ──────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Константы: курсы валют (обновить при необходимости)
# ──────────────────────────────────────────────────────────────────────────────
FX_RATES = {
    "RUB": 1.0,       # база
    "EUR": 1 / 95.0,  # 1 EUR = 95 RUB
    "USD": 1 / 90.0,  # 1 USD = 90 RUB
    "GBP": 1 / 120.0, # 1 GBP = 120 RUB
}
FX_UPDATED = "2026-04-01"

TENURE_BINS = [0, 12, 36, 60, float("inf")]
TENURE_LABELS = ["<1 года", "1–3 года", "3–5 лет", "5+ лет"]

RENAME_MAP = {
    "Full Name": "name",
    "City": "city",
    "Department": "department",
    "Position": "position",
    "Legal Entity": "legal_entity",
    "Line Manager": "line_manager",
    "Total Income": "salary",
    "Currency (Total Income)": "currency",
    "salary_satisfaction_by_employee": "salary_satisfaction",
    "last_performance_appraisal_rating_%": "performance",
    "number_of_projects": "projects",
    "satisfaction_level_%": "satisfaction_raw",
}

_REQUIRED_MAIN = {"ID"}
_REQUIRED_DATES = {"ID", "Hire Date"}

# ──────────────────────────────────────────────────────────────────────────────
# Чтение данных
# ──────────────────────────────────────────────────────────────────────────────
def read_sheets(source) -> Tuple[pd.DataFrame, pd.DataFrame]:
    xls = pd.ExcelFile(source)
    for name in ("Summary Data", "Start Date"):
        if name not in xls.sheet_names:
            raise ValueError(f"Лист '{name}' не найден. Доступные: {xls.sheet_names}")
    df_main = pd.read_excel(source, sheet_name="Summary Data", header=0)
    df_dates = pd.read_excel(source, sheet_name="Start Date", header=0)
    missing_main = _REQUIRED_MAIN - set(df_main.columns)
    missing_dates = _REQUIRED_DATES - set(df_dates.columns)
    if missing_main:
        raise ValueError(f"Summary Data: отсутствуют колонки {missing_main}")
    if missing_dates:
        raise ValueError(f"Start Date: отсутствуют колонки {missing_dates}")
    return df_main, df_dates

# ──────────────────────────────────────────────────────────────────────────────
# Очистка и enrich
# ──────────────────────────────────────────────────────────────────────────────
def clean_float(series: pd.Series) -> pd.Series:
    extracted = series.astype(str).str.extract(r"^=?\s*(-?\d+\.?\d*)", expand=False)
    result = pd.to_numeric(extracted, errors="coerce")
    n_bad = int(result.isna().sum() - series.isna().sum())
    if n_bad > 0:
        logger.warning("clean_float: %d значений не удалось распарсить → NaN", n_bad)
    return result

def clean_data(df_main: pd.DataFrame, df_dates: pd.DataFrame) -> pd.DataFrame:
    for _df in (df_main, df_dates):
        _df["ID"] = pd.to_numeric(_df["ID"], errors="coerce")
    df_main = df_main.dropna(subset=["ID"]).copy().astype({"ID": int})
    df_dates = df_dates.dropna(subset=["ID"]).copy().astype({"ID": int})

    df = df_main.merge(
        df_dates[["ID", "Hire Date"]].rename(columns={"Hire Date": "hire_date"}),
        on="ID", how="left"
    )

    if df["hire_date"].isna().all():
        raise ValueError("Все hire_date = NaN после мёрджа. Проверьте ID.")
    df = df.dropna(subset=["hire_date"]).copy()

    df = df.rename(columns={k: v for k, v in RENAME_MAP.items() if k in df.columns})

    if "satisfaction_raw" in df.columns:
        df["satisfaction"] = clean_float(df["satisfaction_raw"])
        df = df[df["satisfaction"].between(0, 1, inclusive="both")].copy()
        df.drop(columns=["satisfaction_raw"], inplace=True, errors="ignore")
    else:
        df["satisfaction"] = 0.5

    if "performance" in df.columns:
        df["performance"] = clean_float(df["performance"])
        df = df[df["performance"].between(0, 1, inclusive="both")].copy()
    else:
        df["performance"] = 0.5

    return df

def enrich_data(df: pd.DataFrame, today: Optional[date] = None) -> pd.DataFrame:
    df = df.copy()
    _today = today or date.today()
    now = datetime.combine(_today, datetime.min.time())

    df["hire_date"] = pd.to_datetime(df["hire_date"], errors="coerce")
    df = df.dropna(subset=["hire_date"]).copy()

    df["tenure_months"] = ((now - df["hire_date"]).dt.days // 30).astype(int)
    df["tenure_group"] = pd.cut(df["tenure_months"], bins=TENURE_BINS, labels=TENURE_LABELS)

    df["currency"] = df["currency"].str.strip().str.upper()
    df["salary_rub"] = df["salary"]

    #Добавляем конвертации (на случай, если вдруг понадобится)
    df["salary_eur"] = df["salary_rub"] * FX_RATES["RUB"] / FX_RATES["EUR"]  #RUB → EUR
    df["salary_usd"] = df["salary_rub"] * FX_RATES["RUB"] / FX_RATES["USD"]  #RUB → USD

    df["snapshot_date"] = _today
    return df

def load_data(source) -> pd.DataFrame:
    df_main, df_dates = read_sheets(source)
    df = clean_data(df_main, df_dates)
    return enrich_data(df)

# ──────────────────────────────────────────────────────────────────────────────
# KPI
# ──────────────────────────────────────────────────────────────────────────────
def calc_kpis(df: pd.DataFrame, currency: str = "RUB") -> dict:
    col = f"salary_{currency.lower()}"
    if col not in df.columns:
        col = "salary_rub"

    sal = df[col]
    zero = {k: 0 for k in ("headcount", "avg_salary", "median_salary", "min_salary", "max_salary", "satisfaction", "performance")}
    if df.empty:
        return zero

    return {
        "headcount": len(df),
        "avg_salary": int(sal.mean()),
        "median_salary": int(sal.median()),
        "min_salary": int(sal.min()),
        "max_salary": int(sal.max()),
        "satisfaction": round(float(df["satisfaction"].mean()), 2),
        "performance": round(float(df["performance"].mean()), 2),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title="HR Dashboard v4.1 — RUB/EUR")
st.title("📊 HR Dashboard v4.1")

DRILL_LEVELS = ["city", "department", "position", "employee"]
COLORS = {
    "city": "#0068C9",
    "department": "#83C9FF",
    "position": "#FF2B2B",
    "employee": "#29B09D",
}

HISTORY_DIR = os.getenv("HR_HISTORY_DIR", "hr_history")
DATA_PATH = os.getenv("HR_DATA_PATH", "")

# ──────────────────────────────────────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────────────────────────────────────
st.sidebar.title("⚙️ Настройки")
currency = st.sidebar.selectbox("Валюта KPI", options=["RUB", "EUR", "USD"], index=0)
st.sidebar.caption(f"🔄 Курсы обновлены: {FX_UPDATED}")
st.sidebar.info(f"📈 Все метрики — в {currency}")

# ──────────────────────────────────────────────────────────────────────────────
# Авторизация
# ──────────────────────────────────────────────────────────────────────────────
def _build_users():
    try:
        raw = st.secrets["users"]
        return {login: {"password": v["password"], "role": v["role"], "name": v.get("name", login)} for login, v in raw.items()}
    except Exception as e:
        return {
            "hr": {"password": "hr123", "role": "HR", "name": "HR Admin"},
            "manager1": {"password": "123", "role": "Manager", "name": "Manager 1"},
            "manager5": {"password": "123", "role": "Manager", "name": "Manager 5"},
        }

USERS = _build_users()

if "user" not in st.session_state:
    st.sidebar.title("🔐 Вход")
    u = st.sidebar.text_input("Логин")
    p = st.sidebar.text_input("Пароль", type="password")
    if st.sidebar.button("Войти"):
        if u in USERS and USERS[u]["password"] == p:
            st.session_state.update(user=u, role=USERS[u]["role"], manager_name=USERS[u]["name"])
            st.rerun()
        else:
            st.sidebar.error("Неверный логин или пароль")
    st.stop()

st.sidebar.caption(f"👤 {st.session_state.user} ({st.session_state.role})")
if st.sidebar.button("Выйти", key="logout"):
    for k in ["user", "role", "manager_name"]:
        st.session_state.pop(k, None)
    st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# Загрузка данных
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="Загрузка данных...")
def _load_cached(key: str, source) -> pd.DataFrame:
    return load_data(source)

source = None
source_key = ""

if DATA_PATH and os.path.exists(DATA_PATH):
    mtime = datetime.fromtimestamp(os.path.getmtime(DATA_PATH))
    source = DATA_PATH
    source_key = f"{DATA_PATH}_{mtime:%Y%m%d%H%M%S}"
    st.sidebar.success(f"✅ Авто-файл: {os.path.basename(DATA_PATH)}")
else:
    uploaded = st.file_uploader("📁 Загрузите Excel-файл (Headcount)", type=["xlsx"])
    if uploaded:
        source = uploaded
        source_key = f"{uploaded.name}_{uploaded.size}"

if source is None:
    st.info("ℹ️ Установите HR_DATA_PATH или загрузите файл вручную.")
    st.stop()

try:
    df = _load_cached(source_key, source)
except Exception as e:
    st.error("❌ Ошибка загрузки данных:")
    st.exception(e)
    st.stop()

if df.empty:
    st.warning("❌ Нет данных после загрузки.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# RLS: фильтрация по роли
# ──────────────────────────────────────────────────────────────────────────────
if st.session_state.role == "Manager":
    df = df[df["line_manager"] == st.session_state.manager_name]
    if df.empty:
        st.warning(f"❌ Нет данных для: {st.session_state.manager_name}")
        st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# Состояние и Drill-down
# ──────────────────────────────────────────────────────────────────────────────
for key, default in [("drill_path", []), ("filters", {})]:
    if key not in st.session_state:
        st.session_state[key] = default.copy()

drill_path = st.session_state.drill_path
filtered_df = df.copy()

for i, value in enumerate(drill_path):
    if i < len(DRILL_LEVELS) and DRILL_LEVELS[i] != "employee":
        filtered_df = filtered_df[filtered_df[DRILL_LEVELS[i]] == value]

# ──────────────────────────────────────────────────────────────────────────────
# Каскадные фильтры
# ──────────────────────────────────────────────────────────────────────────────
st.sidebar.title("🔍 Фильтры")
filters = st.session_state.filters
for key in ["city", "department", "position", "legal_entity", "line_manager", "salary_satisfaction"]:
    filters.setdefault(key, [])

def _opts(col: str, base: pd.DataFrame) -> list:
    return sorted(base[col].dropna().unique().tolist())

def _clean(key: str, opts: list) -> list:
    return [v for v in filters[key] if v in opts]

#1. Город
_city_opts = _opts("city", filtered_df)
filters["city"] = _clean("city", _city_opts)
sel_city = st.sidebar.multiselect("Город", _city_opts, default=filters["city"])
filters["city"] = sel_city
_city_df = filtered_df[filtered_df["city"].isin(sel_city)] if sel_city else filtered_df

#2. Департамент
_dept_opts = _opts("department", _city_df)
filters["department"] = _clean("department", _dept_opts)
sel_dept = st.sidebar.multiselect("Департамент", _dept_opts, default=filters["department"])
filters["department"] = sel_dept
_dept_df = _city_df[_city_df["department"].isin(sel_dept)] if sel_dept else _city_df

#3. Позиция
_pos_opts = _opts("position", _dept_df)
filters["position"] = _clean("position", _pos_opts)
sel_pos = st.sidebar.multiselect("Позиция", _pos_opts, default=filters["position"])
filters["position"] = sel_pos
_pos_df = _dept_df[_dept_df["position"].isin(sel_pos)] if sel_pos else _dept_df

#4. Юрлицо
_ent_opts = _opts("legal_entity", _pos_df)
filters["legal_entity"] = _clean("legal_entity", _ent_opts)
sel_ent = st.sidebar.multiselect("Юр. лицо", _ent_opts, default=filters["legal_entity"])
filters["legal_entity"] = sel_ent
_ent_df = _pos_df[_pos_df["legal_entity"].isin(sel_ent)] if sel_ent else _pos_df

#5. Line Manager
_mgr_opts = _opts("line_manager", _ent_df)
filters["line_manager"] = _clean("line_manager", _mgr_opts)
sel_mgr = st.sidebar.multiselect("Line Manager", _mgr_opts, default=filters["line_manager"])
filters["line_manager"] = sel_mgr
_mgr_df = _ent_df[_ent_df["line_manager"].isin(sel_mgr)] if sel_mgr else _ent_df

#6. Удовл. ЗП
_sat_opts = ["low", "medium", "high"]
filters["salary_satisfaction"] = _clean("salary_satisfaction", _sat_opts)
sel_sat = st.sidebar.multiselect("Удовл. ЗП", _sat_opts, default=filters["salary_satisfaction"])
filters["salary_satisfaction"] = sel_sat
filtered_df = _mgr_df[_mgr_df["salary_satisfaction"].isin(sel_sat)] if sel_sat else _mgr_df

#Сброс
if st.sidebar.button("⏪ Сбросить всё"):
    st.session_state.drill_path = []
    st.session_state.filters = {}
    st.rerun()

if filtered_df.empty:
    st.warning("❌ Нет данных по условиям.")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# Breadcrumbs
# ──────────────────────────────────────────────────────────────────────────────
st.markdown("### 🛤 Навигация")
cols = st.columns(max(len(drill_path) + 1, 2))
if cols[0].button("🌍 Все", key="root"):
    st.session_state.drill_path = []
    st.rerun()
for i, crumb in enumerate(drill_path):
    if cols[i+1].button(f"▶ {crumb}", key=f"crumb_{i}"):
        st.session_state.drill_path = drill_path[:i]
        st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# KPI
# ──────────────────────────────────────────────────────────────────────────────
kpi = calc_kpis(filtered_df, currency=currency)

st.markdown("---")
st.subheader("🎯 Ключевые метрики")
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("Численность", kpi["headcount"])
c2.metric(f"Ср. ЗП {currency}", f"{kpi['avg_salary']:,}")
c3.metric(f"Медиана {currency}", f"{kpi['median_salary']:,}", help="Устойчива к выбросам")
c4.metric(f"Min {currency}", f"{kpi['min_salary']:,}")
c5.metric(f"Max {currency}", f"{kpi['max_salary']:,}")
c6.metric("Удовл.", kpi["satisfaction"])
c7.metric("Перф.", kpi["performance"])

# ──────────────────────────────────────────────────────────────────────────────
# Процентные доли
# ──────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📊 Доли")
col1, col2 = st.columns(2)

with col1:
    city_pct = filtered_df["city"].value_counts(normalize=True).reset_index()
    city_pct.columns = ["city", "percent"]
    city_pct["percent"] = (city_pct["percent"] * 100).round(1)
    fig_city = px.bar(city_pct, x="city", y="percent", text=city_pct["percent"].astype(str)+"%",
                      title="Доля по городам (%)", color_discrete_sequence=[COLORS["city"]])
    fig_city.update_layout(xaxis_categoryorder="total descending")
    st.plotly_chart(fig_city, use_container_width=True)

with col2:
    sat_pct = filtered_df["salary_satisfaction"].value_counts(normalize=True).reset_index()
    sat_pct.columns = ["salary_satisfaction", "percent"]
    sat_pct["percent"] = (sat_pct["percent"] * 100).round(1)
    fig_sat = px.bar(sat_pct, x="salary_satisfaction", y="percent", text=sat_pct["percent"].astype(str)+"%",
                     title="Удовлетворённость ЗП (%)",
                     color="salary_satisfaction",
                     color_discrete_map={"low": "#FF2B2B", "medium": "#FFA500", "high": "#29B09D"},
                     category_orders={"salary_satisfaction": ["low", "medium", "high"]})
    st.plotly_chart(fig_sat, use_container_width=True)

# ──────────────────────────────────────────────────────────────────────────────
# Drill-down графики
# ──────────────────────────────────────────────────────────────────────────────
next_level = DRILL_LEVELS[len(drill_path)] if len(drill_path) < len(DRILL_LEVELS) else None

def _render_drill_bar(df_base, col, level, key):
    data = df_base.groupby(col).size().reset_index(name="count").sort_values("count", ascending=False)
    fig = px.bar(data, x=col, y="count", text="count", color_discrete_sequence=[COLORS[level]])
    fig.update_layout(xaxis_categoryorder="total descending", clickmode="event+select")
    event = st.plotly_chart(fig, on_select="rerun", key=key, use_container_width=True)
    if event and event.get("selection") and event["selection"].get("points"):
        val = str(event["selection"]["points"][0]["x"])
        if val in df_base[col].values:
            st.session_state.drill_path.append(val)
            st.rerun()

st.markdown("---")

if next_level == "city":
    st.subheader("📍 Выберите город")
    _render_drill_bar(filtered_df, "city", "city", "drill_city")
elif next_level == "department":
    st.subheader("🏢 Выберите департамент")
    _render_drill_bar(filtered_df, "department", "department", "drill_dept")
elif next_level == "position":
    st.subheader("👷 Выберите позицию")
    _render_drill_bar(filtered_df, "position", "position", "drill_pos")
elif next_level == "employee" or len(drill_path) >= len(DRILL_LEVELS):
    st.subheader("📋 Сотрудники (сортировка по ЗП ⬇️)")
    emp_cols = [c for c in [
        "name", "city", "department", "position", "legal_entity",
        "salary", "currency", "salary_satisfaction",
        "satisfaction", "performance", "tenure_months"
    ] if c in filtered_df.columns]
    st.dataframe(filtered_df[emp_cols].sort_values("salary", ascending=False), use_container_width=True, hide_index=True)

# ──────────────────────────────────────────────────────────────────────────────
# Аналитика ЗП и стажа (всё в RUB для наглядности)
# ──────────────────────────────────────────────────────────────────────────────
if len(drill_path) < 3:
    st.markdown("---")
    st.subheader("📈 Анализ зарплат и стажа (в RUB)")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown("#### 📦 Зарплаты (RUB)")
        fig_box = px.box(filtered_df, x="department", y="salary_rub",
                         points="outliers", color="position", title="Распределение ЗП")
        fig_box.update_layout(xaxis_tickangle=-45)
        st.plotly_chart(fig_box, use_container_width=True)

    with c2:
        st.markdown("#### ⏱ Стаж (месяцы)")
        fig_tenure = px.box(filtered_df, x="department", y="tenure_months",
                            points="outliers", title="Стаж по департаментам",
                            color_discrete_sequence=["#29B09D"])
        fig_tenure.update_layout(xaxis_tickangle=-45)
        st.plotly_chart(fig_tenure, use_container_width=True)

    with c3:
        st.markdown("#### 📊 Стаж (группы)")
        tenure_counts = filtered_df["tenure_group"].value_counts().reindex(TENURE_LABELS).fillna(0).reset_index()
        tenure_counts.columns = ["tenure_group", "count"]
        fig_t = px.bar(tenure_counts, x="tenure_group", y="count", text="count", title="Стаж: распределение")
        st.plotly_chart(fig_t, use_container_width=True)