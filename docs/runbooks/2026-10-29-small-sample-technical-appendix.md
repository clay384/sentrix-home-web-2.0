# Sentrix 小样本性能综合报告 v4

本报告只包含已闭合、明确登记的结果。所有数据为固定16题/18turn的小样本；sample_only=true、capacity_qualified=false、formal_fullset=false。重复题集不累加为独立题数，没有全集、30分钟稳定或最大并发认证。新源码652的同node/profile/题集C1/C4/C8优先比较，旧9938单列。

| 节点/配置 | 源码 | QA秒 | QA题/min | 含评分秒 | logical/端点成功HTTP/云HTTP | Judge尾秒 | 状态 |
|---|---|---:|---:|---:|---:|---:|---|
| 100-gpu1 C1 | 9938d9e | 207.237 | 4.632 | 249.200 | 110/112/36 | 41.925 | completed |
| 100-gpu0 C4 | 9938d9e | 95.275 | 10.076 | 438.300 | 93/95/36 | 343.042 | completed |
| 100-gpu1 C4 | 9938d9e | 106.260 | 9.034 | 414.800 | 106/108/36 | 308.555 | completed |
| 123 C1 | 9938d9e | 644.130 | 1.490 | 652.400 | 63/65/36 | 8.219 | completed |
| 200 C1 | 6520527 | 269.197 | 3.566 | 276.700 | 130/133/36 | 7.403 | completed |
| 200 C4 | 6520527 | 107.904 | 8.897 | 223.800 | 97/100/36 | 115.889 | completed |
| 200 C8 | 6520527 | 93.593 | 10.257 | 224.700 | 101/105/36 | 131.018 | completed |
| 100-gpu0 C4 | 6520527 | 105.185 | 9.127 | 448.400 | 102/106/36 | 343.175 | completed |
| 100-gpu1 C4 | 6520527 | 98.566 | 9.740 | 371.000 | 99/102/36 | 272.379 | completed |
| 123 C4 | 6520527 | 208.749 | 4.599 | 243.300 | 66/67/36 | 34.535 | completed |
| 123 C8 | 6520527 | 251.944 | 3.810 | 557.000 | 81/83/66 | 305.052 | completed |
| 100-gpu0 C8 | 6520527 | 89.750 | 10.696 | — | 102/106/15 | — | QA完成；评分取消 |
| 100-gpu1 C8 | 6520527 | 87.560 | 10.964 | — | 97/99/13 | — | QA完成；评分取消 |

同源200从C1到C4的QA小样本吞吐提高2.49倍，C4到C8再提高1.15倍。含评分耗时C4/C8为223.8/224.7s，当前接入gate的6秒节拍已令评分尾部主导，不能把QA加速直接当整轮加速。调用轨迹数与token量并非完全相同，因此不是纯引擎基准。

双100同源C4→C8的QA聚合执行速率为0.3037→0.3561/s，约提高17.2%。C8全QA完成后模型驻留退出，评分未闭合；该数字仅说明QA阶段，整轮不记通过。

本地成功HTTP来自端点counter；model policy HTTP attempts另列，视觉成功观察与成功counter残差核对，denied工具不算模型请求。若observer重试attempt未单独记录则不臆称其总attempt数已完整观测；云HTTP另计。模型/工具墙钟存在重叠或嵌套，不当CPU或GPU利用率。Chat非流式TTFT保留null，服务端TTFT仅为同进程端点直方图增量。

## 节点与轨迹差异

- 100-gpu1-C1-9938：vLLM 0.22.0，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 89, 'claim_verification': 2, 'recovery': 1}。
- 100-gpu0-C4-9938：vLLM 0.22.0，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 73, 'claim_verification': 1, 'recovery': 1}。
- 100-gpu1-C4-9938：vLLM 0.22.0，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 86, 'claim_verification': 1, 'recovery': 1}。
- 123-C1-9938：vLLM 0.19.0+cu126，KV 10.000GiB、seq4、配置窗口262144；调用类型{'planner': 23, 'agent': 40}。
  记录内prompt/输出token总量：109982/7544；单record prompt最大3348.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  后验native证明已与冻结identity/API起点/container/权重/EOS核对：当前PID实际261632+512=262144通过，原identity不改。
- 200-C1-652：vLLM 0.22.0+cu129，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 107, 'recovery': 2, 'claim_verification': 3}。
  记录内prompt/输出token总量：528973/10306；单record prompt最大11559.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  采样后端：PyTorch-native via VLLM_USE_FLASHINFER_SAMPLER=0。
  窗口资格原值：configured262144; actual32768input+512output passed on these PIDs; fullnative request not tested。
