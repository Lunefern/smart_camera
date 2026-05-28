# AviToFrame

从 `.avi` 视频中按"变化程度"提取关键帧

- 
- 界面操作过程的截图抽帧
- 后续做 OCR、标注、数据筛选前的预处理
- 对比不同算法在抽帧效果和速度上的差异

## 项目结构

```text
AviToFrame/
├─ main.py
├─ main_skimage.py
├─ fast_save_absdiff.py
├─ faster_downscale_absdiff.py
├─ tmp.avi
├─ frames/
├─ frames_skimage/
├─ frames_fast_absdiff/
└─ README.md
```

## 脚本说明

### 1. `main.py`

基础版本，使用 OpenCV 的 `absdiff` 计算当前帧与上一帧的平均像素差。

特点：

- 第一帧默认保存
- 后续帧只有在平均差异大于阈值时才保存
- 会打印保存信息和差异值
- 会弹出预览窗口，可按 `q` 提前退出

适合：

- 初步验证思路
- 观察阈值是否合适
- 需要边看边调参数的情况

默认参数：

- 输入视频：`tmp.avi`
- 输出目录：`frames`
- 差异阈值：`8`

### 2. `main_skimage.py`

使用 `skimage.metrics.structural_similarity`（SSIM）比较相邻帧的结构相似度。

特点：

- 基于图像结构相似性，而不是单纯像素差
- 当 SSIM 低于阈值时保存当前帧
- 会打印 SSIM 分数
- 也会弹出预览窗口，可按 `q` 退出

适合：

- 希望比较“画面是否真的发生变化”
- 对亮度小波动不太敏感的场景
- 对结果质量要求高于处理速度的情况

默认参数：

- 输入视频：`tmp.avi`
- 输出目录：`frames_skimage`
- SSIM 阈值：`0.88`

说明：

- SSIM 越接近 `1`，说明两帧越相似。
- 阈值越高，越容易保存更多帧。

### 3. `fast_save_absdiff.py`

快速版本，先转灰度图，再用 `absdiff` 比较，减少三通道运算开销。

特点：

- 不弹窗，不逐帧打印保存日志
- 灰度比较更快
- 支持设置 JPEG 质量
- 运行结束后输出总耗时和处理速度

适合：

- 批量处理
- 更关注速度而不是可视化过程
- 想快速得到一批去重后的关键帧

默认参数：

- 输入视频：`tmp.avi`
- 输出目录：`frames_fast_absdiff`
- 差异阈值：`6.0`
- JPEG 质量：`85`

### 4. `faster_downscale_absdiff.py`

更快版本，先把图像缩小，再转灰度后比较，只在保存时保留原始分辨率。

特点：

- 比较阶段使用低分辨率图像，速度更快
- 输出时仍保存原始帧
- 适合长视频或对吞吐量要求更高的情况

适合：

- 长时间录屏视频
- 机器性能一般但视频分辨率较高
- 对极小变化不敏感、优先追求速度

默认参数：

- 输入视频：`tmp.avi`
- 输出目录：`frames_faster_downscale`
- 比较分辨率：`320 x 180`
- 差异阈值：`4.0`
- JPEG 质量：`80`

注意：当前仓库里已有 `frames_fast_absdiff/`、`frames/`、`frames_skimage/` 目录；而这个脚本默认会输出到 `frames_faster_downscale/`，首次运行时会自动创建该目录。

## 依赖环境

建议使用 Python 3.9 及以上版本。

### 安装依赖

```bash
pip install opencv-python numpy scikit-image
```

如果只运行 `main.py`，最少需要：

```bash
pip install opencv-python
```

## 使用方法

把待处理的视频放到项目目录，并命名为 `tmp.avi`，然后执行对应脚本：

```bash
python main.py
```

或：

```bash
python main_skimage.py
python fast_save_absdiff.py
python faster_downscale_absdiff.py
```

运行后，抽取出的图片会保存到各自对应的输出目录中，文件名格式如下：

```text
frame_0001.jpg
frame_0002.jpg
...
```

## 如何调整效果

这几个脚本都采用“相邻帧比较”的思路，因此最重要的参数就是阈值。

### `main.py` / `fast_save_absdiff.py` / `faster_downscale_absdiff.py`

核心参数：`diff_threshold`

- 阈值越小：越容易判定为“发生变化”，保存的帧会更多
- 阈值越大：越不容易保存，结果更精简

建议：

- 视频变化很小或内容细腻时，可适当降低阈值
- 视频抖动明显、噪声较大时，可适当提高阈值

### `main_skimage.py`

核心参数：`ssim_threshold`

- 阈值越高：越容易保存更多帧
- 阈值越低：只在变化很明显时才保存

建议：

- 如果保存太多帧，可以降低阈值，例如从 `0.88` 调到 `0.85`
- 如果漏掉了明显变化，可以提高阈值，例如调到 `0.90` 或更高

### 速度优化建议

如果你的目标是尽快处理视频，优先尝试下面顺序：

1. `faster_downscale_absdiff.py`
2. `fast_save_absdiff.py`
3. `main.py`
4. `main_skimage.py`

通常来说：

- `SSIM` 效果更稳，但最慢
- 灰度 `absdiff` 速度快，适合大多数场景
- 缩小分辨率后比较，通常是这几个版本里最快的

## 各脚本对比

| 脚本                            | 比较方式       | 速度  | 结果精细度 | 是否弹窗 |
| ----------------------------- | ---------- | --- | ----- | ---- |
| `main.py`                     | 原图像素差      | 中   | 中     | 是    |
| `main_skimage.py`             | SSIM 结构相似度 | 慢   | 高     | 是    |
| `fast_save_absdiff.py`        | 灰度像素差      | 快   | 中     | 否    |
| `faster_downscale_absdiff.py` | 缩小后灰度像素差   | 很快  | 中等偏上  | 否    |

## 当前实现的局限

目前脚本都属于“可直接运行的实验版本”，还有一些可以继续完善的地方：

- 输入视频路径是写死的，暂不支持命令行参数
- 输出目录和阈值需要手动改源码
- 默认只针对 `.avi` 示例文件
- 没有自动清理旧输出文件
- 没有统一封装成一个可复用模块

## 后续可以继续优化的方向

如果后面准备继续完善这个项目，可以考虑增加：

- 命令行参数支持，例如 `--input`、`--output`、`--threshold`
- 支持更多视频格式，如 `.mp4`、`.mov`
- 自动跳帧处理，进一步提升速度
- 多种算法统一到一个脚本里，通过参数切换
- 输出处理统计信息到日志或 CSV
- 增加最小时间间隔控制，避免短时间内连续保存过多相似帧

## 适合怎么选

- 想先看效果、边调边跑：用 `main.py`
- 想要更稳定的相似度判断：用 `main_skimage.py`
- 想快速批量抽帧：用 `fast_save_absdiff.py`
- 想在速度优先的前提下处理高分辨率视频：用 `faster_downscale_absdiff.py`
