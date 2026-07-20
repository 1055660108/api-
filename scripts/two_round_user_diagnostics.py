from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BASE_URL = "http://127.0.0.1:8088"
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
USER_COUNT = 50
OUTPUT_PATH = DATA_DIR / f"two_round_{USER_COUNT}_users_{RUN_ID}.json"
PASSWORD = f"Diag_{RUN_ID}_Safe9"
PROMPTS = [
    "清晨薄雾笼罩的山谷中，一列复古蒸汽火车沿着弯曲铁轨缓慢前行，暖色阳光穿过云层，镜头从高空俯拍平滑下降到车头侧面，树叶随风轻动，电影级光影，真实材质，细腻景深",
    "现代玻璃温室里，园艺师穿行在层叠的热带植物之间检查叶片，水珠沿叶脉滑落，镜头先展示空间全景再跟随人物移动，柔和自然光形成斑驳倒影，真实纪录片质感",
    "雨后的古老石板街道反射着街灯，一位撑透明雨伞的旅行者缓步经过书店橱窗，镜头低机位跟拍并轻微环绕，远处电车驶过，空气中有细小雨雾，写实电影画面",
    "海边悬崖上的白色灯塔在日落时分亮起，海浪有节奏地拍打礁石，几只海鸟掠过天空，镜头从海面向上推进并绕灯塔旋转，金蓝色调，宏大而宁静",
    "冬季森林中的木屋窗户透出温暖灯光，细雪缓慢飘落，屋檐冰晶闪烁，一只鹿从树后走到空地，镜头从树枝前景缓慢推近，真实毛发与雪地脚印细节",
    "未来感城市夜景中，无人驾驶有轨电车穿过多层立体街区，霓虹灯在湿润路面形成彩色反射，镜头沿电车平行高速移动后升至城市全景，丰富建筑细节，稳定运镜",
    "宁静湖面上漂着一艘木质小船，远山与晨霞倒映水中，船夫轻轻划桨产生层层涟漪，镜头贴近水面缓慢前进，雾气逐渐散开，自然色彩，诗意写实",
    "传统陶艺工坊里，匠人双手在旋转陶轮上塑造陶罐，泥土纹理和手指动作清晰可见，镜头从手部特写拉到工作室环境，窗外阳光照亮漂浮微尘，温暖纪实风格",
    "宽阔草原上成群骏马迎着夕阳奔跑，鬃毛和草叶随风摆动，镜头贴近地面侧向追踪后升空展示辽阔地貌，尘土在逆光中形成金色光束，真实动态",
    "大型图书馆的弧形书架之间，一位读者沿旋转楼梯向上行走，阳光从高窗投下几何光影，镜头平稳向后移动并逐渐展现穹顶结构，安静典雅，细节丰富",
    "秋日果园中，果农将成熟苹果放入藤篮，树枝在微风中摇曳，远处木制风车缓慢转动，镜头从苹果特写转焦到人物和整片果园，柔和午后光线，生活化写实",
    "深蓝海水中的珊瑚礁生态景观，彩色鱼群穿过阳光形成的光柱，海龟缓慢游向镜头再转身远去，镜头稳定潜行并轻微上仰，水体通透，真实自然纪录片",
    "繁忙面包坊的清晨，烘焙师将刚出炉的面包摆上木架，热气清晰升起，面粉微粒在侧光中漂浮，镜头从烤炉内部拉出并跟随托盘移动，温暖诱人的真实质感",
    "高山观景台上，摄影师架设相机记录云海日出，云层像潮汐般流动，镜头从人物背后缓慢升高展示群峰，天空颜色由深蓝过渡到金橙，壮阔延时摄影感",
    "江南水乡的清晨，小桥下木船缓慢穿过，岸边白墙灰瓦倒映水面，居民推开窗户晾晒布匹，镜头沿河道平滑前行，薄雾柔光，细腻写实风格",
    "现代舞台排练现场，舞者在巨大落地窗前完成连贯旋转和跳跃，长纱随动作流动，镜头环绕人物并保持稳定焦点，夕阳勾勒轮廓，优雅电影质感",
    "沙漠绿洲旁的旅人牵着骆驼沿沙丘脊线前进，风吹起细小沙粒，天空云影掠过地面，镜头远景跟随后切换到低角度侧面，金色自然光，宏大真实",
    "城市屋顶花园里，自动灌溉装置喷出细密水雾，年轻设计师观察垂直绿墙，镜头穿过植物前景推进到城市天际线，清晨阳光，绿色科技与自然融合",
    "春季樱花林间的小路上，骑行者缓慢经过，花瓣随风飘落并掠过镜头，镜头平行跟拍后绕到前方，柔和粉白色调，真实光照，轻盈舒缓氛围",
    "北方极光下的冰湖营地，帐篷透出暖光，旅行者站在湖面仰望绿色光带流动，镜头从冰面裂纹特写缓慢拉升，星空清晰，冷暖对比，震撼写实",
    "古典音乐厅内，弦乐四重奏正在演奏，琴弓动作整齐，舞台木质纹理反射柔光，镜头从大提琴细节平滑移动到全体演奏者和观众席，庄重电影感",
    "清澈溪流穿过苔藓覆盖的原始森林，小型瀑布溅起水雾，一只翠鸟落在枝头观察水面，镜头沿溪流低空推进，阳光透过树冠，丰富自然细节",
    "港口清晨的鱼市逐渐热闹，渔船靠岸后工人搬运装满鲜鱼的木箱，海面泛着银色晨光，镜头从码头全景切入人物动作，真实纪录片节奏",
    "传统茶室里，茶艺师依次温杯、注水、分茶，蒸汽在深色背景前缓慢升腾，镜头以细腻微距记录水流后拉远展示竹帘和庭院，宁静东方美学",
    "雨林树冠上方的悬索桥连接两座观测塔，研究人员携带设备稳步穿过，云雾在山间移动，镜头从桥下仰拍再升至鸟瞰视角，真实探险纪录片风格",
    "夕阳下的现代机场跑道，一架客机缓慢滑行准备起飞，地勤车辆有序移动，热浪使远处景物轻微波动，镜头长焦跟随并平滑横移，真实航空影像",
    "乡村稻田在微风中形成层层绿色波浪，农人沿田埂行走检查水渠，白鹭从近处飞起，镜头从稻穗微距转为宽阔航拍，清新自然，细腻光影",
    "精密钟表工坊中，制表师使用镊子安装微小齿轮，金属零件在工作灯下闪光，镜头超近距离展示机械运转后缓慢拉远，手部动作准确，工业美学",
    "雪山脚下的蓝色冰川湖边，徒步者走过布满碎石的小径，湖面漂浮细小冰块，镜头从背后稳定跟随并转向广阔山峰，冷色自然光，真实旅行电影",
    "夜晚露天美食街升起阵阵热气，厨师快速翻动铁锅，顾客在暖色灯串下交谈，镜头穿行摊位并在食物特写与街道全景间自然转换，活力写实氛围",
]


