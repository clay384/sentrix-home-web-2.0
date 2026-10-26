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

此固定模型实例已完成一次满窗单请求（122.21秒）与第一对并发请求（253.88 / 256.12秒），实际输出均512token。第一对峰值KV97.752%，无KV抢占。第二对已完成：2/2实际261632+512，整组253.24秒，峰值KV97.752%，无抢占，结束running/waiting归零且进程/启动身份不变。并发3补测单独保存新请求ID、journal、遥测与不可重放标记；结果以各组round.json为准。

资源守卫从每组首个样本累计：KV抢占至少2次才触发反复抢占；队列连续3个采样间隔增长、swap连续2个间隔新增、计数器重置/进程启动时间变化、模型指标缺失或主机可用内存低于8GiB均停止新派发。单次抢占保留证据，不独立判失败。请求结果未知不重发。

上述短组不足以证明最大稳定并发或30分钟资格。失败组默认不构成已确认容量边界；需要明确原因和原始证据才能形成边界结论。200 CPU与远端模型/主机的指标分别归属；GPU0外部负载也需保留，不能称整机独占。

## 真实视觉观察协议与人工限制

最初两批记忆库均未通过完整性验收，保留在隔离目录。processed不能代替结构化观察成功。旧请求2048输出上限导致的length、客户端超时、provider拒绝分别属于操作事实，不能直接当作模型质量或架构结论。

新的共享生成策略为Qwen官方non-thinking参数：temperature0.7、top_p0.8、top_k20、min_p0、presence_penalty1.5、enable_thinking=false，并预先固定seed42。视觉JSON使用json_object。Agent/视觉/因果文本请求取消旧2048/768上限；原生模式由vLLM使用实际模板/图片/工具输入计量剩余窗口，不盲传262144输出。100安装版本的请求解析及generation_config已核验无额外max_new_tokens上限。普通部署仍可选legacy策略。

新独立参数实验使用16张固定图片（8张历史空观察、8张历史正常观察），串行及并发4各一轮：32次首轮均含8个约定字段，最终caption/activity/place/event_type全部非空。39次实际模型调用全部正常stop，输出25–640token，无新unknown，完整JSON后没有新增字符。7次中文化中2次只返回caption，既有合并保留其余字段；部分OCR及个别caption保留拉丁字母，作为内容现象单列。旧180秒超时请求仍为unknown，不修改旧账本。此为协议候选验收，不满足容量档每轮至少32题或连续30分钟要求。

私有原始图片、提示词及响应仅保留内网诊断目录，不进入Git。可公开的汇总证据：seed42-native-cohort-274dafc7cc944cc685878adc9fb96462/summary.json及field-completeness-summary.json。

## 本轮后续部署预算

部署模板已显式选择原生262144、Qwen non-thinking/seed42；Agent最多30轮，各工具总数及search/page/inspection上限28；回合墙钟3600秒，Chat/VLM单请求HTTP1800秒，PhotoBench单题等待4200秒。预算是运行约束而非模型质量标准，触及时必须带具体原因返回。源码默认值保留旧部署兼容性，测试工具必须校验实际运行配置。

服务拒绝、网络/超时、输出/上下文限制通过typed ModelServiceError传播；图片流水线标failed，摘要不写fallback伪装成功，因果图保留degraded及causal_issues，家庭图谱保留failed及stats.operational_issue。内容JSON无效仍保留原质量降级语义。统一API错误响应只含稳定码与安全预算事实，不转述原provider正文。PhotoBench须将对应非2xx或内嵌failed/degraded标为建库不完整，不能ready。

## 多客户端准备状态

runtime/shared_clients.py / shared_client_worker.py仅准备和离线验证，尚未恢复、启动或运行任何多客户端组。每组2或4个软件实例拥有独立数据库/配置/结果/PID，全部共享200 CPU/RAM与18101 BGE、18103 CLIP侧车。每client Judge=4意味着总潜在并发8或16，不等于全局4。

工具同时校验environment与expected-runtime完整预算，并显式传入PhotoBench4200秒；缺键或旧模板拒绝执行。记忆语义embedding身份从未来已验收快照复制，实际连接与health身份由capacity CLI验证，URL不混入语义身份。barrier有限等待，任一预检失败则未释放客户端零POST；原durable journal与unknown不可重放语义不变。新增预算校验后的11项离线测试通过。
