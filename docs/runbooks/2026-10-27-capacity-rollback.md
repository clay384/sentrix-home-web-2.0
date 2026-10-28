# 并发实验隔离部署回滚（2026-10-27）

> 历史记录：下文的状态、PID及后续计划对应当时的测量版本。当前小样本结果、部署与回滚入口见[2026-10-29交付报告](2026-10-29-small-sample-performance.md)。本轮已按用户选择收口为小样本实测与全集估算。

本文提供已核对的回滚步骤，不是回滚执行记录。核查期间未启停任何进程/容器，未读取或公开私有环境内容。以下命令仅在明确执行回滚时使用；当前运行中的实验不得被这些示例打断。正常成功交付保留新 vLLM，100 WeMM 保持停止。

所有节点共同顺序：停止新派发，记录活动 run/request ID 与已知/未知结果，等待已有请求结束并核实 running/waiting=0；不能安全等待时记录终止事实及未知结果，不能自动重放。保存日志、原始指标、launch/identity SHA，随后身份核验与停止。只停止确切本轮进程，不能 `pkill` 或按模糊名称杀进程。停止失败即停在该步骤，不启动旧模型抢资源，不删除数据或结果。

## 200：独立 API / PhotoBench

根目录：`/home/sscy/benchmarks/sentrix-concurrency-20261026`。

身份依据：

- `runtime/sentrix-process.json`、`runtime/photobench-process.json`：PID、start_ticks、cwd、argv、数据根、端点。
- 建库还要核 `runtime/build-source-freeze.json`、进程记录中的 `log_receipt`、`runtime/build-environment.json` 的 SHA；只检查摘要，不打印环境内容。
- 本次只读时最后记录为第四次构建 API 1248280 / start_ticks 39115534、PB 1248561 / start_ticks 39116858，cwd 为 `build-sources/7d3b6187cf5f3a13527aa45535989ddc7df32f59`；这些历史 PID 已由父任务停止。不要复用历史 PID，下轮使用当轮 JSON。

执行前逐一比对 `/proc/<pid>/stat` 的 starttime、`/proc/<pid>/cwd`、完整 NUL 分隔 argv 与当轮记录。已退出不发信号；PID 已复用或任何身份不符立即拒绝。两控制器均有身份核验；建库控制器比对完整 argv 并维护日志凭据。

在对应 run 取消/停止派发且保留证据后，在 200 根目录使用：

```sh
# 冻结源码建库实例（由 build_control.py 启动）
python3 runtime/build_control.py stop sentrix
python3 runtime/build_control.py stop photobench

# 普通 QA 实例（由 control.py 从 repo 启动）；二选一，不混用
python3 runtime/control.py stop sentrix
python3 runtime/control.py stop photobench
```

`stop` 的 SIGTERM 等待 30 秒后仍存活会报错，不自动强杀。此时保留现场，不能继续启动别的实例占用同一数据目录。若另行按身份确认后终止了建库 API，且 PID 已消失但 receipt 尚未关闭，用 `python3 runtime/build_control.py finish sentrix` 关闭原日志区间；已 closed 不重复 finish。启动中断且未完整 bind 的记录，仅在 PID 确认消失后才允许 `abort-start sentrix`，该侧证永远不可充当成功基线。

停止确认：记录的两个 PID 确认退出；18191/18772 无本轮监听；建库 receipt 为 closed（或单独 aborted_start，不能当成功）；结果/未知请求/数据保留。200 原有服务未被替换，无需恢复别的 checkout。独立 BGE/CLIP 若也要撤回，另按 `runtime/bge-process.json` / `runtime/clip-process.json` 的 PID、start_ticks、cwd、argv 核验后停止本轮实例；不能碰旧共享 8101/8103。

## 100：停止 GPU1 新 vLLM，按需恢复原 WeMM

主机：`realmagic@192.168.0.100`。

根目录：`/home/realmagic/benchmarks/sentrix-concurrency-20261026/model-service`。

已核实存在：`OPERATIONS.md`、`control.py`、`service.json`、`launch-fixed262k-seq32-kv16p5g-xgrammar-compact.json`、`wemm-before.json`。200 镜像身份为 `runtime/model-100-identity.json`。

