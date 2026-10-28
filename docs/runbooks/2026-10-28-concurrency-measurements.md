# 并发实测与共享记忆交付记录（2026-10-28）

已交付可复用的 **accepted DATA cache**、独立模型端点与原生满窗有限批次证据。100 当前配置已验证至少 8 个客户端能排队完成满窗请求；真实 QA 的最大稳定并发和吞吐最佳并发尚未确定，1167 题全集未完成。153 主服务未修改，200 结果不能证明各台 M4 Mac mini 已通过相同并发。

## 共享记忆数据

[脱敏记忆清单](../benchmarks/evidence/accepted-memory-v3-public-manifest.json) SHA256：
`bfab1a31d187d9af2559d1c7c5657835f2b0b4955774450d2360455b9cbf27dd`。

Snapshot v3 共 1452 文件，完整私有 manifest SHA256 为 `b229cccf2f95528f1514e660018d4d319654b8d95725f7c2bd758c6cf4c2b4bc`，identity 文件 SHA256 为 `b19ce32c68513cf29908ced2c5909043bad0fbf334825a4dda7d6d6c244a76aa`。Producer 固定为 `e818be8fc3c12af1b7dbfdc31292e19e05c95aac`。

全 scope 362 项 processed、362 observations、178 events；344 个视觉向量与 Qdrant 点逐项一致，688 个 observation 文本向量完整；SQLite/FK、媒体和向量检查通过，7428 nodes / 16071 edges 与冻结源码离线投影逐项相同。

选用 343 项父库成功源观察和 1 项新观察；65 个正常完成的 moments 请求对应 57 行新结果，另复用 2 行未变父 moments 和 10 份逐行哈希未变父画像。画像的 25 个唯一 moment 引用中，23 份当前正文已变化；画像仍依据父版本生成，引用 ID 和证据锚点有效，不能说依赖正文完全一致。

这是明确组合缓存的**数据验收，不等于原建库流程成功**。父构建仍为 completed_with_errors，原 unknown、新画像刷新 unknown 和整个 repair RPC 未知原样保留。repair 的 swap 告警及保护结束后的采样缺口也保留，不构成容量通过。普通错字、漏识和答错按模型质量记录，不用“内容全对”作为门槛。

复用要求完整 migration **filename / version / SQL hash 集合相等**；新增、删除、改名、同名内容变化都拒绝认证恢复。legacy BGE-M3 1024 维、Chinese-CLIP 768 维必须匹配清单 semantic identity，包括权重、tokenizer、预处理；服务地址不属于兼容键。各运行独立恢复 SQLite/Qdrant，不共用活动写库。改变生产语义需新身份和新基线；QA-only 分支保留原 producer 来源。

## 当前模型配置与验收范围

以下是当前保存的部署身份，调用或回滚前仍核现场完整身份。三端点模型别名均为 `qwen35-4b-awq`，AWQ W4A16 group32、激活/视觉 BF16；原生输入加输出 262144 tokens，无 RoPE 外推。共同权重 SHA256：
`902477edf53bc6768bd1f212dd1866856fd5a0627def06887780c95900ffb013`。

| 节点 | OpenAI 兼容端点 | 引擎 | KV / max_num_seqs | 已验证范围 |
| --- | --- | --- | --- | --- |
| 100 物理 GPU1，3090 | http://192.168.0.100:18104/v1 | vLLM 0.22.0 | 16.5 GiB / 32 | 当前恢复进程基本协议、portrait schema smoke；同一进程 C1/C2/C4/C8 满窗有限批次见下表 |
| 118，64GB AGX | http://192.168.0.118:18104/v1 | vLLM 0.19.0+cu126 | 20 GiB / 32，utilization 0.65 | EOS 248046；基本协议、单请求满窗、独立 C2 满窗及产品观察样本；真实 QA 未资格 |
| 123，64GB AGX | http://192.168.1.123:18104/v1 | vLLM 0.19.0+cu126 | 10 GiB / 4，utilization 0.40 | 当前 compact + EOS 248046；新进程仅 5 项基本协议和 synthetic portrait schema smoke，**未重测满窗/QA** |

