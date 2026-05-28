# AviToFrame

这是一个用于说明“相邻帧差异抽取关键帧”思路的小目录。当前仓库里，主程序已经把这套思路直接整合进 [`modules/frame_processor.py`](/E:/DOC2/PyCharm/smart_camera/modules/frame_processor.py)，所以这里更多是保留一个最小可读示例。

## 当前保留内容

```text
AviToFrame/
├─ main.py
├─ tmp.avi
└─ README.md
```

## 作用

- `main.py`：最基础的 OpenCV `absdiff` 抽帧示例
- `tmp.avi`：演示用样例视频
- `README.md`：说明这个示例目录的用途

## 和主项目的关系

主项目不再依赖这里的多份实验脚本，而是直接使用：

- [`modules/frame_processor.py`](/E:/DOC2/PyCharm/smart_camera/modules/frame_processor.py)
- [`AviToFrame/main.py`](/E:/DOC2/PyCharm/smart_camera/AviToFrame/main.py)

其中前者是当前 Web 项目真正运行的关键帧处理器，后者是保留给你快速理解抽帧思路的最小示例。

## 运行示例

如果想单独看看这个抽帧思路，可以直接运行：

```bash
python main.py
```

默认会读取 `tmp.avi`，并把抽出来的图片保存到运行时创建的输出目录里。

## 说明

旧的多版本实验脚本和样例输出目录已经清理掉了，避免目录里堆太多重复文件。
如果后续还要继续做算法对比，建议把不同版本统一放到单独的实验分支里，而不是继续往这个目录里堆脚本。
