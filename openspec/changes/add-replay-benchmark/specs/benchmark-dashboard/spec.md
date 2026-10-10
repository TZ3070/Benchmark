## ADDED Requirements

### Requirement: Browse a model-filtered Prometheus time series
系统 SHALL 提供单页、类 Prometheus 的时间序列图表，横轴为时间、纵轴为指标值。图表 SHALL 使用 LiteLLM Prometheus 中带模型标签的指标，并按所选源模型筛选；面板右上方 SHALL 提供源模型选择器。

#### Scenario: Select a source model
- **WHEN** 用户选择源模型
- **THEN** 图表 SHALL 更新为该模型对应的指标时间序列；源模型 SHALL 同时用于筛选 SpendLogs 回放请求，且不得决定目标模型

#### Scenario: No matching metric data
- **WHEN** 所选源模型在图表时间范围内没有对应指标数据
- **THEN** 图表 SHALL 显示明确的空状态

#### Scenario: Metric has no model label
- **WHEN** 配置的 Prometheus 指标不包含可用于模型筛选的模型标签
- **THEN** 系统 SHALL 拒绝该指标配置并说明需要带模型标签的指标

### Requirement: Select a benchmark time range on the chart
图表 SHALL 支持鼠标按下设置开始时间、拖动预览范围、松开确定结束时间，并显示所选范围。所选范围 SHALL 采用 [开始时间, 结束时间) 语义。

#### Scenario: Select time range
- **WHEN** 用户在绘图区按下、向后拖动并松开
- **THEN** 系统 SHALL 将按下和松开位置映射为起止时间，并显示选区及可启动回放操作

#### Scenario: Invalid or empty selection
- **WHEN** 选择范围为空或范围内没有可回放请求
- **THEN** 系统 SHALL 禁止启动并解释原因

### Requirement: Choose a target model and start replay
选定有效范围后，图表右下方 SHALL 显示目标模型选择器和开始 benchmark 按钮。开始前 SHALL 展示源模型、目标模型、时间范围和预计回放请求数。目标模型 SHALL 独立于源模型选择，并只决定回放请求路由到的模型。

#### Scenario: Start replay
- **WHEN** 用户启动回放
- **THEN** 系统 SHALL 对所选范围和源模型对应的 SpendLogs 请求创建回放运行，并返回可跟踪的 run ID 和状态

### Requirement: Show replay run status and sent count
页面 SHALL 展示回放运行中、完成或失败状态，以及已发送请求数。页面 SHALL NOT 生成或比较 LiteLLM 产生的延迟、吞吐、错误率、队列或 token benchmark 结果。

#### Scenario: Replay is running
- **WHEN** 回放请求仍在调度或发送
- **THEN** 页面 SHALL 展示运行中状态和当前已发送请求数

#### Scenario: Replay finishes
- **WHEN** 回放运行完成或失败
- **THEN** 页面 SHALL 展示完成或失败状态及最终已发送请求数
