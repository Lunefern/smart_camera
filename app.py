"""
智能摄像头管理系统主入口。

这个文件负责把三条主线串起来：
1. HTTP 页面与 API
2. WebSocket 实时推送
3. 后台采集、传感器监听和关键帧处理线程

当前默认配置是“先把 YOLO 识别画面跑通”，因此默认优先使用本地摄像头 `0`。
如果环境变量 `SMART_CAMERA_SOURCE` 存在，则会优先使用用户指定的视频源。
RTSP 仍然保留在切换菜单里，后续可以一键切过去做扩展调试。
"""
from __future__ import annotations

import os
import platform
import queue
import socket
import sys
from pathlib import Path

try:
    from flask import Flask, render_template, jsonify, request
    from flask_socketio import SocketIO, emit
    from flask_cors import CORS
except ModuleNotFoundError as exc:
    # 依赖缺失是最常见的启动失败原因，直接抛 ModuleNotFoundError 很难区分
    # “没装依赖”和“装到了另一个解释器（比如忘了激活 venv）”。
    # 这里把缺的包、当前解释器、以及项目里是否已有可用 venv 都讲清楚。
    _project_dir = Path(__file__).resolve().parent
    _venv_python = _project_dir / ".venv" / "bin" / "python"
    _in_venv = sys.prefix != sys.base_prefix
    _lines = [
        f"\n[启动失败] 缺少 Python 依赖: {exc.name}",
        f"当前解释器: {sys.executable}",
        f"Python 版本: {sys.version.split()[0]}",
        f"是否在虚拟环境中: {'是' if _in_venv else '否'}",
        "",
    ]
    if _venv_python.exists() and not _in_venv:
        # 项目里已经有 .venv 却没激活，这是最常见的情况，直接给可复制的命令。
        _lines += [
            f"检测到项目内已有虚拟环境: {_project_dir / '.venv'}",
            "依赖装在这个环境里，所以要用它来启动（三选一）：",
            "    source .venv/bin/activate && python app.py",
            "    .venv/bin/python app.py",
            "    ./run.sh",
        ]
    else:
        _lines += [
            "请在项目根目录安装依赖:",
            "    python -m pip install -r requirements.txt",
            "（树莓派推荐先建虚拟环境: python -m venv .venv && source .venv/bin/activate）",
        ]
    _lines += [
        "",
        "如果依赖确实已经装过，请确认运行 app.py 的解释器和安装依赖的是同一个。",
        "\n",
    ]
    raise SystemExit("\n".join(_lines))

from modules.sensor_server import SensorServer
from modules.camera_stream import CameraStream
from modules.frame_processor import FastFrameProcessor
from modules.data_store import DataStore
from modules.yolo_processor import YoloProcessor

# 项目根目录，后续所有相对路径都统一从这里派生，避免不同工作目录导致路径错乱。
BASE_DIR = Path(__file__).resolve().parent

# 默认 RTSP 流。建议先启动本地 MediaMTX，然后再通过 ffmpeg 或摄像头推流到这个地址。
DEFAULT_RTSP_STREAM = "rtsp://localhost:8554/webcam"

# YOLO 模型默认路径，优先使用仓库内置的 `best.pt` 权重。
YOLO_MODEL_PATH = BASE_DIR / "YOLOModel" / "best.pt"
# 同目录下的 ONNX 权重，供 OpenCV DNN 后端使用（树莓派推荐）。
YOLO_ONNX_PATH = BASE_DIR / "YOLOModel" / "best.onnx"

# 推理后端：auto / torch / dnn / off
# - auto：ARM 设备自动用 dnn（避免 arm 版 torch 触发 Illegal instruction）
# - off ：完全不加载模型，只显示原始画面，适合算力很小的设备
YOLO_BACKEND = os.getenv("SMART_CAMERA_YOLO_BACKEND", "auto")

# 推理分辨率。树莓派 4B 建议 416 或 320，能明显降低 CPU 占用和延迟。
YOLO_IMGSZ = int(os.getenv("SMART_CAMERA_YOLO_IMGSZ", "640"))

# 每 N 帧推理一次。2GB 内存 / 低算力设备上可以设成 2~3，画面仍然够用。
YOLO_INTERVAL = int(os.getenv("SMART_CAMERA_YOLO_INTERVAL", "1"))

# 默认保留的关键帧数量。超过这个数量时会自动删除最旧的图片。
FRAME_STORAGE_LIMIT = int(os.getenv("FRAME_STORAGE_LIMIT", "50"))

