
import requests

# 模拟车辆的位置（经度、纬度）
origin_lon = 120.890
origin_lat = 31.978

# 模拟充电站 B 的位置（经度、纬度）
station_lon = 120.920
station_lat = 31.970

# OSRM 驾车路线服务
url = (
    "https://router.project-osrm.org/route/v1/driving/"
    f"{origin_lon},{origin_lat};"
    f"{station_lon},{station_lat}"
)

params = {
    "overview": "full",
    "geometries": "geojson"
}

try:
    response = requests.get(
        url,
        params=params,
        timeout=20
    )
    response.raise_for_status()

    data = response.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        print("没有找到路线，请检查坐标或路线服务。")
    else:
        route = data["routes"][0]

        print("路线计算成功！")
        print(f"道路距离：{route['distance'] / 1000:.2f} km")
        print(f"预计行驶时间：{route['duration'] / 60:.1f} 分钟")
        print(
            "路线坐标点数量："
            f"{len(route['geometry']['coordinates'])}"
        )

except requests.RequestException as e:
    print("路线服务连接失败：", e)
    print("请检查网络，稍后重试。")