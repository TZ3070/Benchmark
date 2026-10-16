# LiteLLM Benchmark Replay API

按 LiteLLM SpendLogs JSONL 中 `startTime` 的相对间隔，将历史请求开放环回放到目标 LiteLLM 模型。服务提供 HTTP API，不包含前端页面；响应、延迟、token 和错误等结果由 LiteLLM 记录。

## 后端结构

- `app/benchmark/app.py`：服务启动入口，只负责绑定 `BENCHMARK_HOST` / `BENCHMARK_PORT` 并启动 HTTP server。
- `app/benchmark/api.py`：HTTP 路由、Bearer 认证、请求校验及响应序列化；这里是前端 `/api/*` 请求的直接入口。
- `app/benchmark/core.py`：SpendLogs 查询与 JSONL 回退、回放调度、LiteLLM 请求转发、任务持久化等后端业务。
- `app/requirements.txt`：Python 依赖清单，不包含 wheel 包。
- 项目根目录的 `.env` 保存私密配置；`SPENDLOGS_JSONL` 指向 JSONL 文件或归档目录，可位于项目目录之外或挂载自其他服务器；`data/benchmark-state/` 保存本机任务状态库。
- `spendlogs-export/` 保存独立的 SpendLogs 导出脚本。
- 前端由独立的 TrafficReplay 项目维护；本仓库不包含前端代码。Prometheus 看板接口暂不属于这次回放 API 结构调整范围。

Vite 开发代理把浏览器的 `/api/*` 原样转发至本服务。`/api/models` 与 `/api/time-range` 为当前前端初始化所需的只读接口，暂不校验 API Key；创建、查看及取消回放任务等接口要求 `Authorization: Bearer <BENCHMARK_API_KEY>`。

## 启动方式

以下两种方式都要求：

- Python 3.10 或更高版本并安装 `app/requirements.txt`，或 Docker Engine + Docker Compose 插件（二选一）。
- 一个可从 Benchmark 服务访问的 LiteLLM Proxy，以及对应的 Master Key。
- 可访问的 LiteLLM Postgres（`LiteLLM_SpendLogs` 表），以及可选的 JSONL 周归档；数据库保留的数据优先，归档补齐已清理的数据。

### Docker Compose

适用于服务器上 LiteLLM、Postgres、Redis 和 Prometheus 已经运行的情况。此 Compose **只启动 Benchmark API**，不会启动或改动其他服务。

1. 将项目放到服务器。创建归档目录并将完整 SpendLogs 快照放入其中：

   ```bash
   mkdir -p data/benchmark-state
   ```

2. 复制环境变量模板并编辑：

   ```bash
   cp .env.example .env
   ```

   设置 `LITELLM_URL`、`LITELLM_MASTER_KEY`、Postgres 连接参数、`SPENDLOGS_ARCHIVE_HOST` 和 `BENCHMARK_STATE_HOST`。`SPENDLOGS_ARCHIVE_HOST` 应指向存放 JSONL 的实际目录；模板中的 Python 直连地址是 `127.0.0.1`；Docker Compose 下把 LiteLLM 和 Postgres 地址改成容器可访问的宿主机地址（通常为 `host.docker.internal`）。

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

适用于已有 Python 3.10 或更高版本的环境。服务器目录按 `/opt/benchmark/{app,data,.env}` 布置。`app/run.sh` 会读取项目根目录的 `.env` 并用系统 `python3` 启动服务，不需要 Docker 或虚拟环境：

```bash
cd /opt/benchmark/app
chmod +x run.sh
./run.sh
```

也可以手动加载环境变量后直接运行 Python 模块：

```bash
cd /opt/benchmark
set -a
. ./.env
set +a
PYTHONPATH=app python3 -m benchmark.app
```

