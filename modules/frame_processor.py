"""
modules/frame_processor.py

关键帧处理层。

这里复用了 `AviToFrame` 里的核心思路：
先把图像缩小，再转成灰度图，然后和上一帧做 `absdiff`。
这样做的好处是：
1. 运算比直接对彩色图做差更快
2. 对细小颜色噪声更不敏感
3. 更适合“有没有明显变化”这种判断

当差异超过阈值时，就认为这一帧值得保留，并把它推送到前端。
"""
from __future__ import annotations

import base64
import queue
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


class FastFrameProcessor:
    def __init__(
        self,
        socketio,
        store=None,
        output_dir: str | None = None,
        compare_width: int = 320,
        compare_height: int = 180,
        diff_threshold: float = 4.0,
        jpeg_quality: int = 80,
    ):
        self.socketio = socketio
        self.store = store
        self.output_dir = Path(output_dir) if output_dir else None
        self.compare_width = compare_width
        self.compare_height = compare_height
        self.diff_threshold = diff_threshold
        self.jpeg_quality = jpeg_quality

        # 处理器只消费摄像头的帧队列，不直接读取视频源。
        self._frame_queue: queue.Queue | None = None
        self._thread = None
        self._running = False
        # 保存上一帧缩小后的灰度图，用于和当前帧比较。
        self._prev_small_gray = None
        self._frame_index = 0
        self._saved_count = 0
        # 最新一次被判定为关键帧的结果，供 WebSocket 连接时直接补发。
        self._latest_payload: dict | None = None

        if self.output_dir:
            # 如果配置了输出目录，就在处理器启动前确保目录存在。
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def bind_queue(self, frame_queue):
        """绑定 `CameraStream` 提供的帧队列。"""
        self._frame_queue = frame_queue

    def start(self):
        if self._running:
            return
        if self._frame_queue is None:
            print("[FrameProcessor] 未绑定帧队列，无法启动")
            return
        # 每次重新启动时都从“没有上一帧”的状态开始，避免沿用旧上下文。
        self._prev_small_gray = None
        self._running = True
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
        print("[FrameProcessor] 关键帧处理线程启动")

    def stop(self):
        self._running = False
        print("[FrameProcessor] 关键帧处理线程停止")

    def get_latest_payload(self) -> dict:
        # 返回一份拷贝，避免外部修改内部缓存。
        return dict(self._latest_payload) if self._latest_payload else {}

    def _process_loop(self):
        while self._running:
            try:
                frame = self._frame_queue.get(timeout=1.0)
            except queue.Empty:
                # 队列暂时没有新帧时不报错，继续等待下一帧即可。
                continue
            except Exception as exc:
                print(f"[FrameProcessor] 读取帧失败: {exc}")
                continue

            self._frame_index += 1
            # 第一步：缩小分辨率。
            # 这里比较的是“变化趋势”，不是最终展示效果，所以不需要高分辨率参与计算。
            small = cv2.resize(
                frame,
                (self.compare_width, self.compare_height),
                interpolation=cv2.INTER_AREA,
            )
            # 第二步：转成灰度图，减少通道数，进一步降低差异计算成本。
            small_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if self._prev_small_gray is None:
                # 第一帧没有上一帧可比，默认直接保存。
                diff = 0.0
                should_save = True
            else:
                # 第三步：计算相邻帧的平均绝对差。
                # 平均值越大，说明当前画面和上一帧变化越明显。
                diff = float(np.mean(cv2.absdiff(self._prev_small_gray, small_gray)))
                should_save = diff >= self.diff_threshold

            if should_save:
                self._saved_count += 1
                timestamp = datetime.now().isoformat()
                # 这里把原始帧也转成 Base64，是为了能直接推送到前端展示。
                frame_b64 = self._encode_frame(frame)
                payload = {
                    "timestamp": timestamp,
                    "frame_index": self._frame_index,
                    "saved_count": self._saved_count,
                    "diff": round(diff, 3),
                    "frame_b64": frame_b64,
                    "saved_path": None,
                    "compare_size": [self.compare_width, self.compare_height],
                }

                if self.output_dir:
                    # 关键帧除了推送到前端，也可以落盘，方便后续复查或做数据集。
                    frame_path = self.output_dir / f"frame_{self._saved_count:04d}.jpg"
                    cv2.imwrite(
                        str(frame_path),
                        frame,
                        [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                    )
                    payload["saved_path"] = str(frame_path)

                self._latest_payload = payload
                self.socketio.emit("frame_update", payload)

            # 无论当前帧是否被保存，都要把它更新为“上一帧”，用于下一次比较。
            self._prev_small_gray = small_gray

    @staticmethod
    def _encode_frame(frame) -> str:
        # 将 OpenCV 图像压成 JPEG 后再 Base64 编码，便于 WebSocket 直接传输。
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(buf).decode("utf-8")
