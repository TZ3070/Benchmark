## Context

本工具从只读 JSONL 文件读取历史请求，后端 API 接收源模型和时间范围，再选择目标模型并启动回放。工具按 `startTime` 的相对时间间隔经 LiteLLM 发起请求。LiteLLM 负责处理和记录回放结果；工具只展示运行状态和已发送请求数。

## Goals / Non-Goals

### Goals
- 按源模型和 [开始时间, 结束时间) 从 LiteLLM SpendLogs 选择请求。
- 用带模型标签的 LiteLLM Prometheus 指标展示源模型时间序列，支持鼠标框选时间。
- 按 startTime 的请求间隔发起回放，不等待前序请求完成。
- 允许独立选择目标模型，并展示 run 状态及已发送请求数。

### Non-Goals
- 不生成、聚合、展示或比较延迟、吞吐、token、错误率或队列等 benchmark 结果；这些数据由 LiteLLM 产生。
- 不改写或删除 LiteLLM 数据库中的历史记录。
- 不承诺重放任意协议、任意流式或多模态请求。
- 不声称还原源记录中未保存的请求参数。

## Decisions

1. **数据来源**：以用户提供的 LiteLLM SpendLogs JSONL 为输入，按 model、startTime、request_id 和已持久化请求内容筛选；文件以只读方式挂载。
2. **Prompt 前置条件**：JSONL 必须实际包含 request body/messages/prompt；仅启用 LiteLLM 的 prompt 存储配置不能补全历史上未保存的内容，工具必须阻止此类记录回放并解释原因。
3. **图表语义**：Prometheus 风格仅指时间序列交互；横轴为时间，纵轴为指标值。指标必须有模型标签，按源模型筛选。图表指标用于浏览和选区，不是工具生成的 benchmark 结果。
4. **时间窗口**：选区采用 [start,end)，只纳入 startTime >= start 且 startTime < end 的请求。
5. **回放计划**：按 startTime 排序，首条选定记录在回放开始时发起；后续请求按相对首条记录的时间偏移计划。请求发起不等待前一请求完成。
6. **目标模型独立**：源模型仅筛选历史请求；目标模型单独选择并覆盖请求中的 model 字段用于 LiteLLM 路由。
7. **运行状态**：工具分配 run ID，显示 queued/running/completed/failed/cancelled 和已发送请求数；响应与性能观测由 LiteLLM 记录。运行历史暂存内存，进程重启后清空。

## Risks / Trade-offs

- prompt 存储开启前产生的历史 SpendLogs 可能没有 request/messages，不能回放。
- SpendLogs 字段、JSON 格式和时间类型可能随 LiteLLM 版本变化，需针对实际 schema 校验。
- Prometheus 指标名及模型标签可能随 LiteLLM 配置/版本变化。
- 回放流量可能造成实际费用或上游副作用；目标模型和环境必须由用户明确选择。
- 发送器本身可能因 CPU、数据库读取或网络延迟而错过计划时间，错过后的补发/顺延行为尚待用户确认。

## Open Questions

- 发送器错过某条请求的计划时间后，是立即补发并继续沿用绝对计划时间，还是顺延后续请求以保持相邻发送间隔？
- 初版支持哪些 LiteLLM 请求类型？流式、多模态和工具调用请求是否拒绝回放？
- 目标模型字段覆盖与 fallback/retry/cache 行为是否需要额外控制？
- Prometheus 图表默认选用哪个带模型标签的指标，标签按 model 还是 requested_model 过滤？
- 请求内容留存、敏感数据处理和访问控制策略是什么？
