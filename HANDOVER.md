# 智能摄像头项目 · 交接文档

> 这份文档是给**接手的人**看的：项目现在能做什么、跑在哪、为什么这么设计、
> 出问题怎么查、还差什么。
> 日常开发流程（分支、提交、同步）见 [CONTRIBUTING.md](CONTRIBUTING.md)，
> 安装与接口细节见 [README.md](README.md)，本文不重复，只讲交接需要知道的。
>
> **最后核对时间**：2026-11-26 15:39（树莓派在线核对）
> **对应提交**：`dev` = `2603ef6`，`main` = `3e81db5`
>
> 📌 撰写本文期间树莓派断过一次电（约 15:00–15:37），恢复后**服务再次自动拉起**、
> 各模块正常，第 4 节的数据包含这次验证。接手时仍建议先确认供电稳定。

---

## 1. 项目概况

一套跑在树莓派上的**边缘视觉 + 语音**小系统：摄像头画面做目标检测、按画面变化抽关键帧、
用麦克风做离线语音识别并用语音指令控制摄像头，同时通过 UDP 接收单片机的温湿度/距离，
全部状态通过网页实时展示。

### 1.1 当前已实现

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 实时画面 + YOLO 检测 | ✅ | 9 类目标（lamp/glasses/bottle/cup/book/pen_pencil/cellphone/bread/laptop），树莓派上约 1.4~1.6 帧/秒 |
| 视频源切换 | ✅ | 本机摄像头 0~3 / RTSP / 本地文件，页面可切 |
| 关键帧抽取 + 落盘 | ✅ | 相邻帧灰度差超阈值即保存，自动只留最新 N 张（默认 50） |
| 传感器数据接入 | ✅ | UDP JSON → 内存 + SQLite + 网页仪表盘与趋势图 |
| 离线语音识别 | ✅ | vosk small 中文模型，16kHz 单声道，实时率约 1.0x |
| 语音指令 | ✅ | **打开摄像头 / 关闭摄像头**（详见第 7 节） |
| 开机自启 | ✅ | systemd 服务，重启后约 9 秒拉起（详见第 4 节） |
| 环境自检 | ✅ | `diagnose_env.py` 用子进程隔离定位原生库崩溃 |

### 1.2 明确没做的

- 语音识别只做「转文字 + 两条指令」，没有语义理解/多轮对话
- 没有用户体系、鉴权；网页是局域网内谁都能开
- 没有视频录制、没有回放页面（关键帧只能去 `AviToFrame/frames_local/` 看）
- 没有云端同步；数据全在本机 SQLite

---

## 2. 硬件与环境

### 2.1 硬件（树莓派侧）

| 设备 | 型号/标识 | 说明 |
| --- | --- | --- |
| 主机 | Raspberry Pi 4 Model B **Rev 1.5，2GB** | 已装散热片（散热前满载 81°C 触发软限温，装后约 52°C） |
| 摄像头 | `icSpring icspring camera`（USB `32e6:9211`） | 暴露 `/dev/video0`（采集）+ `video1`（metadata）+ 声卡 card 3 |
| 麦克风 | `Timesintelli USB Audio Device`（USB `cafe:4110`） | 声卡 **card 4**，`plughw:4,0`，语音识别用这个 |
| 单片机 | 通过 WiFi/UDP 发 JSON | 目标端口 **333**（见 4.3） |

> 摄像头**自带麦克风**（card 3），但实测恒定 -70 dBFS，基本是死的，不要用它做识别。

### 2.2 软件版本

| 项目 | 树莓派（部署环境） | PC（开发环境） |
| --- | --- | --- |
| 系统 | Raspberry Pi OS，kernel `6.18.50+rpt-rpi-v8`，aarch64 | Windows 11 |
| Python | 3.13.5（虚拟环境 `.venv`） | 3.14.7 |
| numpy | 2.5.4 | 2.5.4 |
| opencv-python | 5.0.0.93 | 4.14.0.94 |
| vosk | 0.3.45 | 未安装（PC 不做语音） |
| torch / ultralytics | 装了但**不能用**（SIGILL，见 8.1） | 2.15.0+cpu / 8.4.193 |
| flask / flask-socketio | 3.1.3 / 5.7.2 | 3.1.3 / 5.7.0 |

