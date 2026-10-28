# 视觉 JSON 协议变更与观察完整性

## 问题与证据

独立测试库有 40 条 import 图片观察（39 照片、1 视频关键帧）仍为 revision=1、confidence=0 且无语义字段，虽然对应资产状态为 processed。身份种子和原始视频不在这个集合中；detail_json={} 也存在于正常观察，不能用它单独判断失败。

对其中一张失败图片按原观察协议作新的独立诊断请求，端点返回 HTTP 200 / finish_reason=stop / 503 completion tokens，但 facts 的字符串引号混用，标准 JSON 解析失败。同一轮正常图片对照成功。模型和网络没有在该次诊断中报错，回复也未达到 2048 输出上限。原 40 次请求没有保存原始回复，因此不能把每一条历史失败都认定为同一种语法错误。

原请求未指定 temperature 或 response_format；实际 vLLM 版本默认 temperature=1.0。共享 chat_json 对解析失败静默返回 None，ObserveAsset 按既有增强逻辑继续导入，因此 processed 不保证语义成功。后续两次单请求诊断分别只加 temperature=0、再加 JSON 模式，均返回合法 JSON、555 completion tokens。诊断不写数据库，不计入模型成绩。

## 首轮公共修复（后续预算裁决见末节）

chat_json 固定 temperature=0，并请求标准 response_format={"type":"json_object"}。观察器、PhotoInspector 和 PersonVision 共用该协议；普通 Agent ChatModel 请求不变。2048 输出预算与观察器最多三次调用保持不变。JSON 模式约束语法，不保证字段完整或事实正确；入库验收仍须检查实际内容。

HTTP 错误、无效 JSON、finish_reason=length 的截断输出均返回 None。没有新增重试、unsupported fallback 或输出修补；尤其不将失败请求静默改成自由生成再请求。warning 记录端点、模型、状态/错误类型、有限 finish_reason 枚举、内容长度与整数 token 数，不记录图像、提示词、响应正文或异常消息。

[llama.cpp 官方服务文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api) 明确支持 json_object。该证据表示协议兼容，不代表已在所有已安装二进制验证；未访问或修改 153 主服务。实际不支持的端点会明确降级一次。

## 回归与部署边界

真实本地 HTTP 服务回归覆盖固定采样/JSON模式、混引号失败、截断、400不支持且不重试、日志隐私，以及共享照片问询的裁剪件/原图顺序和人物外观调用。既有观察三段调用上限、照片问询与人物视觉测试一并执行。共享协议变更须随模型身份/代码版本记录到运行与记忆快照清单，不能把旧协议与新协议的建库证据混成同一组。

40 条的修复应使用原 observation ID，经应用 ObserveAsset 及派生索引服务完成；不能重新导入造成重复观察，不能直接补 JSON 字符串，也不能只靠 processed 状态宣布通过。已成功内容不重复请求。修复与派生事件/图谱的刷新是独立维护操作，须保留冷备、目标清单、每阶段状态以及结果未知请求，执行前评审完整性方案。本变更自身不修改已有库。


## 后续诊断与用户预算裁决（覆盖前文2048/greedy决定）

新建库观察到greedy策略重复生成，2个已失败图片分别比较temperature=0、加json_object、再显式enable_thinking=false，三种条件下各自正文SHA一致，均在2048人工上限触发length，reasoning为0。官方non-thinking策略±json_object的4次对照则均以stop完成（342–382 tokens）。这是采样策略与操作预算证据，不将预算截断算成模型或架构质量失败。

用户随后明确取消人为2048上限。视觉JSON入口不再依赖Agent的MAX_ANSWER_TOKENS常量，不传max_tokens或max_completion_tokens，采样由ServedModel.generation_parameters()统一提供。100部署的vLLM0.26源码已核验：省略参数→None→min(原生max_model_len减实际输入长度、generation_config覆盖、平台限制)；实际generation_config没有max_new_tokens，CUDA平台无更低输出上限。实际输入包括模板和视觉token，不盲传262144作为输出数。

