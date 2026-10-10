## ADDED Requirements

### Requirement: Replay at original startTime cadence
回放器 SHALL 按 SpendLogs.startTime 排序，并按选定记录之间的原始 startTime 间隔计划发送请求。回放开始后，首条选定请求 SHALL 在回放开始时发起；后续计划时间 SHALL 按相对首条记录的 startTime 偏移计算。回放器 SHALL 在计划时间发起请求，不得等待之前的请求完成；在途并发由原始 startTime 分布及模型处理时长自然决定。

#### Scenario: Preserve source request timing
- **WHEN** 连续两条选定记录的 startTime 差为 Δt
- **THEN** 回放器 SHALL 将后一条请求计划在前一条请求计划时间之后 Δt 发起，并 SHALL 记录计划发起时间和实际发起时间

#### Scenario: Previous request is still processing
- **WHEN** 下一条请求的计划时间到达而先前请求尚未完成
- **THEN** 回放器 SHALL 仍在计划时间发起下一条请求，不得等待先前请求完成

#### Scenario: Equal startTime values
- **WHEN** 多条选定记录具有相同 startTime
- **THEN** 回放器 SHALL 依稳定的次级排序键逐条发起，不得因时间相同而丢弃请求

### Requirement: Route replay to an explicit target
回放器 SHALL 要求用户选择目标 LiteLLM 模型及认证配置，并 SHALL 在发出请求前展示源模型、目标模型、时间范围和预计请求数。

#### Scenario: Request identity
- **WHEN** 回放器发送源请求
- **THEN** 系统 SHALL 保留本地 run ID 与源 request_id 的关联，不得覆盖原始业务字段

### Requirement: Track run status and sent count
系统 SHALL 为每次回放生成 run ID，并记录运行中、完成或失败状态及已发送请求数。请求响应、延迟、token、错误和队列等运行结果由 LiteLLM 记录；本工具 SHALL 不生成、聚合或比较这些 benchmark 结果。

#### Scenario: Run progress
- **WHEN** 回放器发起一条请求
- **THEN** 系统 SHALL 更新该 run 的已发送请求数，并在运行期间展示运行中状态

#### Scenario: Run termination
- **WHEN** 所有选定请求均已发起，或运行因错误终止
- **THEN** 系统 SHALL 展示完成或失败状态及最终已发送请求数