100 开启 chunked prefill、关闭 prefix caching / async scheduling，xgrammar 结构空白约束开启。各节点 `max_num_batched_tokens=2048` 是 prefill 调度块，**不是上下文或输出 2048 限制**。生产 QA 使用 native remaining context，客户端省略 max_tokens / max_completion_tokens。对三端当前实际 generation_config、launch 和 vLLM 合并路径的只读静态核查已确认：没有隐藏 max_new_tokens、没有 launch 输出 override；有效预算为 262144 减实际 prompt tokens。这里证明的是实际配置与源码计算路径，不冒称已真实生成 261875 个输出 token；下文 512 输出只属于受控满窗探针。

输出预算静态证据在内网 `evidence/model-native-output-budget-readonly-audit-20261028-062dbb.json`，实算 SHA256 `aef10d0323ec216e9848d1f0bfa2a638f77e93d24cca4321335206019e980cd4`。核查没有模型 POST、在线 tokenize 或 CUDA 初始化；实际安装的预算函数离线验证了 269→261875、32768→229376、261632→512 等输入／剩余预算。100 的 HF 默认 max_length=20 未被 vLLM 映射为输出采样上限。

200 任务根 `runtime/` 中的当前身份文件与 SHA256：

- 100：`model-100-identity-v022-size-restored-current.json` — `062dbb803f91e46d2f7bd3470ad5b5e6819e10a692fc11ab26a3875b46f6c2d2`。
- 118：`model-118-identity-eos-rot90-qualified.json` — `a968b1f022f651aa9f96561ad757160aa7a890273a851111c2f8b551b16af132`。
- 123：`model-123-identity-eoscompact-util40-current.json` — `4a65c8063af1b62813391644432fc9829e3e2615845fbf20ba9cd4669fe1c5ae`。历史 `4e2eab67…` 仅是已停止 repair producer，不能当 current。相同有效 KV/window 参数的历史满窗证明独立保留，不冒称此新 PID 已通过。

118 的独立 EOS C2 客户端运行在 200，调用 118 模型端点；2 客户端、running 峰值 2 / waiting 峰值 1、POST 完成跨度 760.35 s；guard 覆盖 836.48 s，最大 gap 5.043 s、swap-out 0、抢占 0。它仍非 30 分钟 QA 稳定性证明。三节点不同引擎/部署的成绩分别保留，不混成同一组。

## 100 原生满窗口有限批次

[脱敏逐档指标](../benchmarks/evidence/model100-native-capacity-sanitized-1ad051586caf4c468f589e7774de6fc7.json) SHA256：
`20746ba141a51d60ff4e939d94b08799c2101a54b23da870e928a16acfda075f`。

每请求实际 tokenizer 计量 **261632 输入 + 512 输出 = 262144 tokens**，输入包含模板和图片 token。探针固定 min_tokens / ignore_eos，finish_reason=length 是预设终点，不能套用为真实 QA 的正常截断。每档一轮；C1 为同一当前进程已有记录，未重放；新 C2/C4/C8 共 14 个唯一请求全部完成、unknown=0，结束端点 idle。

| 档位 | 客户端请求数 | 模型 running / waiting 峰值 | 整批 POST 至最后完成 s | 单请求 TTFT 范围 s | 完成请求/s | KV 峰值 | host swap-out 页 | guard 实际最大 gap s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C1 | 1 | 1 / 0 | 122.95 | 112.28–112.28 | 0.008133 | 48.88% | 0 | 5.10 |
| C2 | 2 | 2 / 1 | 261.67 | 112.59–238.95 | 0.007643 | 97.75% | 0 | 10.77 |
| C4 | 4 | 2 / 3 | 523.60 | 115.82–508.75 | 0.007639 | 97.75% | 51,819 | 10.74 |
| C8 | 8 | 2 / 7 | 1058.53 | 115.56–1040.03 | 0.007558 | 97.75% | 264 | 10.74 |

