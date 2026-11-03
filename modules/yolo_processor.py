"""
modules/yolo_processor.py

YOLO 检测处理层。

这个模块从 `CameraStream` 的队列中消费帧，进行推理，然后把结果广播给前端。
为了适配不同算力的设备，这里支持两种推理后端：

1. `torch`（ultralytics + torch）
   精度好、通用，适合 PC。但在树莓派 4B 上要注意：部分 aarch64 版 torch 轮子
   用到了 Cortex-A72 不支持的 ARMv8.2 指令（dotprod / fp16 等），推理时会直接
   触发 SIGILL，表现为进程突然打印 `Illegal instruction` 后退出。这类崩溃发生在
   C 扩展里，Python 无法用 try/except 捕获，只能换后端规避。

2. `dnn`（OpenCV `cv2.dnn` + ONNX）
   只需要 OpenCV，不需要 torch / ultralytics，内存占用小得多，在树莓派上更稳。
   `YOLOModel/best.onnx` 由 `best.pt` 导出，输出格式与 torch 后端一致。

后端通过环境变量控制：

- `SMART_CAMERA_YOLO_BACKEND=auto`（默认）：ARM 设备优先用 `dnn`，其余优先用 `torch`
- `SMART_CAMERA_YOLO_BACKEND=torch` / `dnn`：强制指定
- `SMART_CAMERA_YOLO_BACKEND=off`：完全不加载模型，只显示原始画面
"""
from __future__ import annotations

import base64
import json
import platform
import threading
from datetime import datetime
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import numpy as np
except ImportError:
    np = None


BACKEND_TORCH = "torch"
BACKEND_DNN = "dnn"
BACKEND_OFF = "off"

# 不同类别用不同颜色画框，方便肉眼核对检测结果。
_PALETTE = [
    (56, 56, 255), (151, 157, 255), (31, 112, 255), (29, 178, 255),
    (49, 210, 207), (10, 249, 72), (23, 204, 146), (134, 219, 61),
    (52, 147, 26), (187, 212, 0), (168, 153, 44), (255, 194, 0),
]