当前紧凑结构空白配置：API PID 1436424，start_ticks 48820917；EngineCore PID 1437396，start_ticks 48822052；launch SHA256 `764e2df33fe06a73199beecc12f68042f4de92e9880c71de746e2995773c00c7`；物理 GPU1 UUID `GPU-4ef9200c-d8f3-638a-3eeb-26fe4fa3d7b0`。执行时仍须重新核对，不能把本报告里的 PID 当永久身份。

前置核验：`service.json` 指向上述 launch；launch SHA 与验收身份一致；活进程 start_ticks/cwd/完整 argv 与记录及 launch 的 `argv` 一致；物理 GPU UUID 对应 GPU1。不要读取或打印 launch 的 environment 字段。`control.py status/stop` 自带 PID/start_ticks/cwd/vLLM 模块身份保护；执行前补全 launch SHA/完整 argv 比对。

```sh
cd /home/realmagic/benchmarks/sentrix-concurrency-20261026/model-service
python3 control.py status
# 确认运行/排队为0，或已明确记录强制终止后的未知结果
python3 control.py stop
```

确认 API 及本轮 EngineCore 子进程退出、18104 无本轮监听、GPU1 的本轮显存分配消失。不得杀 GPU0 进程。随后核验原 WeMM 配置未变，按 `wemm-before.json` 的保存命令恢复：

```sh
/home/realmagic/wemm-embedding-lab/.venv/bin/python /home/realmagic/wemm-embedding-lab/scripts/manage.py start
/home/realmagic/wemm-embedding-lab/.venv/bin/python /home/realmagic/wemm-embedding-lab/scripts/manage.py status
```

该 manager 路径已核实存在；原进程 cwd 为 `/home/realmagic/wemm-embedding-lab`，服务记录为该目录 `run/service.json`；交接配置 SHA 为 `53f77318b715bf981dcf87e71f3d590fa78b263115f6076aaf5f38213cd0c58d`。不从公开文档重建环境，使用原未改配置。不得让 WeMM 与本轮 vLLM 同时驻留 GPU1。若另行需要恢复本轮 vLLM，先停止 WeMM并确认释放，再用 `python3 control.py start --launch launch-fixed262k-seq32-kv16p5g-xgrammar-compact.json`，这是另一次明确切换，不是自动 fallback。

## 118：完整 ID 停止本轮容器，再恢复旧 llama / SGLang

主机：`orin@192.168.0.118`。

根目录：`/home/orin/VLM/qwen3.5/services/sentrix-concurrency-20261026`。

身份与交接文件：

- `kv20-c32/deployment-identity.json`、`kv20-c32/active-process.json`；后者指向 `cache-restart-7232d43f` 的进程边界证据。
- 200 的 `runtime/model-118-identity.json` 保存当前容器、镜像、Docker server ID 和进程 start_ticks 的只读观测。
- `previous-processes.private.json` 保存旧 SGLang 完整 cmd/cwd/env；`llama-systemd-unit.private.txt` / `llama-systemd-properties.private.txt` 保存旧 llama 单元；均存在，勿输出私有内容。
- `restore_previous.py` 为原恢复参考，**不直接运行**，其按名称 stop/忽略失败的问题未修。

已记录完整容器 ID：`1998ecfaf1dc2efdaa63a3a746f75fa070d0c0fdb32b54a998d4b30ae36c4658`；名称 `/sentrix-qwen35-awq-18104`；镜像 `sha256:959f59339610d5cd19f6f083b56ec710997f3bdf896afdeaec732e3d55e770b5`；Docker server ID `e5f34a49-7554-4670-97e3-75fa2aa78fa9`。最近保存 API PID 2856263 / start_ticks 27393999；执行前必须与最新活动证据相符。

安全顺序：

1. 确认模型无在途/等待任务，或已保存取消/未知证据；核 Docker server ID、完整容器 ID、名称、镜像、StartedAt，以及容器 State.Pid 对应 `/proc` start_ticks 和完整 argv。任一变化立即停止，先重新交接，不能自动接受同名新实例。
2. 仅停止完整 ID，必须检查命令成功：

```sh
docker stop --time 30 1998ecfaf1dc2efdaa63a3a746f75fa070d0c0fdb32b54a998d4b30ae36c4658
```

