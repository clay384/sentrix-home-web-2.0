# 2026-10-26 并发评测恢复差异

已知旧行为：恢复忽略 QA 已完成但 Judge pending 的题，并可能重跑中断时的 running QA。Judge 全部请求在独立线程池内排队，派发前无持久标记。

本轮将 QA 与 Judge 的执行池提交限制在各自并发额度内，外部派发前先保存检查点。恢复只补明确未派发的评分，保留已完成、已失败与结果未知的记录；旧 pending 无派发证据时保守未知。这是防止重复请求的有意变化。显式全量重新评分语义保持不变。取消后不派发新的云请求，已有请求按原超时完成；不承诺已发送的请求能撤销。Judge 已发出的 POST 若响应未知，不再被 broad retry 重放，而是保存 outcome_unknown；明确收到429/5xx仍按既有策略有界重试。

接口新增字段及统计口径见 `docs/architecture/photobench-metrics-contract.md`。单元及临时文件检查覆盖评分补齐、合法0分保留、未知结果不重发、独立Judge4、有界取消、派发前保存失败和实际HTTP/429计数。旧数据不改写。回滚可回退本轮代码；保留结果记录，勿把未知请求改回pending自动重跑。


另确认200文件系统中同长度原地重写会出现 inode、size、mtime_ns、ctime_ns全部一致，stat-only缓存因此返回重评分前的旧分。结果仓储仅把不可变snapshot generation作为稳定revision；没有原子提交清单的legacy文件返回空revision，跳过跨请求终态/投影缓存和stat-only磁盘索引。新格式仍直接读小metadata并缓存，不逐次hash大run.json；旧历史文件要按需重读，这是保证评分可见性的有意性能取舍，不静默迁移或修改旧数据。确定回归模拟相同完整stat，另验证新格式重复读取仍命中小元数据缓存、发布新generation后失效。
