# Smart Camera

当前版本已经做了本地化处理，默认会优先使用仓库自带的样例视频 [`AviToFrame/tmp.avi`](/E:/DOC2/PyCharm/smart_camera/AviToFrame/tmp.avi) 作为视频源，并结合 `AviToFrame` 里的快速抽帧思路做关键帧处理。

## 功能

- Web 页面展示实时画面
- 基于相邻帧变化程度的关键帧抽取
- 传感器数据面板与趋势图
- WebSocket 实时推送
- 支持将关键帧保存到本地目录
- 支持切换到本地摄像头、RTSP 或其他视频文件

## 项目结构

```text
smart_camera/
├─ app.py
├─ requirements.txt
├─ README.md
├─ modules/
│  ├─ camera_stream.py
│  ├─ data_store.py
│  ├─ frame_processor.py
│  ├─ sensor_server.py
│  └─ yolo_processor.py
├─ templates/
│  └─ index.html
└─ AviToFrame/
   ├─ tmp.avi
   ├─ main.py
   ├─ fast_save_absdiff.py
   ├─ faster_downscale_absdiff.py
   ├─ main_skimage.py
   └─ README.md
```

## 环境要求

- Python 3.9+

## 安装依赖

```bash
py -3 -m pip install -r requirements.txt
```

当前默认配置不强依赖 YOLO 和 `eventlet`。

## 本地运行

直接启动：

```bash
py -3 app.py
```

启动后访问：

```text
http://127.0.0.1:5000
```

默认行为：

- 自动优先读取 `AviToFrame/tmp.avi`
- 自动启动视频采集
- 自动启动关键帧处理
- 页面会直接显示最新抽到的关键帧

## 视频源配置

默认视频源由环境变量 `SMART_CAMERA_SOURCE` 控制。

可以把它设置成以下任意一种：

- 本地摄像头编号，例如 `0`
- RTSP 地址，例如 `rtsp://192.168.1.10/stream`
- 本地视频文件路径，例如 `D:\video\test.avi`

示例：

```powershell
$env:SMART_CAMERA_SOURCE="0"
py -3 app.py
```

## 关键帧处理

项目已经把 `AviToFrame` 里的快速抽帧思路接入到主程序中，核心逻辑是：

1. 先把帧缩小到较低分辨率
2. 再转成灰度图
3. 用相邻帧的 `absdiff` 计算平均差异
4. 当差异超过阈值时保存并推送为关键帧

默认参数：

- 比较分辨率：`320 x 180`
- 差异阈值：`4.0`
- JPEG 质量：`80`
- 保存目录：`AviToFrame/frames_local/`

## 后端接口

### 页面

- `GET /`：主页

### 传感器

- `GET /api/sensor/latest`
- `GET /api/sensor/history?n=50`

### 检测记录

- `GET /api/detections?n=20`

### 摄像头控制

- `POST /api/camera/start`
- `POST /api/camera/stop`

## WebSocket 事件

- `sensor_update`：推送传感器数据
- `frame_update`：推送关键帧处理结果
- `frame`：推送单帧快照
- `detection_result`：YOLO 检测结果

## 传感器输入格式

如果接入单片机数据，默认监听：

- `0.0.0.0:9000`

数据格式为每行一条 JSON，例如：

```json
{"temp": 25.3, "humi": 60.1, "status": "ok", "device_id": "mcu-01"}
```

## 可选的 YOLO

仓库里保留了 `modules/yolo_processor.py`，如果你后续想重新启用 YOLO 检测，可以再安装：

```bash
py -3 -m pip install ultralytics
```

然后把 `app.py` 里的视频处理链路切回 YOLO 逻辑即可。

## 常见问题

### 1. 打开页面后没有画面

- 检查 `AviToFrame/tmp.avi` 是否存在
- 如果想连真摄像头，设置 `SMART_CAMERA_SOURCE=0`
- 如果是 RTSP，确认地址可以被 OpenCV 打开

### 2. 端口被占用

默认 Web 服务端口是 `5000`，传感器监听端口是 `9000`。

如果占用冲突，可以修改 `app.py` 和 `modules/sensor_server.py` 里的端口配置。

### 3. 画面不更新

- 确认页面已经启动
- 确认视频源是可读的
- 可以点击“刷新帧”按钮手动请求最新画面

## 后续处理

- 把前端静态资源从 CDN 改为本地依赖
- 把关键帧保存记录做成页面可查看列表
- 给视频源配置做成页面可选项
- 增加命令行参数，方便切换输入文件和阈值
