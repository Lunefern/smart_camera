"""
modules/camera_stream.py

视频采集层。

它的职责非常单一：从某个视频源持续读取帧，然后把“最新帧”提供给
其他模块消费。这里既可以是本地摄像头，也可以是 RTSP 码流，甚至可以是
仓库里的样例视频文件。

为了适配后面的关键帧处理器，这里额外维护了一个很短的队列：
队列里永远尽量只保留最新帧，旧帧会被丢掉，从而降低延迟。
"""
from __future__ import annotations

import base64
from pathlib import Path
import queue
import threading

import cv2


class CameraStream:
    def __init__(self, socketio, stream_url: str, fps_limit: int = 10):
        self.socketio   = socketio
        self.stream_url = stream_url
        self.fps_limit  = fps_limit
        # 这个队列主要给“后续处理模块”消费。
        # 它刻意保持很小的容量，避免积压太多旧帧导致显示延迟越来越大。
        self.frame_queue: queue.Queue = queue.Queue(maxsize=2)
        self._cap       = None
        self._thread    = None
        self._running   = False
        # `_latest` 用于 WebSocket 的单帧快照接口，前端手动刷新时会用到。
        self._latest    = None
        self._active_source = None
        self._source_is_file = False
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
        # 如果当前目标源不可用，会自动尝试样例视频和本机摄像头，尽量保证“能跑起来”。
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
                    # 重连失败时稍微等一下，避免一直高频重试。
                    threading.Event().wait(1.0)
                continue

            # 保存最新帧，供快照接口和上层处理器使用。
            self._latest = frame

            # 将帧送入处理队列。
            # 如果队列已满，先丢掉最旧的一帧，再塞入当前帧，
            # 这样保证处理器始终更接近“最新现场画面”。
            if not self.frame_queue.full():
                self.frame_queue.put_nowait(frame)
            else:
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    pass
                self.frame_queue.put_nowait(frame)

            # 用简单等待代替忙轮询，降低 CPU 占用。
            threading.Event().wait(interval)

    def _open_capture(self):
        # 尝试按优先级依次打开候选视频源：
        # 先开用户指定的源，再退回样例视频，最后尝试本机摄像头 0。
        for source in self._candidate_sources():
            cap = cv2.VideoCapture(source)
            if cap.isOpened():
                self._active_source = source
                self._source_is_file = isinstance(source, str) and Path(source).exists()
                print(f"[Camera] 打开视频源: {source}")
                return cap
            cap.release()
        return None

    def _candidate_sources(self):
        sources = []
        # 把用户传入的 stream_url 标准化成 OpenCV 可理解的形式。
        normalized = self._normalize_source(self.stream_url)
        if normalized is not None:
            sources.append(normalized)

        # 如果仓库自带了样例视频，优先把它作为“本地可跑”的兜底方案。
        sample = Path(__file__).resolve().parent.parent / "AviToFrame" / "tmp.avi"
        if sample.exists() and sample not in sources:
            sources.append(str(sample))

        # 最后再尝试摄像头编号 0，避免用户没有配置时完全没法启动。
        if 0 not in sources:
            sources.append(0)

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
