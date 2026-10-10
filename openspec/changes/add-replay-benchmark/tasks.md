## 1. SpendLogs source

- [x] 1.1 检查提供的 SpendLogs JSONL 记录结构（source: litellm-data-source）
- [x] 1.2 校验只读 JSONL、model/startTime/request_id 及可回放请求内容（source: litellm-data-source）
- [x] 1.3 实现按源模型和 [start,end) 选择记录、排序并返回请求数（source: litellm-data-source）
- [x] 1.4 检查逐记录请求内容；缺失时阻止启动（source: litellm-data-source）

## 2. Timed replay

- [x] 2.1 支持 OpenAI-compatible chat messages；拒绝缺少 payload 或不支持的请求（source: traffic-replay）
- [x] 2.2 按 startTime 相对偏移调度请求，不等待前一条请求完成（source: traffic-replay）
- [x] 2.3 实现目标模型选择、LiteLLM 路由、服务端认证和启动前预览（source: traffic-replay）
- [x] 2.4 实现 run ID、运行状态、取消及已发送请求计数（source: traffic-replay）

## 3. Dashboard

- [ ] 3.1 实现带模型标签的 LiteLLM Prometheus 时间序列查询与源模型筛选（source: benchmark-dashboard）
- [ ] 3.2 实现图表拖选并映射为 [start,end) 时间范围（source: benchmark-dashboard）
- [ ] 3.3 实现目标模型选择及启动确认流程（source: benchmark-dashboard）
- [ ] 3.4 展示运行中/完成/失败状态和已发送请求数（source: benchmark-dashboard）

## 4. Open decision

- [ ] 4.1 明确发送器错过计划发送时间时的调度策略（source: traffic-replay）
