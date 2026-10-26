# 2026-10-26 原生窗口与观察协议验收

本记录区分模型服务器容量验证、视觉观察协议验证及后续 QA 评测。153 未参与修改或写请求。200 是共享 CPU 客户端实验机，不能据此声称 M4 Mac mini 通过相同并发。

## 100 固定模型实例

- 物理 GPU1；原 GPU0 任务不改动。WeMM 按已保存的进程身份正常停止，恢复方式保留在内网运行目录。
- Qwen3.5-4B AWQ W4A16 / group32，激活与视觉模块 BF16；vLLM 0.26.0。
- 完整权重 SHA256：902477edf53bc6768bd1f212dd1866856fd5a0627def06887780c95900ffb013。
- 原生总窗口 262144，不使用 RoPE 扩展；seq32，固定 KV 17716740096 bytes（16.5 GiB），chunked prefill 2048，prefix caching 关闭。
- 启动配置 SHA256：decb644d30498474babaae7c855f924c589141f6e2ed96e3451532715e3d345c。
- 模型端口18104，统一别名qwen35-4b-awq。image8/video0/max_pixels262144；视频业务通过关键帧输入。
- 先前 auto-KV、固定16GiB实验保留在独立记录，不能混入此配置。118及其他节点也须单独记录引擎、窗口和KV预算。

## 原生窗口测试口径

固定合成图片与填充文本，以实际 /tokenize 计量261632输入token（含图片与模板），另生成512token，合计262144。用于容量探测的请求故意固定temperature0、min_tokens512、ignore_eos；它与下文真实观察的seed42采样策略是两个实验协议，不混为质量成绩。

此固定模型实例已完成一次满窗单请求（122.21秒）与第一对并发请求（253.88 / 256.12秒），实际输出均512token。第一对峰值KV97.752%，无KV抢占。第二对已完成：2/2实际261632+512，整组253.24秒，峰值KV97.752%，无抢占，结束running/waiting归零且进程/启动身份不变。并发3亦完成：3/3实际261632+512，整组389.93秒，峰值running2/waiting2、KV97.752%，抢占增量0，结束running/waiting/KV归零。服务通过排队承接第三个客户端，不表示三个完整KV窗口同时驻留。两组使用新请求ID、durable journal、原始遥测与不可重放标记；汇总为runtime/model-100-native-followup.json，原始结果见各组round.json。

资源守卫从每组首个样本累计：KV抢占至少2次才触发反复抢占；队列连续3个采样间隔增长、swap连续2个间隔新增、计数器重置/进程启动时间变化、模型指标缺失或主机可用内存低于8GiB均停止新派发。单次抢占保留证据，不独立判失败。请求结果未知不重发。

这轮未触达失败边界，只能报告至少3个客户端满窗请求可以完成。2/3客户端短组吞吐相差约2.6%，增加到3主要延长等待；该现象不能替代真实QA推荐配置。上述短组不足以证明最大稳定并发或30分钟资格。失败组默认不构成已确认容量边界；需要明确原因和原始证据才能形成边界结论。200 CPU与远端模型/主机的指标分别归属；GPU0外部负载也需保留，不能称整机独占。

## 真实视觉观察协议与人工限制

最初两批记忆库均未通过完整性验收，保留在隔离目录。processed不能代替结构化观察成功。旧请求2048输出上限导致的length、客户端超时、provider拒绝分别属于操作事实，不能直接当作模型质量或架构结论。

新的共享生成策略为Qwen官方non-thinking参数：temperature0.7、top_p0.8、top_k20、min_p0、presence_penalty1.5、enable_thinking=false，并预先固定seed42。视觉JSON使用json_object。Agent/视觉/因果文本请求取消旧2048/768上限；原生模式由vLLM使用实际模板/图片/工具输入计量剩余窗口，不盲传262144输出。100安装版本的请求解析及generation_config已核验无额外max_new_tokens上限。普通部署仍可选legacy策略。

新独立参数实验使用16张固定图片（8张历史空观察、8张历史正常观察），串行及并发4各一轮：32次首轮均含8个约定字段，最终caption/activity/place/event_type全部非空。39次实际模型调用全部正常stop，输出25–640token，无新unknown，完整JSON后没有新增字符。7次中文化中2次只返回caption，既有合并保留其余字段；部分OCR及个别caption保留拉丁字母，作为内容现象单列。旧180秒超时请求仍为unknown，不修改旧账本。此为协议候选验收，不满足容量档每轮至少32题或连续30分钟要求。