依赖清单分两份：`requirements.txt`（PC，含 torch）/ `requirements-pi.txt`（树莓派，不含 torch）。

### 2.3 模型

| 模型 | 路径 | 大小 | 是否在仓库 |
| --- | --- | --- | --- |
| YOLO 检测（DNN 后端用） | `YOLOModel/best.onnx` | 10.1 MB | ✅ 在仓库 |
| YOLO 检测（torch 后端用） | `YOLOModel/best.pt` | 5.2 MB | ✅ 在仓库 |
| 类别名 | `YOLOModel/classes.json` | — | ✅ 在仓库 |
| 语音识别 | `vosk-model-small-cn-0.22/` | 66 MB | ❌ 不入库，用 `./scripts/download_vosk_model.sh` 下载 |

---

## 3. 架构与数据流

```
┌──────────────┐  帧   ┌──────────────┐
│ CameraStream │──────▶│  frame_queue │──┬──▶ YoloProcessor ──▶ 带框画面 + 检测列表 ──┐
│  (采集线程)   │       │  (maxsize=2) │  └──▶ FastFrameProcessor ──▶ 关键帧图片 + 事件 ─┤
└──────────────┘       └──────────────┘                                              │
┌──────────────┐  PCM  ┌──────────────┐                                             │
│  USB 麦克风   │──────▶│ arecord 子进程│──▶ vosk KaldiRecognizer ──▶ 文本 ──┬──▶ 语音指令路由
└──────────────┘       └──────────────┘                                     └──▶ 事件推送
┌──────────────┐  UDP  ┌──────────────┐
│   单片机      │──────▶│ SensorServer │──▶ DataStore(SQLite) ──▶ 事件推送
└──────────────┘       └──────────────┘
                                    全部事件 ──▶ Flask-SocketIO ──▶ 浏览器页面
```

### 模块职责

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `app.py` | 组装所有模块、HTTP 路由、WebSocket 事件、启动与优雅退出 | 端口占用预检；SIGTERM 时按序停模块 |
| `modules/camera_stream.py` | 采集线程 + 多消费者队列（只保最新帧） | 停止时先 join 再 release，避免 OpenCV 断言 |
| `modules/yolo_processor.py` | 检测：`torch`（ultralytics）/`dnn`（cv2.dnn+ONNX）双后端 | ARM 自动选 dnn；启动探测 ONNX 固定输入 |
| `modules/frame_processor.py` | 关键帧：缩放→灰度→absdiff→阈值 | 文件统计逐项容错（避免与清理线程竞争） |
| `modules/sensor_server.py` | UDP 监听传感器 JSON | 端口可配；Linux 特权端口自动回退 |
| `modules/speech_recognizer.py` | arecord→vosk 流式识别 | 自动选麦克风；RMS 电平门限；掉线重连 |
| `modules/voice_commands.py` | 识别文本 → 指令匹配 | 纯字符串逻辑，可脱离麦克风单测 |
| `modules/data_store.py` | 内存缓存 + SQLite 持久化 | 表：`sensor_log` / `detection_log` / `speech_log` |
| `diagnose_env.py` | 环境自检（子进程隔离） | 定位 `Illegal instruction` 这类原生崩溃 |
| `deploy/smart-camera.service` | systemd 单元 | 开机自启、崩溃重启、等 USB 设备就绪 |
| `run.sh` | 启动脚本 | 固定用 `.venv` 解释器，不依赖有没有 activate |

---

## 4. 树莓派部署现状

### 4.1 目录与运行方式