- 200-C4-652：vLLM 0.22.0+cu129，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 76, 'claim_verification': 2, 'recovery': 1}。
  记录内prompt/输出token总量：305617/8747；单record prompt最大8099.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  采样后端：PyTorch-native via VLLM_USE_FLASHINFER_SAMPLER=0。
  窗口资格原值：configured262144; actual32768input+512output passed on these PIDs; fullnative request not tested。
- 200-C8-652：vLLM 0.22.0+cu129，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 20, 'agent': 75, 'claim_verification': 5, 'recovery': 1}。
  记录内prompt/输出token总量：310717/8916；单record prompt最大8099.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  采样后端：PyTorch-native via VLLM_USE_FLASHINFER_SAMPLER=0。
  窗口资格原值：configured262144; actual32768input+512output passed on these PIDs; fullnative request not tested。
- 100-gpu0-C4-652：vLLM 0.22.0，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 81, 'claim_verification': 2, 'recovery': 1}。
  记录内prompt/输出token总量：322137/8802；单record prompt最大8100.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  窗口资格原值：new_PID_actual_32768_plus512_only_full_native_historical。
- 100-gpu1-C4-652：vLLM 0.22.0，KV 16.500GiB、seq32、配置窗口262144；调用类型{'planner': 18, 'agent': 76, 'claim_verification': 4, 'recovery': 1}。
  记录内prompt/输出token总量：305918/8665；单record prompt最大8096.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  窗口资格原值：new_PID_actual_32768_plus512_only_full_native_historical。
- 123-C4-652：vLLM 0.19.0+cu126，KV 10.000GiB、seq4、配置窗口262144；调用类型{'planner': 21, 'agent': 43, 'claim_verification': 2}。
  记录内prompt/输出token总量：112565/6979；单record prompt最大3167.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  后验native证明已与冻结identity/API起点/container/权重/EOS核对：当前PID实际261632+512=262144通过，原identity不改。
- 123-C8-652：vLLM 0.19.0+cu126，KV 10.000GiB、seq4、配置窗口262144；调用类型{'planner': 21, 'agent': 58, 'claim_verification': 2}。
  记录内prompt/输出token总量：212513/7939；单record prompt最大10684.0。与262144配置窗口分开，QA小样本不冒称每请求满窗。
  后验native证明已与冻结identity/API起点/container/权重/EOS核对：当前PID实际261632+512=262144通过，原identity不改。

同题集合的轨迹、prompt长度、调用数、引擎/采样后端不同，跨节点耗时比不是纯硬件倍率。200模型与QA客户端同主机，host内存/CPU读数不能当两台相加；100两卡也共用一台host。

## 服务端阶段（每请求均值ms）

| 记录 | queue | prefill | decode | serverTTFT | serverE2E |
|---|---:|---:|---:|---:|---:|
| 100-gpu1-C1-9938 | 0.055 | 571.338 | 1088.284 | 579.585 | 1667.661 |
| 100-gpu0-C4-9938 | 5.720 | 471.273 | 1636.673 | 514.564 | 2151.007 |
| 100-gpu1-C4-9938 | 13.065 | 502.478 | 1755.993 | 555.259 | 2311.015 |
| 123-C1-9938 | 3.241 | 725.455 | 8859.430 | 758.152 | 9617.067 |
| 200-C1-652 | 0.011 | 560.298 | 1281.326 | 569.008 | 1850.747 |
| 200-C4-652 | 11.229 | 468.688 | 1951.446 | 519.848 | 2471.386 |
| 200-C8-652 | 37.239 | 484.313 | 2422.885 | 577.336 | 3000.193 |
| 100-gpu0-C4-652 | 0.022 | 459.332 | 1652.298 | 499.305 | 2151.404 |
| 100-gpu1-C4-652 | 2.668 | 439.708 | 1645.324 | 477.464 | 2122.558 |
| 123-C4-652 | 0.053 | 763.841 | 10116.170 | 870.685 | 10971.461 |
| 123-C8-652 | 8526.586 | 1132.711 | 9858.729 | 9841.228 | 19699.135 |
| 100-gpu0 C8 652 QA阶段 | 41.564 | 469.426 | 2272.792 | 546.516 | 2819.081 |
| 100-gpu1 C8 652 QA阶段 | 30.272 | 484.668 | 2271.079 | 565.817 | 2836.667 |

