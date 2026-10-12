# LiteLLM Benchmark Replay API

按 LiteLLM SpendLogs JSONL 中 `startTime` 的相对间隔，将历史请求开放环回放到目标 LiteLLM 模型。服务提供 HTTP API，不包含前端页面；响应、延迟、token 和错误等结果由 LiteLLM 记录。

## 启动方式

以下两种方式都要求：

- Python 3.10 或更高版本，或 Docker Engine + Docker Compose 插件（二选一）。
- 一个可从 Benchmark 服务访问的 LiteLLM Proxy，以及对应的 Master Key。
- 一个 JSONL 文件或归档目录，其中记录包含 `startTime`、`model` 和可回放请求内容。
- 可选：可访问的 Prometheus 地址，仅供 `GET /api/series` 使用。

### Docker Compose

适用于服务器上 LiteLLM、Postgres、Redis 和 Prometheus 已经运行的情况。此 Compose **只启动 Benchmark API**，不会启动或改动其他服务。

1. 将项目放到服务器。创建归档目录并将完整 SpendLogs 快照放入其中：

   ```bash
   mkdir -p data/spendlogs
   ```

2. 复制环境变量模板并编辑：

   ```bash
   cp .env.example .env
   ```

   设置 `LITELLM_URL`、`LITELLM_MASTER_KEY` 和 `SPENDLOGS_ARCHIVE_HOST`。默认 LiteLLM 地址 `http://host.docker.internal:4000` 适用于宿主机发布了 4000 端口的情况；如果 LiteLLM 在其他主机或 Docker 网络中，填写 Benchmark 容器可访问的地址。

3. 在这台 arm64 Mac 上从项目根目录构建 Linux ARM64 镜像包：

   ```bash
   docker buildx build --platform linux/arm64 -t litellm-benchmark:latest \
     --output type=docker,dest=litellm-benchmark-linux-arm64.tar .
   ```

   将 `litellm-benchmark-linux-arm64.tar` 传到服务器，并在服务器导入：

   ```bash
   docker load -i litellm-benchmark-linux-arm64.tar
   ```

4. 启动。Compose 使用已导入的本地镜像，不构建也不尝试拉取：

   ```bash
   docker compose --env-file .env -f compose.server.yaml up -d
   ```

   查看日志：

   ```bash
   docker compose --env-file .env -f compose.server.yaml logs -f benchmark
   ```

API 默认仅绑定服务器 `127.0.0.1:8088`。远程访问可使用 SSH 隧道或受保护的反向代理。

### Python 直接启动

适用于已有 Python 3.10 或更高版本的环境。程序只使用 Python 标准库，不需要安装第三方包。Docker 镜像则使用 Dockerfile 中指定的 Python 3.12。

```bash
export LITELLM_URL=http://你的LiteLLM地址:4000
export LITELLM_MASTER_KEY=你的LiteLLM密钥
export SPENDLOGS_JSONL=./data/spendlogs
export PROMETHEUS_URL=http://你的Prometheus地址:9090  # 可选，仅供 /api/series
export BENCHMARK_HOST=127.0.0.1  # 默认仅本机访问；远程访问请使用 SSH 隧道/受保护代理
export BENCHMARK_PORT=8088
python3 app.py
```

`SPENDLOGS_JSONL` 可指向单个 `.jsonl` 文件或递归包含多个 `.jsonl` 文件的目录。默认值为项目目录下的 `LiteLLM_SpendLogs.jsonl`。

## 环境变量

| 变量 | 用途 | 默认值 |
|---|---|---|
| `LITELLM_URL` | LiteLLM Proxy 地址 | `http://127.0.0.1:4000` |
| `LITELLM_MASTER_KEY` | 获取目标模型列表并发送回放请求 | 无；必填 |
| `SPENDLOGS_JSONL` | JSONL 文件或归档目录 | `./LiteLLM_SpendLogs.jsonl` |
| `SPENDLOGS_MANIFEST` | 可选的 JSONL 文件清单路径 | 无 |
| `PROMETHEUS_URL` | `/api/series` 查询地址 | `http://127.0.0.1:9090` |
| `BENCHMARK_PORT` | HTTP API 监听端口 | `8088` |
| `BENCHMARK_HOST` | HTTP API 监听地址 | `127.0.0.1` |
| `BENCHMARK_MAX_WORKERS` | 最大并发请求数 | `128` |
| `LITELLM_PROMETHEUS_METRIC` | Prometheus 指标名称 | `litellm_requests_metric` |
| `LITELLM_PROMETHEUS_QUERY_TEMPLATE` | 可选 PromQL 模板，支持 `{metric}`、`{model}` | `sum(rate({metric}{model="{model}"}[1m]))` |

Docker Compose 用 `.env` 中的 `SPENDLOGS_ARCHIVE_HOST` 将服务器目录只读挂载到容器 `/data/spendlogs`，并自动设置容器内 `SPENDLOGS_JSONL`。也可用 `BENCHMARK_HOST_PORT` 修改宿主机绑定的本地端口。

如需从现有 LiteLLM Postgres 导出 JSONL 快照，使用独立目录 [`spendlogs-export/`](spendlogs-export/README.md) 中的脚本。导出脚本不属于 API 启动依赖。

## HTTP API

- `GET /api/models`：返回 JSONL 中的源模型和 LiteLLM 当前可用的目标模型。
- `GET /api/preview?model=...&start=...&end=...`：返回选区记录数、可回放数、缺少请求内容数。时间范围为 `[start,end)`，时间必须带时区。
- `POST /api/runs`：异步启动回放，返回 `run_id`。可选 `speed` 为正数，默认 `1.0`；调度间隔为原始相对间隔除以 `speed`。
- `GET /api/runs/{run_id}`：返回任务状态及 `sent_count`。
- `POST /api/runs/{run_id}/cancel`：请求取消回放；已发送的请求会继续由 LiteLLM 处理。
- `GET /api/series?model=...&start=...&end=...`：读取 Prometheus 时间序列。

`sent_count` 表示已提交给 HTTP 客户端的请求数，不是已完成响应数。服务重启后任务历史会丢失。服务没有独立用户认证，应保持本机回环访问或放在受保护的反向代理之后。

## JSONL 回放规则

每行一个 JSON 对象。至少需要 `model`、`startTime`（或 Unix 秒/ISO-8601 格式的 `timestamp`），以及可还原为请求体的字段。例如：

```json
{"request_id":"req-1","model":"source-model","startTime":"2026-10-09T08:23:21.665+00:00","request_body":{"messages":[{"role":"user","content":"hello"}],"temperature":0.2}}
```

可从 `proxy_server_request`、`request_body`、`request`、`body`、`messages` 或文本 `prompt` 读取请求内容。无时区的 LiteLLM `startTime` 按 UTC 解释。回放保留已保存的模型参数（如 `temperature`、`top_p`、`max_tokens`），只替换目标模型；历史凭证、内部字段和 metadata 不会转发。`call_type=anthropic_messages` 使用 `/v1/messages`，`acompletion` 等 Chat Completions 类型使用 `/v1/chat/completions`。

回放会根据所选模型与 `[start,end)` 时间范围筛选记录，合并归档快照后按 `(request_id, startTime)` 去重，并按原始相对间隔发出请求。每个服务实例同时只允许一个活动回放任务。
