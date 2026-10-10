## ADDED Requirements

### Requirement: Select source requests from JSONL
系统 SHALL 从只读 JSONL 输入文件按源模型和带时区的时间范围筛选请求。时间范围 SHALL 为 [开始时间, 结束时间)，优先使用 `startTime`，并兼容 trace 格式的 `timestamp`。

#### Scenario: Valid source model and time window
- **WHEN** 用户指定源模型和合法时间窗口
- **THEN** 系统 SHALL 只选择 model 匹配且时间戳满足 startTime >= 开始时间 AND startTime < 结束时间的记录，并返回候选请求数

#### Scenario: Invalid time window
- **WHEN** 起始时间不早于结束时间，或时间缺少时区
- **THEN** 系统 SHALL 拒绝查询并给出可操作的校验错误

### Requirement: Validate replayable request content
JSONL 记录 SHALL 包含 model、时间戳，以及完整 request body、messages 或 prompt。系统 SHALL 仅回放包含非空 messages 数组的 OpenAI-compatible chat 请求。

#### Scenario: SpendLog metadata without prompt
- **WHEN** 记录只包含 token 数、request ID、元数据或响应，缺少可回放请求内容
- **THEN** 系统 SHALL 标记其不可回放并在启动前说明原因；系统 SHALL NOT 构造模拟 prompt 或补造内容

#### Scenario: Reconstructed request
- **WHEN** JSONL 只保存 messages 而没有完整请求参数
- **THEN** 系统 SHALL 只使用可获得的原始 messages 构造请求，并 SHALL NOT 声称恢复了未记录的采样参数

### Requirement: Protect source data and credentials
系统 SHALL 以只读方式读取 JSONL；不得向客户端或普通应用日志返回源文件中的凭据。回放时 SHALL 使用服务端 LiteLLM 认证配置，不得转发历史 API 凭证。
