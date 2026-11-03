#!/usr/bin/env python3
"""
diagnose_env.py

运行环境自检脚本，专门用来定位这类“没有 Python 报错、进程直接挂掉”的问题：

    Illegal instruction

这种崩溃发生在 C 扩展（numpy / OpenCV / torch）里，Python 的 try/except 捕获不到，
所以本脚本把每一项检查都放进独立子进程里跑：子进程被信号杀死时，主进程仍然活着，
从而能准确指出到底是哪个库、哪一步在当前 CPU 上非法。

用法：

    python diagnose_env.py

输出示例：

    [OK]   numpy 导入 + 运算
    [崩溃] torch 导入 + 矩阵乘  (signal SIGILL)
           -> 建议：用 DNN 后端...

只依赖标准库，不需要任何第三方包。
"""
from __future__ import annotations

import importlib.metadata as metadata
import platform
import subprocess
import sys

# 每项检查：名称 + 一段在子进程里执行的代码 + 失败时的建议
CHECKS: list[tuple[str, str, str]] = [
    (
        "numpy 导入 + 数组运算",
        "import numpy as np;"
        "a = np.arange(9, dtype=np.float32).reshape(3, 3);"
        "print('numpy', np.__version__, float((a @ a).sum()))",
        "numpy 的轮子和当前 CPU 指令集不匹配。改用系统包："
        "sudo apt install -y python3-numpy",
    ),
    (
        "OpenCV 导入 + 图像运算",
        "import numpy as np, cv2;"
        "img = np.zeros((240, 320, 3), dtype=np.uint8);"
        "small = cv2.resize(img, (160, 90));"
        "gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY);"
        "cv2.absdiff(gray, gray);"
        "cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 70]);"
        "print('cv2', cv2.__version__)",
        "OpenCV 的轮子和当前 CPU 不匹配。改用系统包："
        "sudo apt install -y python3-opencv（或用 OPENCV_CPU_DISABLE=NEON 之类的兜底，"
        "最简单还是换包）",
    ),
    (
        "torch 导入 + 矩阵乘",
        "import torch;"
        "x = torch.randn(64, 64);"
        "print('torch', torch.__version__, float((x @ x).sum()))",
        "torch 的 aarch64 轮子用到了 ARMv8.2 指令（dotprod/fp16），"
        "树莓派 4B 的 Cortex-A72 不支持，会直接 SIGILL。"
        "改用 OpenCV DNN 后端：export SMART_CAMERA_YOLO_BACKEND=dnn",
    ),
    (
        "ultralytics 导入",
        "import ultralytics; print('ultralytics', ultralytics.__version__)",
        "ultralytics 会连带导入 torch，torch 崩溃时这里也会崩。"
        "在树莓派上建议直接不装 ultralytics + torch，只用 DNN 后端。",
    ),
    (
        "YOLO torch 推理 (best.pt)",
        "import sys, numpy as np;"
        "from pathlib import Path;"
        "from ultralytics import YOLO;"
        "p = Path('YOLOModel/best.pt');"
        "assert p.exists(), f'模型不存在: {p.resolve()}';"
        "m = YOLO(str(p));"
        "r = m.predict(np.zeros((480, 640, 3), dtype=np.uint8), verbose=False, device='cpu');"
        "print('torch 推理 OK, results =', len(r))",
        "torch 推理在当前 CPU 上会崩溃，请切换到 DNN 后端"
        "（export SMART_CAMERA_YOLO_BACKEND=dnn）。",
    ),
    (
        "OpenCV DNN 推理 (best.onnx)",
        "import numpy as np, cv2;"
        "from pathlib import Path;"
        "p = Path('YOLOModel/best.onnx');"
        "assert p.exists(), f'ONNX 不存在: {p.resolve()}（先用 best.pt 导出，或从开发机拷贝过来）';"
        "net = cv2.dnn.readNetFromONNX(str(p));"
        "net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV);"
        "net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU);"
        "blob = cv2.dnn.blobFromImage(np.zeros((640, 640, 3), dtype=np.uint8), 1/255.0, (640, 640), swapRB=True);"
        "net.setInput(blob);"
        "out = net.forward();"
        "print('DNN 推理 OK, 输出形状 =', out.shape if hasattr(out, 'shape') else 'n/a')",
        "OpenCV 本身在这个 CPU 上就有问题，建议装系统版 OpenCV："
        "sudo apt install -y python3-opencv",
    ),
]


