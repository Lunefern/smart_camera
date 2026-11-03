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

端口说明（在 Linux / 树莓派上很重要）：
`333` 属于 1024 以下的“特权端口”，普通用户绑定时会直接抛 PermissionError，
而且原来是抛在线程里，表现为监听线程静默退出。所以这里做了两件事：
1. 端口可配置（环境变量 `SENSOR_PORT` 或 `SMART_CAMERA_SENSOR_PORT`）
2. 绑定失败只打印处理建议并禁用传感器，不影响摄像头 / YOLO / 网页

推荐做法：把单片机推送目标端口改成 1333 之类的非特权端口，然后
`SENSOR_PORT=1333 python app.py` 启动。
"""
import json
import os
import socket
import threading
from datetime import datetime

# 默认端口：保持和单片机原有约定一致。
DEFAULT_PORT = 333


def _env_port() -> int:
    raw = os.getenv("SENSOR_PORT") or os.getenv("SMART_CAMERA_SENSOR_PORT") or str(DEFAULT_PORT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"[Sensor] 端口配置无效: {raw!r}，回退到 {DEFAULT_PORT}")
        return DEFAULT_PORT


class SensorServer:
    def __init__(self, socketio, store, host="0.0.0.0", port=None):
        self.socketio = socketio
        self.store    = store
        self.host     = host
        self.port     = _env_port() if port is None else int(port)
        self._thread  = None
        self._running = False
        self._bound   = False
        self._error   = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        print(f"[Sensor] UDP 监听 {self.host}:{self.port}")

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "is_running": self._running,
            "is_bound": self._bound,
            "error": self._error,
        }

    # ── 内部方法 ──────────────────────────────────────────────
    def _listen(self):
        # 这里直接使用原生 UDP socket，单片机/网关端只要发单个 JSON 数据包即可。
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            self._fail(f"创建 UDP socket 失败: {exc}")
            return

        with sock:
            if not self._bind(sock):
                return

            self._bound = True
            self._error = None
            sock.settimeout(1.0)
            while self._running:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError as exc:
                    # 单个数据包出错不应该结束整个监听线程。
                    print(f"[Sensor] 接收数据失败: {exc}")
                    continue
                self._handle_packet(data, addr)

        self._bound = False

    def _bind(self, sock) -> bool:
        """绑定监听端口；特权端口被拒时自动回退到高位端口。

        树莓派这类 Linux 设备上，`333` 需要 root 才能绑定。与其让传感器功能整体不可用，
        不如自动退到 `SENSOR_PORT_FALLBACK`（默认 1333）并把提示打清楚，
        这样 `python app.py` 直接就能跑起来。
        """
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            return True
        except PermissionError:
            fallback = self._fallback_port()
            if fallback and fallback != self.port:
                try:
                    sock.bind((self.host, fallback))
                except OSError as exc:
                    self._fail(f"端口 {self.port} 和回退端口 {fallback} 都无法绑定: {exc}")
                    return False
                original, self.port = self.port, fallback
                print(
                    f"[Sensor] 端口 {original} 需要特权，已自动改用 {fallback}\n"
                    f"  请把单片机推送的目标端口改成 {fallback}（或用 SENSOR_PORT 指定其它端口）。"
                )
                return True
            self._fail(self._permission_hint())
            return False
        except OSError as exc:
            self._fail(
                f"绑定端口 {self.port} 失败: {exc}\n"
                "  可能是上个进程没退干净，占用了该端口，用下面的命令确认：\n"
                f"      Linux:   ss -lunp | grep {self.port}\n"
                f"      Windows: netstat -ano | findstr {self.port}\n"
                "  确认后 kill 掉旧进程，或者用 SENSOR_PORT 换一个端口。\n"
                "  传感器功能已禁用，摄像头 / YOLO / 网页仍然正常工作。"
            )
            return False

    def _fallback_port(self) -> int | None:
        """只有「Linux + 特权端口」这种确定的情况才自动回退。

        Windows 上 PermissionError 通常表示端口被独占，此时换端口会掩盖真实问题，
        因此不做自动回退。
        """
        if os.name == "nt" or self.port >= 1024:
            return None
        raw = os.getenv("SENSOR_PORT_FALLBACK", "1333")
        try:
            fallback = int(raw)
        except (TypeError, ValueError):
            return 1333
        return fallback if fallback >= 1024 else 1333

    def _permission_hint(self) -> str:
        """根据平台给出可执行的修复建议。

        Linux 上 <1024 的端口是特权端口，普通用户绑定会 EACCES；
        而 Windows 上「端口已被独占占用」也会被映射成 PermissionError（WSAEACCES），
        所以这里要区分两种情况，避免给出误导性的提示。
        """
        if os.name != "nt" and self.port < 1024:
            return (
                f"绑定端口 {self.port} 失败：权限不足（Linux 上 1024 以下的端口属于特权端口）。\n"
                "  方案 1（推荐）：把单片机推送目标端口改成 1333，然后用非特权端口启动：\n"
                "      SENSOR_PORT=1333 python app.py\n"
                "  方案 2：只给 Python 授权绑定低位端口（不用整个程序跑 root）：\n"
                "      sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python))\n"
                "  方案 3：sudo python app.py（不推荐，会把整个程序提权）\n"
                "  传感器功能已禁用，摄像头 / YOLO / 网页仍然正常工作。"
            )
        return (
            f"绑定端口 {self.port} 失败：没有权限或端口已被其他进程独占。\n"
            f"  1) 确认端口是否被占用：\n"
            f"      Linux:   ss -lunp | grep {self.port}\n"
            f"      Windows: netstat -ano | findstr {self.port}\n"
            "  2) 换一个端口启动：\n"
            f"      SENSOR_PORT=1333 python app.py\n"
            "  3) 同步修改单片机侧推送的目标端口。\n"
            "  传感器功能已禁用，摄像头 / YOLO / 网页仍然正常工作。"
        )

    def _fail(self, message: str):
        """统一处理监听线程的致命错误：只打印并记录状态，不抛出异常。"""
        self._running = False
        self._bound = False
        self._error = message
        print(f"[Sensor] {message}")

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