# 关键帧判定阈值：相邻帧平均灰度差异超过该值才认为“画面有变化”并保存。
# 光线抖动、传感器噪声大的设备（比如树莓派接的普通 USB 摄像头）可以调高，
# 否则会每秒写十几张图，既费 CPU 又费 SD 卡寿命。
FRAME_DIFF_THRESHOLD = float(os.getenv("SMART_CAMERA_DIFF_THRESHOLD", "4.0"))

# 视频源优先级：
# 1) 环境变量 SMART_CAMERA_SOURCE
# 2) 本机摄像头 0
# 3) 本地 RTSP 流 rtsp://localhost:8554/webcam
# 4) 本机摄像头 1~3
DEFAULT_STREAM_SOURCE = os.getenv(
    "SMART_CAMERA_SOURCE",
    "0",
)

VIDEO_SOURCE_OPTIONS = [
    {
        "label": "本地 RTSP（MediaMTX）",
        "value": DEFAULT_RTSP_STREAM,
        "description": "默认开发流，来自本机 MediaMTX:8554/webcam",
    },
    {
        "label": "本机摄像头 0",
        "value": "0",
        "description": "直接接本地摄像头设备",
    },
    {
        "label": "本机摄像头 1",
        "value": "1",
        "description": "直接接本地摄像头设备",
    },
    {
        "label": "本机摄像头 2",
        "value": "2",
        "description": "直接接本地摄像头设备",
    },
    {
        "label": "本机摄像头 3",
        "value": "3",
        "description": "直接接本地摄像头设备",
    },
    {
        "label": "自定义 RTSP / 文件",
        "value": "__custom__",
        "description": "后续可在前端输入框中填写其他地址或视频文件路径",
    },
]

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"
CORS(app)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ── 全局模块实例 ─────────────────────────────────────────────
# 这里采用“单例式全局实例”而不是在请求内动态创建，原因是：
# 后台采集、传感器监听和帧处理都依赖长期运行的线程，
# 如果每个请求都重新构建对象，会导致重复启动线程和资源泄漏。
store    = DataStore()
# 端口不写死：由 SENSOR_PORT 环境变量控制（Linux 上 <1024 的端口需要特权）。
sensor   = SensorServer(socketio, store, host="0.0.0.0")
camera   = CameraStream(socketio, stream_url=DEFAULT_STREAM_SOURCE)
frame_queue = queue.Queue(maxsize=2)
camera.register_consumer_queue(frame_queue)

# 关键帧处理器和 YOLO 处理器共享同一个帧队列。
# 摄像头负责“采集”，处理器负责“消费并判断是否保存/识别”。
yolo = YoloProcessor(
    socketio,
    store,
    model_path=str(YOLO_MODEL_PATH),
    onnx_path=str(YOLO_ONNX_PATH),
    imgsz=YOLO_IMGSZ,
    inference_interval=YOLO_INTERVAL,
    backend=YOLO_BACKEND,
    source_name="local_camera_0",
)
yolo.bind_queue(frame_queue)

frame_processor = FastFrameProcessor(
    socketio,
    store,
    output_dir=str(BASE_DIR / "AviToFrame" / "frames_local"),
    diff_threshold=FRAME_DIFF_THRESHOLD,
    max_saved_images=FRAME_STORAGE_LIMIT,
)
frame_processor.bind_queue(frame_queue)

# ── HTTP 路由 ─────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/sensor/latest")
def api_sensor_latest():
    """返回最新传感器数据。

    前端页面加载时会先请求这个接口，用于尽快显示最近一次有效状态。
    如果暂时没有传感器数据，则返回空字典。
    """
    return jsonify(store.get_latest_sensor())

@app.route("/api/sensor/history")
def api_sensor_history():
    """返回最近 N 条传感器历史。

    参数 `n` 是可选的，用于前端图表回放或扩展页面展示。
    """
    n = request.args.get("n", 50, type=int)
    return jsonify(store.get_sensor_history(n))

@app.route("/api/detections")
def api_detections():
    """返回最近检测记录。

    当前项目已经把 YOLO 路径替换成关键帧处理，但这里保留接口，
    方便未来重新启用检测算法时不必改前端调用方式。
    """
    n = request.args.get("n", 20, type=int)
    return jsonify(store.get_detections(n))

@app.route("/api/camera/start", methods=["POST"])
def api_camera_start():
    # 启动顺序：
    # 先启动摄像头，确保 frame_queue 中开始有帧流入；
    # 再启动 YOLO 和关键帧处理器，避免处理器启动时队列还是空的。
    frame_processor.reset_state()
    camera.start()
    yolo.start()
    frame_processor.start()
    return jsonify({"status": "started"})

