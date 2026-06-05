# Smart Camera

基于 Flask + Socket.IO ，前端可以实时查看 YOLO 识别画面、视频源状态、关键帧处理结果和传感器数据。

当前默认使用仓库内置的 [`YOLOModel/best.pt`](/E:/DOC2/PyCharm/smart_camera/YOLOModel/best.pt) 做识别，默认输入源是本地摄像头 `0`。接 RTSP、文件流或其它摄像头编号，可以直接在页面里切换。

## 功能

- Web 页面展示 YOLO 识别后的带框画面
- 支持本地摄像头、RTSP 和本地视频文件切换
- 关键帧抽取与本地保存
- 关键帧自动清理和手动清理
- 传感器数据面板与趋势图
- WebSocket 实时推送
- 预留 YOLO 分类、分割等后续扩展入口

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
- Windows 下建议直接使用本地摄像头编号 `0~3` 或 RTSP 方式接入

## 安装依赖

```bash
py -3 -m pip install -r requirements.txt
```

当前已验证的依赖组合是：

- `opencv-python==4.11.0.86`
- `numpy<2`
- `ultralytics`

`eventlet` 不是必需项，当前项目使用的是 `threading` 模式。

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
- 自动启动 YOLO 推理
- 自动启动关键帧处理
- 页面会直接显示 YOLO 识别后的带框画面
- 页面顶部会显示当前视频源和 YOLO 状态
- 页面支持调整关键帧本地保留数量，并可手动触发清理

## 视频源配置

默认视频源由环境变量 `SMART_CAMERA_SOURCE` 控制，也可以在页面里切换。

可用输入包括：

- 本地摄像头编号，例如 `0`
- RTSP 地址，例如 `rtsp://localhost:8554/webcam`
- 其他 RTSP 地址，例如 `rtsp://192.168.1.10/stream`
- 本地视频文件路径，例如 `D:\video\test.avi`

示例：

```powershell
$env:SMART_CAMERA_SOURCE="0"
py -3 app.py
```

页面上的视频源面板支持：

- 查看当前请求源、活动源和打开后端
- 预留 `0~3` 本地摄像头切换
- 输入自定义 RTSP 或本地文件路径

## RTSP 推流示例

先启动本地 MediaMTX，再用 `ffmpeg` 把摄像头推到默认地址：

```bash
ffmpeg -f dshow -rtsp_transport tcp -i video="icspring camera" -vcodec libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 30 -bf 0 -f rtsp rtsp://localhost:8554/webcam
```

把 `video="icspring camera"` 改成实际设备。如果仍然看到 H.264 解码报错，优先检查推流端是否稳定输出了关键帧，以及 MediaMTX 是否收到了完整的 TCP 连接。

## YOLO 说明

当前 YOLO 识别流程是：

1. 摄像头采集线程持续输出最新帧
2. `YoloProcessor` 从队列中读取帧
3. 使用 `YOLOModel/best.pt` 做推理
4. 前端展示带框结果图和检测列表

页面里会显示：

- YOLO 当前状态
- 模型路径
- 输入源
- 当前任务类型

如果模型加载失败，页面会显示状态提示，前端仍然会回退显示原始画面，方便排查摄像头链路。

## 关键帧清理

关键帧默认保存在 `AviToFrame/frames_local/`，并且只保留最新的一定数量。

- 默认保留数量：`200`
- 可通过页面上的“关键帧本地存储”面板调整
- 也可以通过环境变量 `FRAME_STORAGE_LIMIT` 在启动时修改默认保留值
- 可以在页面里直接点“立即清理”

## 关键帧处理

项目把 `AviToFrame` 里的快速抽帧思路接入到了主程序中，核心逻辑是：

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

### 摄像头控制

- `GET /api/camera/source`
- `POST /api/camera/source`
- `POST /api/camera/start`
- `POST /api/camera/stop`

### 关键帧存储

- `GET /api/frame/storage`
- `POST /api/frame/storage`
- `POST /api/frame/cleanup`

### 检测记录

- `GET /api/detections?n=20`

## WebSocket 事件

- `sensor_update`：推送传感器数据
- `camera_source_update`：推送视频源状态
- `yolo_status_update`：推送 YOLO 状态
- `frame_storage_update`：推送关键帧存储状态
- `frame_update`：推送关键帧处理结果
- `frame`：推送单帧快照
- `detection_result`：推送 YOLO 检测结果

## 传感器输入格式(未完成)

- `0.0.0.0:9000`

数据格式为每行一条 JSON，例如：

```json
{"temp": 25.3, "humi": 60.1, "status": "ok", "device_id": "mcu-01"}
```

## 常见问题

### 1. 打开页面后没有画面

- 检查摄像头是否被其它程序占用
- 如果想连真摄像头，设置 `SMART_CAMERA_SOURCE=0`
- 如果是 RTSP，确认地址可以被 OpenCV 打开
- 查看页面上的 YOLO 状态和视频源状态

### 2. 切换摄像头失败

- Windows 下默认后端有时不稳定，项目已经自动尝试 `DirectShow` 和 `Media Foundation`
- 如果 `0~3` 都打不开，先确认设备驱动是否正常
- 也可以先切到 RTSP 或本地文件验证采集链路

### 3. 端口被占用

- Web 服务默认端口：`5000`
- 传感器监听端口：`9000`

如果冲突，可以修改 `app.py` 和 `modules/sensor_server.py` 里的端口配置。

### 4. YOLO 没有结果

- 确认 [`YOLOModel/best.pt`](/E:/DOC2/PyCharm/smart_camera/YOLOModel/best.pt) 存在
- 确认当前环境已经安装 `ultralytics`
- 确认 `numpy` 和 `opencv-python` 版本是 README 里写的兼容组合

## TODO

- 把前端 CDN 依赖改成本地静态资源
- 增加检测结果统计卡片
- 增加模型切换菜单
- 把关键帧列表做成可视化历史页面
- 增加命令行参数，方便切换输入源和阈值