def request_json(method: str, path: str, payload: dict | None = None, token: str = "", form: dict | None = None) -> dict:
    headers = {}
    data = None
    if token:
        headers["X-API-Token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code} {body}") from exc


def save(report: dict) -> None:
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    config = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    admin_token = str(config["api_token"])
    report = {"run_id": RUN_ID, "started_at": datetime.now(timezone.utc).isoformat(), "users": [], "rounds": [], "snapshots": []}
    users = []
    for index in range(USER_COUNT):
        username = f"diag50{RUN_ID[-6:]}_{index + 1:02d}"
        registered = request_json("POST", "/auth/register", {"username": username, "password": PASSWORD, "confirm_password": PASSWORD})
        users.append({"username": username, "token": str(registered["token"])})
    listing = request_json("GET", "/users?page=1&page_size=100", token=admin_token)
    rows = {str(item["username"]): item for item in listing.get("users", [])}
    for user in users:
        row = rows[user["username"]]
        user["id"] = str(row["id"])
        request_json("POST", f"/users/{user['id']}/points", {"amount": 10}, token=admin_token)
        request_json("PATCH", f"/users/{user['id']}", {"concurrency": 2}, token=admin_token)
        report["users"].append({"id": user["id"], "username": user["username"], "points": 10, "concurrency": 2})
    save(report)
    all_tasks = []
    for round_number in (1, 2):
        if round_number == 2:
            time.sleep(120)
        round_tasks = []
        for index, user in enumerate(users):
            prompt = PROMPTS[index % len(PROMPTS)]
            prompt += f"，采用前景中景远景三层构图，主体动作保持连续，环境中的光线、风、反射和细节产生自然变化，镜头进行稳定的推进、侧向跟随与轻微环绕，画面保持真实比例和电影级质感，第{index + 1}组场景具有独立的色彩节奏"
            if round_number == 2:
                prompt += "，第二段镜头强调环境层次变化、自然运动连续性和稳定的空间关系"
            submitted_at = datetime.now(timezone.utc).isoformat()
            try:
                response = request_json("POST", "/tasks", token=user["token"], form={"prompt": prompt, "ratio": "9:16", "platform": "dola", "model": "Seedance 2.0", "task_type": "video"})
                item = {"round": round_number, "username": user["username"], "task_id": str(response["id"]), "submitted_at": submitted_at, "submit_error": ""}
            except Exception as exc:
                item = {"round": round_number, "username": user["username"], "task_id": "", "submitted_at": submitted_at, "submit_error": str(exc)}
            round_tasks.append(item)
            all_tasks.append({**item, "token": user["token"]})
        report["rounds"].append({"round": round_number, "submitted_at": datetime.now(timezone.utc).isoformat(), "tasks": round_tasks})
        save(report)
    deadline = time.time() + 30 * 60
    while time.time() < deadline:
        counts = {"success": 0, "waiting": 0, "failed": 0, "query_error": 0}
        snapshot_tasks = []
        for item in all_tasks:
            if not item["task_id"]:
                counts["failed"] += 1
                continue
            try:
                result = request_json("GET", f"/tasks/{item['task_id']}", token=item["token"])
                if str(result.get("code") or "") == "2" and result.get("url"):
                    category = "success"
                elif any(marker in str(result.get("text") or "") for marker in ("违规", "无法生成", "生成失败", "多次生成失败", "地区不可用", "请登录")):
                    category = "failed"
                else:
                    category = "waiting"
                counts[category] += 1
                snapshot_tasks.append({"task_id": item["task_id"], "category": category, "code": result.get("code"), "text": result.get("text"), "has_url": bool(result.get("url"))})
            except Exception as exc:
                counts["query_error"] += 1
                snapshot_tasks.append({"task_id": item["task_id"], "category": "query_error", "error": str(exc)})
        report["snapshots"].append({"at": datetime.now(timezone.utc).isoformat(), "counts": counts, "tasks": snapshot_tasks})
        save(report)
        if counts["waiting"] == 0 and counts["query_error"] == 0:
            break
        time.sleep(30)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(report)
    print(str(OUTPUT_PATH))


if __name__ == "__main__":
    main()