```
/home/admin/smart_camera/          # 代码仓库（git，分支 dev）
├─ .venv/                          # 所有 Python 依赖都在这里（系统 python 没装）
├─ YOLOModel/                      # 检测模型（在仓库）
├─ vosk-model-small-cn-0.22/       # 语音模型（不在仓库，66MB）
├─ AviToFrame/frames_local/        # 关键帧输出（gitignore）
├─ smart_camera.db                 # SQLite（gitignore）
├─ run.log                         # 服务输出（gitignore）
└─ ~/smart_camera_backup_20261103_213409/   # 首次同步前的代码备份
```

**运行方式只有一个：systemd 服务**（`smart-camera.service`）。手动 `./run.sh` 仅用于调试，
且必须先停服务（摄像头与 5000 端口都是独占的）。

### 4.2 开机自启（重点）

| 项目 | 值 |
| --- | --- |
| 单元文件（仓库） | [`deploy/smart-camera.service`](deploy/smart-camera.service) |
| 安装位置 | `/etc/systemd/system/smart-camera.service`（`100755`? 实际 `644`，root 所有） |
| 开机自启 | `systemctl is-enabled smart-camera.service` → **enabled**（`multi-user.target.wants` 软链） |
| 运行身份 | `admin:admin`，`WorkingDirectory=/home/admin/smart_camera` |
| 启动命令 | `ExecStart=/home/admin/smart_camera/run.sh`（内部 `exec .venv/bin/python app.py`） |
| 启动前等待 | `ExecStartPre` 轮询 `/dev/video0` 与 `arecord -l`，最多 30 秒，避免 USB 没枚举完就启动 |
| 崩溃重启 | `Restart=always`，`RestartSec=5` |
| 停止行为 | `KillMode=mixed` + `TimeoutStopSec=20`；进程收到 SIGTERM 后按序释放摄像头/麦克风 |
| 日志 | `journalctl -u smart-camera.service` + 追加写 `/home/admin/smart_camera/run.log` |

**实测（2026-11-26，共 4 次重启，其中 1 次是意外断电恢复）**：

| 场景 | 结果 |
| --- | --- |
| 主动 `reboot`（3 次，最后一次为最终代码） | 开机完成 → **9~25 秒**服务 `active (running)`；SSH 恢复约 **42~54 秒**；网页**自动 200** |
| 意外断电约 35 分钟后上电（1 次） | 开机 `15:37:46` → 服务 `15:38:11` 就绪；摄像头/YOLO/语音/传感器全部自动恢复 |
| 恢复后检查 | `NRestarts=0`、journal 只有 `Starting/Started`、`run.log` **0 Traceback** |
| 功能复验 | 摄像头 running、YOLO `dnn`、语音 `running`、传感器 333 绑定、语音指令「打开/关闭摄像头」可用 |

> 说明：启动耗时的差异来自 `ExecStartPre` 等 USB 设备就绪的时间（摄像头/声卡枚举快慢），
> 属于正常范围。

**如果开机后没自启，按这个顺序查**：

```bash
systemctl is-enabled smart-camera.service     # 是否 enabled
systemctl status smart-camera.service         # 是否 active / 退出码
journalctl -u smart-camera.service -b         # 本次开机的完整日志
ls -l /dev/video0 && arecord -l               # 设备是否就绪
```

### 4.3 传感器端口（需要 setcap）

单片机往 **UDP 333** 发 JSON。333 < 1024 属于特权端口，因此给 Python 加了能力：

```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))
```

已生效（进程 `CapEff=0x400`），**重启后依然有效**（文件 capability 跟着二进制走）。
如果哪天换了解释器/系统升级导致失效，程序会自动回退到 **1333** 并在日志里提示，
此时要么重新 setcap，要么把单片机目标端口改成 1333。

### 4.4 网络限制（重要）

**树莓派无法直连 GitHub**：`git fetch` 报 `GnuTLS recv error (-110)`，`codeload` 也超时；
但 PyPI、`github.com` 的 HTTP HEAD 偶尔能通（极慢）。所以：

