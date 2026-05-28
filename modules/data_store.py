"""
modules/data_store.py

轻量数据存储层。

当前实现采取“两层存储”的方式：
1. 内存缓存：用于页面快速读取最近状态，响应最快
2. SQLite 持久化：用于保留历史记录，方便本地调试和回看

这样做的好处是结构简单、依赖少，适合先把项目跑起来。
后续如果要上生产，可以把这层替换成 Redis / PostgreSQL。
"""
import json
import sqlite3
import threading
from collections import deque
from datetime import datetime


DB_FILE = "smart_camera.db"


class DataStore:
    def __init__(self, history_limit: int = 500):
        # 内存缓存需要线程安全，因为传感器线程、采集线程和 Web 请求都可能同时访问。
        self._lock           = threading.Lock()
        # 只保留最近若干条传感器记录，避免内存无限增长。
        self._sensor_cache   = deque(maxlen=history_limit)
        # 检测记录单独保存一份最近历史，供前端回看。
        self._detection_cache= deque(maxlen=200)
        # 页面刚打开时会优先读这个值，作为“当前最新状态”。
        self._latest_sensor  = {}
        self._init_db()

    # ── 传感器数据 ────────────────────────────────────────────
    def save_sensor(self, payload: dict):
        with self._lock:
            self._latest_sensor = payload
            self._sensor_cache.append(payload)
        # 异步落库，避免把传感器接收线程卡住。
        self._db_insert("sensor_log", payload)

    def get_latest_sensor(self) -> dict:
        return dict(self._latest_sensor)

    def get_sensor_history(self, n: int = 50) -> list:
        with self._lock:
            data = list(self._sensor_cache)
        return data[-n:]

    # ── 检测记录 ──────────────────────────────────────────────
    def save_detection(self, payload: dict):
        # SQLite 表里只保留必要字段，检测列表本身转成 JSON 字符串存储。
        record = {
            "timestamp":  payload["timestamp"],
            "detections": json.dumps(payload["detections"]),
        }
        with self._lock:
            self._detection_cache.append({
                "timestamp":  payload["timestamp"],
                "detections": payload["detections"],
                "count":      len(payload["detections"]),
            })
        self._db_insert("detection_log", record)

    def get_detections(self, n: int = 20) -> list:
        with self._lock:
            data = list(self._detection_cache)
        return data[-n:]

    # ── SQLite 内部方法 ───────────────────────────────────────
    def _init_db(self):
        # 本地数据库初始化时自动建表，避免用户手动准备 schema。
        con = sqlite3.connect(DB_FILE)
        con.execute("""
            CREATE TABLE IF NOT EXISTS sensor_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                temp REAL, humi REAL, status TEXT,
                device_id TEXT, timestamp TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS detection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, detections TEXT
            )
        """)
        con.commit()
        con.close()

    def _db_insert(self, table: str, data: dict):
        """异步写入，避免阻塞推理/采集线程。

        这里不直接在当前线程执行 SQL，而是新开一个后台线程处理。
        这样即使 SQLite 暂时写慢，也不会影响视频采集和前端刷新。
        """
        t = threading.Thread(target=self._do_insert, args=(table, data), daemon=True)
        t.start()

    @staticmethod
    def _do_insert(table: str, data: dict):
        try:
            con = sqlite3.connect(DB_FILE)
            cols = ", ".join(data.keys())
            vals = ", ".join("?" * len(data))
            # 使用参数化 SQL，避免手动拼接值造成转义问题。
            con.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})", list(data.values()))
            con.commit()
            con.close()
        except Exception as e:
            print(f"[DB] 写入失败: {e}")