| QA内部GPU采样归属 | memory.used峰MiB | memory.free低MiB | util采样均值% | util采样峰% |
|---|---:|---:|---:|---:|
| 123-C1-9938 e4bc3522-baf | — | — | — | — |
| 200-C1-652 GPU-7c08ec6f | 21766.0 | 2493.0 | 61.2 | 100.0 |
| 200-C4-652 GPU-7c08ec6f | 21766.0 | 2493.0 | 72.6 | 100.0 |
| 200-C8-652 GPU-7c08ec6f | 21766.0 | 2493.0 | 73.8 | 100.0 |
| 100-gpu0-C4-652 GPU-bd82fa9d | 21773.0 | 2354.0 | 68.4 | 100.0 |
| 100-gpu1-C4-652 GPU-4ef9200c | 21773.0 | 2354.0 | 77.7 | 100.0 |
| 123-C4-652 e4bc3522-baf | — | — | — | — |
| 123-C8-652 e4bc3522-baf | — | — | — | — |

GPUutil为离散nvidia-smi采样，非墙钟占比推断；AGX nvidia-smi的N/A保留未知，共享内存另看host可用值。

## QA与评分在哪里等待

- 100-gpu1-C1-9938：模型waiting峰0.0；Judge waiting峰4，QA结束尚有云HTTP 见旧版次未完成，评分尾部41.925s。
- 100-gpu0-C4-9938：模型waiting峰0.0；Judge waiting峰9，QA结束尚有云HTTP 见旧版次未完成，评分尾部343.042s。
- 100-gpu1-C4-9938：模型waiting峰0.0；Judge waiting峰10，QA结束尚有云HTTP 见旧版次未完成，评分尾部308.555s。
- 123-C1-9938：模型waiting峰0.0；Judge waiting峰0，QA结束尚有云HTTP 2次未完成，评分尾部8.219s。
- 200-C1-652：模型waiting峰0.0；Judge waiting峰1，QA结束尚有云HTTP 2次未完成，评分尾部7.403s。
- 200-C4-652：模型waiting峰1.0；Judge waiting峰8，QA结束尚有云HTTP 19次未完成，评分尾部115.889s。
- 200-C8-652：模型waiting峰1.0；Judge waiting峰8，QA结束尚有云HTTP 22次未完成，评分尾部131.018s。
- 100-gpu0-C4-652：模型waiting峰0.0；Judge waiting峰10，QA结束尚有云HTTP 30次未完成，评分尾部343.175s。
- 100-gpu1-C4-652：模型waiting峰0.0；Judge waiting峰10，QA结束尚有云HTTP 27次未完成，评分尾部272.379s。
- 123-C4-652：模型waiting峰0.0；Judge waiting峰0，QA结束尚有云HTTP 6次未完成，评分尾部34.535s。
- 123-C8-652：模型waiting峰4.0；Judge waiting峰4，QA结束尚有云HTTP 33次未完成，评分尾部305.052s。
- 共享组100-dual-C4-9938：QA聚合包络106.260s、执行速率0.301147/s，尾部332.197s；同16题重复不是unique32；包络不取各client duration之和。
- 共享组100-dual-C4-652：QA聚合包络105.359s、执行速率0.303723/s，尾部343.204s；同16题重复不是unique32；包络不取各client duration之和。
- speed16v2-100dual-c8-r3-652-20261029-mae0：QA聚合包络89.868s、0.356078执行/s，32执行来自同16题重复。云评分未完成，不填全轮成功耗时。

## 客户端CPU与资源归属

| CPU集合 | 观测秒 | 所选进程CPU秒 | 平均核数 | host换出页 |
|---|---:|---:|---:|---:|
| 100-gpu1-C1-9938所选进程 | 255.294 | 33.610 | 0.1317 | 14325 |
| 123-C1-9938所选进程 | 655.800 | 37.910 | 0.0578 | 13829 |
| 200-C1-652所选进程 | 285.514 | 154.130 | 0.5398 | 3096 |
| 200-C4-652所选进程 | 226.405 | 128.500 | 0.5676 | 0 |
| 200-C8-652所选进程 | 226.274 | 125.640 | 0.5553 | 1290 |
| 123-C4-652所选进程 | 250.262 | 25.710 | 0.1027 | 43932 |
| 123-C8-652所选进程 | 565.729 | 129.950 | 0.2297 | 20517 |
| shared-group100-C4共享sidecars一次 | 446.801 | 112.820 | 0.2525 | 3523 |
| 100-dual-C4-652共享sidecars一次 | 455.598 | 103.580 | 0.2273 | 60158 |
| speed16v2-100dual-c8-r3-652-20261029-mae0 QA内部共享sidecars一次 | 85.102 | 38.260 | 0.4496 | 0 |

