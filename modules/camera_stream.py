"""
modules/camera_stream.py

视频采集层。

它的职责非常单一：从某个视频源持续读取帧，然后把“最新帧”提供给
其他模块消费。这里既可以是本地摄像头，也可以是 RTSP 码流。
当前项目的主开发路径已经切到本地 MediaMTX 暴露的 RTSP 流：
`rtsp://localhost:8554/webcam`。

为了适配后面的关键帧处理器，这里额外维护了一个很短的队列：
队列里永远尽量只保留最新帧，旧帧会被丢掉，从而降低延迟。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import queue
import time
import threading

try:
    import cv2
except ImportError:
    cv2 = None


class CameraStream:
    def __init__(self, socketio, stream_url: str, fps_limit: int = 10):
        self.socketio   = socketio
        self.stream_url = stream_url
        self.fps_limit  = fps_limit
        # 这个队列主要给“后续处理模块”消费。
        # 它刻意保持很小的容量，避免积压太多旧帧导致显示延迟越来越大。
        self.frame_queue: queue.Queue = queue.Queue(maxsize=2)
        # 支持多个消费者同时订阅同一帧流，例如 YOLO 和关键帧抽取。
        self._consumer_queues: list[queue.Queue] = [self.frame_queue]
        self._cap       = None
        self._thread    = None
        self._running   = False
        # `_latest` 用于 WebSocket 的单帧快照接口，前端手动刷新时会用到。
        self._latest    = None
        self._active_source = None
        self._source_is_file = False
        self._is_rtsp_source = False
        self._reconnect_delay = 1.0
        # 摄像头句柄会被采集线程和停止逻辑同时访问，因此用锁保护。
        self._lock = threading.Lock()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        print(f"[Camera] 开始采集: {self.stream_url}")

    def stop(self):
        # 先把运行标记清掉，让采集线程尽快退出。
        self._running = False
        with self._lock:
            cap = self._cap
            self._cap = None
        if cap:
            # 释放底层视频句柄，避免文件锁或摄像头占用问题。
            cap.release()
        print("[Camera] 已停止")

    def switch_source(self, stream_url: str):
        """切换视频源。

        由于当前采集逻辑只维护一个活动句柄，最稳妥的切换方式是：
        先停掉现有采集，再更新 URL，最后按需要重新启动。
        """
        was_running = self._running
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        self.stream_url = stream_url
        self._latest = None
        self._active_source = None
        self._source_is_file = False
        self._is_rtsp_source = False
        self._reconnect_delay = 1.0

        # 清空旧帧，避免切换时前端还短暂看到老视频源内容。
        while not self.frame_queue.empty():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break

        if was_running:
            self.start()

    def get_source_status(self) -> dict:
        """返回给前端展示用的视频源状态。"""
        return {
            "requested_source": self.stream_url,
            "active_source": self._active_source,
            "is_running": self._running,
            "is_rtsp": self._is_rtsp_source,
            "is_file": self._source_is_file,
        }

    def register_consumer_queue(self, consumer_queue: queue.Queue):
        """注册额外的帧消费者队列。"""
        if consumer_queue not in self._consumer_queues:
            self._consumer_queues.append(consumer_queue)

    def unregister_consumer_queue(self, consumer_queue: queue.Queue):
        """移除额外的帧消费者队列。"""
        if consumer_queue in self._consumer_queues and consumer_queue is not self.frame_queue:
            self._consumer_queues.remove(consumer_queue)

    def get_frame(self):
        """非阻塞获取最新帧。

        返回值是 OpenCV 的原始 BGR 图像数组，方便其他模块做进一步处理。
        如果还没有采集到任何帧，就返回 None。
        """
        return self._latest

    def get_frame_base64(self) -> str | None:
        """将最新帧压缩成 JPEG 并转为 Base64。

        WebSocket 不适合直接传 numpy 数组，所以这里先做 JPEG 压缩，
        再编码成 Base64 字符串，前端收到后可直接当作图片 src 使用。
        """
        frame = self._latest
        if frame is None:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return base64.b64encode(buf).decode("utf-8")

    # ── 内部循环 ──────────────────────────────────────────────
    def _capture_loop(self):
        # 采集线程启动时先尝试打开可用视频源。
        # 如果当前目标源不可用，会自动退回到本机摄像头，尽量保证“能跑起来”。
        with self._lock:
            self._cap = self._open_capture()
            cap = self._cap
        if cap is None:
            print("[Camera] 无法打开任何视频源")
            self._running = False
            return

        # 通过 sleep 限制最大采集帧率，避免 CPU 空转和队列被快速刷爆。
        interval  = 1.0 / self.fps_limit

        while self._running:
            with self._lock:
                cap = self._cap
            if cap is None:
                break

            ret, frame = cap.read()
            if not ret:
                if self._source_is_file:
                    # 如果当前源是文件，那么读到末尾后直接回到开头循环播放。
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue

                # 对摄像头 / RTSP 这类实时源，读失败时尝试重新连接。
                print("[Camera] 读帧失败，尝试重连...")
                cap.release()
                with self._lock:
                    self._cap = self._open_capture()
                    cap = self._cap
                if cap is None:
                    # 重连失败时做指数退避，避免在 RTSP 不稳定时疯狂刷日志和占用 CPU。
                    time.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 1.5, 5.0)
                continue

            # 保存最新帧，供快照接口和上层处理器使用。
            self._latest = frame
            self._reconnect_delay = 1.0

            # 将帧广播给所有消费者队列。
            # 任何一个处理器都不应该阻塞采集线程，因此每个队列都只保留最新帧。
            self._broadcast_frame(frame)

            # 用简单等待代替忙轮询，降低 CPU 占用。
            threading.Event().wait(interval)

    def _open_capture(self):
        # 尝试按优先级依次打开候选视频源：
        # 先开用户指定的源，再退回样例视频，最后尝试本机摄像头 0。
        for source in self._candidate_sources():
            cap = self._create_capture(source)
            if cap is None:
                print(f"[Camera] _create_capture 返回 None，跳过源: {source}")
                continue
            if cap.isOpened():
                self._active_source = source
                self._source_is_file = isinstance(source, str) and Path(source).exists()
                self._is_rtsp_source = isinstance(source, str) and source.lower().startswith("rtsp://")
                print(f"[Camera] 打开视频源: {source}")
                return cap
            cap.release()
        return None

    def _create_capture(self, source):
        if cv2 is None:
            return None
        # RTSP 流在 Windows 上更适合走 FFMPEG + TCP，能明显降低乱序包和丢包引发的解码错误。
        if isinstance(source, str) and source.lower().startswith("rtsp://"):
            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                "rtsp_transport;tcp|stimeout;5000000|max_delay;500000|fflags;nobuffer",
            )
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap

        # 其他类型源保持默认后端即可。
        return cv2.VideoCapture(source)

    def _candidate_sources(self):
        sources = []
        # 把用户传入的 stream_url 标准化成 OpenCV 可理解的形式。
        normalized = self._normalize_source(self.stream_url)
        if normalized is not None:
            sources.append(normalized)


        # 最后再尝试摄像头编号 0~3，覆盖大多数常见的本地采集设备编号。
        # 这样即使用户没有手动指定，也能自动枚举更多本地视频源。
        for index in range(4):
            if index not in sources:
                sources.append(index)

        return sources

    @staticmethod
    def _normalize_source(source):
        # 允许用户传入 None、空字符串、摄像头编号字符串、RTSP 地址或本地路径。
        if source is None:
            return None
        if isinstance(source, int):
            return source
        if isinstance(source, str):
            stripped = source.strip()
            if not stripped:
                return None
            if stripped.isdigit():
                return int(stripped)
            return stripped
        return source

    def _broadcast_frame(self, frame):
        for consumer_queue in list(self._consumer_queues):
            if consumer_queue.full():
                try:
                    consumer_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                consumer_queue.put_nowait(frame)
            except queue.Full:
                # 即便刚刚清掉旧帧，个别队列仍可能因为竞争而满，直接跳过即可。
                continue
