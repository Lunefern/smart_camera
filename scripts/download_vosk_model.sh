#!/usr/bin/env bash
# 下载 vosk 中文模型到项目根目录。
#
# 用法：
#   ./scripts/download_vosk_model.sh                    # 默认 small 模型（约 42MB）
#   ./scripts/download_vosk_model.sh vosk-model-cn-0.22 # 大模型（1.3GB，2GB 内存吃力）
#
# 模型解压后约 66MB（small），不进 git 仓库。
# 如果目标机器（比如树莓派）下载很慢，可以在这台机器上跑本脚本，再把解压出来的
# 目录整个拷过去： scp -r vosk-model-small-cn-0.22 admin@<pi>:~/smart_camera/
set -eu

cd "$(dirname "$0")/.."

MODEL="${1:-vosk-model-small-cn-0.22}"
URL="https://alphacephei.com/vosk/models/${MODEL}.zip"

if [ -d "$MODEL" ]; then
    echo "已存在: $MODEL （如需重装请先删除该目录）"
    exit 0
fi

echo "下载: $URL"
if command -v curl >/dev/null 2>&1; then
    curl -L --fail --progress-bar -o "${MODEL}.zip" "$URL"
else
    wget -O "${MODEL}.zip" "$URL"
fi

echo "校验并解压..."
python3 - <<PY
import zipfile

path = "${MODEL}.zip"
with zipfile.ZipFile(path) as z:
    bad = z.testzip()
    if bad:
        raise SystemExit("压缩包损坏: " + bad)
    z.extractall(".")
print("解压完成")
PY

rm -f "${MODEL}.zip"
if [ -d "$MODEL" ]; then
    echo "模型就绪: $MODEL"
    echo "启动时它会自动被识别（SMART_CAMERA_SPEECH_MODEL 可覆盖路径）"
else
    echo "解压后没有找到 $MODEL 目录，请检查压缩包结构" >&2
    exit 1
fi
