# 协作与开发流程

本文约定本仓库的分支模型、提交方式和树莓派同步方式。改动代码前请先读一遍。

## 1. 分支模型

| 分支 | 用途 | 规则 |
| --- | --- | --- |
| `dev` | 日常开发 | 所有改动先提交到这里 |
| `main` | 稳定分支 | **只通过 `--no-ff` 合并 `dev` 进入**，保留合并记录 |

要点：

- 不要在 `main` 上直接提交改动
- 合并必须用 `--no-ff`，这样 `main` 上每次都有一个明确的合并提交，
  历史和以前合并 PR 的风格一致，方便回溯「哪一批改动进了主线」
- `main` 与 `dev` 的内容在合并后是相同的，区别只是 `main` 多一个合并提交

## 2. 一次改动的标准流程

```bash
# 1) 在 dev 上开发和提交
git switch dev
git add <改动文件>
git commit -m "一句话说明改了什么"      # 需要细节时用 -m 多段

# 2) 推送 dev
git push origin dev

# 3) 用 --no-ff 合并到 main 并推送
git switch main
git merge --no-ff dev -m "Merge branch 'dev' into main：<这批改动的一句话概括>"
git push origin main
git switch dev
```

推送前确认 `git status` 干净、`git ls-remote origin refs/heads/main` 与本地 `main` 一致。

### Windows 上的注意事项

- 仓库目录可能不属于当前账户，`git` 会报 `dubious ownership`。
  **不要改全局配置**，按次放行即可：
  ```bash
  git -c safe.directory=E:/DOC2/PyCharm/smart_camera status
  ```
- 仓库没有配置 `user.name` / `user.email`，提交时用 `-c` 临时指定，
  沿用历史作者身份：`Lunefern <zch2166202772@163.com>`
- 推送凭据存在 Windows 凭据管理器（GCM），首次需要交互登录；
  脚本里加 `GIT_TERMINAL_PROMPT=0` 可避免卡在密码输入
- `core.autocrlf=true`：`.sh` 必须在仓库里保持 LF（见 `.gitattributes`），
  `run.sh` 等脚本要检查 git 里记的是 `100755`

## 3. 提交信息

- 标题用中文，一句话说清「改了什么/为什么」，不加 `feat:` 之类前缀以外的冗余
- 涉及排查过程时，把「现象 → 根因 → 做法」写进正文（这类信息半年后最有用）
- 一个提交只做一件事；顺手的小修可以单独提一个 `fix:` 提交

## 4. 树莓派同步

**树莓派无法直连 GitHub**（`git fetch` 报 `GnuTLS recv error (-110)`），
所以 Pi 上的代码用「PC 打包增量 + SFTP 传输」的方式同步：

```bash
# PC 上：以 Pi 当前 HEAD 为基点打增量包（会很小，通常几 KB）
git bundle create /tmp/sc.bundle <Pi当前HEAD>..dev

# 传到 Pi
scp /tmp/sc.bundle admin@192.168.137.77:/tmp/

# Pi 上：导入并对齐工作区
git fetch /tmp/sc.bundle 'refs/heads/dev:refs/remotes/bundle/dev'
git update-ref refs/remotes/origin/dev refs/remotes/bundle/dev
git reset --hard origin/dev
rm -f /tmp/sc.bundle
```

说明：

- `git reset --hard` 只影响被 git 跟踪的文件；运行时产物（`smart_camera.db`、
  `log.log`、`AviToFrame/frames_local/`）和语音模型目录都已被 `.gitignore` 排除，
  不会被删除
- 如果 Pi 以后能上网了，可以直接改成 `git pull`

## 5. 树莓派运行约定

```bash
cd ~/smart_camera
pkill -f '[a]pp.py'                  # 摄像头和 5000 端口都是独占的，先停旧实例
./run.sh                             # 前台；后台用 setsid nohup ./run.sh > run.log 2>&1 &
```

- 依赖只装在 `.venv`，**不要**用系统 `/usr/bin/python` 直接跑（会报缺依赖）
- PC 与 Pi 用两份依赖清单：`requirements.txt`（含 torch）/ `requirements-pi.txt`（不含 torch）
- 模型不进仓库：YOLO 用 `YOLOModel/best.onnx`（已入库），
  语音模型用 `./scripts/download_vosk_model.sh` 单独下载
- 自检：`python diagnose_env.py`（定位 `Illegal instruction` 这类崩溃）、
  `curl -s http://127.0.0.1:5000/api/system/status`（各模块状态）
