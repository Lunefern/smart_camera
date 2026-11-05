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
├─ run.sh                   # 启动脚本：自动使用 .venv 里的解释器
├─ diagnose_env.py          # 运行环境自检（定位 Illegal instruction 之类崩溃）
├─ requirements.txt         # PC：含 ultralytics + torch
├─ requirements-pi.txt      # 树莓派：不含 torch，走 OpenCV DNN
├─ README.md
├─ modules/
│  ├─ camera_stream.py
│  ├─ data_store.py
│  ├─ frame_processor.py
│  ├─ sensor_server.py
│  └─ yolo_processor.py
├─ templates/
│  └─ index.html
├─ YOLOModel/
│  ├─ best.pt               # torch 后端权重
│  ├─ best.onnx             # OpenCV DNN 后端权重
│  └─ classes.json          # 类别名
└─ AviToFrame/
   ├─ tmp.avi
   ├─ main.py
   └─ README.md
```

## 环境要求

- Python 3.9+（当前已在 Windows + Python 3.14、树莓派 4B + Python 3.13 上验证）
- Windows 下建议直接使用本地摄像头编号 `0~3` 或 RTSP 方式接入
- 没有独立显卡也能跑，`torch` 会自动使用 CPU 推理；树莓派等 ARM 设备用 `dnn` 后端
- 树莓派部署见下面的「树莓派 4B（2GB）部署」一节

## 安装依赖

在项目根目录执行：

```bash
python -m pip install -r requirements.txt
```

当前验证通过的依赖组合（Windows + Python 3.14 64 位，CPU 推理）：

- `flask 3.1.3`
- `flask-socketio 5.7.0` + `flask-cors 6.0.5`
- `numpy 2.5.4`
- `opencv-python 4.14.0.94`
- `ultralytics 8.4.193`
- `torch 2.15.0+cpu` + `torchvision 0.29.2`

关于版本选择：

- 不要再把 `numpy` 固定成 `numpy<2`。`numpy 1.x` 没有 Python 3.13+ 的预编译轮子，
  在 Python 3.14 上会退化成源码编译并直接失败；同时 `torch` / `ultralytics` 现在都已支持 `numpy 2.x`。
- `opencv-python` 需要 `>=4.12`，更早的版本不提供对应新解释器的轮子。
- `numpy` 和 `torch` 必须安装互相兼容的版本，否则推理时会报
  `RuntimeError: Numpy is not available`。如果遇到这个报错，先卸载两者再按
  `requirements.txt` 重装，而不是只降级 `numpy`。
- `eventlet` 不是必需项，当前项目使用的是 `threading` 模式。

## 本地运行

确认依赖装好之后，在项目根目录启动：

```bash
python app.py
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
python app.py
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
3. 按所选后端推理（`best.pt` 走 torch，`best.onnx` 走 OpenCV DNN）
4. 前端展示带框结果图和检测列表

### 两种推理后端

| 后端 | 依赖 | 适用场景 |
| --- | --- | --- |
| `torch` | `ultralytics` + `torch` | PC / 有正常 torch 轮子的平台，精度和功能最全 |
| `dnn` | 只要 `opencv-python` | 树莓派等 ARM 设备，不需要 torch，内存占用小 |
| `off` | 无 | 只做采集 / 关键帧 / 网页，不跑识别 |

后端由环境变量 `SMART_CAMERA_YOLO_BACKEND` 控制，默认 `auto`：
ARM 设备（`aarch64`）自动选 `dnn`，其余平台自动选 `torch`。

```bash
SMART_CAMERA_YOLO_BACKEND=dnn python app.py   # 强制用 OpenCV DNN
SMART_CAMERA_YOLO_BACKEND=off python app.py   # 完全不跑识别
```

`YOLOModel/best.onnx` 是 `best.pt` 的等价格权重（`imgsz=640`），类别名放在
`YOLOModel/classes.json`。两份权重保持一致，切换后端时检测结果基本一致。

页面里会显示：

- YOLO 当前状态
- 模型路径
- 输入源
- 当前任务类型

如果模型加载失败，页面会显示状态提示，前端仍然会回退显示原始画面，方便排查摄像头链路。

## 树莓派 4B（2GB）部署

### 1. 系统要求

- 必须是 **64 位系统**：`uname -m` 输出 `aarch64`
  （32 位 `armv7l` 上很多轮子不完整，YOLO 相关依赖基本装不上）
