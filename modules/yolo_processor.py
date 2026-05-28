"""
modules/yolo_processor.py

YOLO 检测处理层。

这个模块目前保留在项目中，主要是为了未来需要重新启用目标检测时，
不必重新设计整条视频处理链路。它的模式和关键帧处理器类似：
从 `CameraStream` 的队列中消费帧，进行推理，然后把结果广播给前端。

如果当前环境没有安装 `ultralytics`，模块也能正常启动，只是不会真正执行检测。
"""
import base64
import threading
from datetime import datetime

import cv2


class YoloProcessor:
    def __init__(self, socketio, store, model_path: str = "yolov8n.pt"):
        self.socketio   = socketio
        self.store      = store
        self.model_path = model_path
        self.model      = None
        self._thread    = None
        self._running   = False

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
        print("[YOLO] 推理线程启动")

    def stop(self):
        self._running = False
        print("[YOLO] 推理线程停止")

    # ── 内部方法 ──────────────────────────────────────────────
    def _load_model(self):
        try:
            # 延迟导入 ultralytics，避免项目在没有该依赖的环境里直接启动失败。
            from ultralytics import YOLO
            self.model = YOLO(self.model_path)
            print(f"[YOLO] 模型加载: {self.model_path}")
        except ImportError:
            print("[YOLO] ultralytics 未安装，推理禁用")

    def _infer_loop(self):
        if self._frame_queue is None:
            print("[YOLO] 未绑定帧队列，请调用 bind_queue()")
            return

        while self._running:
            try:
                # 取帧时设置超时，避免队列暂时为空时线程一直卡死在阻塞调用里。
                frame = self._frame_queue.get(timeout=1.0)
            except Exception:
                continue

            result_frame, detections = self._process_frame(frame)
            if detections:
                payload = {
                    "timestamp":  datetime.now().isoformat(),
                    "detections": detections,
                    "frame_b64":  self._encode_frame(result_frame),
                }
                self.store.save_detection(payload)
                self.socketio.emit("detection_result", payload)

    def _process_frame(self, frame):
        """
        这里是 YOLO 的核心处理入口。

        你后续如果要换成自己的模型、自己的后处理逻辑，通常只需要改这里：
        - 输入：OpenCV 的 BGR 帧
        - 输出：标注后的帧 + 检测结果列表
        """
        detections = []
        if self.model is None:
            return frame, detections

        results = self.model(frame, verbose=False)
        for r in results:
            for box in r.boxes:
                detections.append({
                    "label":  r.names[int(box.cls)],
                    "conf":   round(float(box.conf), 3),
                    "bbox":   [round(v, 1) for v in box.xyxy[0].tolist()],
                })

        # 把检测框画回帧上，前端就可以直接看到带框画面。
        annotated = results[0].plot() if results else frame
        return annotated, detections

    @staticmethod
    def _encode_frame(frame) -> str:
        # 和关键帧处理器一样，这里把图像压缩后再转 Base64，便于通过 WebSocket 发送。
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(buf).decode("utf-8")