私有原始图片、提示词及响应仅保留内网诊断目录，不进入Git。可公开的汇总证据：seed42-native-cohort-274dafc7cc944cc685878adc9fb96462/summary.json及field-completeness-summary.json。

## 本轮后续部署预算

第三批冻结部署使用原生262144、Qwen non-thinking/seed42；Agent最多30步，工具总数及search/page/inspection上限28；回合墙钟3600秒，Chat/VLM单请求HTTP1800秒，PhotoBench单题等待4200秒。后续复核发现28工具记录的上限可在30模型步之前限制取证，待新批部署的源码已支持用显式null关闭这四项工具次数限制；不能把新设置回填到第三批历史记录。预算是运行约束而非模型质量标准，触及时必须带具体原因返回。源码默认值保留旧部署兼容性，测试工具必须校验实际运行配置。

服务拒绝、网络/超时、输出/上下文限制通过typed ModelServiceError传播；图片流水线标failed，摘要不写fallback伪装成功，因果图保留degraded及causal_issues，家庭图谱保留failed及stats.operational_issue。内容JSON无效仍保留原质量降级语义。统一API错误响应只含稳定码与安全预算事实，不转述原provider正文。PhotoBench须将对应非2xx或内嵌failed/degraded标为建库不完整，不能ready。

## 多客户端准备状态

runtime/shared_clients.py / shared_client_worker.py仅准备和离线验证，尚未恢复、启动或运行任何多客户端组。每组2或4个软件实例拥有独立数据库/配置/结果/PID，全部共享200 CPU/RAM与18101 BGE、18103 CLIP侧车。每client Judge=4意味着总潜在并发8或16，不等于全局4。

工具同时校验environment与expected-runtime完整预算，并显式传入PhotoBench4200秒；缺键或旧模板拒绝执行。记忆语义embedding身份从未来已验收快照复制，实际连接与health身份由capacity CLI验证，URL不混入语义身份。barrier有限等待，任一预检失败则未释放客户端零POST；原durable journal与unknown不可重放语义不变。新增预算校验后的11项离线测试通过。

## 第三批建库与长生成诊断补充

第三批使用上述原生预算和seed42，仍未获得可验收基线。一次图像观察关联的长调用持续约1800秒后，该资产变为failed；阻塞期间其它已准备结果因既有按序提交机制等待。模型持续生成且没有队列积压、KV抢占或新增swap。该请求为非流式，未保存生成正文；不能据计数推断具体复读形态，也不能把1800秒客户端限制归为模型架构失败。新API/PB随后按PID及启动身份停止，已失败与4个未确认processing资产保留，未恢复重发。

仅建库的只读守卫在诊断前停止，采样段2513秒、483样本：峰值running4、waiting0、KV24.44%；抢占和swap增量均0，主机最低可用约92.97GiB。原始遥测与汇总为evidence/build-native-seed42-model-100-guard/，不混入后续诊断成绩。

独立的新实验保留原图片、native无输出上限、seed42，并只将HTTP响应改为流式私有记录；不写数据库，也不更改原请求状态。100同图单请求完成5.39秒/402输出token。原4候选按0/2/4/22秒错峰及同步并发分别完成39.13秒（5次调用）和24.33秒（6次调用），全部stop，最多684输出token，完整JSON后0字符，没有异常重复或unknown。同一候选在单请求/错峰/同步首轮分别输出402/403/435token，同步组还触发中文化；固定seed不等于不同批组织下输出逐token一致。以上均未复现原长调用，不能据短通过认定已修复，也不无限追加重复实验。

