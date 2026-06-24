"""
modules/sensor_server.py

传感器监听层。

这个模块负责接收单片机通过 UDP 发来的 JSON 数据。为了简单和稳定，
这里约定每个 UDP 数据包里都直接放一条 JSON，并且只接收
`distance`、`humidity`、`temperature` 这三个字段。

收到数据后会做三件事：
1. 解析 JSON
2. 整理成统一的 payload
3. 写入存储层并广播给前端
"""
import json
import socket
import threading
from datetime import datetime


class SensorServer:
    def __init__(self, socketio, store, host="0.0.0.0", port=333):
        self.socketio = socketio
        self.store    = store
        self.host     = host
        self.port     = port
        self._thread  = None
        self._running = False

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        print(f"[Sensor] UDP 监听 {self.host}:{self.port}")

    def stop(self):
        self._running = False

    # ── 内部方法 ──────────────────────────────────────────────
    def _listen(self):
        # 这里直接使用原生 UDP socket，单片机/网关端只要发单个 JSON 数据包即可。
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.settimeout(1.0)
            while self._running:
                try:
                    data, addr = srv.recvfrom(4096)
                    self._handle_packet(data, addr)
                except socket.timeout:
                    continue

    def _handle_packet(self, raw_bytes: bytes, addr):
        raw = raw_bytes.decode("utf-8", errors="ignore").strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[Sensor] 无法解析: {raw!r}")
            return

        if not isinstance(data, dict):
            print(f"[Sensor] 数据格式错误: {raw!r}")
            return

        # 只接收约定的三个字段，其他字段一律忽略。
        missing = [key for key in ("distance", "humidity", "temperature") if key not in data]
        if missing:
            print(f"[Sensor] 缺少字段 {missing}: {raw!r}")
            return

        try:
            distance = float(data["distance"])
            humidity = float(data["humidity"])
            temperature = float(data["temperature"])
        except (TypeError, ValueError):
            print(f"[Sensor] 数值转换失败: {raw!r}")
            return

        payload = {
            "distance":   distance,
            "humidity":   humidity,
            "temperature": temperature,
            "timestamp": datetime.now().isoformat(),
        }
        self.store.save_sensor(payload)
        # 广播给所有前端客户端，让仪表盘的数值和图表即时刷新。
        self.socketio.emit("sensor_update", payload)