[Qwen3.5-4B官方模型卡](https://huggingface.co/Qwen/Qwen3.5-4B#best-practices)给出非思考通用策略：temperature0.7、top_p0.8、top_k20、min_p0、presence_penalty1.5；本轮配置通过统一profile选择该策略，其他部署保留显式legacy选择。llama.cpp文档也支持chat_template_kwargs传enable_thinking；兼容性以实际端点验收为准，不操作153。

原生预算候选的完整观察链验收在第5张的中文化调用触发诊断客户端180秒超时，留下1个结果未知请求并停止后续；其前4张通过不足以批准重新建库。该请求没有重放。新规范化样本以流式诊断继续定位，600秒诊断期限单独记录。预算、provider拒绝/上下文限制、客户端超时、JSON解析失败分别保留证据，不能把操作限制伪装为质量结论，也不能把单图通过推广到全集。

新的两条流式规范化样本正常 stop（77 / 448 tokens），完整 JSON 后没有追加内容；固定 seed=42 的新配置对照在同一规范化输入上亦正常 stop（25 tokens），但只返回 caption 字段。它们没有复现旧未知请求的实际生成形态，因此不把旧超时标记改为成功。后续固定 16 图串行与并发 4 使用新实验 ID，逐请求保留流式证据；是否可接受还需完整观察链验收。


## 最终运行错误传播与固定参数验收

后续用户明确要求运行限制可见，覆盖前文 HTTP/length 返回 None 的首轮行为。共享 chat_json 对 HTTP 拒绝、网络故障、超时和 finish_reason=length 抛 ModelServiceError，分别携带 provider_rejected/provider_unavailable/time_limit/output_limit、HTTP 状态、无私有内容的请求策略及已知 token 数。读取超时/断连标结果未知，连接建立失败标未派发；均不新增重试。模型名字探测失败同样传播。合法完成但内容 JSON 无效仍是质量降级，继续返回 None，不能改称基础设施故障。

ObserveAsset.prepare 与 ImportAssets.prepare_image 自然传播异常，现有 ProcessAlbumPipeline 将对应图片置 failed 并记录 AlbumFailure；视频关键帧路径显式重抛 ModelServiceError，避免泛异常处理把源视频标为成功。照片问询/人物视觉共用同一错误契约；助手工具层另将错误转为可见 ToolOutcome。错误消息和策略不保存图片、提示词、返回正文或未经清洗的 provider error body。

固定 seed=42、HTTP 600 秒诊断期限、无输出 cap 的新实验，16 张固定图片（8 个历史空观察、8 个正常观察）分别串行与并发 4 完成。32 次首轮全部包含 8 个约定字段，32 个最终观察的 caption/activity/place/event_type 均非空；39 次模型请求全部 stop，输出 25–640 tokens，完整 JSON 后没有追加字符，无新的未知请求。7 次中文化里 2 次仅返回 caption，既有字段合并保留其他观察；拉丁 OCR/个别 caption 内容未完全中文化单列为质量现象。旧 180 秒实验的未知请求未被更改或重试。这里验证的是协议候选，样本数和运行时长不足以形成最大安全并发结论。


因果判断文本入口同样使用统一采样并省略输出 cap。CausalInferenceError 继承 ModelServiceError，HTTP 拒绝/网络/超时/length 保留操作原因及实际 token 信息；正常完成但模型 JSON 缺行/语义非法仍是 CausalParseError，既有小批解析恢复语义不变。图构建保留其他投影和 causal_status=degraded，同时返回 causal_issues 列表，经流水线 API 原样传递；不会把运行失败作为 has_causal=false 的合法判定。没有修改数据库结构或发起新的实际推理。


事件总结的 typed 操作失败沿 SummarizeEvent → ImportAlbum / ProcessAlbumPipeline 原样传播，不写确定性摘要伪装成功；正常完成但内容无效仍使用原有 fallback。统一 API 异常处理按稳定错误码返回安全信息：time_limit=504、provider_unavailable=503、context_limit=422、其他已知模型操作错误=502；detail 保留上游 HTTP 状态、token、预算与 unknown 标记。响应不转述异常原文，策略仅允许数值预算、已知采样参数与布尔模板开关，过滤提示词、图片、凭据、provider body和未知文本。FamilyGraph 继续返回既有 failed run，在 stats.operational_issue 保存 typed 事实，不增加表或接口类型；PhotoBench 必须据 failed 状态拒绝把该建库标为 ready。