100实装vLLM0.26的xgrammar后端只读取服务级disable_any_whitespace，json_object以任意对象schema编译，当前启动配置允许结构空白。GrammarMatcher默认需要stop token才终止，完整JSON本身不强制结束。这与官方问题[#38696](https://github.com/vllm-project/vllm/issues/38696)及[#48765](https://github.com/vllm-project/vllm/issues/48765)描述的风险相符，但只是候选机制；本轮没有原长请求正文，尚不能确认为根因。模型服务配置未因此变更。

118的一个独立诊断先调用/tokenize后生成，触发vLLM0.19多模态缓存接收侧缺项错误，SSE仅返回空内容及480输入/0输出usage，没有finish_reason。源码与精确请求日志关联表明/tokenize预处理更新了API侧缓存，却没有把输入提交到EngineCore；这不是省略输出上限的默认行为。该次记录仍保留unknown，未重发。当前部署未开启dev mode，因此/reset_mm_cache返回404；没有为此开放开发RPC。随后按原容器、原参数重启清空两侧缓存，并保存进程边界；新独立实验省略前置/tokenize、使用生成完成后的usage计量，初次观察234输出token/23.88秒、中文化281token/24.53秒，均正常stop，完整JSON后0字符，无unknown。

后续多模态诊断应避免在vLLM0.19同服务先/tokenize再首次提交该图片。token计量可使用实际生成usage，或明确隔离的离线tokenizer路径；不能把/tokenize视作没有缓存副作用的预检。100及118上述诊断结束均确认running/waiting/KV归零。私有证据目录分别为long-observe-100-no-tokenize-19277aa073a6423ba61d3be64d64b5cd、long-observe-batch4-e74679c955f546d693b8f8f720f6ba48、long-observe-118-after-cache-restart-327db9a571f540c2b8e6e7e4caf62d0f。


## 118 与 123 的实际窗口验收

118 的20GiB KV/seq32配置完成两客户端与三客户端满窗口测试。两客户端整组768.72秒；三客户端整组1165.21秒，三次均实际261632输入+512输出，峰值running2/waiting2、KV80.65%，无新增swap或抢占，最低主机可用18.52GiB。三客户端完成包含排队时间，不表示同时驻留三个完整窗口。0.19多模态缓存故障后的同参数重启改变了进程身份，重启前容量记录与重启后的观察诊断分别保留。

123 地址为192.168.1.123。项目私有Docker/containerd与NVIDIA运行配置均位于SSD任务目录；原Sentrix、PhotoBench、BGE服务未停止，也没有修改全局Docker或系统内存策略。镜像与完整AWQ权重哈希分别匹配118。当前KV10GiB、seq1、原生262144，启动后文本、图片、工具及流式协议和32K/64K/128K/192K/完整窗口均通过。最后一档实际261632输入+512输出，382.03秒，最低主机可用21.35GiB，无新增swap，满窗抢占增量0。seq1是当前实验配置，不能当作硬件最大并发。

123的旧服务在916.20秒测量窗内仍分别消耗1.08/0.30/0.16 CPU秒，RSS范围已保留；GPU按PID归属指标不可用，不能标成零负载。原始证据位于内网evidence/node123-native-validation/，validation-report.json SHA256为1663b70c95ec766ff7385c2e1e51919d1bbb4798af6d65bce56f69e45dd1b7e2。

上述512只属于固定长度的原生窗口容量探针。真实Agent和视觉观察不设置2048或512输出上限，由服务按实际输入计算262144总窗口的剩余空间。启动参数中的chunked prefill 2048仅控制每批处理输入的数量，不是回答长度或上下文上限。


## 待新批部署的报错与观测修复

原生模式下一批显式设置30个模型步，主循环、上下文恢复、L2评审与强制收尾共用该上限；规划调用独立记录。工具总数、search、page、inspection四个环境变量设为字符串null，实际配置报告为JSON null，不再另以28次限制取证。legacy默认行为保留。若第30步的L2明确要求修复而已无剩余步，即使保留已有答案，也记录轮次限制，不把它送去质量评分。

模型发现失败、HTTP拒绝、content_filter、流式缺少完整结束、SQLite锁竞争、检索超时/内存故障与向量索引不可用均保留结构化运行证据。正常无命中及按设计未配置的派生索引保持原语义。云Judge被过滤时，即使响应里带合法的0分JSON也不计分，不自动重试；真正完成且可评估的错误答案仍保留0分。

新增默认关闭的VLM JSON流式传输，不添加输出token上限，也不改变采样参数。开启后按完整POST总时限控制等待，记录首响应、终止原因、usage、字符/空白/重复统计、请求ID及内容哈希；默认不保存正文或图片。建库诊断将单独开启仅内网保存的生成正文记录，正式QA关闭该正文开关。状态写入失败会明确记日志，冷验收同时核对本次API日志范围与每个请求的完整终态，不能以缺文件冒充没有失败。

PhotoBench保存人物洞察原始计数，moment_failures大于0标记建库不完整；缺失历史计数保持未知。上述变更已通过专项回归，完整CI和真实新建库结果仍需分别记录，不能用代码测试替代实跑验收。