@app.route("/api/camera/stop", methods=["POST"])
def api_camera_stop():
    # 停止时反过来收尾：
    # 先停止处理器，避免它继续从队列取帧；
    # 再关闭摄像头释放底层视频句柄。
    yolo.stop()
    frame_processor.stop()
    camera.stop()
    return jsonify({"status": "stopped"})

@app.route("/api/camera/source", methods=["GET"])
def api_camera_source():
    """返回当前视频源状态和可选切换项。"""
    payload = camera.get_source_status()
    payload["options"] = VIDEO_SOURCE_OPTIONS
    payload["default_source"] = DEFAULT_STREAM_SOURCE
    return jsonify(payload)

@app.route("/api/camera/source", methods=["POST"])
def api_camera_source_update():
    """切换当前视频源。

    允许前端提交 `source`，既可以是预设值，也可以是自定义 RTSP 或文件路径。
    """
    data = request.get_json(silent=True) or request.form or {}
    source = data.get("source", "")
    if isinstance(source, str):
        source = source.strip()

    if source is None or source == "":
        return jsonify({"error": "missing source"}), 400

    if source == "0":
        source = 0

    was_running = camera._running
    camera.switch_source(source)
    yolo.source_name = str(source)
    socketio.emit("yolo_status_update", yolo.get_status())
    frame_processor.reset_state()
    if was_running and not frame_processor._running:
        frame_processor.start()

    socketio.emit("camera_source_update", camera.get_source_status() | {"options": VIDEO_SOURCE_OPTIONS})
    return jsonify({"status": "ok", **camera.get_source_status(), "options": VIDEO_SOURCE_OPTIONS})

@app.route("/api/frame/storage", methods=["GET"])
def api_frame_storage():
    """返回关键帧存储状态。"""
    return jsonify(frame_processor.get_storage_status())

@app.route("/api/frame/storage", methods=["POST"])
def api_frame_storage_update():
    """调整自动保留的关键帧数量。"""
    data = request.get_json(silent=True) or request.form or {}
    max_saved_images = data.get("max_saved_images", FRAME_STORAGE_LIMIT)
    try:
        max_saved_images = int(max_saved_images)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid max_saved_images"}), 400

    frame_processor.set_max_saved_images(max_saved_images)
    frame_processor.cleanup_old_images(max_saved_images)
    status = frame_processor.get_storage_status()
    socketio.emit("frame_storage_update", status)
    return jsonify({"status": "ok", **status})

@app.route("/api/frame/cleanup", methods=["POST"])
def api_frame_cleanup():
    """手动清理旧图片。"""
    data = request.get_json(silent=True) or request.form or {}
    keep_count = data.get("keep_count")
    if keep_count is not None and keep_count != "":
        try:
            keep_count = int(keep_count)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid keep_count"}), 400
    else:
        keep_count = None

    result = frame_processor.cleanup_old_images(keep_count)
    payload = {
        **frame_processor.get_storage_status(),
        **result,
    }
    socketio.emit("frame_storage_update", payload)
    return jsonify({"status": "ok", **payload})

@app.route("/api/system/status")
def api_system_status():
    """返回运行环境与各模块状态，方便在树莓派这类设备上快速排查问题。"""
    return jsonify({
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "yolo": yolo.get_status(),
        "sensor": sensor.get_status(),
        "camera": camera.get_source_status(),
        "frames": {
            "output_dir": str(BASE_DIR / "AviToFrame" / "frames_local"),
            "max_saved_images": FRAME_STORAGE_LIMIT,
        },
    })

# ── WebSocket 事件 ────────────────────────────────────────────
# 注意：flask-socketio 5.x 会先按 `handler(auth)` 调用 connect 回调，多带一个 auth 参数。
# 之前写成 on_connect() 不带参数，会先抛 TypeError；库内部虽会回退到 `handler()` 再调一次，
# 但一旦回退路径里再抛异常（比如读关键帧目录失败），连接就会整体失败、页面收不到任何状态。
# 所以这里显式声明可选参数，不依赖库内部的回退行为。
@socketio.on("connect")
def on_connect(auth=None):
    print(f"[WS] 客户端接入: {request.sid}")
    # 连接回调里任何一步失败都会让客户端连不上，这里逐块兜底。
    try:
        emit("camera_source_update", camera.get_source_status() | {"options": VIDEO_SOURCE_OPTIONS})
    except Exception as exc:
        print(f"[WS] 推送视频源状态失败: {exc}")
    try:
        emit("frame_storage_update", frame_processor.get_storage_status())
    except Exception as exc:
        print(f"[WS] 推送关键帧存储状态失败: {exc}")
    emit("yolo_status_update", yolo.get_status())
    latest_yolo = yolo.get_latest_payload()
    if latest_yolo:
        emit("detection_result", latest_yolo)
    latest_sensor = store.get_latest_sensor()
    if latest_sensor:
        # 客户端刚连上时就推送一份“最后已知状态”，避免页面需要等待下一次采样。
        emit("sensor_update", latest_sensor)
    latest_frame = frame_processor.get_latest_payload()
    if latest_frame:
        # 如果关键帧处理器已经产出了结果，优先让页面直接显示这个结果。
        emit("frame_update", latest_frame)
    else:
        # 如果还没有关键帧结果，就退回到最新原始帧，保证页面不会空白太久。
        frame_b64 = camera.get_frame_base64()
        if frame_b64:
            emit("frame", {"image": frame_b64})

