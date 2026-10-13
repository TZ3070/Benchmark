# LiteLLM Benchmark Replay API

按 LiteLLM SpendLogs JSONL 中 `startTime` 的相对间隔，将历史请求开放环回放到目标 LiteLLM 模型。服务提供 HTTP API，不包含前端页面；响应、延迟、token 和错误等结果由 LiteLLM 记录。

## 启动方式

以下两种方式都要求：

- Python 3.10 或更高版本并安装 `requirements.txt`，或 Docker Engine + Docker Compose 插件（二选一）。
- 一个可从 Benchmark 服务访问的 LiteLLM Proxy，以及对应的 Master Key。
- 可访问的 LiteLLM Postgres（`LiteLLM_SpendLogs` 表），以及可选的 JSONL 周归档；数据库保留的数据优先，归档补齐已清理的数据。
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

   设置 `LITELLM_URL`、`LITELLM_MASTER_KEY`、Postgres 连接参数和 `SPENDLOGS_ARCHIVE_HOST`。默认 Postgres 地址使用宿主机的 `host.docker.internal:32100`；如果服务在其他主机或 Docker 网络中，填写 Benchmark 容器可访问的地址。

3. 在可联网的 arm64 Mac 上构建完整 Linux/ARM64 应用镜像（包含 Postgres 驱动）并打包：

   ```bash
   docker buildx build --platform linux/arm64 --load -t benchmark-replay:offline .
   docker save -o benchmark-replay-linux-arm64.tar benchmark-replay:offline
   ```

   将 tar 包作为 GitHub Release 附件上传（不要提交到 Git 源码历史），或通过文件传输送到服务器。GitHub Release 的下载需要服务器能访问 GitHub；若服务器离线，请用 `scp` 等方式传输。服务器导入：

   ```bash
   docker load -i benchmark-replay-linux-arm64.tar
   ```

4. 启动已导入的 Benchmark 镜像：

   ```bash
   docker compose --env-file .env -f compose.server.yaml up -d
   ```

   `compose.server.yaml` 使用本地 `benchmark-replay:offline` 标签，不会尝试下载应用镜像。

   查看日志：

   ```bash
   docker compose --env-file .env -f compose.server.yaml logs -f benchmark
   ```

API 默认仅绑定服务器 `127.0.0.1:8088`。远程访问可使用 SSH 隧道或受保护的反向代理。

### Python 直接启动

适用于已有 Python 3.10 或更高版本的环境。正常联网安装时，可以直接使用 pip 安装依赖。服务器无法联网时，可下载仓库中的 `psycopg-linux-aarch64-cp310.tar.gz` 离线 wheel 包；此包适用于 Linux ARM64、CPython 3.10，无需创建虚拟环境。

```bash
python3 -m pip install -r requirements.txt
export LITELLM_URL=http://你的LiteLLM地址:4000
export LITELLM_MASTER_KEY=你的LiteLLM密钥
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=32100
export POSTGRES_DB=litellm
export POSTGRES_USER=litellm
export POSTGRES_PASSWORD=你的Postgres密码
export SPENDLOGS_JSONL=./data/spendlogs
export PROMETHEUS_URL=http://你的Prometheus地址:9090  # 可选，仅供 /api/series
export BENCHMARK_HOST=127.0.0.1  # 默认仅本机访问；远程访问请使用 SSH 隧道/受保护代理
export BENCHMARK_PORT=8088
python3 app.py
```

#### Linux ARM64 / Python 3.10 离线安装（系统 Python，无虚拟环境）

从 GitHub 下载 `psycopg-linux-aarch64-cp310.tar.gz`，或通过其他文件传输方式将压缩包放到服务器。解压后，用系统 Python 安装到当前解释器：

```bash
mkdir -p psycopg-wheels
tar -xzf psycopg-linux-aarch64-cp310.tar.gz -C psycopg-wheels
python3 -m pip install --no-index --find-links=./psycopg-wheels --user 'psycopg[binary]==3.2.13'
python3 -c 'import psycopg; print(psycopg.__version__)'
```

若服务器的 pip 禁止对系统 Python 执行安装（externally-managed-environment），由管理员按服务器发行版的方式安装这些本地 wheel，或在确认接受系统级 pip 安装后使用该发行版支持的 override 选项。不要用 `sudo pip` 覆盖系统管理的 Python 包。安装后，用同一个 `python3` 执行 `app.py`。该 wheel 包只适用于 Linux ARM64、CPython 3.10；其他架构或 Python 版本须准备对应 wheel。

`SPENDLOGS_JSONL` 可指向单个 `.jsonl` 文件或递归包含多个 `.jsonl` 文件的目录，用于补齐 Postgres 中已清理的记录。默认值为项目目录下的 `LiteLLM_SpendLogs.jsonl`。

## 环境变量