- 建议开启 swap 或 zram，避免 2GB 内存被推理峰值打满：
  `sudo apt install -y zram-tools`（或扩大 `/etc/dphys-swapfile` 里的 `CONF_SWAPSIZE`）

### 2. 安装依赖（不装 torch）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-pi.txt
```

如果 pip 编译 OpenCV 太慢或失败，直接用系统包更省事：

```bash
sudo apt install -y python3-opencv python3-numpy
```

### 3. 启动

**推荐用 `./run.sh`**：它总是用项目内 `.venv` 的解释器启动，不依赖你有没有 `source .venv/bin/activate`。

```bash
./run.sh
```

也可以手动指定解释器（效果相同）：

```bash
.venv/bin/python app.py
```

如果你习惯先激活再启动：

```bash
source .venv/bin/activate
python app.py
```

⚠️ 树莓派系统自带的 `/usr/bin/python` 里**没有装依赖**（依赖只在 `.venv` 里）。
在没激活 venv 的 shell 里直接 `python app.py` 会看到 `[启动失败] 缺少 Python 依赖: flask`，
提示里会指出 `.venv` 的位置和可用命令。

两个默认行为已经适配好：

- `SMART_CAMERA_YOLO_BACKEND=auto` 在 `aarch64` 上自动选 `dnn`，不会碰 torch
- 传感器端口 `333` 绑不上时自动回退到 `1333`，并在日志里提示

后台常驻：

```bash
setsid nohup ./run.sh > run.log 2>&1 < /dev/null &
```

2GB 机型如果想再省 CPU，可以把推理改成隔帧（`imgsz` 保持 640，因为 ONNX 是固定输入）：

```bash
SMART_CAMERA_YOLO_INTERVAL=2 ./run.sh
```

### 4. 传感器端口权限

`333` 是 1024 以下的特权端口，普通用户绑定会被拒绝。现在的行为是**自动回退到 1333**，
所以你只需要把单片机推送的目标端口改成 `1333` 即可。其它几种做法：

1. 想让程序继续监听 `333`：只给 Python 授权绑定低位端口（不用整体提权）：
   ```bash
   sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python))
   ```
2. 换一个自定义端口：`SENSOR_PORT=2333 SENSOR_PORT_FALLBACK=2333 python app.py`
3. `sudo python app.py`（不推荐，整个程序都变成 root）

绑定失败时传感器线程只会打印提示并退出，摄像头 / YOLO / 网页不受影响。

### 5. 出现 `Illegal instruction`

这是 C 扩展（numpy / OpenCV / torch）用到了当前 CPU 不支持的指令，Python 捕获不到。
先跑自检定位是哪一个库：

```bash
python diagnose_env.py
```

常见结论：

- 只有 `torch` 相关检查崩溃 → 用 `SMART_CAMERA_YOLO_BACKEND=dnn`，或直接
  `pip uninstall -y ultralytics torch torchvision`
- numpy / OpenCV 也崩溃 → 改用系统包：`sudo apt install -y python3-opencv python3-numpy`
- 全部正常但仍有问题 → 看 `/api/system/status` 接口返回的模块状态

### 6. 运行状态自查

```bash
curl -s http://127.0.0.1:5000/api/system/status | python -m json.tool
```

会返回 Python 版本、CPU 架构、YOLO 后端、传感器绑定状态和摄像头状态。

## 关键帧清理

关键帧默认保存在 `AviToFrame/frames_local/`，并且只保留最新的一定数量。

- 默认保留数量：`50`
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
- 差异阈值：`4.0`（可用 `SMART_CAMERA_DIFF_THRESHOLD` 覆盖）
- JPEG 质量：`80`
- 保存目录：`AviToFrame/frames_local/`

注意：摄像头刚启动的几秒内自动曝光/白平衡还在收敛，画面变化很大，会一次性写较多关键帧
（实测树莓派 4B 上启动瞬间 10 秒内可达 60+ 张，稳态只有约 3 张）。如果设备噪声大、
光线抖得厉害，可以把 `SMART_CAMERA_DIFF_THRESHOLD` 调到 `8~12` 以减少写盘。

## 环境变量一览

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SMART_CAMERA_SOURCE` | `0` | 视频源：摄像头编号、RTSP 地址或本地视频文件 |
| `SMART_CAMERA_PORT` | `5000` | Web 服务端口 |
| `SMART_CAMERA_DEBUG` | `1` | 是否开启 Flask debug（`0` 关闭） |
| `SMART_CAMERA_YOLO_BACKEND` | `auto` | `auto` / `torch` / `dnn` / `off`；ARM 设备自动选 `dnn` |
| `SMART_CAMERA_YOLO_IMGSZ` | `640` | 推理分辨率；DNN 后端会按 ONNX 固定输入自动纠正 |
| `SMART_CAMERA_YOLO_INTERVAL` | `1` | 每 N 帧推理一次，低算力设备可设 `2~3` |
| `SENSOR_PORT` | `333` | 传感器 UDP 监听端口 |
| `SENSOR_PORT_FALLBACK` | `1333` | Linux 上 `SENSOR_PORT` 需要特权时自动回退到的端口 |
| `FRAME_STORAGE_LIMIT` | `50` | 关键帧本地保留数量 |
| `SMART_CAMERA_DIFF_THRESHOLD` | `4.0` | 关键帧判定阈值，越大越省盘 |