- **Pi 上永远不要指望 `git pull`**
- 代码同步方式：PC 打包增量 bundle → SFTP → Pi 上 `git fetch <bundle>` + `reset --hard`
  （完整命令见 [CONTRIBUTING.md](CONTRIBUTING.md) 第 4 节）
- 语音模型同理：PC 下载（Pi 直连只有 14 KB/s）→ SFTP → 解压

---

## 5. 日常运维

### 5.1 服务

```bash
systemctl status smart-camera.service        # 状态 + 进程树
sudo systemctl restart smart-camera.service  # 改完代码后重启（约 20 秒可用）
sudo systemctl stop smart-camera.service     # 停止
sudo systemctl start smart-camera.service    # 启动
sudo systemctl disable smart-camera.service  # 取消开机自启
journalctl -u smart-camera.service -b -f     # 实时日志（-b 仅本次开机）
```

### 5.2 自检

```bash
cd ~/smart_camera
curl -s http://127.0.0.1:5000/api/system/status | python3 -m json.tool   # 各模块状态
.venv/bin/python diagnose_env.py                                        # 原生库自检
tail -50 run.log                                                        # 应用日志
vcgencmd measure_temp; vcgencmd get_throttled                           # 温度/降频
```

### 5.3 手动调试（必须先停服务）

```bash
sudo systemctl stop smart-camera.service
cd ~/smart_camera && ./run.sh          # 前台跑，Ctrl+C 结束
# 调完恢复服务
sudo systemctl start smart-camera.service
```

### 5.4 更新代码

1. 在 PC 上开发、提交到 `dev`、推送（流程见 CONTRIBUTING.md）
2. 打包增量：`git bundle create sc.bundle <Pi当前HEAD>..dev`
3. 传上去：`scp sc.bundle admin@192.168.137.77:/tmp/`
4. Pi 上：

```bash
cd ~/smart_camera
git fetch /tmp/sc.bundle 'refs/heads/dev:refs/remotes/bundle/dev'
git update-ref refs/remotes/origin/dev refs/remotes/bundle/dev
git reset --hard origin/dev
rm -f /tmp/sc.bundle
sudo systemctl restart smart-camera.service
```

> `reset --hard` 只影响被 git 跟踪的文件；`smart_camera.db`、`run.log`、
> `AviToFrame/frames_local/`、`vosk-model-*/` 都已被 `.gitignore` 排除，不会被删。

---

## 6. 接口清单

### HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 主页 |
| GET | `/api/system/status` | 全部模块状态（排查首选） |
| GET | `/api/sensor/latest` · `/api/sensor/history?n=50` | 传感器 |
| GET/POST | `/api/camera/source` | 查看/切换视频源 |
| POST | `/api/camera/start` · `/api/camera/stop` | 启停采集链路 |
| GET/POST | `/api/frame/storage` | 关键帧保留数量 |
| POST | `/api/frame/cleanup` | 手动清理关键帧 |
| GET | `/api/detections?n=20` | 检测记录 |
| GET | `/api/speech/status` · `/latest` · `/history?n=20` | 语音识别 |
| POST | `/api/speech/start` · `/api/speech/stop` | 启停语音识别 |
| GET | `/api/voice/commands` | 支持的指令 + 最近一次执行结果 |
| POST | `/api/voice/command` | `{"text":"打开 摄像头"}` 用文本模拟指令（免开麦测试） |

### WebSocket 事件

`camera_source_update` · `frame_storage_update` · `yolo_status_update` · `detection_result` ·
`frame_update` · `frame` · `sensor_update` · `speech_status` · `speech_partial` ·
`speech_result` · `voice_command`

---

## 7. 语音指令