`.env` 使用 shell 兼容的 `KEY=value` 格式。后端不设运行配置默认值，表格中标为“必填”的变量必须写入 `.env`。可留空的变量仍需显式设为空值。脚本默认读取项目根目录的 `.env`；也可以通过 `BENCHMARK_ENV_FILE=/path/to/env ./run.sh` 指定其他文件。不要把含密钥的 `.env` 上传到 GitHub。

`app/requirements.txt` 只是依赖清单，不含打包 wheel。需要安装依赖时，在项目根目录执行：

```bash
python3 -m pip install -r app/requirements.txt
python3 -c 'import psycopg; print(psycopg.__version__)'
```

服务器无法联网时，请在服务器外按其 Python 版本和 Linux 架构准备并传输 psycopg 安装包，或由管理员预装 psycopg；本仓库不携带 wheel 包。若不安装 psycopg，Postgres 数据源不可用时服务会回退到 JSONL 归档。

`SPENDLOGS_JSONL` 可指向单个 `.jsonl` 文件或递归包含多个 `.jsonl` 文件的目录，用于补齐 Postgres 中已清理的记录。路径通过 `.env` 配置；相对路径按项目根目录解析，也可使用绝对路径或服务器挂载路径。

## 环境变量

| 变量 | 用途 | 要求 |
|---|---|---|
| `LITELLM_URL` | LiteLLM Proxy 地址 | 必填 |
| `LITELLM_MASTER_KEY` | 获取目标模型列表并发送回放请求 | 必填 |
| `BENCHMARK_API_KEY` | 保护 Benchmark API；前端使用 `Authorization: Bearer <密钥>` | 必填，使用独立随机密钥 |
| `POSTGRES_DSN` | 可选的完整连接串；使用独立字段时显式留空 | 必填，可留空 |
| `POSTGRES_HOST`、`POSTGRES_PORT` | LiteLLM Postgres 地址 | 必填 |
| `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD` | Postgres 数据库凭证；密码可为空 | 必填，密码可留空 |
| `SPENDLOGS_JSONL` | JSONL 文件或归档目录 | 必填 |
| `SPENDLOGS_MANIFEST` | 可选的 JSONL 文件清单路径 | 必填，可留空 |
| `BENCHMARK_PORT`、`BENCHMARK_HOST` | HTTP API 监听端口和地址 | 必填 |
| `BENCHMARK_MAX_WORKERS` | 最大并发请求数 | 必填 |
| `BENCHMARK_RESPONSE_READ_TIMEOUT` | 请求响应读取超时秒数；流式响应持续读取至结束 | 必填 |
| `BENCHMARK_STATE_DB` | SQLite 任务及幂等记录文件 | 必填 |
| `BENCHMARK_STATE_HOST` | Docker Compose 持久化目录 | Docker 启动必填 |
| `SPENDLOGS_ARCHIVE_HOST` | Docker Compose 挂载的 JSONL 归档目录 | Docker 启动必填 |

Docker Compose 用 `.env` 中的 `SPENDLOGS_ARCHIVE_HOST` 将服务器目录只读挂载到容器 `/data/spendlogs`，并将 `BENCHMARK_STATE_HOST` 持久挂载到 `/data/state` 保存任务与幂等记录。也可用 `BENCHMARK_HOST_PORT` 修改宿主机绑定的本地端口。

如需从现有 LiteLLM Postgres 导出 JSONL 快照，使用独立目录 [`spendlogs-export/`](spendlogs-export/README.md) 中的脚本。导出脚本不属于 API 启动依赖。

## 日志与失败定位

服务日志输出到标准错误流，前台运行时会显示在启动终端；systemd 可从 journal 查看，Docker 可用 `docker compose logs -f benchmark` 查看。Python 直接运行时可保存终端输出：

```bash
bash app/run.sh 2>&1 | tee -a benchmark.log
```