各档 KV 抢占增量均为 0。客户端数是同时提交请求数；running/waiting 是调度指标，**8 客户端不等于 8 份满上下文同时驻留或执行**，running 观察峰值仅 2。本组增加客户端未观察到吞吐提高，排队增多；单批次不能给统计意义的推荐并发，也没触及确认的失败边界。

C4 约 15.19 s 的一个间隔发生 51819 页 swap-out（4096 bytes/page），下一采样增量 0；C8 约 164.33 s 有 264 页，下一采样增量 0。均未触发“连续两个观测间隔增长”停止条件；这是 **100 整机**计数，不归因到特定进程，也不抹成零 swap。guard 目标为读后间隔 5 s，表中保留实际最大 gap，不宣称严格 5 s 连续采样。C1 guard 区间还含加载、协议及 portrait smoke。native 客户端和 guard 均在 100，不是 200 或 M4 CPU 成绩。

原始组合清单 SHA256：
`a0ade138d226d23f5e730df61d849e772147bcbe82db7b78edd1c4860607dcd1`。
新 C2/C4/C8 清单为 `12547f8c88f71e7f0dd917624d22bd9a93fd9697b4a10ebe8bd5e7d8b2143ac7`（146 文件），既有 C1 为 `26e797148bd37e7a313d1b51a199f509fa7a9d11b45c376fea9c3351ffa5b652`（60 文件）。原始内容留内网。

结论仅为 **原生窗口有限批次验证至少 8 客户端可排队完成**；未证明最大稳定值、30 分钟持续稳定、重复轮次、真实 QA 推荐配置或 2–4 台 M4 的共享上限。

## 最新真实 QA / Judge

200 独立 Sentrix `18191` / PhotoBench `18772`，每轮独立恢复库。d4 固定 64 题、75 个预设对话回合，QA 并发 1 / Judge 池 4；Agent 每回合最多 30 模型 steps，其他运行预算见[主流程](concurrency-capacity.md)。30 steps 不等于全部 HTTP 请求数。

d4 已因 `judge_failed` 停止派发，最终 cancelled，**不具容量资格**：

| 观测单位 | 精确终态 |
| --- | --- |
| 64 题 | 53 条 item 落盘：52 execution_completed、1 outcome_unknown；另 11 从未派发 |
| 整题 Judge | 51 completed、1 failed、1 skipped（未知 Agent） |
| 质量子 Judge | 52 completed：2 分 19、1 分 8、0 分 25，包含整题失败项的质量 0 分 |
| 整题有效评分 | 51 条：2 分 19、1 分 8、0 分 24；不能说 52 条整题评分成功 |
| task 子 Judge | 51 completed、1 failed；evidence 子 Judge 未启用/无 status，不计失败 |
| 实际云请求 | 196 starts、117 responses、79 次 HTTP 429；不是 196 道题 |

表中的质量/task/evidence 子 Judge 是 item 级投影，未把多轮 runtime_turns 内的额外子 Judge 调用逐一相加；117 个云 responses 不是“52 个质量 + 51 个 task”的简单总和。HTTP 请求计数与 item 投影使用不同单位。

唯一整题失败是 task Judge 六次 HTTP 429 重试耗尽，保留最终供应商分类 `AccountRateLimitExceeded` / `TooManyRequests`。没有 JSON/协议失败证据；Retry-After 未保存，明确 unknown。本地资源故障计数 0；不能将供应商限流当成本地容量边界，也不能据此断言 Doubao 能接住更高 QA 并发。`judge_completed` 包含失败，不能直接当成功评分数；正常 0 分是合法质量结果，不是运行故障。

