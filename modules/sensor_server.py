"""
modules/sensor_server.py

传感器监听层。

这个模块负责接收单片机通过 TCP 发来的温湿度数据。为了简单和稳定，
这里约定每一条数据都是一行 JSON，也就是“JSON over TCP，每行一条”。

收到数据后会做三件事：
1. 解析 JSON
2. 整理成统一的 payload
3. 写入存储层并广播给前端
"""
import json
import socket
import threading
import time
from datetime import datetime


class SensorServer:
    def __init__(self, socketio, store, host="0.0.0.0", port=9000):
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
        print(f"[Sensor] TCP 监听 {self.host}:{self.port}")

    def stop(self):
        self._running = False

    # ── 内部方法 ──────────────────────────────────────────────
    def _listen(self):
        # 这里用原生 socket 而不是更高层封装，是因为单片机端常常更容易实现纯 TCP 发送。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(5)
            srv.settimeout(1.0)
            while self._running:
                try:
                    conn, addr = srv.accept()
                    threading.Thread(
                        target=self._handle_client,
                        args=(conn, addr),
                        daemon=True
                    ).start()
                except socket.timeout:
                    continue

    def _handle_client(self, conn, addr):
        print(f"[Sensor] 单片机接入: {addr}")
        buffer = ""
        with conn:
            conn.settimeout(30)
            while self._running:
                try:
                    # TCP 是流式协议，不保证一条消息一次 recv 就完整到达，
                    # 所以这里要自己维护缓冲区，再按换行切分出完整 JSON。
                    chunk = conn.recv(1024).decode("utf-8", errors="ignore")
                    if not chunk:
                        break
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        self._process(line.strip(), addr)
                except (socket.timeout, ConnectionResetError):
                    break
        print(f"[Sensor] 单片机断开: {addr}")

    def _process(self, raw: str, addr):
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[Sensor] 无法解析: {raw!r}")
            return

        # 统一数据结构，尽量补齐默认值，保证前端和存储层拿到的是稳定格式。
        payload = {
            "temp":      data.get("temp", 0),
            "humi":      data.get("humi", 0),
            "status":    data.get("status", "unknown"),
            "device_id": data.get("device_id", str(addr[0])),
            "timestamp": datetime.now().isoformat(),
        }
        self.store.save_sensor(payload)
        # 广播给所有前端客户端，让仪表盘的数值和图表即时刷新。
        self.socketio.emit("sensor_update", payload)
