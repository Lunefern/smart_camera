"""
modules/speech_recognizer.py

语音识别层（vosk + arecord）。

职责很单一：
1. 用 `arecord` 从 USB 麦克风读 16kHz / 16bit / 单声道 PCM
2. 交给 vosk 的 `KaldiRecognizer` 做**离线**识别
3. 把「中间结果」和「最终结果」通过 WebSocket 推给前端，最终结果同时写进存储层

为什么用 arecord 而不是 pyaudio：
- 树莓派上 `arecord` 随 alsa-utils 一起就有；pyaudio 要装 portaudio 开发包，在 ARM 上更容易出错
- `arecord -t raw` 直接输出裸 PCM，可以原样喂给 vosk

线程模型：
- 一个后台线程负责「读音频 → 喂识别器 → 推结果」，`arecord` 是它拉起的子进程
- `stop()` 先 terminate 子进程，让阻塞中的 `read()` 立刻返回，线程随之退出
- 设备被拔掉 / 占用时不会退出线程，而是按退避策略重连，并在状态里体现

环境变量（在 app.py 里读取后传进来）：
- `SMART_CAMERA_SPEECH`         auto / 1 / 0
- `SMART_CAMERA_SPEECH_MODEL`   模型目录
- `SMART_CAMERA_SPEECH_DEVICE`  ALSA 设备，例如 `plughw:4,0`
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import vosk
except ImportError:  # 未安装时模块仍可导入，只是启动后状态显示"未安装"
    vosk = None

try:
    import numpy as np
except ImportError:
    np = None


# vosk 要求 16kHz / 16bit / 单声道，写死避免调用方传错。
SAMPLE_RATE = 16000
# 每次从 arecord 读多少秒的音频。0.2s 在延迟和 CPU 之间比较平衡。
CHUNK_SECONDS = 0.2

# 自动选择录音设备时要排除的关键字：内置声卡和摄像头自带的麦克风都不可靠
_EXCLUDE_KEYWORDS = ("camera", "hdmi", "headphone", "bcm2835")


def list_capture_devices() -> list[dict]:
    """用 `arecord -l` 列出系统上的录音设备。

    返回 [{"card": 4, "device": 0, "alsa": "plughw:4,0", "name": ..., "note": ...}, ...]
    """
    if shutil.which("arecord") is None:
        return []

    try:
        proc = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    devices: list[dict] = []
    # 形如: card 4: Device [USB Audio Device], device 0: USB Audio [USB Audio]
    pattern = re.compile(r"^card (\d+): (\S+) \[([^\]]*)\], device (\d+): (.*)$")
    for line in proc.stdout.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        card, card_id, card_desc, device, dev_desc = match.groups()
        devices.append({
            "card": int(card),
            "device": int(device),
            "alsa": f"plughw:{card},{device}",
            "name": card_desc.strip(),
            "card_id": card_id,
            "description": dev_desc.strip(),
        })
    return devices


def detect_capture_device(preferred: str | None = None) -> str | None:
    """挑一个可用于语音识别的录音设备。

    优先用调用方指定的；否则跳过摄像头/HDMI/板载声卡，剩下的第一个就是外接 USB 麦克风。
    """
    if preferred:
        return preferred

    devices = list_capture_devices()
    if not devices:
        return None

    for dev in devices:
        text = f"{dev['name']} {dev['card_id']} {dev['description']}".lower()
        if any(keyword in text for keyword in _EXCLUDE_KEYWORDS):
            continue
        return dev["alsa"]

    # 全都被排除了，那就退回第一个，总比不录音好
    return devices[0]["alsa"]


class SpeechRecognizer:
    def __init__(
        self,
        socketio,
        store=None,
        model_dir: str | Path | None = None,
        device: str | None = None,
        sample_rate: int = SAMPLE_RATE,
        enabled: str | bool = "auto",
        partial_interval: float = 0.5,
        min_dbfs: float = -50.0,
        text_callback=None,
    ):
        self.socketio = socketio
        self.store = store
        # 每句话的最终文本除了推送/存库，还会交给这个回调（用于语音指令解析）
        self.text_callback = text_callback
        self.model_dir = Path(model_dir) if model_dir else None
        # 为 None 时在启动阶段自动探测
        self.device = device
        self.sample_rate = int(sample_rate)
        self.enabled_setting = enabled
        self.partial_interval = float(partial_interval)
        # 电平门限：低于这个 dBFS 的片段不产出结果。
        # 麦克风灵敏度低时环境噪声会被强行解码成随机汉字，加门限可以避免刷屏。
        self.min_dbfs = float(min_dbfs)

        self._model = None
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._latest: dict | None = None
        self._active_device: str | None = None
        # 最近一次判定时测到的电平，放在状态里方便调门限
        self._last_level_db: float | None = None
        self._status: dict = {
            "state": "idle",
            "message": "等待启动",
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "device": None,
            "sample_rate": self.sample_rate,
            "available_devices": [],
        }

    # ── 生命周期 ──────────────────────────────────────────────
    @property
    def model_ready(self) -> bool:
        return bool(self.model_dir and self.model_dir.exists())

    def should_enable(self) -> bool:
        """把 auto/1/0 三种配置解析成布尔值。

        auto 的含义：模型目录存在、vosk 装了、麦克风也在，才自动开启。
        缺少任何一项都只打印一行提示，不影响主程序。
        """
        setting = str(self.enabled_setting).strip().lower()
        if setting in ("0", "false", "off", "no"):
            return False
        if setting in ("1", "true", "on", "yes"):
            return True
        # auto
        return vosk is not None and self.model_ready and shutil.which("arecord") is not None

    def start(self):
        if self._running:
            return
        if not self.should_enable():
            reason = self._auto_disable_reason()
            self._emit_status("disabled", reason)
            print(f"[Speech] {reason}")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[Speech] 语音识别线程启动")

    def stop(self):
        self._running = False
        self._terminate_proc()
        self._emit_status("stopped", "语音识别已停止")
        print("[Speech] 语音识别线程停止")

    def get_status(self) -> dict:
        status = dict(self._status)
        status["enabled_setting"] = str(self.enabled_setting)
        status["model_exists"] = self.model_ready
        status["running"] = self._running
        # _status 只在状态变化时刷新，这里补上实时电平，方便现场调门限
        status["last_level_dbfs"] = (
            round(self._last_level_db, 1) if self._last_level_db is not None else None
        )
        return status

    def get_latest(self) -> dict:
        return dict(self._latest) if self._latest else {}

    # ── 内部实现 ──────────────────────────────────────────────
    def _auto_disable_reason(self) -> str:
        if vosk is None:
            return "未安装 vosk（pip install vosk）"
        if not self.model_ready:
            return f"模型目录不存在: {self.model_dir}"
        if shutil.which("arecord") is None:
            return "未找到 arecord（sudo apt install alsa-utils）"
        return "语音识别已关闭"

    def _run(self):
        devices = list_capture_devices()
        self._status["available_devices"] = devices
        if not devices:
            self._fail("没有找到任何录音设备（arecord -l 为空）")
            return

        device = detect_capture_device(self.device)
        if not device:
            self._fail("没有可用的录音设备")
            return
        self._active_device = device

        # 模型加载比较耗时（树莓派上约 1~2 秒），放到线程里以免拖慢整个应用启动
        self._emit_status("loading", f"正在加载模型: {self.model_dir.name}")
        try:
            vosk.SetLogLevel(-1)  # 关掉 kaldi 的 INFO 日志，否则会刷屏
            self._model = vosk.Model(str(self.model_dir))
        except Exception as exc:
            self._fail(f"模型加载失败: {exc}")
            return

        recognizer = vosk.KaldiRecognizer(self._model, self.sample_rate)
        delay = 2.0
        while self._running:
            try:
                self._stream(recognizer, device)
                delay = 2.0
            except Exception as exc:
                print(f"[Speech] 音频流异常: {exc}")
            if not self._running:
                break
            # 设备掉线/被占用时退避重试，不退出线程
            self._emit_status("error", f"音频流中断，{delay:.0f}s 后重试（{self._active_device}）")
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)

    def _stream(self, recognizer, device: str):
        """拉起 arecord 并持续识别，直到停止或设备断开。"""
        if shutil.which("arecord") is None:
            return
        cmd = [
            "arecord",
            "-D", device,
            "-f", "S16_LE",
            "-r", str(self.sample_rate),
            "-c", "1",
            "-t", "raw",
            "-q",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._proc = proc
        self._emit_status("running", f"正在监听 {device}")
        chunk_bytes = int(self.sample_rate * 2 * CHUNK_SECONDS)

        last_partial = ""
        last_partial_at = 0.0
        # 当前这句话里“最响的那一小段 RMS”，用来判断结果是否可信。
        # 注意用 RMS 而不是峰值：环境噪声常有单个尖峰（峰值能到 -35 dBFS），
        # 但整体能量很低（RMS 约 -61 dBFS），用峰值做门限会被尖峰骗过去。
        seg_rms_max = 0.0
        try:
            while self._running:
                data = proc.stdout.read(chunk_bytes)
                if not data:
                    # 进程退出或设备断开
                    break
                rms = self._chunk_rms(data)
                seg_rms_max = max(seg_rms_max, rms)
                if recognizer.AcceptWaveform(data):
                    text = json.loads(recognizer.Result()).get("text", "").strip()
                    heard_db = 20 * math.log10(seg_rms_max / 32768.0) if seg_rms_max > 0 else -99.0
                    seg_rms_max = 0.0
                    last_partial = ""
                    self._last_level_db = heard_db
                    if text and heard_db >= self.min_dbfs:
                        self._publish(text, final=True, dbfs=heard_db)
                    elif text:
                        # 电平太低，基本可以确定是环境噪声被硬解出来的
                        print(f"[Speech] 忽略低电平结果 ({heard_db:.0f} dBFS < {self.min_dbfs:.0f}): {text}")
                else:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                    now = time.time()
                    heard_db = 20 * math.log10(seg_rms_max / 32768.0) if seg_rms_max > 0 else -99.0
                    if (
                        partial
                        and partial != last_partial
                        and now - last_partial_at >= self.partial_interval
                        and heard_db >= self.min_dbfs
                    ):
                        last_partial = partial
                        last_partial_at = now
                        self._publish(partial, final=False, dbfs=heard_db)
        finally:
            self._terminate_proc()

    @staticmethod
    def _chunk_rms(data: bytes) -> float:
        """返回这段 16bit PCM 的 RMS 幅度（0~32768），用于电平门限判断。"""
        if np is None or not data:
            return 32768.0  # 没有 numpy 就不做门限，保证功能可用
        try:
            samples = np.frombuffer(data, dtype=np.int16)
        except ValueError:
            return 0.0
        if samples.size == 0:
            return 0.0
        as_float = samples.astype(np.float32)
        return float(np.sqrt(np.mean(as_float * as_float)))

    def _terminate_proc(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _publish(self, text: str, final: bool, dbfs: float | None = None):
        payload = {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "is_final": final,
            "device": self._active_device,
            "model": self.model_dir.name if self.model_dir else None,
        }
        if dbfs is not None:
            payload["dbfs"] = round(dbfs, 1)
        if final:
            self._latest = payload
            if self.store is not None:
                try:
                    self.store.save_speech(payload)
                except Exception as exc:
                    print(f"[Speech] 保存识别结果失败: {exc}")
            self.socketio.emit("speech_result", payload)
            # 最终文本交给指令解析；解析失败不能影响识别主流程
            if self.text_callback is not None:
                try:
                    self.text_callback(text)
                except Exception as exc:
                    print(f"[Speech] 指令回调异常: {type(exc).__name__}: {exc}")
        else:
            self.socketio.emit("speech_partial", payload)

    def _fail(self, message: str):
        self._running = False
        self._emit_status("error", message)
        print(f"[Speech] {message}")

    def _emit_status(self, state: str, message: str):
        self._status = {
            "state": state,
            "message": message,
            "model_dir": str(self.model_dir) if self.model_dir else None,
            "device": self._active_device,
            "sample_rate": self.sample_rate,
            "min_dbfs": self.min_dbfs,
            "last_level_dbfs": round(self._last_level_db, 1) if self._last_level_db is not None else None,
            "available_devices": list_capture_devices(),
            "running": self._running,
        }
        self.socketio.emit("speech_status", self._status)
