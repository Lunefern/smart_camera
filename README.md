# Smart Camera

当前版本默认会先用本地摄像头 `0` 做 YOLO 识别，前端直接显示带框画面；RTSP 仍然保留在切换菜单里，后续可以切过去接其他流，并结合 `AviToFrame` 里的快速抽帧思路做关键帧处理。
当前 YOLO 识别默认使用仓库内置的 [`YOLOModel/best.pt`](/E:/DOC2/PyCharm/smart_camera/YOLOModel/best.pt)。

## 功能

- Web 页面展示实时画面
- Web 页面展示 YOLO 识别后的带框画面
- 基于相邻帧变化程度的关键帧抽取
- 传感器数据面板与趋势图
- WebSocket 实时推送
- 支持将关键帧保存到本地目录
- 支持切换到本地摄像头、RTSP 或其他视频文件
- 支持后续扩展分类、分割等 YOLO 任务

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
   └─ README.md
```

## 环境要求

- Python 3.9+

## 安装依赖

```bash
py -3 -m pip install -r requirements.txt
```

当前默认配置会直接加载 YOLO 和 `ultralytics`，`eventlet` 仍然不是必需项。
当前环境组合固定为 `opencv-python==4.11.0.86` 和 `numpy<2`，这是这套 YOLO 推理链路在本机上已经验证过的稳定组合。

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

- 自动优先读取本地摄像头 `0`
- 自动启动视频采集
- 自动启动关键帧处理
- 页面会直接显示 YOLO 识别后的带框画面
- 页面顶部会显示当前视频源状态，并提供预留的切换菜单
- 页面支持调整关键帧本地保留数量，并可手动触发清理

## 视频源配置

默认视频源由环境变量 `SMART_CAMERA_SOURCE` 控制。

可以把它设置成以下任意一种：

- 本地摄像头编号，例如 `0`
- RTSP 地址，例如 `rtsp://localhost:8554/webcam`
- 其他 RTSP 地址，例如 `rtsp://192.168.1.10/stream`
- 本地视频文件路径，例如 `D:\video\test.avi`

示例：

```powershell
$env:SMART_CAMERA_SOURCE="0"
py -3 app.py
```

## RTSP 推流示例

先启动本地 MediaMTX，再用 `ffmpeg` 把摄像头推到默认地址：

```bash
ffmpeg -f dshow -rtsp_transport tcp -i video="icspring camera" -vcodec libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 30 -bf 0 -f rtsp rtsp://localhost:8554/webcam
```

如果你使用的是其他输入设备，把 `video="icspring camera"` 改成你的实际设备名即可。

如果仍然看到 H.264 解码报错，优先检查推流端是否稳定输出了关键帧，以及 MediaMTX 是否收到了完整的 TCP 连接。

## 关键帧清理

默认情况下，关键帧会保存在 `AviToFrame/frames_local/`，并且只保留最新的一定数量。

- 默认保留数量：`200`
- 可通过页面上的“关键帧本地存储”面板调整
- 也可以通过环境变量 `FRAME_STORAGE_LIMIT` 在启动时修改默认保留值
- 如果你想立即清理旧图，可以直接在页面里点“立即清理”

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
