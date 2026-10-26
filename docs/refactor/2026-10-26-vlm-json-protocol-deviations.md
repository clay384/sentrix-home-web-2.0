# 视觉 JSON 协议变更与观察完整性

## 问题与证据

独立测试库有 40 条 import 图片观察（39 照片、1 视频关键帧）仍为 revision=1、confidence=0 且无语义字段，虽然对应资产状态为 processed。身份种子和原始视频不在这个集合中；detail_json={} 也存在于正常观察，不能用它单独判断失败。

对其中一张失败图片按原观察协议作新的独立诊断请求，端点返回 HTTP 200 / finish_reason=stop / 503 completion tokens，但 facts 的字符串引号混用，标准 JSON 解析失败。同一轮正常图片对照成功。模型和网络没有在该次诊断中报错，回复也未达到 2048 输出上限。原 40 次请求没有保存原始回复，因此不能把每一条历史失败都认定为同一种语法错误。

原请求未指定 temperature 或 response_format；实际 vLLM 版本默认 temperature=1.0。共享 chat_json 对解析失败静默返回 None，ObserveAsset 按既有增强逻辑继续导入，因此 processed 不保证语义成功。后续两次单请求诊断分别只加 temperature=0、再加 JSON 模式，均返回合法 JSON、555 completion tokens。诊断不写数据库，不计入模型成绩。

## 公共修复

chat_json 固定 temperature=0，并请求标准 response_format={"type":"json_object"}。观察器、PhotoInspector 和 PersonVision 共用该协议；普通 Agent ChatModel 请求不变。2048 输出预算与观察器最多三次调用保持不变。JSON 模式约束语法，不保证字段完整或事实正确；入库验收仍须检查实际内容。

HTTP 错误、无效 JSON、finish_reason=length 的截断输出均返回 None。没有新增重试、unsupported fallback 或输出修补；尤其不将失败请求静默改成自由生成再请求。warning 记录端点、模型、状态/错误类型、有限 finish_reason 枚举、内容长度与整数 token 数，不记录图像、提示词、响应正文或异常消息。

[llama.cpp 官方服务文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api) 明确支持 json_object。该证据表示协议兼容，不代表已在所有已安装二进制验证；未访问或修改 153 主服务。实际不支持的端点会明确降级一次。

## 回归与部署边界

真实本地 HTTP 服务回归覆盖固定采样/JSON模式、混引号失败、截断、400不支持且不重试、日志隐私，以及共享照片问询的裁剪件/原图顺序和人物外观调用。既有观察三段调用上限、照片问询与人物视觉测试一并执行。共享协议变更须随模型身份/代码版本记录到运行与记忆快照清单，不能把旧协议与新协议的建库证据混成同一组。

40 条的修复应使用原 observation ID，经应用 ObserveAsset 及派生索引服务完成；不能重新导入造成重复观察，不能直接补 JSON 字符串，也不能只靠 processed 状态宣布通过。已成功内容不重复请求。修复与派生事件/图谱的刷新是独立维护操作，须保留冷备、目标清单、每阶段状态以及结果未知请求，执行前评审完整性方案。本变更自身不修改已有库。