| 指令 | 触发短语（去空格标点后） | 动作 |
| --- | --- | --- |
| 打开摄像头 | `打开摄像头` `开启摄像头` `启动摄像头` `打开相机` `开摄像头` `摄像头打开` | 启动采集链路 |
| 关闭摄像头 | `关闭摄像头` `关掉摄像头` `停止摄像头` `关闭相机` `关摄像头` `摄像头关闭` | 停止采集链路并释放摄像头 |

**匹配规则**：短语必须在句首/句尾或整句，多余字符总数 ≤ 2（允许两边各多 1 字）。
所以「请打开摄像头吧」命中，「把摄像头打开一点角度吧」「我待会儿去关掉那个摄像头」不命中。

**加新指令**（两步）：

1. `modules/voice_commands.py` 的 `COMMANDS` 里加一条（`id` / `description` / `phrases`）
2. `app.py` 的 `voice_router` 注册动作函数

**不用开口就能测**：

```bash
curl -X POST http://127.0.0.1:5000/api/voice/command \
     -H 'Content-Type: application/json' -d '{"text": "关闭 摄像头"}'
```

---

## 8. 关键设计决策与踩坑记录

这一节是最容易「再踩一次」的地方，按现象→根因→做法记录。

### 8.1 树莓派上 torch 推理直接 `Illegal instruction`

- **现象**：启动几秒后进程突然退出，日志最后一行 `Illegal instruction`，没有任何 Python 异常
- **根因**：torch 的 aarch64 轮子用了 Cortex-A72 不支持的 ARMv8.2 指令（dotprod/fp16），
  触发 SIGILL。Python 捕获不到，`diagnose_env.py` 用子进程隔离才定位到（numpy、OpenCV 都正常）
- **做法**：新增 **OpenCV DNN 后端**（`cv2.dnn` + `best.onnx`），ARM 上自动选 `dnn`；
  PC 仍可用 torch。`requirements-pi.txt` 不含 torch

### 8.2 `numpy<2` 在 Python 3.13+ 装不上

- **根因**：numpy 1.x 没有 3.13+ 的预编译轮子，会退化成源码编译并失败
- **做法**：`requirements.txt` 改为 `numpy>=2` + `opencv-python>=4.12`；
  同时要求 torch 与 numpy 版本匹配（不匹配会报 `RuntimeError: Numpy is not available`）

### 8.3 传感器 333 绑不上

- **根因**：Linux 上 <1024 是特权端口（Windows 上同样的 `PermissionError` 含义不同，是端口被独占）
- **做法**：`SENSOR_PORT` 可配；Linux 特权端口自动回退 `1333`；同时给出 `setcap` 方案；
  错误提示按平台区分

### 8.4 `nohup`/systemd 启动时被 flask-socketio 拒绝

- **现象**：`RuntimeError: The Werkzeug web server is not designed to run in production`
- **根因**：flask-socketio 判断 `sys.stdin.isatty()`，不是终端就拒绝（与 debug 无关）
- **做法**：`socketio.run(..., allow_unsafe_werkzeug=True)`

### 8.5 WebSocket 连接报 `on_connect() takes 0 positional arguments`

- **根因**：flask-socketio 5.x 先按 `handler(auth)` 调用；回退路径里若再抛异常整条连接都会失败
- **做法**：`on_connect(auth=None)` / `on_disconnect(reason=None)`，连接回调里每段推送单独兜底

### 8.6 关键帧 `FileNotFoundError: frame_0002.jpg`

- **根因**：清理线程与本机统计竞争，`_list_saved_files()` 之后文件被删
- **做法**：逐文件 `stat()` 容错；这个异常原来会打挂 WebSocket connect 回调

### 8.7 `systemctl restart` 连续失败 2 次才起来

- **现象**：`Main process exited, status=1`，`run.log` 是「Web 端口已被占用: 5000」，`NRestarts` 涨 2
- **根因**：① 端口预检没设 `SO_REUSEADDR`，TIME_WAIT 的残留连接被误判成占用；
  ② 进程没处理 SIGTERM，`arecord` 被 systemd SIGKILL、摄像头句柄只能靠内核回收
