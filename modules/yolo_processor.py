"""
modules/yolo_processor.py

YOLO 检测处理层。

这个模块目前保留在项目中，主要是为了未来需要重新启用目标检测时，
不必重新设计整条视频处理链路。它的模式和关键帧处理器类似：
从 `CameraStream` 的队列中消费帧，进行推理，然后把结果广播给前端。

如果当前环境没有安装 `ultralytics`，模块也能正常启动，只是不会真正执行检测。
"""
from __future__ import annotations

import base64
import threading
from datetime import datetime
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None


class YoloProcessor:
    def __init__(
        self,
        socketio,
        store,
        model_path: str | None = None,
        task: str = "detect",
        conf_threshold: float = 0.25,
        source_name: str = "local_camera_0",
    ):
        self.socketio   = socketio
        self.store      = store
        self.model_path = Path(model_path) if model_path else Path(__file__).resolve().parent.parent / "YOLOModel" / "best.pt"
        self.task       = task
        self.conf_threshold = conf_threshold
        self.source_name = source_name
        self.model      = None
        self._thread    = None
        self._running   = False
        self._latest_payload: dict | None = None
        self._status: dict = {
            "state": "idle",
            "message": "等待启动",
            "model_path": str(self.model_path),
            "source_name": self.source_name,
            "task": self.task,
        }

        # 帧队列由外部绑定，便于把采集和处理解耦。
        self._frame_queue = None

    def bind_queue(self, frame_queue):
        """绑定 `CameraStream` 的帧队列。"""
        self._frame_queue = frame_queue

    def start(self):
        if self._running:
            return
        self._load_model()
        self._running = True
        self._thread  = threading.Thread(target=self._infer_loop, daemon=True)
        self._thread.start()
        if self.model is None:
            self._emit_status("running", "YOLO 已启动，但当前将回退到原始帧显示")
        else:
            self._emit_status("running", "YOLO 推理线程启动")
        print("[YOLO] 推理线程启动")

    def stop(self):
        self._running = False
        self._emit_status("stopped", "YOLO 已停止")
        print("[YOLO] 推理线程停止")

    def get_latest_payload(self) -> dict:
        return dict(self._latest_payload) if self._latest_payload else {}

    def get_status(self) -> dict:
        return dict(self._status)

    # ── 内部方法 ──────────────────────────────────────────────
    def _load_model(self):
        try:
            # 延迟导入 ultralytics，避免项目在没有该依赖的环境里直接启动失败。
            from ultralytics import YOLO
            if not self.model_path.exists():
                print(f"[YOLO] 模型文件不存在: {self.model_path}")
                self.model = None
                self._emit_status("error", f"模型文件不存在: {self.model_path}")
                return

            try:
                import numpy as np
                numpy_major = int(str(np.__version__).split(".", 1)[0])
                if numpy_major >= 2:
                    print("[YOLO] 检测到 NumPy 2.x，若推理报错请降级到 numpy<2")
            except Exception:
                pass

            self.model = YOLO(str(self.model_path))
            print(f"[YOLO] 模型加载: {self.model_path}")
            self._emit_status("ready", f"模型已加载: {self.model_path}")
        except ImportError:
            print("[YOLO] ultralytics 未安装，推理禁用")
            self.model = None
            self._emit_status("error", "ultralytics 未安装，推理禁用")
        except Exception as exc:
            self.model = None
            print(f"[YOLO] 模型加载失败: {exc}")
            self._emit_status("error", f"模型加载失败: {exc}")

    def _infer_loop(self):
        if self._frame_queue is None:
            print("[YOLO] 未绑定帧队列，请调用 bind_queue()")
            self._emit_status("error", "未绑定帧队列，请先调用 bind_queue()")
            return

        while self._running:
            try:
                # 取帧时设置超时，避免队列暂时为空时线程一直卡死在阻塞调用里。
                frame = self._frame_queue.get(timeout=1.0)
            except Exception:
                continue

            try:
                result_frame, detections = self._process_frame(frame)
            except Exception as exc:
                self.model = None
                self._emit_status("error", f"YOLO 推理失败，已回退到原始帧: {exc}")
                result_frame, detections = frame, []

            payload = {
                "timestamp":  datetime.now().isoformat(),
                "detections": detections,
                "frame_b64":  self._encode_frame(result_frame),
                "source_name": self.source_name,
                "task": self.task,
            }

            if detections:
                self.store.save_detection(payload)
            self._latest_payload = payload
            self.socketio.emit("detection_result", payload)

    def _process_frame(self, frame):
        """
        这里是 YOLO 的核心处理入口。

        - 输入：OpenCV 的 BGR 帧
        - 输出：标注后的帧 + 检测结果列表
        """
        detections = []
        if self.model is None:
            return frame, detections

        results = self.model.predict(frame, verbose=False, conf=self.conf_threshold)
        for r in results:
            if hasattr(r, "boxes") and r.boxes is not None:
                for box in r.boxes:
                    detections.append({
                        "label":  r.names[int(box.cls)],
                        "conf":   round(float(box.conf), 3),
                        "bbox":   [round(v, 1) for v in box.xyxy[0].tolist()],
                    })
            elif hasattr(r, "probs") and r.probs is not None:
                top1 = int(r.probs.top1)
                detections.append({
                    "label": r.names[top1],
                    "conf": round(float(r.probs.top1conf), 3),
                    "bbox": None,
                })

        # 把检测框画回帧上，前端就可以直接看到带框画面。
        annotated = results[0].plot() if results else frame
        return annotated, detections

    def _emit_status(self, state: str, message: str):
        self._status = {
            "state": state,
            "message": message,
            "model_path": str(self.model_path),
            "source_name": self.source_name,
            "task": self.task,
        }
        self.socketio.emit("yolo_status_update", self._status)

    @staticmethod
    def _encode_frame(frame) -> str:
        # 和关键帧处理器一样，这里把图像压缩后再转 Base64，便于通过 WebSocket 发送。
        if cv2 is None:
            return ""
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(buf).decode("utf-8")
