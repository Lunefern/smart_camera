"""
智能摄像头管理系统主入口。

这个文件负责把三条主线串起来：
1. HTTP 页面与 API
2. WebSocket 实时推送
3. 后台采集、传感器监听和关键帧处理线程

当前默认配置是“先让项目在本地跑起来”，因此优先使用仓库内置的
样例视频 `AviToFrame/tmp.avi` 作为视频源；如果环境变量
`SMART_CAMERA_SOURCE` 存在，则会优先使用用户指定的视频源。
"""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from flask_cors import CORS

from modules.sensor_server import SensorServer
from modules.camera_stream import CameraStream
from modules.frame_processor import FastFrameProcessor
from modules.data_store import DataStore

# 项目根目录，后续所有相对路径都统一从这里派生，避免不同工作目录导致路径错乱。
BASE_DIR = Path(__file__).resolve().parent

# 默认样例视频。这样即使没有接真实摄像头，页面也能直接看到画面和关键帧效果。
DEFAULT_SAMPLE_VIDEO = BASE_DIR / "AviToFrame" / "tmp.avi"

# 视频源优先级：
# 1) 环境变量 SMART_CAMERA_SOURCE
# 2) 仓库内置样例视频 tmp.avi
# 3) 本机摄像头 0
DEFAULT_STREAM_SOURCE = os.getenv(
    "SMART_CAMERA_SOURCE",
    str(DEFAULT_SAMPLE_VIDEO if DEFAULT_SAMPLE_VIDEO.exists() else 0),
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"
CORS(app)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ── 全局模块实例 ─────────────────────────────────────────────
# 这里采用“单例式全局实例”而不是在请求内动态创建，原因是：
# 后台采集、传感器监听和帧处理都依赖长期运行的线程，
# 如果每个请求都重新构建对象，会导致重复启动线程和资源泄漏。
store    = DataStore()
sensor   = SensorServer(socketio, store, host="0.0.0.0", port=9000)
camera   = CameraStream(socketio, stream_url=DEFAULT_STREAM_SOURCE)
frame_processor = FastFrameProcessor(
    socketio,
    store,
    output_dir=str(BASE_DIR / "AviToFrame" / "frames_local"),
)
# 关键帧处理器和摄像头共享同一个帧队列。
# 摄像头负责“采集”，处理器负责“消费并判断是否保存”。
frame_processor.bind_queue(camera.frame_queue)

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
    # 再启动关键帧处理器，避免处理器启动时队列还是空的。
    camera.start()
    frame_processor.start()
    return jsonify({"status": "started"})

@app.route("/api/camera/stop", methods=["POST"])
def api_camera_stop():
    # 停止时反过来收尾：
    # 先停止处理器，避免它继续从队列取帧；
    # 再关闭摄像头释放底层视频句柄。
    frame_processor.stop()
    camera.stop()
    return jsonify({"status": "stopped"})

# ── WebSocket 事件 ────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    print(f"[WS] 客户端接入: {request.sid}")
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
def on_disconnect():
    print(f"[WS] 客户端断开: {request.sid}")

@socketio.on("request_frame")
def on_request_frame():
    """客户端请求单帧画面。

    这个事件适合做“手动刷新”或“页面初始化兜底”。
    它不启动新的处理流程，只是把当前最新帧直接推回前端。
    """
    frame_b64 = camera.get_frame_base64()
    if frame_b64:
        emit("frame", {"image": frame_b64})

# ── 启动 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    # 本地启动时直接把后台服务都拉起来，这样打开浏览器就能看到效果。
    sensor.start()          # 监听单片机传来的温湿度数据
    camera.start()          # 从样例视频 / 摄像头开始采集帧
    frame_processor.start()  # 持续抽取关键帧并向前端推送
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