def _signal_name(returncode: int) -> str:
    """把子进程的负返回值翻译成信号名，例如 -4 -> SIGILL。"""
    import signal

    try:
        name = signal.Signals(-returncode).name
    except (ValueError, TypeError):
        return f"exit={returncode}"
    extra = {
        "SIGILL": "（Illegal instruction，CPU 指令集不兼容）",
        "SIGSEGV": "（段错误，通常是二进制和系统库不匹配）",
        "SIGBUS": "（总线错误，常见于内存不足或对齐问题）",
        "SIGKILL": "（被系统杀掉，常见于内存不足 OOM）",
    }.get(name, "")
    return f"{name}{extra}"


def run_check(name: str, code: str, hint: str) -> str:
    """在子进程里执行一段检查代码，返回 PASS / FAIL / CRASH。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        print(f"[超时] {name}")
        return "timeout"

    if proc.returncode == 0:
        detail = (proc.stdout or "").strip().splitlines()
        print(f"[OK]   {name}" + (f"  ({detail[-1]})" if detail else ""))
        return "pass"

    if proc.returncode < 0:
        # 被信号杀死：这是最关键的信号，说明发生了非法指令之类的硬件级错误。
        print(f"[崩溃] {name}  {_signal_name(proc.returncode)}")
        print(f"       -> {hint}")
        return "crash"

    stderr = (proc.stderr or "").strip().splitlines()
    last = stderr[-1] if stderr else f"exit={proc.returncode}"
    print(f"[失败] {name}  {last}")
    print(f"       -> {hint}")
    return "fail"


def print_environment():
    print("=" * 68)
    print("运行环境")
    print("-" * 68)
    print(f"Python      : {platform.python_version()}  ({sys.executable})")
    print(f"系统        : {platform.system()} {platform.release()}")
    print(f"CPU 架构    : {platform.machine()}")
    print(f"完整平台    : {platform.platform()}")
    print(f"CPU 核心数  : {__import__('os').cpu_count()}")

    # 树莓派：把型号和内存读出来，方便判断是不是 2G 版本
    try:
        with open("/proc/device-tree/model", "r", encoding="utf-8", errors="ignore") as fp:
            print(f"设备型号    : {fp.read().strip()}")
    except OSError:
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("MemTotal"):
                    print(f"内存        : {line.strip()}")
                    break
    except OSError:
        pass

    print("-" * 68)
    print("关键依赖版本")
    print("-" * 68)
    for dist in ("numpy", "opencv-python", "opencv-python-headless", "torch", "torchvision",
                 "ultralytics", "flask", "flask-socketio", "onnxruntime"):
        try:
            print(f"{dist:26s}: {metadata.version(dist)}")
        except metadata.PackageNotFoundError:
            print(f"{dist:26s}: 未安装")
    print("=" * 68)


def main() -> int:
    print_environment()
    print("各项检查（每项都在独立子进程里执行，崩溃不会影响本脚本）")
    print("-" * 68)

    results: dict[str, str] = {}
    for name, code, hint in CHECKS:
        results[name] = run_check(name, code, hint)

    print("=" * 68)
    print("结论")
    print("-" * 68)

    arch = platform.machine().lower()
    dnn_ok = results.get("OpenCV DNN 推理 (best.onnx)") == "pass"
    torch_ok = results.get("YOLO torch 推理 (best.pt)") == "pass"
    torch_crash = results.get("torch 导入 + 矩阵乘") == "crash"
    torch_missing = results.get("torch 导入 + 矩阵乘") == "fail"

    if arch not in ("aarch64", "x86_64", "amd64", "arm64"):
        print(f"注意：当前架构是 {arch}，32 位系统上很多轮子都不完整，建议换 64 位系统。")

    if torch_ok and dnn_ok:
        print("两种推理后端都可用，用默认配置即可。")
    elif torch_crash or torch_missing:
        if dnn_ok:
            print("torch 后端在当前设备上不可用（见上面的崩溃/失败信息），但 OpenCV DNN 可用。")
            print("请这样启动（不需要 torch，内存占用也小很多）：")
            print()
            print("    SMART_CAMERA_YOLO_BACKEND=dnn python app.py")
            print()
            print("想彻底不加载 torch，可以在树莓派上卸载它：")
            print("    pip uninstall -y ultralytics torch torchvision")
        else:
            print("torch 和 OpenCV DNN 都不可用，先解决 OpenCV / numpy 的安装问题：")
            print("    sudo apt install -y python3-opencv python3-numpy")
            print("    python app.py   # 仍可用 SMART_CAMERA_YOLO_BACKEND=off 先只跑采集和网页")
    elif not dnn_ok and torch_ok:
        print("OpenCV DNN 不可用，但 torch 正常，直接用默认配置即可。")
        print("如果 OpenCV DNN 检查失败是因为找不到 best.onnx，从开发机拷一份到 YOLOModel/ 即可。")
    else:
        print("请根据上面每一项的提示逐一处理。")

    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