3. 再次按完整 ID inspect，要求 State.Running=false、State.Pid=0，并确认原 PID/本轮 GPU 子进程退出、18104 不再监听、资源已释放。按名称 inspect 必须仍解析到相同完整 ID且已停止，或明确不存在；出现同名新容器立即中止，不能启动旧模型。其它外部模型/编译任务不在本轮停止权限内。
4. 检查 8103 / 31183 未被其他进程占用。旧 llama 已健康运行则记录现状，不重复启动；31183 已占用则拒绝启动第二个 SGLang，不根据端口猜其归属。
5. 恢复旧 llama 用户单元，命令如下；核 `systemctl --user show ...` 的 ActiveState/MainPID 与服务健康，勿打印单元环境：

```sh
systemctl --user start qwen35-mixed-3p1.service
systemctl --user is-active qwen35-mixed-3p1.service
```

6. 恢复旧 SGLang 时，仅使用 `previous-processes.private.json` 中交接 PID 为 `223749` 的条目，其 cmdline/cwd/environ 与已保存脚本一致；在启动前再查 31183 空闲、步骤3的容器仍停止。可将旧 `restore_previous.py:5-13` 的加载/端口检查/启动逻辑在明确恢复操作中单独执行，**不执行该脚本第3行**。保存新 PID/start_ticks、独立恢复日志，并检查31183健康；不把私有 env/cmd 展示或提交 PR。

原 llama 单元仍 enabled；本轮容器 restart policy 为 no。若之后安排持久化开机方案，需另行协调，避免主机重启后旧服务与 vLLM 同时启动。这里只给显式回滚，不更改开机策略。

## 123：只撤回本轮私有容器 / 私有引擎

主机为 `orin@192.168.1.123`，不是最初清单中的0.123。

根目录 `/home/orin/sentrix-concurrency-20261026`。已核实：

- 根目录 `active-model.json`（不是 runtime 子目录）指向 `model-kv10-c1-csv3/deployment-identity.json` 与 validation report。
- 容器完整 ID `a129b581441fa52e41d7615705fdb04997633958cebfcef518ce0599ea9e9d5d`，名称 `/sentrix-qwen35-awq-18104-csv3`。
- 私有 socket `unix:///home/orin/sentrix-concurrency-20261026/engine/run/docker.sock`；server ID `1f35e9f1-1c7b-42df-946a-cfe8782f976e`，与系统 daemon 不同。
- `runtime/engine-processes.json` 保存私有 dockerd/containerd PID、start_ticks、cmdline SHA；`runtime/launch-plan.json` 固定私有 socket/数据目录。
- 最近保存在200身份中的模型 PID 400551/start_ticks278530204。执行时仍核完整 identity；主机旧9598/9596/8101进程保持不动。

先确认无在途，核上述私有 server ID、容器完整 ID/名称/镜像、进程 start_ticks/argv 与 active identity。停止单个新模型可执行：

```sh
docker --host unix:///home/orin/sentrix-concurrency-20261026/engine/run/docker.sock stop --time 30 a129b581441fa52e41d7615705fdb04997633958cebfcef518ce0599ea9e9d5d
```

停止确认同118：按完整ID inspect Running=false/Pid=0、原PID及模型子进程退出、18104无本轮监听；再确认旧9598/9596/8101服务PID/start_ticks与原保护记录一致。若只是停止模型，不需要停止私有引擎。

撤回整套本轮私有引擎时，先确认私有 socket 中只含本轮授权容器，再执行：

```sh
cd /home/orin/sentrix-concurrency-20261026
sudo python3 runtime/engine.py stop
```

该脚本只通过私有 socket 停止 `sentrix.capacity.task=20261026` 标签容器，然后按 PID/start_ticks/cmdline SHA 核验并 SIGTERM 私有 dockerd/containerd，保留镜像、权重、数据及日志。核 `runtime/engine-stopped.json` 与对应 PID退出。不要使用全局 Docker socket、`systemctl stop docker` 或删除 `/var/lib/docker`。本轮独立 NVIDIA wrapper/CSV 位于任务目录，停止后可原样保留；无需恢复全局运行时配置，因为从未更改。私有引擎的旧进程记录仍存在，脚本刻意拒绝直接重复 start；重新启用前需另行审阅/归档，不能删除记录伪造首次启动。

内网只读核查原件：evidence/rollback-review.md，SHA256 69053413261e4552eacd3f9decea1d656310b838f5b594ced901e103671d3682。后续变更须以当轮身份文件为准，不使用本文历史 PID 代替现场核对。
