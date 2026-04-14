
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, date
from streamlit_plotly_events import plotly_events  # pip install streamlit-plotly-events


# =========================
# CONFIG
# =========================
st.set_page_config(layout="wide", page_title="HR Dashboard")
st.title("📊 HR Dashboard v3.2")


# =========================
# АВТОРИЗАЦИЯ
# =========================
try:
    USERS = {
        "hr": {"password": st.secrets["users"]["hr_password"], "role": "HR"},
        "manager1": {"password": st.secrets["users"]["manager1_password"], "role": "Manager", "name": "Manager 1"},
        "manager5": {"password": st.secrets["users"]["manager5_password"], "role": "Manager", "name": "Manager 5"},
    }
except:
    # Fallback — ТОЛЬКО для разработки
    USERS = {
        "hr": {"password": "hr123", "role": "HR"},
        "manager1": {"password": "123", "role": "Manager", "name": "Manager 1"},
        "manager5": {"password": "123", "role": "Manager", "name": "Manager 5"},
    }

def login():
    st.sidebar.title("🔐 Вход")
    u = st.sidebar.text_input("Логин")
    p = st.sidebar.text_input("Пароль", type="password")
    if st.sidebar.button("Войти"):
        if u in USERS and USERS[u]["password"] == p:
            st.session_state.user = u
            st.session_state.role = USERS[u]["role"]
            st.session_state.manager_name = USERS[u].get("name", "")
            st.rerun()
        else:
            st.sidebar.error("Неверный логин или пароль")

if "user" not in st.session_state:
    login()
    st.stop()


# =========================
# КОНСТАНТЫ
# =========================
FX = {
    "EUR": 1,
    "USD": 0.92,
    "GBP": 1.17,
    "RUB": 0.0137,  # ~1/73
}

DRILL_LEVELS = ["city", "department", "position", "employee"]
COLORS = {
    "city": "#0068C9",
    "department": "#83C9FF",
    "position": "#FF2B2B",
    "employee": "#29B09D",
    "global": "#29B09D",
}

# Инициализация состояния
for key, default in [("drill_path", []), ("filters", {})]:
    if key not in st.session_state:
        st.session_state[key] = default.copy() if isinstance(default, (dict, list)) else default