## 后台运行

`nohup`、`setsid`、systemd 这类方式启动时 stdin 不是终端，flask-socketio 默认会拒绝
用 Werkzeug 开发服务器运行。本项目已在 `app.py` 里传了 `allow_unsafe_werkzeug=True`，
所以可以直接后台跑：

```bash
setsid nohup python app.py > run.log 2>&1 < /dev/null &
```

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

### 运行状态

- `GET /api/system/status`：Python 版本、CPU 架构、YOLO 后端、传感器绑定状态、摄像头状态

## WebSocket 事件

- `sensor_update`：推送传感器数据
- `camera_source_update`：推送视频源状态
- `yolo_status_update`：推送 YOLO 状态
- `frame_storage_update`：推送关键帧存储状态
- `frame_update`：推送关键帧处理结果
- `frame`：推送单帧快照
- `detection_result`：推送 YOLO 检测结果

## 传感器输入格式

- 默认地址：`0.0.0.0:333`
- 端口可用环境变量 `SENSOR_PORT` 覆盖（Linux 上小于 1024 的端口需要特权，建议用 `1333`）

传感器通过 UDP 发送 JSON，后端只接收这三个字段：

```json
{"distance": 12.4, "humidity": 60.1, "temperature": 25.3}
```

说明：

- `distance`：距离，单位 `mm`
- `humidity`：湿度
- `temperature`：温度

其它字段会被忽略。

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

- Web 服务默认端口：`5000`，可用 `SMART_CAMERA_PORT` 覆盖
- 传感器监听端口：默认 `333`，可用 `SENSOR_PORT` 覆盖

Linux / 树莓派上 `333` 属于特权端口：绑不上会自动回退到 `1333`。
如果希望继续用 `333`，给 Python 授权即可（详见树莓派部署一节）：

```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))
```

### 4. YOLO 没有结果

- 确认 `YOLOModel/best.pt`（torch 后端）或 `YOLOModel/best.onnx`（DNN 后端）存在
- `torch` 后端确认已安装 `ultralytics`；`dnn` 后端只需要 `opencv-python`
- 确认 `numpy` / `opencv-python` / `torch` 是 README 里写的兼容组合
- 如果日志里出现 `RuntimeError: Numpy is not available`，说明 `numpy` 和 `torch` 的版本互相不匹配（常见于 numpy 2.x 配了只支持 numpy 1.x 的 torch），按 `requirements.txt` 重装这两个包即可
- 如果进程直接退出、日志最后一行是 `Illegal instruction`，见上面的“树莓派部署 → 出现 Illegal instruction”

### 5. 启动时报 `[启动失败] 缺少 Python 依赖: flask`

说明依赖没装到当前解释器里。启动时的提示已经会打印「当前解释器」和「是否在虚拟环境中」，
最省事的做法是直接用项目里的启动脚本：

```bash
./run.sh
```

树莓派上最常见的坑：依赖装在 `.venv` 里，但新开的 SSH 会话没有激活 venv，
于是 `python` 指向了没装依赖的 `/usr/bin/python`。三种正确启动方式：

```bash
source .venv/bin/activate && python app.py     # 先激活
.venv/bin/python app.py                        # 直接用 venv 的解释器
./run.sh                                       # 用项目脚本（推荐）
```

Windows / PC 上如果确实没装依赖，按提示执行：

```bash
python -m pip install -r requirements.txt
```

如果 PyCharm 里运行，检查 `Settings → Project → Python Interpreter` 是否指向同一个环境。

## TODO

- 把前端 CDN 依赖改成本地静态资源
- 增加检测结果统计卡片
- 增加模型切换菜单
- 把关键帧列表做成可视化历史页面
- 增加命令行参数，方便切换输入源和阈值