所选进程CPU不等于全部host CPU；RSS相加不是PSS。共享BGE/CLIP只计一次，均为200客户端观测，不是M4验证。0硬故障/0守卫报警不等于所有host无小幅换出；原换出/告警/unknown数值保留。

- 123-C1-9938的200客户端CI重叠：CI1约239.178s；CI2约196.180s。没有据时间重叠推因果。
- 123-C1-9938模型host新增swapout 1030页，原stop/孤立事件见JSON；不是自动OOM判断。
- 123-C1-9938资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。
- 200-C1-652模型host新增swapout 2550页，原stop/孤立事件见JSON；不是自动OOM判断。
- 200-C1-652资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。
- 200-C4-652模型host新增swapout 0页，原stop/孤立事件见JSON；不是自动OOM判断。
- 200-C4-652资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。
- 200-C8-652模型host新增swapout 1290页，原stop/孤立事件见JSON；不是自动OOM判断。
- 200-C8-652资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。
- 100-gpu0-C4-652模型host新增swapout 0页，原stop/孤立事件见JSON；不是自动OOM判断。
- 100-gpu1-C4-652模型host新增swapout 0页，原stop/孤立事件见JSON；不是自动OOM判断。
- 123-C4-652模型host新增swapout 0页，原stop/孤立事件见JSON；不是自动OOM判断。
- 123-C4-652资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。
- 123-C8-652模型host新增swapout 0页，原stop/孤立事件见JSON；不是自动OOM判断。
- 123-C8-652资源策略/警告原值已保留；独立小波动不隐去，也不自动冒充硬故障。

## 全集估算

| 配置 | scope/turn加权QA估计小时 |
|---|---:|
| 100-gpu1-C1-9938 | 3.89 |
| 100-gpu0-C4-9938 | 2.00 |
| 100-gpu1-C4-9938 | 2.13 |
| 123-C1-9938 | 13.32 |
| 200-C1-652 | 4.88 |
| 200-C4-652 | 2.27 |
| 200-C8-652 | 1.96 |
| 100-gpu0-C4-652 | 2.18 |
| 100-gpu1-C4-652 | 2.03 |
| 123-C4-652 | 4.38 |
| 123-C8-652 | 4.77 |
| 100-gpu0 C8 652（仅QA完整） | 1.87 |
| 100-gpu1 C8 652（仅QA完整） | 1.80 |

公式：Σscope 全集turn ×（样本item QA墙钟和/样本turn）×（本档QA包络/样本item墙钟和）。并发档的最后一项含固定16题重叠/尾部效应，不是通用效率。题型、轨迹、长上下文和缓存偏差未消除，不给狭窄区间，也不把估计当1167实测。

全集1167题/1340turn；假设本次每turn两次基础云HTTP，工作量为2680次（重试额外增加）。有限首末启动需2679个间隔，再加末请求耗时。异步总耗时按max(QA,Judge)+供给/启动/尾部，不能相加重叠阶段。
最近登记后验短批零429、无重试的条件性节拍为12.0s，2680次稳态放行工作量约8.93h，2/4个PR同gate约17.87/35.73h。单次短批未证明长期安全或供应商硬quota。

gate只协调接入相同UID/DIR/SCOPE的评测参与者，外部未接入消费者不受控。短批零429仅证明该条件下完成，没有测出供应商硬上限。历史6秒零429批不覆盖后续时段；新时段出现429后只按后验实验报告节拍，不把未完成数据填0。

## 质量和运行失败

- 429时段反证：123-C8-qei4-6second-later-429；配置6s，HTTP429=30，epoch 1793212093.1947954–1793212592.5932212。

| gate节拍情景秒 | 1PR云工作小时 | 2PR云工作小时 | 4PR云工作小时 |
|---|---:|---:|---:|
| 6 | 4.47 | 8.93 | 17.87 |
| 12 | 8.93 | 17.87 | 35.73 |

情景并非已通过/供应商配额；后续流量、外部消费者和重试均可改变结果。

