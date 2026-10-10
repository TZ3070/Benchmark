## Why

需要从 LiteLLM SpendLogs 选取历史请求，并根据原始 startTime 节奏向选定目标模型发出请求。请求结果及性能数据由 LiteLLM 产生和记录；本工具负责筛选、调度和显示回放运行状态。

## What Changes

- 按源模型和时间范围从只读 JSONL 请求记录选择历史请求，时间范围为 [开始时间, 结束时间)。
- 按 SpendLogs.startTime 的原始时间间隔向用户选择的目标模型发起请求，请求通过 LiteLLM 路由。
- 提供类 Prometheus 时间序列图表，使用带模型标签的 LiteLLM Prometheus 指标，横轴为时间、纵轴为指标值，并按源模型筛选。
- 提供鼠标框选时间范围、选择目标模型并启动回放的交互。
- 展示回放运行中/完成/失败状态及已发送请求数。回放产生的性能数据由 LiteLLM 记录和展示，本工具不生成比较报告。
- 明确 prompt 留存、凭据保护、JSONL 只读访问及回放目标隔离要求。

## Capabilities

### New Capabilities
- `litellm-data-source`: 按时间和源模型读取、筛选及校验 LiteLLM SpendLogs 请求。
- `traffic-replay`: 按 SpendLogs.startTime 节奏经 LiteLLM 向目标模型发出请求。
- `benchmark-dashboard`: 按源模型查看 Prometheus 时间序列、选择时间范围和目标模型，并查看运行状态和已发送请求数。

### Modified Capabilities
- 无。

## Impact

- 影响 benchmark service、JSONL 读取器和请求调度器。
- 依赖 JSONL 中存在可回放的请求内容、model、startTime 和 request_id 字段。
- 依赖 LiteLLM Prometheus 提供带模型标签的可查询指标。
- LiteLLM 的 prompt 存储开关必须启用，新请求才会保存回放所需内容；历史上未存储的 prompt 不可回放。
