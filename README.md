# LiteLLM Benchmark

LiteLLM 历史流量回放后端：从 SpendLogs JSONL 快照筛选历史请求，按 `startTime` 到达间隔开放环回放到目标 LiteLLM 模型，并提供任务状态与已发送请求数 API。

## 启动

1. 在 `.env` 中设置 compose 需要的 LiteLLM、Postgres 和 Redis 密钥。
2. 配置 `config.yaml` 中 LiteLLM 可用的 `model_list`。
3. 准备符合下方格式要求的 JSONL 文件或归档目录；如需指定离散文件，可配置清单。
4. 运行 `docker compose up --build`。

Benchmark 服务只绑定到本机回环地址。它只读挂载 `LiteLLM_SpendLogs.jsonl`，并通过 LiteLLM Master Key 将回放请求发送到本 compose 中的 LiteLLM。模型调用会产生实际费用或副作用，请在隔离测试环境和可控模型上运行。

## 部署到已有 LiteLLM 服务器

服务器已经运行 LiteLLM、Postgres 和 Redis 时，使用 `compose.server.yaml`，它只启动 Benchmark 容器，不会再启动或修改这些服务：

1. 将本项目放到服务器，并准备归档目录，例如 `data/spendlogs/`。
2. 将 `.env.example` 复制为 `.env`，设置 `LITELLM_MASTER_KEY`，并确认容器可通过 `LITELLM_URL` 访问现有 LiteLLM。默认地址 `http://host.docker.internal:4000` 适用于 LiteLLM 在宿主机发布 4000 端口的情况；若服务在其他主机或 Docker 网络，填写从 Benchmark 容器可达的地址。
3. 把每周导出的全表快照放进归档目录。默认挂载 `./data/spendlogs`；也可通过 `SPENDLOGS_ARCHIVE_HOST` 指定宿主机目录。
4. 启动：

   ```bash
   docker compose --env-file .env -f compose.server.yaml up -d --build
   ```

   查看日志：`docker compose --env-file .env -f compose.server.yaml logs -f benchmark`。

若由服务器定时导出全表快照，可运行 `scripts/export_spendlogs_weekly.sh`。脚本默认查找项目内的 `compose.yaml` 和 `postgres` 服务；服务器已有 LiteLLM Compose 时，设置 `LITELLM_COMPOSE_DIR`、可选的 `LITELLM_COMPOSE_FILE` 和 `LITELLM_POSTGRES_SERVICE`，并将 `SPENDLOGS_OUTPUT_DIR` 指向 Benchmark 的归档目录（默认 `data/spendlogs/`）。导出文件名为 UTC ISO 周，如 `2026-W41.jsonl`；同目录会生成校验用 manifest JSON。

Benchmark API 默认仅绑定服务器的 `127.0.0.1:8088`。远程访问时通过已有的 SSH 隧道或受保护的反向代理接入。`PROMETHEUS_URL` 只影响 `/api/series`；不配置可达的 Prometheus 时，回放接口仍可使用。

## HTTP API

- `GET /api/models`：返回 JSONL 中的源模型和 LiteLLM 当前可用目标模型。
- `GET /api/preview?model=...&start=...&end=...`：返回选区记录数、可回放数、缺少请求内容数。时间范围为 `[start,end)`，时间必须带时区。
- `POST /api/runs`：异步启动回放，返回 `run_id`。可选 `speed` 为正数，默认 `1.0`；调度间隔为原始相对间隔除以 `speed`，例如 `2.0` 表示两倍速，`0.5` 表示半速。
- `GET /api/runs/{run_id}`：返回 `queued`、`running`、`completed`、`failed` 或 `cancelled` 状态及 `sent_count`。
- `POST /api/runs/{run_id}/cancel`：请求取消回放；已发出的请求会继续由 LiteLLM 处理。
- `GET /api/series?model=...&start=...&end=...`：返回 Prometheus 时间序列，供未来 UI 使用。

`sent_count` 表示已提交给 HTTP 客户端的请求数，不是已完成响应数。响应、延迟、token、错误等运行指标由 LiteLLM 记录；本工具不生成性能对比报告。

## JSONL 格式

每行一个 JSON 对象，至少有 `model`、`startTime`（或 Unix 秒/ISO-8601 格式的 `timestamp`）及一个可回放请求体来源。LiteLLM SpendLogs 导出的无时区 `startTime` 按 UTC 解释：

```json
{"request_id":"req-1","model":"source-model","startTime":"2026-10-09T08:23:21.665+00:00","request_body":{"messages":[{"role":"user","content":"hello"}],"temperature":0.2}}
```

LiteLLM SpendLogs 顶层的 `proxy_server_request` 也可作为请求体来源；也支持 `request_body`、`request`、`body`、`messages` 或文本 `prompt`。回放保留请求体中已保存的模型参数（如 `temperature`、`top_p`、`max_tokens`），只将 `model` 替换为目标模型；未保存的参数不补默认值。LiteLLM 注入的 `metadata`、内部字段和历史 API 凭证不会转发，请求使用服务端配置的 LiteLLM Master Key。请求须含非空 `messages` 数组。`call_type=anthropic_messages` 会重放到 LiteLLM `/v1/messages`；`acompletion` 会重放到 `/v1/chat/completions`。仅有 token 数、request ID 或响应信息无法还原 prompt，后端会在启动前拒绝该选区。

## 配置

- `SPENDLOGS_JSONL`：JSONL 输入文件或归档目录；目录会递归发现 `.jsonl` 文件。每周导出文件名中的 UTC ISO 周（例如 `2026-W41.jsonl`）表示全表快照导出时间，不表示文件只含该周请求。因此回放会读取归档中的所有快照，再按记录时间筛选。Docker Compose 使用 `SPENDLOGS_JSONL_HOST` 指定宿主机文件或目录；服务器部署使用 `SPENDLOGS_ARCHIVE_HOST`。
- `SPENDLOGS_MANIFEST`：可选的逐行文件清单。相对路径以清单所在目录解析；每行可写一个 `.jsonl` 文件或目录，空行和 `#` 注释行会忽略。Compose 中路径应指向挂载目录内的清单，例如 `/data/spendlogs/files.txt`。

文件清单示例（每行一个相对或绝对路径）：

```text
2026/week-40.jsonl
2026/week-41.jsonl
archive/older-weeks/
```
- `PROMETHEUS_URL`、`LITELLM_URL`：compose 内部服务地址。
- `LITELLM_PROMETHEUS_METRIC`：Prometheus 指标名。
- `LITELLM_PROMETHEUS_QUERY_TEMPLATE`：PromQL 模板，支持 `{metric}`、`{model}` 占位符。
- `BENCHMARK_MAX_WORKERS`：最多并发 HTTP 请求数，默认 128。

回放会合并配置的数据源中的快照，按所选 UTC 时间段精确筛选，再按 `(request_id, startTime)` 去重；同一键重复时按文件路径排序保留首次出现的记录。没有 `request_id` 的行不参与去重。回放按相对首条请求的 `startTime` 偏移提交并发请求，不等待前一请求完成；默认 `speed: 1.0`，按原始相对间隔发送。运行状态暂存在服务内存；服务重启后任务历史会丢失。服务未配置独立用户认证，应保持本机回环访问，不要直接暴露到不可信网络。