- 100-gpu1-C1-9938：质量分布{'0': 6, '1': 2, '2': 8}，均值1.125；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 100-gpu0-C4-9938：质量分布{'0': 5, '1': 3, '2': 8}，均值1.188；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 100-gpu1-C4-9938：质量分布{'0': 5, '1': 3, '2': 8}，均值1.188；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 123-C1-9938：质量分布{'0': 5, '1': 6, '2': 5}，均值1.0；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 200-C1-652：质量分布{'0': 5, '1': 4, '2': 7}，均值1.125；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 200-C4-652：质量分布{'0': 3, '1': 5, '2': 8}，均值1.312；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 200-C8-652：质量分布{'0': 6, '1': 2, '2': 8}，均值1.125；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 100-gpu0-C4-652：质量分布{'0': 5, '1': 2, '2': 9}，均值1.25；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 100-gpu1-C4-652：质量分布{'0': 7, '1': 2, '2': 7}，均值1.0；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 123-C4-652：质量分布{'0': 4, '1': 4, '2': 8}，均值1.25；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 123-C8-652：质量分布{'0': 4, '1': 4, '2': 8}，均值1.25；unknown=0。质量0保留，不能拿执行完成冒称全答对。
- 运维/故障 a719：runtime_PIDrecord_missing_runtime_connection; manager_restart_requested；原记录/unknown保留，不当吞吐边界。
- 运维/故障 b836：runtime import repo sys.path missing；原记录/unknown保留，不当吞吐边界。
- 运维/故障 dc71：earlierC1preflight；原记录/unknown保留，不当吞吐边界。
- 运维/故障 e284：earlierC1preflight；原记录/unknown保留，不当吞吐边界。
- 运维/故障 d3a6：visual_ann persistent Qdrant lazy-initialization race; source652 fixed under offline concurrency regression and fullCI；原记录/unknown保留，不当吞吐边界。
- 运维/故障 f5c8：old strict owned-API/Engine VmSwap two consecutive growth hardstop; noOOM evidence; later v2 diagnostic policy different；原记录/unknown保留，不当吞吐边界。
- 运维/故障 h7ea：runtime specSHA path missing; zeroPOST; corrected future preparation；原记录/unknown保留，不当吞吐边界。
- 运维/故障 mae0：post-QA model service residency lost; graceful shutdown initiator unknown; whole-run guard cancels Judge；原记录/unknown保留，不当吞吐边界。
- speed16v2-100dual-c8-r3-652-20261029-mae0：各端QA16完成、0QA unknown；Judge各6完成/4失败/6待派，28云HTTP全200。两端模型在QA封存后正常shutdown，触发全程驻留守卫取消整轮，发起者未知，无OOM/Xid证据。本轮保留保护规则，不为补压力成绩修改或重发。

未收到/尚未登记的结果：。只按操作者告知的明确结果ID追加新版本，不扫活动目录。

数据report.json；原始文件哈希input-inventory.json；新登记记录逐turn为new-per-turn.csv。旧v1/v2不修改。

## 未给稳定档的节点

- 118：partial_only；resource_safety_stop_sustained_remote_swap_growth；receipt SHA 5b57a6cb6d092691bbfcd9441bbde6dac21e65897dd5dc68794f6444fb574d3f。不算质量错误、不计算全集速率，已知/unknown保持原样。

## 历史原生满窗口组件证据

这些是历史固定PID的独立组件实测，不转给新驻留PID，也不代替真实QA吞吐。

- 100-native-historical：evidence/model100-native-capacity-sanitized-1ad051586caf4c468f589e7774de6fc7.json；SHA 20746ba141a51d60ff4e939d94b08799c2101a54b23da870e928a16acfda075f。

| client数 | 实际每请求input+output | 组秒 | 完成请求/s | model running峰 | waiting峰 |
|---|---|---:|---:|---:|---:|
| 1 | 261632+512 | 122.951 | 0.00813 | 1.0 | 0 |
| 2 | 261632+512 | 261.672 | 0.00764 | 2.0 | 1.0 |
| 4 | 261632+512 | 523.597 | 0.00764 | 2.0 | 3.0 |
| 8 | 261632+512 | 1058.527 | 0.00756 | 2.0 | 7.0 |
- 123-current-same-PID-native-C1：evidence/model123-native-current-sanitized-39883a693417495ab85869fd42be2275.json；SHA f286cd3dfe7b69c3883d946ebfb1d525c50a3c8fff621ddd76fb20afb9e8d259。
