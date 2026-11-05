#!/usr/bin/env bash
# 启动脚本：始终使用项目内的虚拟环境，不依赖当前 shell 有没有 activate。
#
#   ./run.sh                  # 正常启动
#   SMART_CAMERA_YOLO_INTERVAL=2 ./run.sh
#
# 之所以需要它：树莓派上依赖装在 .venv 里，而系统的 /usr/bin/python 没有装，
# 直接 `python app.py` 会报「缺少 Python 依赖: flask」。
set -eu

cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python app.py "$@"
fi

# 没有 .venv 时退回系统 python3，并提示怎么装依赖
echo "[run.sh] 没找到 .venv，改用系统 python3 启动" >&2
echo "[run.sh] 如需隔离环境: python3 -m venv .venv && .venv/bin/pip install -r requirements-pi.txt" >&2
exec python3 app.py "$@"