- **做法**：预检加 `SO_REUSEADDR`；新增 SIGTERM/SIGINT 处理，按序停关键帧→YOLO→语音→摄像头→传感器

### 8.8 摄像头「关闭再打开」后采集线程带 traceback 死掉

- **现象**：`cv2.error: (-215:Assertion failed) is_m1_vector in function 'getContinuousSize2D'`
- **根因**：`stop()` 在采集线程还卡在 `cap.read()` 时就 `release()`，OpenCV 内部状态被并发破坏
- **做法**：`stop()` 先 join 线程再 release；`read()` 包 try/except，停止流程中的异常静默退出；
  循环结束时把 `_running` 置回 False

### 8.9 麦克风把环境噪声识别成随机汉字

- **现象**：静默时数据库里出现「而是」「口号」「呃 地方」这类结果
- **实测**：这颗麦克风大部分时间输出**纯数字静音**（RMS 中位数 -100 dBFS），偶发 -27 dBFS 突发；
  频谱重心 1466Hz、低频仅 2%，**不是风扇噪声**
- **做法**：加 **RMS 电平门限**（默认 -50 dBFS，`SMART_CAMERA_SPEECH_MIN_DBFS` 可调）；
  一开始误用**峰值**做门限，尖峰（-35 dBFS）能骗过去，已改 RMS

### 8.10 依赖装好了却报 `缺少 Python 依赖: flask`

- **根因**：依赖在 `.venv` 里，新 SSH 会话没激活 venv，`python` 指向系统解释器
- **做法**：`run.sh` 固定用 `.venv`；依赖守卫会打印「是否在虚拟环境中」并检测项目内 `.venv`

---

## 9. 已知限制与风险

| 限制 | 说明 / 影响 | 可能的改进方向 |
| --- | --- | --- |
| 检测帧率低 | 树莓派 4B 上 `dnn` @640 约 **1.4~1.6 帧/秒**，CPU 250~280% | `SMART_CAMERA_YOLO_INTERVAL=2` 降载；换更大算力设备 |
| 语音与视觉抢 CPU | vosk 解码实时率约 1.0x，和 YOLO 同时跑 CPU 接近打满 | 不需要语音时 `SMART_CAMERA_SPEECH=0` 或调 `/api/speech/stop` |
| 麦克风灵敏度低 | 电平偏低、大部分时间数字静音，识别依赖说话距离（10~30cm） | 换更好的 USB 麦克风；或调低 `MIN_DBFS` |
| 指令按文本匹配 | 背景人声若完整说出「关闭摄像头」也会触发 | 加唤醒词/声纹；或提高电平门限 |
| Werkzeug 开发服务器 | 非法生产部署（已显式 `allow_unsafe_werkzeug`） | 需要时换 gunicorn/eventlet |
| Debug 模式默认开 | `SMART_CAMERA_DEBUG=1`，暴露 Werkzeug 调试页 | 局域网自用可接受；对外请设 `0` |
| 无鉴权 | 局域网内任何人都能开页面、调接口 | 加 HTTP Basic / 内网白名单 |
| 单实例 | 摄像头与 5000 端口独占；重复启动会直接退出 | 已有明确提示，无需改进 |
| Pi 无法直连 GitHub | 只能靠 bundle 同步 | 给 Pi 配代理后可 `git pull` |

**凭据与敏感信息**：仓库里**不含**任何密码、token、私钥。SSH 账号为 `admin`，
密码由项目负责人线下单独交接；Windows 端的 GitHub 凭据存在 Windows 凭据管理器（GCM）。

---

## 10. 待办与扩展建议

按性价比排序：

1. **语音指令扩展**：拍照存盘、控制单片机（开灯/关灯）、查询传感器（"现在多少度"）
2. **唤醒词**：先说「小摄像头」再接受指令，解决背景人声误触发
3. **前端本地化**：`templates/index.html` 目前从 CDN 引 socket.io-client 与 chart.js，
   断网就白板 —— 建议改成本地静态文件（README 的 TODO 里已有）
