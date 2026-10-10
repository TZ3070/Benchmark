# SpendLogs JSONL 导出

此目录用于导出脚本，与 Benchmark API 启动配置分开。脚本从正在运行的 LiteLLM Docker Compose 项目里的 Postgres 服务导出整张 `LiteLLM_SpendLogs` 表，按 UTC ISO 周命名，例如 `2026-W41.jsonl`，并生成校验用的 `.manifest.json`。

每周文件是**完整表快照**，不是只包含该周新增的请求。回放服务会合并这些快照、按所选时间过滤，并根据 `(request_id, startTime)` 去重。

## 使用方法

在 Benchmark 项目根目录执行，并把 `LITELLM_COMPOSE_DIR` 指向服务器上现有 LiteLLM Compose 项目目录：

```bash
LITELLM_COMPOSE_DIR=/srv/litellm \
LITELLM_COMPOSE_FILE=/srv/litellm/docker-compose.yml \
LITELLM_POSTGRES_SERVICE=postgres \
bash spendlogs-export/export_spendlogs_weekly.sh
```

`LITELLM_COMPOSE_FILE`、`LITELLM_POSTGRES_SERVICE` 可按部署调整；默认 Compose 文件是指定目录下的 `compose.yaml`，默认 Postgres 服务名为 `postgres`。导出默认写入 `data/spendlogs/`，可用 `SPENDLOGS_OUTPUT_DIR` 指定其他目录。容器中默认使用 `POSTGRES_USER`、`POSTGRES_DB` 环境变量，未设置时采用 `litellm`。

脚本依赖 Docker Compose、容器内的 `psql`，以及本机的 `sha256sum` 或 `shasum`。输出权限限制为当前用户可读写。SpendLogs 可能包含 prompt 等敏感内容；请仅将文件保存在受控服务器目录。`.jsonl` 和 manifest 文件被 Git 忽略，不会随代码提交。