class YoloProcessor:
    def __init__(
        self,
        socketio,
        store,
        model_path: str | None = None,
        onnx_path: str | None = None,
        task: str = "detect",
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        imgsz: int = 640,
        inference_interval: int = 1,
        backend: str = "auto",
        source_name: str = "local_camera_0",
    ):
        self.socketio   = socketio
        self.store      = store
        base_dir        = Path(__file__).resolve().parent.parent
        self.model_path = Path(model_path) if model_path else base_dir / "YOLOModel" / "best.pt"
        # 默认使用和权重同名的 ONNX 文件，方便直接替换模型。
        self.onnx_path  = Path(onnx_path) if onnx_path else self.model_path.with_suffix(".onnx")
        # 类别名单独放在 classes.json 里，避免 DNN 后端为了读名字去 import ultralytics。
        self.classes_path = self.onnx_path.with_name("classes.json")
        self.task       = task
        self.conf_threshold = conf_threshold
        self.iou_threshold  = iou_threshold
        # 推理分辨率。树莓派这类设备可以降到 320/416 换取帧率。
        self.imgsz      = max(64, int(imgsz))
        # 每 N 帧推理一次，用来在低算力设备上降低 CPU 占用。
        self.inference_interval = max(1, int(inference_interval))
        self.backend    = (backend or "auto").strip().lower()
        self.source_name = source_name

        # torch 后端的模型对象
        self.model      = None
        # OpenCV dnn 后端的网络对象
        self._net       = None
        self._out_names = None
        self._names     = None
        self._active_backend = None

        self._thread    = None
        self._running   = False
        self._latest_payload: dict | None = None
        self._frame_counter = 0
        self._status: dict = {
            "state": "idle",
            "message": "等待启动",
            "model_path": str(self.model_path),
            "backend": self.backend,
            "active_backend": None,
            "source_name": self.source_name,
            "task": self.task,
            "imgsz": self.imgsz,
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
        if self._active_backend == BACKEND_OFF:
            self._emit_status("disabled", "YOLO 已关闭，页面只显示原始画面")
            print("[YOLO] 已按配置关闭，不加载模型")
            return
        self._running = True
        self._thread  = threading.Thread(target=self._infer_loop, daemon=True)
        self._thread.start()
        if not self._model_ready():
            self._emit_status("running", "YOLO 已启动，但当前将回退到原始帧显示")
        else:
            self._emit_status("running", f"YOLO 推理线程启动（后端: {self._active_backend}）")
        print(f"[YOLO] 推理线程启动（后端: {self._active_backend}）")

    def stop(self):
        self._running = False
        self._emit_status("stopped", "YOLO 已停止")
        print("[YOLO] 推理线程停止")

    def get_latest_payload(self) -> dict:
        return dict(self._latest_payload) if self._latest_payload else {}

    def get_status(self) -> dict:
        return dict(self._status)

    # ── 内部方法 ──────────────────────────────────────────────
    def _model_ready(self) -> bool:
        if self._active_backend == BACKEND_TORCH:
            return self.model is not None
        if self._active_backend == BACKEND_DNN:
            return self._net is not None
        return False

    def _resolve_backend(self) -> str:
        """决定这次实际使用哪个推理后端。"""
        if self.backend == BACKEND_OFF:
            return BACKEND_OFF
        if self.backend in (BACKEND_TORCH, BACKEND_DNN):
            return self.backend

        # auto 模式：ARM 设备（树莓派之类）优先走 OpenCV DNN，避免 torch 触发 SIGILL。
        machine = platform.machine().lower()
        if machine.startswith(("arm", "aarch64")):
            if self.onnx_path.exists() and cv2 is not None and np is not None:
                return BACKEND_DNN
            return BACKEND_TORCH
        return BACKEND_TORCH

    def _load_model(self):
        self._active_backend = self._resolve_backend()

        if self._active_backend == BACKEND_OFF:
            self._status = self._build_status("disabled", "YOLO 已关闭")
            return

        # 记录一次推理环境，方便排查 “Numpy is not available” 这类版本不匹配问题。
        try:
            print(
                f"[YOLO] 推理环境: Python {platform.python_version()} / {platform.machine()} / "
                f"NumPy {np.__version__ if np else '未安装'} / "
                f"OpenCV {cv2.__version__ if cv2 else '未安装'}"
            )
        except Exception:
            pass

        if self._active_backend == BACKEND_DNN:
            self._load_dnn()
        else:
            self._load_torch()

    def _load_torch(self):
        if not self.model_path.exists():
            print(f"[YOLO] 模型文件不存在: {self.model_path}")
            self._emit_status("error", f"模型文件不存在: {self.model_path}")
            return
        try:
            # 延迟导入 ultralytics，避免项目在没有该依赖的环境里直接启动失败。
            from ultralytics import YOLO

            self.model = YOLO(str(self.model_path))
            print(f"[YOLO] 模型加载(torch): {self.model_path}")
            self._emit_status("ready", f"模型已加载(torch): {self.model_path}")
        except ImportError:
            print("[YOLO] ultralytics 未安装，推理禁用")
            self.model = None
            self._emit_status("error", "ultralytics 未安装，推理禁用")
        except Exception as exc:
            self.model = None
            print(f"[YOLO] 模型加载失败: {exc}")
            self._emit_status("error", f"模型加载失败: {exc}")

    def _load_dnn(self):
        if cv2 is None or np is None:
            print("[YOLO] 缺少 opencv-python / numpy，DNN 后端不可用")
            self._emit_status("error", "缺少 opencv-python / numpy，DNN 后端不可用")
            return
        try:
            if not self.onnx_path.exists():
                print(f"[YOLO] ONNX 模型不存在: {self.onnx_path}")
                self._emit_status(
                    "error",
                    f"ONNX 模型不存在: {self.onnx_path}（可用 best.pt 导出，或改用 torch 后端）",
                )
                return
            self._net = cv2.dnn.readNetFromONNX(str(self.onnx_path))
            try:
                # 固定用 CPU，避免在树莓派/容器里探测到不可用的加速后端。
                self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            except Exception:
                pass
            self._out_names = self._net.getUnconnectedOutLayersNames()
            # ONNX 多数是固定输入尺寸（本项目导出的是 640×640）。
            # 如果用户把 imgsz 调小，前向会因形状不匹配失败，所以这里先用空 blob 探测一次，
            # 顺便完成图初始化（首次推理不再额外慢一拍）。
            self._probe_dnn_input()
            print(f"[YOLO] 模型加载(dnn): {self.onnx_path} (imgsz={self.imgsz})")
            self._emit_status("ready", f"模型已加载(dnn): {self.onnx_path}")
        except Exception as exc:
            self._net = None
            print(f"[YOLO] ONNX 模型加载失败: {exc}")
            self._emit_status("error", f"ONNX 模型加载失败: {exc}")

    def _probe_dnn_input(self):
        """探测 ONNX 的真实输入尺寸，必要时修正 imgsz。"""
        candidates = [self.imgsz]
        if self.imgsz != 640:
            candidates.append(640)
        last_error = None
        for size in candidates:
            try:
                blob = np.zeros((1, 3, size, size), dtype=np.float32)
                self._net.setInput(blob)
                self._net.forward(self._out_names)
            except Exception as exc:
                last_error = exc
                continue
            if size != self.imgsz:
                print(
                    f"[YOLO] ONNX 输入固定为 {size}x{size}，已把 imgsz 从 {self.imgsz} 调整为 {size}"
                    "（想降低算力请改用 SMART_CAMERA_YOLO_INTERVAL）"
                )
                self.imgsz = size
            return
        raise RuntimeError(f"ONNX 前向探测失败: {last_error}")

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

            # 低算力设备上可以隔帧推理，显著降低 CPU 占用。
            self._frame_counter += 1
            if self.inference_interval > 1 and (self._frame_counter % self.inference_interval):
                continue

            try:
                result_frame, detections = self._process_frame(frame)
            except Exception as exc:
                self.model = None
                self._net = None
                self._emit_status("error", f"YOLO 推理失败，已回退到原始帧: {exc}")
                result_frame, detections = frame, []

            payload = {
                "timestamp":  datetime.now().isoformat(),
                "detections": detections,
                "frame_b64":  self._encode_frame(result_frame),
                "source_name": self.source_name,
                "task": self.task,
                "backend": self._active_backend,
            }

            if detections:
                self.store.save_detection(payload)
            self._latest_payload = payload
            self.socketio.emit("detection_result", payload)

    def _process_frame(self, frame):
        """按当前后端处理一帧，返回 (标注后的帧, 检测结果列表)。"""
        if self._active_backend == BACKEND_DNN:
            return self._process_dnn(frame)
        if self._active_backend == BACKEND_TORCH:
            return self._process_torch(frame)
        return frame, []

    def _process_torch(self, frame):
        """
        torch 后端：交给 ultralytics 完成预处理、推理和画框。

        - 输入：OpenCV 的 BGR 帧
        - 输出：标注后的帧 + 检测结果列表
        """
        detections = []
        if self.model is None:
            return frame, detections

        results = self.model.predict(
            frame,
            verbose=False,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            imgsz=self.imgsz,
            device="cpu",
        )
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

    def _process_dnn(self, frame):
        """OpenCV DNN 后端：自己完成 letterbox、前向计算、解码和 NMS。"""
        if self._net is None or cv2 is None or np is None:
            return frame, []

        canvas, ratio, pad_x, pad_y = self._letterbox(frame)
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 255.0, (self.imgsz, self.imgsz),
            swapRB=True, crop=False,
        )
        self._net.setInput(blob)
        outputs = self._net.forward(self._out_names)

        preds = np.squeeze(outputs[0] if isinstance(outputs, (list, tuple)) else outputs)
        if preds.ndim != 2:
            return frame, []
        # 有的导出结果是 (8400, 4+nc)，有的是 (4+nc, 8400)，统一成“每行一个候选框”。
        if preds.shape[0] < preds.shape[1]:
            preds = preds.T

        boxes, confidences, class_ids = [], [], []
        for row in preds:
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])
            if confidence < self.conf_threshold:
                continue
            cx, cy, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            # 把 letterbox 坐标还原回原图坐标。
            x1 = (cx - bw / 2.0 - pad_x) / ratio
            y1 = (cy - bh / 2.0 - pad_y) / ratio
            w  = bw / ratio
            h  = bh / ratio
            boxes.append([x1, y1, w, h])
            confidences.append(confidence)
            class_ids.append(class_id)

        detections = []
        annotated = frame
        if boxes:
            nms_boxes = [[float(x), float(y), float(w), float(h)] for x, y, w, h in boxes]
            indices = cv2.dnn.NMSBoxes(
                nms_boxes, confidences,
                float(self.conf_threshold), float(self.iou_threshold),
            )
            indices = np.array(indices).flatten().tolist() if len(indices) else []
            annotated = frame.copy()
            names = self._class_names()
            for idx in indices:
                x, y, w, h = boxes[idx]
                x1, y1 = max(0, int(round(x))), max(0, int(round(y)))
                x2, y2 = int(round(x + w)), int(round(y + h))
                class_id = class_ids[idx]
                label = names.get(class_id, str(class_id))
                color = _PALETTE[class_id % len(_PALETTE)]
                cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                text = f"{label} {confidences[idx]:.2f}"
                (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(annotated, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
                cv2.putText(
                    annotated, text, (x1 + 2, max(10, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
                )
                detections.append({
                    "label": label,
                    "conf":  round(confidences[idx], 3),
                    "bbox":  [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                })

        return annotated, detections

    def _letterbox(self, frame):
        """等比缩放到 imgsz×imgsz 并补边，避免直接拉伸影响检测精度。"""
        height, width = frame.shape[:2]
        ratio = min(self.imgsz / float(height), self.imgsz / float(width))
        new_w, new_h = int(round(width * ratio)), int(round(height * ratio))
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        pad_x = (self.imgsz - new_w) // 2
        pad_y = (self.imgsz - new_h) // 2
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return canvas, ratio, pad_x, pad_y

    def _class_names(self) -> dict:
        """从 classes.json 读取类别名；读不到时退回序号，绝不为此 import ultralytics。"""
        if self._names is not None:
            return self._names
        names: dict[int, str] = {}
        try:
            if self.classes_path.exists():
                data = json.loads(self.classes_path.read_text(encoding="utf-8"))
                names = {int(key): str(value) for key, value in data.items()}
        except Exception as exc:
            print(f"[YOLO] 读取类别名失败: {exc}")
        self._names = names
        return names

    def _build_status(self, state: str, message: str) -> dict:
        active_path = self.onnx_path if self._active_backend == BACKEND_DNN else self.model_path
        return {
            "state": state,
            "message": message,
            "model_path": str(active_path),
            "backend": self.backend,
            "active_backend": self._active_backend,
            "source_name": self.source_name,
            "task": self.task,
            "imgsz": self.imgsz,
            "inference_interval": self.inference_interval,
        }

    def _emit_status(self, state: str, message: str):
        self._status = self._build_status(state, message)
        self.socketio.emit("yolo_status_update", self._status)

    @staticmethod
    def _encode_frame(frame) -> str:
        # 和关键帧处理器一样，这里把图像压缩后再转 Base64，便于通过 WebSocket 发送。
        if cv2 is None:
            return ""
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(buf).decode("utf-8")
