import math
import time
from typing import Optional

import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

st.set_page_config(page_title="ChargeFlow", page_icon="🔋", layout="wide")
st.title("🔋 ChargeFlow")
st.subheader("县区级预测性智能充电导航系统")
st.caption("V0.6｜加入相对基础行程的绕行距离与额外时间成本。动态状态仍为模拟数据。")

# -------------------------
# 模拟车辆档案：将来可替换为授权车载数据接口
# 坐标顺序统一使用 (longitude, latitude)
# -------------------------
vehicle = {
    "lat": 31.978,
    "lon": 120.890,
    "soc": 28.0,
    "battery_kwh": 60.0,
    "max_charge_kw": 100.0,
    "energy_use_kwh_per_km": 0.18,
    "reserve_soc": 15.0,
}

# -------------------------
# 模拟充电站数据库：不是实际运营站点
# -------------------------
stations = pd.DataFrame([
    {"name": "模拟充电站 A", "lat": 31.990, "lon": 120.900, "power_kw": 60, "free_piles": 2, "wait_min": 25, "price_kwh": 1.20},
    {"name": "模拟充电站 B", "lat": 31.970, "lon": 120.920, "power_kw": 120, "free_piles": 4, "wait_min": 5, "price_kwh": 1.35},
    {"name": "模拟充电站 C", "lat": 31.985, "lon": 120.875, "power_kw": 30, "free_piles": 1, "wait_min": 0, "price_kwh": 0.95},
])

# -------------------------
# 外部服务：地名 -> 经纬度（Nominatim）
# 每次提交行程才查；结果缓存一天，减少请求
# -------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def geocode_place(query: str):
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "ChargeFlowStudentPrototype/0.5 (educational GIS demo)"}
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "cn",
        "accept-language": "zh-CN",
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        results = response.json()
        if not results:
            return None, f"没有找到地点：{query}"
        item = results[0]
        return {
            "query": query,
            "label": item.get("display_name", query),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
        }, None
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return None, f"地点查询失败（{query}）：{exc}"