@socketio.on("disconnect")
def on_disconnect(reason=None):
    # 5.x 的 disconnect 回调同样可能带一个 reason 参数。
    print(f"[WS] 客户端断开: {request.sid} ({reason})")

@socketio.on("request_frame")
def on_request_frame():
    """客户端请求单帧画面。

    这个事件适合做“手动刷新”或“页面初始化兜底”。
    它不启动新的处理流程，只是把当前最新帧直接推回前端。
    """
    latest_yolo = yolo.get_latest_payload()
    if latest_yolo:
        emit("detection_result", latest_yolo)
        return

    frame_b64 = camera.get_frame_base64()
    if frame_b64:
        emit("frame", {"image": frame_b64})

# ── 启动 ──────────────────────────────────────────────────────
def _port_is_free(port: int) -> bool:
    """检查 TCP 端口能否绑定，用于在启动后台线程之前给出明确提示。

    这样端口冲突时就不会先抢一遍摄像头：摄像头是独占设备，抢到再退出会让
    正在运行的实例看起来“摄像头坏了”。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _abort_if_port_busy(port: int) -> None:
    """端口被占用时打印可执行的解决办法并退出。"""
    if _port_is_free(port):
        return
    print()
    print(f"[启动失败] Web 端口已被占用: {port}")
    print("  最常见的原因：已经有一个 app.py 在运行，它同时占着这个端口和摄像头。")
    print("  1) 查看占用进程：")
    print(f"       Linux:   ss -ltnp | grep :{port}")
    print(f"       Windows: netstat -ano | findstr :{port}")
    print("  2) 停掉旧实例：")
    print("       Linux:   pkill -f '[a]pp.py'")
    print(f"  3) 或者换一个端口启动（摄像头仍然只能被一个实例打开）：")
    print(f"       SMART_CAMERA_PORT={port + 1} ./run.sh")
    print()
    raise SystemExit(1)


if __name__ == "__main__":
    # 本地启动时直接把后台服务都拉起来，这样打开浏览器就能看到效果。
    print("=" * 60)
    print(f"[App] Python {platform.python_version()} / {platform.machine()} / {platform.system()}")
    print(f"[App] YOLO 后端: {YOLO_BACKEND} (imgsz={YOLO_IMGSZ}, interval={YOLO_INTERVAL})")
    print(f"[App] 视频源: {DEFAULT_STREAM_SOURCE}")
    print(f"[App] 传感器端口: {sensor.port}（Linux 上 <1024 会自动回退到 1333）")
    print("=" * 60)
    # 先确认端口可用，再启动会独占摄像头的采集线程。
    web_port = int(os.getenv("SMART_CAMERA_PORT", "5000"))
    _abort_if_port_busy(web_port)
    sensor.start()          # 监听单片机传来的温湿度数据
    camera.start()          # 从本地摄像头 / RTSP 开始采集帧
    yolo.start()            # YOLO 识别结果直接推给前端
    frame_processor.start()  # 持续抽取关键帧并向前端推送
    # Flask 的 debug 模式默认会启用自动重载，这会让后台线程在父进程和子进程里各启动一次。
    # 对视频流和 UDP 监听来说，这通常会造成重复连接、重复消费和难以排查的解码噪声。
    # 这里保留 debug 页面，但关闭 reloader，保证采集链路只启动一份。
    debug_enabled = os.getenv("SMART_CAMERA_DEBUG", "1") not in ("0", "false", "False")
    socketio.run(
        app,
        host="0.0.0.0",
        port=web_port,
        debug=debug_enabled,
        use_reloader=False,
        # flask-socketio 在 threading 模式下只要 stdin 不是终端（nohup / setsid / systemd /
        # Docker 都是这种情况）就会抛 “The Werkzeug web server is not designed to run in
        # production”。本项目就是局域网自用 + Werkzeug 开发服务器，这里显式关掉该检查，
        # 否则后台启动必然失败。
        allow_unsafe_werkzeug=True,
    )