日志按事件记录服务启动、回放创建/准备阶段、每条失败请求的 request_id、目标模型、endpoint、HTTP 状态、耗时和脱敏后的异常详情；成功请求不逐条写日志，任务结束时汇总成功数、失败数、已发送数、已完成数和总耗时。不会记录 prompt、请求体或认证密钥。可用 `run_id` 关联同一任务的日志；前端创建失败但尚未收到 replay_id 时，先按时间查找 `event=replay_create_rejected`。逐请求失败查找 `event=replay_request_failed`，结合 `status_code`、`elapsed_seconds`、`error_kind` 和 `error_detail` 定位。

## HTTP API

- `GET /api/models`：返回 JSONL 中的源模型和 LiteLLM 当前可用的目标模型。
- `GET /api/time-range`：返回 SpendLogs 的 `min`、`max`、`count` 和数据来源；Postgres 不可用时从 JSONL 计算。
- `GET /api/replays/active`：返回当前活动任务；有任务时为 `{"active":true,"task":{...}}`，无任务时为 `{"active":false,"task":null}`。
- `GET /api/preview?model=...&start=...&end=...`：返回选区记录数、可回放数、缺少请求内容数，以及可选的 `warnings`。时间范围为 `[start,end)`，时间必须带时区。
- `POST /api/replays`：按前端契约提交 `model_id`、Unix 秒 `start_time`、`end_time`，并发送必需的 `Idempotency-Key`。新任务异步返回 `202` 与 `replay_id`；重复请求返回原任务。
- `GET /api/replays/{replay_id}`：返回状态、回放方式 `dispatch_mode`、速度 `speed`、请求总数、已发送数、已完成数、`skipped_count`、`warnings` 和逐请求 `results`，时间字段为 Unix 秒。每条结果包含 `request_id`、`elapsed_seconds`、`input_tokens`、`output_tokens`、`status_code`、`output`，失败时带英文 `error`；上游 HTTP 错误会附带经过脱敏且最多 3,000 字符的响应体摘要。
- `POST /api/replays/{replay_id}/cancel`：请求取消任务。接受时返回 `202` 与当前任务对象，状态为 `cancelling`；重复请求仍返回 `202`。任务已结束时返回 `409` 和最终 `task`，任务 ID 不存在时返回 `404`。
- 创建响应和状态响应都包含 `progress`：`ready` 表示总数已经计算完成；`total` 是可回放请求总数；`sent`、`completed`、`skipped` 分别是已发出、已完成和跳过数量；`sent_percent` 和 `percent` 分别表示发送与完成百分比。准备阶段还会返回 `selected_count`（当前筛选出的 SpendLogs 行数）。总数尚未计算完成时，`progress.total` 和百分比为 `null`，前端可继续轮询状态接口。
- 同一时刻只允许一个活动回放任务；已有任务进行中时，新建请求返回 `409`。
- 活动任务导致创建返回 `409` 时，响应同时包含 `active_task`，其结构与状态查询响应相同，便于调用方恢复当前任务展示。
- 相同 `Idempotency-Key` 和请求内容始终复用原任务，包括任务结束或服务重启后；同键不同请求内容返回 `409`。不同键启动时若已有活动任务，也返回 `409`。
- 带 `stream: true` 的历史请求会以流式方式发送。服务持续读取响应至 EOF，提取输出文本与 usage，再更新 `completed_count`。

### 前端接入待办（由前端项目实现）

以下仅定义前端接入要求；本仓库不包含前端代码。