# -------------------------
# 道路路线服务：OSRM 多途经点路线
# points 是 tuple[(lon, lat), ...]，方便 Streamlit 缓存
# -------------------------
@st.cache_data(ttl=900, show_spinner=False)
def get_osrm_route(points: tuple):
    if len(points) < 2:
        return None, "路线至少需要起点和终点"

    coordinates = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
    url = f"https://router.project-osrm.org/route/v1/driving/{coordinates}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}
    try:
        response = requests.get(url, params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
        routes = payload.get("routes", [])
        if payload.get("code") != "Ok" or not routes:
            return None, "道路服务没有返回可用路线；地点之间可能没有可用道路连接。"
        route = routes[0]
        coords_lonlat = route["geometry"]["coordinates"]
        route["geometry_latlon"] = [[lat, lon] for lon, lat in coords_lonlat]
        return route, None
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return None, f"道路路线查询失败：{exc}"


def route_summary(route):
    return {
        "distance_km": route["distance"] / 1000.0,
        "drive_min": route["duration"] / 60.0,
        "geometry": route["geometry_latlon"],
        "legs": route.get("legs", []),
    }


def evaluate_station_for_trip(station, trip_points, target_soc, preference):
    """将一个充电站依次插入行程的每一段，选取可行且评分较好的插入位置。"""
    base_stops = [(p["lon"], p["lat"]) for p in trip_points]
    station_point = (float(station["lon"]), float(station["lat"]))
    variants = []

    for insert_index in range(len(base_stops) - 1):
        route_points = (
            base_stops[: insert_index + 1]
            + [station_point]
            + base_stops[insert_index + 1 :]
        )
        route, error = get_osrm_route(tuple(route_points))
        if route is None:
            continue

        legs = route.get("legs", [])
        # 插入点前的 legs 含有“驶入充电站”的那一段
        distance_before_km = sum(leg.get("distance", 0) for leg in legs[: insert_index + 1]) / 1000.0
        distance_after_km = sum(leg.get("distance", 0) for leg in legs[insert_index + 1 :]) / 1000.0
        route_total_km = route["distance"] / 1000.0
        route_drive_min = route["duration"] / 60.0

        battery = vehicle["battery_kwh"]
        initial_energy = battery * vehicle["soc"] / 100.0
        reserve_energy = battery * vehicle["reserve_soc"] / 100.0
        energy_to_station = distance_before_km * vehicle["energy_use_kwh_per_km"]
        arrival_energy = initial_energy - energy_to_station
        arrival_soc = arrival_energy / battery * 100.0

        actual_power = min(vehicle["max_charge_kw"], float(station["power_kw"]))
        if actual_power <= 0:
            continue

        # 目标电量按“到站后希望达到的电量”理解；若抵达时已高于目标，就不需要额外补到更低的电量。
        charge_target_soc = max(float(target_soc), arrival_soc)
        charge_energy = max(0.0, battery * (charge_target_soc - arrival_soc) / 100.0)
        charge_min = charge_energy / actual_power * 60.0
        wait_min = float(station["wait_min"])
        drive_to_charge_min = sum(leg.get("duration", 0) for leg in legs[: insert_index + 1]) / 60.0
        charge_complete_min = drive_to_charge_min + wait_min + charge_min
        full_trip_min = route_drive_min + wait_min + charge_min
        estimated_cost = charge_energy * float(station["price_kwh"])

        energy_after_charge = battery * charge_target_soc / 100.0
        energy_after_trip = energy_after_charge - distance_after_km * vehicle["energy_use_kwh_per_km"]
        arrival_destination_soc = energy_after_trip / battery * 100.0

        can_reach_station = arrival_energy >= reserve_energy
        station_available = int(station["free_piles"]) > 0
        enough_energy_after_charge = energy_after_trip >= reserve_energy
        feasible = can_reach_station and station_available and enough_energy_after_charge

        if not can_reach_station:
            reason = "到站前预计电量低于安全余量"
        elif not station_available:
            reason = "模拟状态下没有空闲桩"
        elif not enough_energy_after_charge:
            reason = "按目标电量充电后，预计无法保留足够电量抵达终点"
        else:
            reason = "通过当前模拟约束检查"

        if preference == "优先省时间":
            objective = full_trip_min
        elif preference == "优先省充电费":
            objective = estimated_cost
        else:
            objective = full_trip_min + estimated_cost * 2.0

        variants.append({
            "route": route,
            "insert_index": insert_index,
            "路线距离(km)": route_total_km,
            "全程驾驶时间(min)": route_drive_min,
            "到充电站行驶时间(min)": drive_to_charge_min,
            "预计等待时间(min)": wait_min,
            "预计充电时间(min)": charge_min,
            "补能完成时间(min)": charge_complete_min,
            "预计全程时间(min)": full_trip_min,
            "估算电费(元)": estimated_cost,
            "到站预计电量(%)": arrival_soc,
            "终点预计剩余电量(%)": arrival_destination_soc,
            "可达充电站": can_reach_station,
            "站点有空闲桩": station_available,
            "终点电量满足安全余量": enough_energy_after_charge,
            "可推荐": feasible,
            "筛选说明": reason,
            "评分": objective,
            "geometry": route["geometry_latlon"],
        })

    if not variants:
        return None

    feasible_variants = [v for v in variants if v["可推荐"]]
    candidate_set = feasible_variants if feasible_variants else variants
    return min(candidate_set, key=lambda v: v["评分"])

# -------------------------
# 出行输入
# -------------------------
st.sidebar.header("出行需求")
with st.sidebar.form("trip_form"):
    destination_query = st.text_input(
        "最终目的地",
        value="江苏省南通市崇川区南通大学启秀校区",
        help="尽量填写城市/区县 + 地点名称，方便地图检索。",
    )
    waypoints_text = st.text_area(
        "途经点（可选，每行一个）",
        value="",
        help="每行输入一个地点，并按计划经过的顺序排列。",
    )
    target_soc = st.slider("充电后目标电量（%）", 20, 100, 80, 5)
    time_choices = {"30 分钟": 30, "1 小时": 60, "2 小时": 120, "4 小时": 240}
    time_label = st.selectbox("可接受的补能完成时间", list(time_choices.keys()), index=2)
    time_budget = time_choices[time_label]
    preference = st.selectbox("推荐偏好", ["综合考虑", "优先省时间", "优先省充电费"])
    submit_trip = st.form_submit_button("📍 规划完整行程", type="primary", use_container_width=True)

if submit_trip:
    destination_query = destination_query.strip()
    waypoint_queries = [line.strip() for line in waypoints_text.splitlines() if line.strip()]
    if not destination_query:
        st.session_state["trip_points"] = None
        st.session_state["trip_error"] = "请先填写最终目的地。"
    else:
        all_queries = waypoint_queries + [destination_query]
        points = [{"query": "模拟车辆当前位置", "label": "模拟车辆当前位置", "lat": vehicle["lat"], "lon": vehicle["lon"]}]
        error = None
        with st.spinner("正在查询目的地和途经点的地图坐标……"):
            for idx, query in enumerate(all_queries):
                # Nominatim 公共服务有访问频率约束；对不同地点查询放慢请求速度。
                if idx > 0:
                    time.sleep(1.1)
                place, place_error = geocode_place(query)
                if place_error:
                    error = place_error
                    break
                points.append(place)
        if error:
            st.session_state["trip_points"] = None
            st.session_state["trip_error"] = (
                error + "。请尝试补充省、市、区县信息后重试。"
            )
        else:
            st.session_state["trip_points"] = points
            st.session_state["trip_error"] = None
            st.session_state["trip_queries"] = all_queries

trip_points = st.session_state.get("trip_points")
trip_error = st.session_state.get("trip_error")

# -------------------------
# 车辆状态与基础资料
# -------------------------
remaining_energy = vehicle["battery_kwh"] * vehicle["soc"] / 100.0
needed_energy_at_current_soc = max(0.0, vehicle["battery_kwh"] * (target_soc - vehicle["soc"]) / 100.0)

c1, c2, c3, c4 = st.columns(4)
c1.metric("模拟车辆剩余电量", f"{vehicle['soc']:.0f}%")
c2.metric("电池容量", f"{vehicle['battery_kwh']:.0f} kWh")
c3.metric("车辆最大可接受充电功率", f"{vehicle['max_charge_kw']:.0f} kW")
c4.metric("充电后目标电量", f"{target_soc}%")
st.caption("车辆位置、电量、车辆能耗、充电站坐标、价格、空闲桩与等待时间为模拟数据。" )

if trip_error:
    st.error(trip_error)

if not trip_points:
    st.info("请在左侧填写最终目的地（途经点可选），点击“规划完整行程”。系统会先解析地名，再计算经过候选充电站的道路路线。")
    st.stop()

# 目的地和途经点形成按顺序排列的路线节点
st.header("一、已解析的行程节点")
route_nodes = pd.DataFrame([
    {"顺序": i, "节点": p["label"], "纬度": p["lat"], "经度": p["lon"]}
    for i, p in enumerate(trip_points)
])
st.dataframe(route_nodes, hide_index=True, width="stretch")

# 先计算不经过充电站的基础行程
base_coords = tuple((p["lon"], p["lat"]) for p in trip_points)
with st.spinner("正在计算基础行程和各候选充电站的路线……"):
    base_route, base_error = get_osrm_route(base_coords)
    base_distance_km = base_route["distance"] / 1000.0 if base_route is not None else None
    base_drive_min = base_route["duration"] / 60.0 if base_route is not None else None
    station_results = []
    station_route_geometries = {}
    for _, station in stations.iterrows():
        result = evaluate_station_for_trip(station, trip_points, target_soc, preference)
        if result is not None:
            station_route_geometries[station["name"]] = result["geometry"]
            station_results.append({
                "站点": station["name"],
                "路线距离(km)": result["路线距离(km)"],
                "相较基础路线增加距离(km)": max(0.0, result["路线距离(km)"] - base_distance_km) if base_distance_km is not None else None,
                "到充电站行驶时间(min)": result["到充电站行驶时间(min)"],
                "额外驾驶时间(min)": max(0.0, result["全程驾驶时间(min)"] - base_drive_min) if base_drive_min is not None else None,
                "预计等待时间(min)": result["预计等待时间(min)"],
                "预计充电时间(min)": result["预计充电时间(min)"],
                "补能完成时间(min)": result["补能完成时间(min)"],
                "充电方案增加总耗时(min)": max(0.0, result["预计全程时间(min)"] - base_drive_min) if base_drive_min is not None else None,
                "预计全程时间(min)": result["预计全程时间(min)"],
                "估算电费(元)": result["估算电费(元)"],
                "到站预计电量(%)": result["到站预计电量(%)"],
                "终点预计剩余电量(%)": result["终点预计剩余电量(%)"],
                "可推荐": result["可推荐"],
                "筛选说明": result["筛选说明"],
                "评分": result["评分"],
                "lat": float(station["lat"]),
                "lon": float(station["lon"]),
                "power_kw": int(station["power_kw"]),
                "free_piles": int(station["free_piles"]),
                "wait_min": int(station["wait_min"]),
                "price_kwh": float(station["price_kwh"]),
            })

if base_error:
    st.warning("基础行程路线计算失败：" + base_error)
else:
    b = route_summary(base_route)
    base_col1, base_col2 = st.columns(2)
    base_col1.metric("不插入充电站的基础行程距离", f"{b['distance_km']:.2f} km")
    base_col2.metric("不插入充电站的基础驾驶时间", f"{b['drive_min']:.1f} 分钟")
    st.caption("后续方案将与这条基础路线比较：额外绕行距离、额外驾驶时间，以及等待和充电造成的总时间增量。OSRM 时间是规划估计，不代表实时路况。")

if not station_results:
    st.error("所有候选站点的路线查询都失败了。请检查网络后重试。")
    st.stop()

result_df = pd.DataFrame(station_results)
valid_candidates = result_df[result_df["可推荐"]].copy()
recommended = None
if not valid_candidates.empty:
    if preference == "优先省时间":
        valid_candidates["展示评分"] = valid_candidates["预计全程时间(min)"]
    elif preference == "优先省充电费":
        valid_candidates["展示评分"] = valid_candidates["估算电费(元)"]
    else:
        tmax = max(float(valid_candidates["预计全程时间(min)"].max()), 1.0)
        cmax = max(float(valid_candidates["估算电费(元)"].max()), 1.0)
        valid_candidates["展示评分"] = 0.7 * valid_candidates["预计全程时间(min)"] / tmax + 0.3 * valid_candidates["估算电费(元)"] / cmax
    valid_candidates = valid_candidates.sort_values("展示评分")
    recommended = valid_candidates.iloc[0]

# -------------------------
# 推荐与预警
# -------------------------
st.header("二、完整行程的充电推荐")
if recommended is not None:
    st.success(
        f"首选：{recommended['站点']}｜预计补能完成约 {recommended['补能完成时间(min)']:.1f} 分钟｜"
        f"预计全程约 {recommended['预计全程时间(min)']:.1f} 分钟｜估算电费 {recommended['估算电费(元)']:.2f} 元。"
    )
    if recommended["补能完成时间(min)"] > time_budget:
        st.warning("首选方案的预计补能完成时间超过了你设定的时间预算。可调整目标电量或选择其他方案。")
    if recommended["终点预计剩余电量(%)"] < vehicle["reserve_soc"]:
        st.warning("按当前简化模型，充电后抵达目的地时的预计剩余电量低于安全余量。")
    show_df = valid_candidates[[
        "站点", "路线距离(km)", "相较基础路线增加距离(km)",
        "到充电站行驶时间(min)", "额外驾驶时间(min)",
        "预计等待时间(min)", "预计充电时间(min)",
        "补能完成时间(min)", "充电方案增加总耗时(min)",
        "预计全程时间(min)", "估算电费(元)",
        "到站预计电量(%)", "终点预计剩余电量(%)"
    ]].copy()
    st.dataframe(show_df.round(2), hide_index=True, width="stretch")
else:
    st.error("当前模拟条件下，没有站点通过全部可行性检查。请检查行程、车辆状态，或调整目标电量。")

with st.expander("查看所有站点及未通过原因"):
    st.dataframe(
        result_df[["站点", "到站预计电量(%)", "终点预计剩余电量(%)", "可推荐", "筛选说明"]].round(2),
        hide_index=True,
        width="stretch",
    )

# -------------------------
# WebGIS 地图：基础行程 + 插入充电站后的完整路线
# -------------------------
st.header("三、完整行程与候选充电站地图")
map_obj = folium.Map(location=[vehicle["lat"], vehicle["lon"]], zoom_start=13, control_scale=True)

# 基础路线用灰色虚线表示
if base_route is not None:
    folium.PolyLine(
        locations=base_route["geometry_latlon"],
        color="#666666",
        weight=3,
        opacity=0.65,
        dash_array="6, 8",
        tooltip="基础行程（不插入充电站）",
    ).add_to(map_obj)

folium.Marker(
    [vehicle["lat"], vehicle["lon"]],
    tooltip="模拟车辆当前位置",
    popup=f"模拟车辆｜剩余电量 {vehicle['soc']:.0f}%",
    icon=folium.Icon(color="blue", icon="car", prefix="fa"),
).add_to(map_obj)

# 标记途经点与最终目的地
for idx, point in enumerate(trip_points[1:], start=1):
    is_final = idx == len(trip_points) - 1
    marker_color = "red" if is_final else "purple"
    marker_label = "最终目的地" if is_final else f"途经点 {idx}"
    folium.Marker(
        [point["lat"], point["lon"]],
        tooltip=f"{marker_label}：{point['query']}",
        popup=point["label"],
        icon=folium.Icon(color=marker_color, icon="flag" if is_final else "map-marker", prefix="fa"),
    ).add_to(map_obj)

# 绘制每个候选充电站的完整行程路线；推荐方案加粗标红
for _, row in result_df.iterrows():
    name = row["站点"]
    geometry = station_route_geometries.get(name, [])
    is_best = recommended is not None and name == recommended["站点"]
    is_feasible = bool(row["可推荐"])
    line_color = "red" if is_best else ("#3182bd" if is_feasible else "#999999")

    if geometry:
        folium.PolyLine(
            locations=geometry,
            color=line_color,
            weight=5 if is_best else 2,
            opacity=0.85 if is_best else 0.45,
            tooltip=f"{name}插入行程后的路线",
        ).add_to(map_obj)

    station_popup = (
        f"<b>{name}</b><br>额定功率：{row['power_kw']} kW<br>"
        f"模拟空闲桩：{row['free_piles']} 个<br>模拟等待：{row['wait_min']} 分钟<br>"
        f"预计补能完成：{row['补能完成时间(min)']:.1f} 分钟<br>"
        f"相较基础路线增加距离：{row['相较基础路线增加距离(km)']:.2f} km<br>"
        f"充电方案增加总耗时：{row['充电方案增加总耗时(min)']:.1f} 分钟<br>"
        f"可推荐：{'是' if is_feasible else '否'}<br>"
        f"说明：{row['筛选说明']}"
    )
    marker_color = "red" if is_best else ("green" if is_feasible else "orange")
    folium.Marker(
        [row["lat"], row["lon"]],
        tooltip=name,
        popup=folium.Popup(station_popup, max_width=320),
        icon=folium.Icon(color=marker_color, icon="plug", prefix="fa"),
    ).add_to(map_obj)

st_folium(map_obj, height=560, width=1100)
st.caption("充电站坐标、价格、空闲桩、等待时间及车辆参数均为模拟数据。目的地/途经点坐标由地名检索服务解析；OSRM 路线是规划路线，不等同于实时交通路线。公共服务可能暂时不可用。")
st.info("V0.6 已显示充电方案相较原始行程增加的距离、驾驶时间和总耗时。当前只模拟单次补能停靠；它是教学原型，不能用于真实车辆安全决策。")