4. **关键帧查看页面**：现在只能去目录里翻图片
5. **检测结果统计**：按类别/时段出图表
6. **语音模型升级**：`vosk-model-cn-0.22`（1.3GB）准确率更高，但 2GB 内存很吃力，
   需要先评估内存或加 swap
7. **数据清理策略**：`smart_camera.db` 会一直增长（传感器每包一行），可以加保留天数

---

## 11. 排障速查表

| 症状 | 先看什么 | 常见原因 / 处理 |
| --- | --- | --- |
| 页面打不开 | `systemctl status smart-camera.service` | 服务没起 → 看 journal；端口被手动实例占用 → 停掉手动实例 |
| 页面能开但没画面 | `curl /api/system/status` 里的 `camera` | 摄像头被其它进程占用（独占）；USB 松动；`v4l2-ctl --list-devices` 确认节点 |
| 有画面没检测框 | `yolo.active_backend`、`yolo.state` | 后端为 `dnn` 但 `best.onnx` 缺失；模型加载失败会回退原始画面 |
| 进程突然消失、日志末尾 `Illegal instruction` | `python diagnose_env.py` | torch 在当前 CPU 上不可用 → 用 `SMART_CAMERA_YOLO_BACKEND=dnn` |
| 传感器没数据 | `/api/system/status` 的 `sensor.port/is_bound` | 端口 333 没权限（会回退 1333）；单片机目标端口不一致；`ss -lunp \| grep 333` |
| 语音没反应 | `/api/speech/status` 的 `state/device/last_level_dbfs` | 服务 disabled（缺模型/vosk/arecord）；麦克风被占用；电平太低被门限丢弃（看日志「忽略低电平结果」） |
| 语音识别出乱码 | 上面的 `last_level_dbfs` | 环境噪声/媒体声；调高 `SMART_CAMERA_SPEECH_MIN_DBFS`；说话靠近麦克风 |
| `systemctl restart` 失败 | journal + `run.log` | 端口 TIME_WAIT 误判（已修）；手动实例还在跑 |
| 开机后没自启 | `systemctl is-enabled` / `journalctl -b` | 未 enable；`ExecStartPre` 等设备超时；服务报错退出 |
| 树莓派连不上 SSH | 供电、网线/WiFi、IP 是否变 | 本文写作时它就离线了；重启后 IP 若变化需重新确认（路由器里看 `smartCamera`） |

---

## 12. 交接清单

- [ ] **能连上树莓派**：`ssh admin@192.168.137.77`（密码线下交接；IP 由路由器 DHCP，建议在路由器上做静态绑定）
- [ ] **确认服务自启**：重启一次，等约 60 秒，`systemctl is-active smart-camera.service` 应为 `active`
- [ ] **打开网页确认功能**：`http://192.168.137.77:5000` → 画面有框、传感器有数、语音卡片状态正常
- [ ] **确认语音**：对着麦克风说「打开 摄像头」/「关闭 摄像头」
- [ ] **拿到仓库**：<https://github.com/Lunefern/smart_camera>（`dev` 开发、`main` 稳定）
- [ ] **知道语音模型怎么来**：`./scripts/download_vosk_model.sh`（66MB，不在仓库）
- [ ] **知道 Pi 不能直连 GitHub**：更新代码走 bundle 同步（4.4 / CONTRIBUTING.md）
- [ ] **知道现有备份位置**：`~/smart_camera_backup_20261103_213409/`
- [ ] **知道数据在哪**：`smart_camera.db`（传感器/检测/语音记录）、`AviToFrame/frames_local/`（关键帧图片）
- [ ] **知道怎么排出厂设置**：删 `smart_camera.db` 与 `frames_local/` 即可清空历史（模型与代码不受影响）