1. 页面加载时带 `Authorization: Bearer <BENCHMARK_API_KEY>` 请求 `GET /api/replays/active`。若 `active` 为 `true`，用 `task.replay_id` 恢复当前任务并继续轮询 `GET /api/replays/{id}`。
2. 在创建回放前提供回放方式选择。原有数值倍数仍按历史相对时间间隔回放，`speed` 接受数值或 `*N` 字符串（例如 `5` 或 `"*5"`）；新增突发模式发送 `"speed":"burst"` 或 `"dispatch_mode":"burst"`。突发模式忽略历史请求间隔，按 `BENCHMARK_MAX_WORKERS` 限制同时在途请求数，并在请求完成后继续投放后续请求。继续发送 `Idempotency-Key`，并将回放方式及速度纳入创建请求签名；若收到 `409` 且响应含 `active_task`，接管该任务并展示状态，不要再次创建任务。
3. 轮询状态时读取 `progress.ready`、`progress.total`、`progress.sent`、`progress.completed`、`progress.sent_percent` 和 `progress.percent`。`ready=false` 时显示正在准备；就绪后显示总请求数和发送/完成进度。也可展示 `selected_count` 与 `skipped_count`。在第一条请求发出前，后端已完成筛选和请求准备，`request_count` 与 `progress.total` 会返回可回放总数；若图表尚未有指标数据，前端应在图表占位区显示该总数，而不是只显示“等待实际打流开始时间”。
4. 活动任务显示“取消”按钮，仅在 `cancellable=true` 时启用。点击后带相同认证头调用 `POST /api/replays/{id}/cancel`，接受 `202` 后继续轮询，直到 `cancelled` 等终态；不要在 `cancelling` 时立即清除任务。
5. 取消会阻止后续尚未按计划提交的请求。已提交给请求工作线程的请求无法撤回，仍可能完成；因此 `cancelling` 到 `cancelled` 之间可能有延迟，期间计数仍会变化。

`sent_count` 表示已提交给 HTTP 客户端的请求数；`completed_count` 在完整读取响应、流结束或请求确定失败后增加。非流式和流式响应的文本输出会保存在每条结果的 `output` 中，token 数只在上游返回 usage 时填写。无法提取请求体或不支持 endpoint 的记录会跳过；全部跳过时任务失败。Postgres 查询出错时服务记录包含原因的 warning，脱敏后通过 `warnings` 返回，并改用 JSONL；数据库凭证等秘密不会写入 warning。任务和幂等键保存在 `BENCHMARK_STATE_DB` 指定的 SQLite 文件中；服务重启后保留终态任务，重启时仍处于活动状态的任务会标记失败且不会自动续跑。状态文件包含任务结果，需限制其文件访问权限并纳入持久化备份。`/api/replays`、任务状态、取消和预览接口需要 `Authorization: Bearer <BENCHMARK_API_KEY>`；为兼容当前未附认证头的前端只读请求，`/api/models` 和 `/api/time-range` 暂不要求认证。请将密钥作为私密配置，不要放入前端构建变量。API 仍应保持内网可达或放在 HTTPS 反向代理之后。

## JSONL 回放规则

每行一个 JSON 对象。至少需要 `model`、`startTime`（或 Unix 秒/ISO-8601 格式的 `timestamp`），以及可还原为请求体的字段。例如：

```json
{"request_id":"req-1","model":"source-model","startTime":"2026-10-09T08:23:21.665+00:00","request_body":{"messages":[{"role":"user","content":"hello"}],"temperature":0.2}}
```

可从 `proxy_server_request`、`request_body`、`request`、`body`、`messages` 或文本 `prompt` 读取请求内容。无时区的 LiteLLM `startTime` 按 UTC 解释。回放保留已保存的模型参数（如 `temperature`、`top_p`、`max_tokens`），只替换目标模型；历史凭证、内部字段和 metadata 不会转发。`call_type=anthropic_messages` 使用 `/v1/messages`，`acompletion` 等 Chat Completions 类型使用 `/v1/chat/completions`。

回放会根据所选模型与 `[start,end)` 时间范围筛选记录，合并数据库和归档后按 `request_id` 单键去重。定时模式按原始相对间隔发出请求；突发模式忽略时间间隔，在可用工作线程内尽快发送。Anthropic Messages endpoint 优先根据请求体顶层的 `system`、`thinking`、`context_management` 字段判断，再参考 `call_type`。每个服务实例同时只允许一个活动回放任务。