# =========================
# ЗАГРУЗКА ДАННЫХ С ДВУХ ЛИСТОВ
# =========================
@st.cache_data
def load_data(uploaded_file):
    try:
        xls = pd.ExcelFile(uploaded_file)
        if "Summary Data" not in xls.sheet_names:
            st.error("❌ Лист 'Summary Data' не найден.")
            st.stop()
        if "Start Date" not in xls.sheet_names:
            st.error("❌ Лист 'Start Date' не найден.")
            st.stop()

        # --- Summary Data ---
        df_main = pd.read_excel(uploaded_file, sheet_name="Summary Data", header=0)
        if "ID" not in df_main.columns:
            st.error("❌ Колонка 'ID' не найдена в листе 'Summary Data'.")
            st.stop()

        if "satisfaction_level_%" in df_main.columns:
            df_main.rename(columns={"satisfaction_level_%": "satisfaction_raw"}, inplace=True)

        df_main["ID"] = pd.to_numeric(df_main["ID"], errors="coerce")
        df_main = df_main.dropna(subset=["ID"])
        df_main["ID"] = df_main["ID"].astype(int)

        # --- Start Date ---
        df_dates = pd.read_excel(uploaded_file, sheet_name="Start Date", header=0)
        if "ID" not in df_dates.columns or "Hire Date" not in df_dates.columns:
            st.error("❌ В листе 'Start Date' должны быть 'ID' и 'Hire Date'.")
            st.stop()

        df_dates = df_dates[["ID", "Hire Date"]].copy()
        df_dates.rename(columns={"Hire Date": "hire_date"}, inplace=True)
        df_dates["ID"] = pd.to_numeric(df_dates["ID"], errors="coerce")
        df_dates = df_dates.dropna(subset=["ID"])
        df_dates["ID"] = df_dates["ID"].astype(int)

        # --- Объединение ---
        df = df_main.merge(df_dates, on="ID", how="left")
        if df["hire_date"].isna().all():
            st.warning("⚠️ Все даты найма — NaN. Проверьте соответствие ID.")
        df = df.dropna(subset=["hire_date"])

        # --- Переименование ---
        rename_cols = {
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
        }
        df = df.rename(columns={k: v for k, v in rename_cols.items() if k in df.columns})

        # --- Очистка satisfaction ---
        def clean_float(col):
            return (col.astype(str)
                     .str.replace(r'=.*', '', regex=True)
                     .str.replace(r'[^\d.]', '', regex=True)
                     .replace("", "0")
                     .astype(float))

        if "satisfaction_raw" in df.columns:
            df["satisfaction"] = clean_float(df["satisfaction_raw"])
            df = df[(df["satisfaction"] >= 0) & (df["satisfaction"] <= 1)]
            df.drop(columns=["satisfaction_raw"], inplace=True, errors="ignore")
        else:
            df["satisfaction"] = 0.5

        if "performance" in df.columns:
            df["performance"] = clean_float(df["performance"])
            df = df[(df["performance"] >= 0) & (df["performance"] <= 1)]
        else:
            df["performance"] = 0.5

        # --- Стаж ---
        df["hire_date"] = pd.to_datetime(df["hire_date"], errors="coerce")
        df = df.dropna(subset=["hire_date"])
        df["tenure_months"] = ((datetime.now() - df["hire_date"]).dt.days // 30).astype(int)

        df["tenure_group"] = pd.cut(
            df["tenure_months"],
            bins=[0, 12, 36, 60, float('inf')],
            labels=["<1 года", "1–3 года", "3–5 лет", "5+ лет"]
        )

        # --- ЗП в EUR ---
        df["currency"] = df["currency"].str.strip().str.upper()
        df["salary_eur"] = df["salary"] * df["currency"].map(FX).fillna(1)

        # --- История ---
        df["snapshot_date"] = date.today()

        return df

    except Exception as e:
        st.error("❌ Ошибка при загрузке данных:")
        st.exception(e)
        st.stop()


# =========================
# ЗАГРУЗКА ФАЙЛА
# =========================
uploaded_file = st.file_uploader("📁 Загрузите Excel-файл (Headcount)", type=["xlsx"])

if uploaded_file is None:
    st.info("Ожидается загрузка файла...")
    st.stop()

try:
    df = load_data(uploaded_file)
except Exception as e:
    st.stop()

if df is None or df.empty:
    st.warning("❌ Данные не загружены или пусты.")
    st.stop()


# =========================
# Фильтрация по роли
# =========================
if st.session_state.role == "Manager":
    manager_name = st.session_state.manager_name
    df = df[df["line_manager"] == manager_name]
    if df.empty:
        st.warning(f"❌ Нет данных для руководителя: {manager_name}")
        st.stop()


# =========================
# DRILL-DOWN
# =========================
drill_path = st.session_state.drill_path
filtered_df = df.copy()

for i, value in enumerate(drill_path):
    if i >= len(DRILL_LEVELS):
        break
    level = DRILL_LEVELS[i]
    if level != "employee":
        filtered_df = filtered_df[filtered_df[level] == value]

if filtered_df.empty:
    st.warning("❌ Нет данных по выбранным условиям.")
    if st.button("🔄 Сбросить всё"):
        st.session_state.drill_path = []
        st.session_state.filters = {}
        st.rerun()
    st.stop()


# =========================
# ФИЛЬТРЫ (безопасные — значения сверяются с текущими опциями)
# =========================
filters = st.session_state.filters
for key in ["satisfaction", "position", "legal_entity"]:
    if key not in filters:
        filters[key] = []

# Актуальные опции (после фильтров)
sats_opt = ["low", "medium", "high"]
pos_opt = filtered_df["position"].dropna().unique().tolist()
ent_opt = filtered_df["legal_entity"].dropna().unique().tolist()

# 🔐 Очистка фильтров от устаревших значений
filters["satisfaction"] = [s for s in filters["satisfaction"] if s in sats_opt]
filters["position"] = [p for p in filters["position"] if p in pos_opt]
filters["legal_entity"] = [e for e in filters["legal_entity"] if e in ent_opt]

# Применение
sats = st.multiselect("Удовл. ЗП", options=sats_opt, default=filters["satisfaction"])
positions = st.multiselect("Позиция", options=pos_opt, default=filters["position"])
entities = st.multiselect("Юр.лицо", options=ent_opt, default=filters["legal_entity"])

# Сохранение
filters.update({
    "satisfaction": sats,
    "position": positions,
    "legal_entity": entities
})

# Применение фильтрации
mask = pd.Series([True] * len(filtered_df), index=filtered_df.index)
if sats: mask &= filtered_df["salary_satisfaction"].isin(sats)
if positions: mask &= filtered_df["position"].isin(positions)
if entities: mask &= filtered_df["legal_entity"].isin(entities)
filtered_df = filtered_df[mask]


# =========================
# BREADCRUMBS
# =========================
st.markdown("### 🛤 Навигация")
cols = st.columns(len(drill_path) + 2)

if cols[0].button(" 🌍 Все ", key="root"):
    st.session_state.drill_path = []
    st.session_state.filters = {}
    st.rerun()

for i, crumb in enumerate(drill_path):
    if cols[i+1].button(f" {crumb} ", key=f"crumb_{i}"):
        st.session_state.drill_path = drill_path[:i]
        st.rerun()

if cols[-1].button("⏪ Сброс"):
    st.session_state.drill_path = []
    st.session_state.filters = {}
    st.rerun()


# =========================
# KPI
# =========================
@st.cache_data
def calc_kpis(_df):
    return {
        "headcount": len(_df),
        "avg_salary": int(_df["salary_eur"].mean()) if not _df.empty else 0,
        "median_salary": int(_df["salary_eur"].median()) if not _df.empty else 0,
        "satisfaction": round(_df["satisfaction"].mean(), 2) if not _df.empty else 0.0,
        "performance": round(_df["performance"].mean(), 2) if not _df.empty else 0.0,
    }

kpi_data = calc_kpis(filtered_df)

st.markdown("---")
st.subheader("🎯 Ключевые метрики")
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Численность", kpi_data["headcount"])
k2.metric("Ср. ЗП €", f"{kpi_data['avg_salary']:,}")
k3.metric("Медиана €", f"{kpi_data['median_salary']:,}")
k4.metric("Удовл.", kpi_data["satisfaction"])
k5.metric("Перф.", kpi_data["performance"])


# =========================
# ПРОЦЕНТЫ 📊
# =========================
st.markdown("---")
st.subheader("📊 Доли (проценты)")

col1, col2 = st.columns(2)

with col1:
    st.markdown("### % по городам")
    city_pct = filtered_df["city"].value_counts(normalize=True).reset_index()
    city_pct.columns = ["city", "percent"]
    city_pct["percent"] = (city_pct["percent"] * 100).round(1)
    fig_city = px.bar(city_pct, x="city", y="percent", text=city_pct["percent"].astype(str) + "%",
                      title="Доля сотрудников по городам", color_discrete_sequence=["#0068C9"])
    fig_city.update_layout(xaxis_categoryorder="total descending")
    st.plotly_chart(fig_city, width="stretch")

with col2:
    st.markdown("### % Удовлетворённости ЗП")
    sat_pct = filtered_df["salary_satisfaction"].value_counts(normalize=True).reset_index()
    sat_pct.columns = ["salary_satisfaction", "percent"]
    sat_pct["percent"] = (sat_pct["percent"] * 100).round(1)
    fig_sat = px.pie(sat_pct, names="salary_satisfaction", values="percent", title="Удовлетворённость ЗП (%)")
    st.plotly_chart(fig_sat, width="stretch")


# =========================
# DRILL-DOWN CHARTS
# =========================
@st.cache_data
def get_agg(_df, col):
    return _df.groupby(col).size().reset_index(name="count").sort_values("count", ascending=False)

next_level = DRILL_LEVELS[len(drill_path)] if len(drill_path) < len(DRILL_LEVELS) else None

if next_level == "city":
    st.subheader("📍 Выберите город")
    data = get_agg(filtered_df, "city")
    fig = px.bar(data, x="city", y="count", text="count", title="Города", color_discrete_sequence=[COLORS["city"]])
    fig.update_layout(xaxis_categoryorder="total descending")
    selected = plotly_events(fig, click_event=True, override_height=400)
    if selected:
        val = str(selected[0]["x"])
        if val in filtered_df["city"].values:
            st.session_state.drill_path.append(val)
            st.rerun()

elif next_level == "department":
    st.subheader("🏢 Выберите департамент")
    data = get_agg(filtered_df, "department")
    fig = px.bar(data, x="department", y="count", text="count", title="Департаменты", color_discrete_sequence=[COLORS["department"]])
    fig.update_layout(xaxis_categoryorder="total descending")
    selected = plotly_events(fig, click_event=True, override_height=400)
    if selected:
        val = str(selected[0]["x"])
        if val in filtered_df["department"].values:
            st.session_state.drill_path.append(val)
            st.rerun()

elif next_level == "position":
    st.subheader("👷 Выберите позицию")
    data = get_agg(filtered_df, "position")
    fig = px.bar(data, x="position", y="count", text="count", title="Позиции", color_discrete_sequence=[COLORS["position"]])
    fig.update_layout(xaxis_categoryorder="total descending")
    selected = plotly_events(fig, click_event=True, override_height=400)
    if selected:
        val = str(selected[0]["x"])
        if val in filtered_df["position"].values:
            st.session_state.drill_path.append(val)
            st.rerun()

elif next_level == "employee" or len(drill_path) >= len(DRILL_LEVELS):
    st.subheader("📋 Сотрудники")
    emp_df = filtered_df[[
        "name", "city", "department", "position", "legal_entity", "salary",
        "salary_satisfaction", "satisfaction", "performance", "tenure_months"
    ]].sort_values("salary", ascending=False)
    st.dataframe(emp_df, use_container_width=True, hide_index=True)


# =========================
# BOX PLOT СТАЖА
# =========================
if len(drill_path) < 3:
    st.markdown("---")
    st.subheader("📈 Дополнительные графики")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("### 📦 Зарплаты (€)")
        fig_box = px.box(filtered_df, x="department", y="salary_eur", points="outliers",
                         color="position", title="Распределение ЗП")
        fig_box.update_layout(xaxis_tickangle=-45)
        st.plotly_chart(fig_box, width="stretch")

    with col2:
        st.markdown("### ⏱ Стаж по департаментам")
        fig_tenure = px.box(filtered_df, x="department", y="tenure_months", points="outliers",
                            title="Стаж (месяцы)", color_discrete_sequence=["#29B09D"])
        fig_tenure.update_layout(xaxis_tickangle=-45)
        st.plotly_chart(fig_tenure, width="stretch")

    with col3:
        st.markdown("### 📊 Стаж (группы)")
        tenure_df = filtered_df["tenure_group"].value_counts().reset_index()
        tenure_df.columns = ["tenure_group", "count"]
        fig_t = px.bar(tenure_df, x="tenure_group", y="count", text="count", title="Распределение стажа")
        st.plotly_chart(fig_t, width="stretch")