| 变量 | 用途 | 默认值 |
|---|---|---|
| `LITELLM_URL` | LiteLLM Proxy 地址 | `http://127.0.0.1:4000` |
| `LITELLM_MASTER_KEY` | 获取目标模型列表并发送回放请求 | 无；必填 |
| `POSTGRES_HOST` | LiteLLM Postgres 主机名/IP | `127.0.0.1` |
| `POSTGRES_PORT` | LiteLLM Postgres 端口 | `32100` |
| `POSTGRES_DB` | 数据库名 | `litellm` |
| `POSTGRES_USER` | 数据库用户 | `litellm` |
| `POSTGRES_PASSWORD` | 数据库密码 | 无 |
| `SPENDLOGS_JSONL` | JSONL 文件或归档目录 | `./LiteLLM_SpendLogs.jsonl` |
| `SPENDLOGS_MANIFEST` | 可选的 JSONL 文件清单路径 | 无 |
| `PROMETHEUS_URL` | `/api/series` 查询地址 | `http://127.0.0.1:9090` |
| `BENCHMARK_PORT` | HTTP API 监听端口 | `8088` |
| `BENCHMARK_HOST` | HTTP API 监听地址 | `127.0.0.1` |
| `BENCHMARK_MAX_WORKERS` | 最大并发请求数 | `128` |
| `BENCHMARK_RESPONSE_READ_TIMEOUT` | 请求响应读取超时秒数；流式响应持续读取至结束 | `600` |
| `LITELLM_PROMETHEUS_METRIC` | Prometheus 指标名称 | `litellm_requests_metric` |
| `LITELLM_PROMETHEUS_QUERY_TEMPLATE` | 可选 PromQL 模板，支持 `{metric}`、`{model}` | `sum(rate({metric}{model="{model}"}[1m]))` |

Docker Compose 用 `.env` 中的 `SPENDLOGS_ARCHIVE_HOST` 将服务器目录只读挂载到容器 `/data/spendlogs`，并自动设置容器内 `SPENDLOGS_JSONL`。也可用 `BENCHMARK_HOST_PORT` 修改宿主机绑定的本地端口。

如需从现有 LiteLLM Postgres 导出 JSONL 快照，使用独立目录 [`spendlogs-export/`](spendlogs-export/README.md) 中的脚本。导出脚本不属于 API 启动依赖。

## HTTP API

- `GET /api/models`：返回 JSONL 中的源模型和 LiteLLM 当前可用的目标模型。
- `GET /api/preview?model=...&start=...&end=...`：返回选区记录数、可回放数、缺少请求内容数，以及可选的 `warnings`。时间范围为 `[start,end)`，时间必须带时区。
- `POST /api/replays`：按前端契约提交 `model_id`、Unix 秒 `start_time`、`end_time`，并发送 `Idempotency-Key`。异步返回 `replay_id`。可选 `speed` 默认 `1.0`。
- `GET /api/replays/{replay_id}`：返回状态、请求总数、已发送数、已完成数、`skipped_count`、`warnings` 和逐请求 `results`，时间字段为 Unix 秒。每条结果包含 `request_id`、`elapsed_seconds`、`input_tokens`、`output_tokens`、`status_code`、`output`，失败时带英文 `error`；上游 HTTP 错误会附带经过脱敏且最多 3,000 字符的响应体摘要。
- 同一时刻只允许一个活动回放任务；已有任务进行中时，新建请求返回 `409`。
- 后端当前忽略 `Idempotency-Key`，不保证同键重试复用原任务；重试前端应先查询/恢复原任务，避免重复投放。
- 带 `stream: true` 的历史请求会以流式方式发送。服务持续读取响应至 EOF，提取输出文本与 usage，再更新 `completed_count`。
- 旧版 `/api/runs` 创建/状态路径仍作为兼容别名保留。
- `GET /api/series?model=...&start=...&end=...`：读取 Prometheus 时间序列。

`sent_count` 表示已提交给 HTTP 客户端的请求数；`completed_count` 在完整读取响应、流结束或请求确定失败后增加。非流式和流式响应的文本输出会保存在每条结果的 `output` 中，token 数只在上游返回 usage 时填写。无法提取请求体或不支持 endpoint 的记录会跳过；全部跳过时任务失败。Postgres 查询出错时服务记录包含原因的 warning，脱敏后通过 `warnings` 返回，并改用 JSONL；数据库凭证等秘密不会写入 warning。服务重启后任务状态会丢失。服务没有独立用户认证，应保持本机回环访问或放在受保护的反向代理之后。

## JSONL 回放规则

每行一个 JSON 对象。至少需要 `model`、`startTime`（或 Unix 秒/ISO-8601 格式的 `timestamp`），以及可还原为请求体的字段。例如：

```json
{"request_id":"req-1","model":"source-model","startTime":"2026-10-09T08:23:21.665+00:00","request_body":{"messages":[{"role":"user","content":"hello"}],"temperature":0.2}}
```

可从 `proxy_server_request`、`request_body`、`request`、`body`、`messages` 或文本 `prompt` 读取请求内容。无时区的 LiteLLM `startTime` 按 UTC 解释。回放保留已保存的模型参数（如 `temperature`、`top_p`、`max_tokens`），只替换目标模型；历史凭证、内部字段和 metadata 不会转发。`call_type=anthropic_messages` 使用 `/v1/messages`，`acompletion` 等 Chat Completions 类型使用 `/v1/chat/completions`。

回放会根据所选模型与 `[start,end)` 时间范围筛选记录，合并数据库和归档后按 `request_id` 单键去重，并按原始相对间隔发出请求。Anthropic Messages endpoint 优先根据请求体顶层的 `system`、`thinking`、`context_management` 字段判断，再参考 `call_type`。每个服务实例同时只允许一个活动回放任务。