原取消、未知回合与旧结果保持，未自动重发。精确只读盘点在内网 `evidence/d4-terminal-Judge-readonly-inventory.json`，SHA256 `8859bceeced58490e773b9d0e6b1c0b874ddd261cf0d2b4812a71409f4685ac4`。新增 HTTP 尝试证据只适用于后续显式开启的新运行，不能倒填本轮缺失响应头，见[可选 Judge HTTP 证据](concurrency-capacity.md#可选-judge-http-尝试证据)。

### 独立 task-only 补测

只对 item 50 已知六次拒绝的 task 子 Judge 发起一次新的 derived attempt，原 payload SHA256 为 `2760ea6761e85c7e2ee08d20be82cff650299231edb86855f286ba366f30b7db`。实际 1 次 HTTP 200，耗时 1.729 s，无 429，协议正常完成。原 d4 的质量 0 分和未知 item 52 均未修改或重发；这不是原 run 的恢复，也不是容量通过或正式模型总成绩。

内网证据 `evidence/derived-task-judge-single-6ac108974256479fa562fb6d0d06ced7/summary.json`，实算 SHA256 `367bf82e95ff291744fe7e922a8c61e9bed58d439dc3cc981bf4617f8a8a9dc1`。独立 Judge-only 实验 `judge-only-i2-c4-32-86e36566291f42b3962398fb84b5212c` 已完成：32/32 cases、38 turns；基础 76 个 subscores 对应实际 76 个 HTTP 请求，全部 200，429、重试、unknown 和额外一致性请求均为 0。总耗时 152.51674 s；实际派发间隔 min / median / max 为 1.997209 / 2.000005 / 2.002890 s，HTTP 在途峰值 2，最终队列 0。HTTP 区间 11:20:15–11:22:48 UTC，没有 full CI 竞争。

这是单批、单进程/run 内限速结果，未测跨客户端共享账户的 global 配额或最大并发。建议下一轮单客户端真实 QA 先采用 `PHOTOBENCH_JUDGE_REQUEST_INTERVAL_SECONDS=2.0`、`PHOTOBENCH_JUDGE_HTTP_ATTEMPT_EVIDENCE=1`、Judge 池 4；这是测量配置，不改变产品对所有 provider 的默认设置，也不构成尚未运行的下一轮 QA 成绩。

内网最终审计 `evidence/judge-only-i2-c4-32-86e36566291f42b3962398fb84b5212c/final-audit.json` SHA256 `5e349e5a2e3faff404a1180f0184f43059ebe733c1e34fed0cee7d622de20ca2`；summary SHA256 `3f3df602cbf7b4240e4e5a3e7145fcfaf937001c74f7d09caf5ba8e6bb7c0734`。

200 的只读命令：
`repo/.venv/bin/python runtime/read_capacity_status.py --round <round-name> --live`。
它读 checkpoint/采样并可 GET 本机 18772，输出独立 QA/Judge 队列、实际 HTTP attempts/429、200 CPU 和共享模型端点指标。缺字段为 null/unknown；端点指标可能含其他客户端。检索 tool wall time 含 HTTP/存储/CPU，不是纯 CPU 时间；200 客户端、远端模型与各台 M4 分开归属。

## 内网获取与独立恢复

Git 仅存清单、哈希和本文；实体、完整库存和证据留内网，按公开清单哈希向保管方只读获取。当前来源：

- `/home/sscy/benchmarks/sentrix-concurrency-20261026/snapshots/accepted-baseline-v3`
- `/home/sscy/benchmarks/sentrix-concurrency-20261026/runtime/memory-baseline-identity.json`

用安装了项目依赖的 Python，在使用方 checkout 中执行。下例私有缓存目录须先取得原件，目标目录须不存在；参数已按本版本 `memory_snapshot.py --help` 核对：

```sh
python scripts/benchmarks/memory_snapshot.py verify --source /private/sentrix-cache/accepted-baseline-v3 --identity /private/sentrix-cache/memory-baseline-identity.json --consumer-migrations apps/api/src/sentrix/infrastructure/persistence/migrations
python scripts/benchmarks/memory_snapshot.py restore --source /private/sentrix-cache/accepted-baseline-v3 --destination /private/sentrix-runs/new-run/data --identity /private/sentrix-cache/memory-baseline-identity.json --consumer-migrations apps/api/src/sentrix/infrastructure/persistence/migrations
python scripts/benchmarks/memory_snapshot.py verify-restored --source /private/sentrix-cache/accepted-baseline-v3 --destination /private/sentrix-runs/new-run/data --identity /private/sentrix-cache/memory-baseline-identity.json --consumer-migrations apps/api/src/sentrix/infrastructure/persistence/migrations
```

默认收据在目标 data 旁；自定义时 restore / verify-restored 使用相同 `--receipt`，放在数据树外。完整离线验证后才启动副本 API；运行中不重哈希活动 SQLite。快照恢复可用于 macOS；当前 Linux 容量认证 CLI 要求 `/proc` 身份证据，不能据此声称 M4 已认证。

真实评测入口 `scripts/benchmarks/photobench_capacity.py --help`：需显式端点/allowed host、固定题目文件/hash、消费者源码根、模型身份、快照/身份、Sentrix PID/data 与收据。`--judge-concurrency 4` 只断言配置一致，不修改云池；`--resume` 仅观察已知 run，不重派 QA/Judge。完整参数及独立部署见[主流程](concurrency-capacity.md)。

## 代码验证

功能版本 `6eafb09dac20c680193f4b5824dd33ebad3ba928` 的完整 CI 已通过：backend 3492 passed，49 个 real_environment 用例 deselected（未运行，不冒称实机验证）；Web 143 passed、PhotoBench Web 486 passed，backend coverage 93%。总耗时 490.2049 s，实际导入模块绑定到本轮 repo，测试前后 source unchanged。此前环境绑定失败的记录另保留，最终已绑定验证通过。

内网报告 `evidence/ci-20261028-judge-http-evidence-pathbound-v2.json`，实算 SHA256 `78a1171a37b90bab9f4fcd4ca413d87200955c20b5ea8141633714dc3cc5a0c8`；对应完整日志 SHA256 `8bf3639f92c5260593b58b73b0a5b335d644985e98657cc2d4878feeec07e549`。本次提交仅新增文档和脱敏清单，额外检查本地链接、JSON、清单字节哈希与凭据泄漏；没有因文档改动重复运行完整 CI。

## 保留服务与回滚

保留新 vLLM，100 WeMM 继续停止；GPU0 现有工作、153 不在切换范围。共同顺序及 200/118/123 的核验见[隔离部署回滚](2026-10-27-capacity-rollback.md)；其中旧 PID/容器 ID 仅历史交接，实际须按当前身份核对，不能照抄旧 ID 发信号。

100 当前使用新**单次**控制目录：
`/home/realmagic/benchmarks/sentrix-concurrency-20261026/model-service/restore-v022-size-c9549b65a2ac4770a49a1e1957149063`。
launch SHA256 为 `eab5914cc39425906b761d1df069489d9ed6423d0433817fe81c5f775266e6b0`。已实际核 `control_ordinal.py --help` 只提供 start / stop / status；service.json 已存在，start 拒绝第二次运行，不能删记录假装首次。

回滚先停止新派发、保存 unknown、确认 running/waiting=0 或明确记下终止结果，再核 PID/start_ticks/cwd/完整 argv、launch SHA、物理 GPU1 UUID 和 PCI selector；仅对该目录执行 `python3 control_ordinal.py status` / `stop`。确认本轮 API/EngineCore、18104监听退出后，才按已有文档恢复保存的 WeMM 配置。禁止按名称批量杀进程、触碰 GPU0、自动恢复旧服务或重放 unknown。
