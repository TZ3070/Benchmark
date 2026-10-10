from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPENDLOGS_JSONL = os.environ.get("SPENDLOGS_JSONL", os.path.join(BASE_DIR, "LiteLLM_SpendLogs.jsonl"))
SPENDLOGS_MANIFEST = os.environ.get("SPENDLOGS_MANIFEST", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
LITELLM_API_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
PORT = int(os.environ.get("BENCHMARK_PORT", "8088"))
MAX_WORKERS = max(1, int(os.environ.get("BENCHMARK_MAX_WORKERS", "128")))

jobs: dict[str, dict] = {}
cancel_events: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()
active_runs = False


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("时间格式无效，请使用带时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.astimezone(timezone.utc)


def record_time(record: dict) -> datetime:
    value = record.get("startTime", record.get("timestamp"))
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ValueError("缺少 startTime/timestamp")
    # Normalize arbitrary LiteLLM fractional-second precision for Python 3.9.
    fraction = re.search(r"\.(\d+)(?=(?:Z|[+-]\d{2}:\d{2})?$)", value)
    if fraction:
        digits = fraction.group(1)
        if len(digits) != 6:
            normalized = (digits + "000000")[:6]
            value = value[:fraction.start(1)] + normalized + value[fraction.end(1):]
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("JSONL startTime/timestamp 格式无效") from exc
    # LiteLLM's UTC SpendLogs timestamps are timezone-naive in the supplied export.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_body(record: dict) -> dict | None:
    """Read the archived request body, including LiteLLM proxy_server_request."""
    metadata = record.get("metadata")
    body = next(
        (record.get(key) for key in ("request_body", "request", "proxy_server_request", "body") if record.get(key) is not None),
        None,
    )
    if body is None and isinstance(metadata, dict):
        body = metadata.get("proxy_server_request") or metadata.get("request_body") or metadata.get("request")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return None
    if isinstance(body, dict):
        body = dict(body)
    elif body is None and isinstance(record.get("messages"), list):
        body = {"messages": record["messages"]}
    elif body is None and isinstance(record.get("prompt"), str):
        body = {"messages": [{"role": "user", "content": record["prompt"]}]}
    else:
        return None
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        return None
    if body.get("stream") is True:
        return None
    # SpendLogs proxy_server_request can contain LiteLLM-enriched metadata with
    # credentials, old routing state, and IDs. Replay API/model parameters only.
    body = {
        key: value for key, value in body.items()
        if not key.lower().startswith("litellm_")
        and key.lower() not in {"drop_params", "api_key", "authorization", "headers", "metadata"}
    }
    return body


def endpoint_for_record(record: dict) -> str:
    call_type = str(record.get("call_type", ""))
    if call_type == "anthropic_messages":
        return "/v1/messages"
    if call_type in {"acompletion", "completion", "chat_completion"}:
        return "/v1/chat/completions"
    raise RuntimeError(f"不支持回放 call_type={call_type or '(empty)'}")


def _expand_jsonl_source(path: str) -> list[str]:
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        found = []
        for root, dirs, filenames in os.walk(path):
            dirs.sort()
            found.extend(os.path.join(root, name) for name in sorted(filenames) if name.lower().endswith(".jsonl"))
        return found
    if os.path.isfile(path) and path.lower().endswith(".jsonl"):
        return [path]
    raise RuntimeError(f"JSONL 文件或归档目录不存在: {path}")


def jsonl_files() -> list[str]:
    files = []
    if SPENDLOGS_JSONL and os.path.exists(SPENDLOGS_JSONL):
        files.extend(_expand_jsonl_source(SPENDLOGS_JSONL))
    elif not SPENDLOGS_MANIFEST:
        raise RuntimeError(f"JSONL 文件或归档目录不存在: {SPENDLOGS_JSONL}")
    if SPENDLOGS_MANIFEST:
        manifest = os.path.abspath(os.path.expanduser(SPENDLOGS_MANIFEST))
        if not os.path.isfile(manifest):
            raise RuntimeError(f"JSONL 文件清单不存在: {manifest}")
        with open(manifest, "r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                entry_path = entry if os.path.isabs(entry) else os.path.join(os.path.dirname(manifest), entry)
                try:
                    files.extend(_expand_jsonl_source(entry_path))
                except RuntimeError as exc:
                    raise RuntimeError(f"清单第 {line_number} 行: {exc}") from exc
    files = sorted({os.path.realpath(path) for path in files})
    if not files:
        raise RuntimeError("数据源中没有找到 .jsonl 文件")
    return files


def iter_records(start: datetime | None = None, end: datetime | None = None):
    seen = set()
    for path in jsonl_files():
        with open(path, "r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{path} 第 {line_number} 行格式无效: {exc.msg}") from exc
                if not isinstance(record, dict):
                    raise RuntimeError(f"{path} 第 {line_number} 行必须是对象")
                timestamp = record_time(record)
                if start is not None and end is not None and not start <= timestamp < end:
                    continue
                request_id = record.get("request_id")
                if request_id is not None and str(request_id):
                    dedupe_key = (str(request_id), timestamp)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                record["_timestamp"] = timestamp
                record["_line_number"] = line_number
                record["_source_file"] = path
                yield record


def select_records(model: str, start: datetime, end: datetime) -> list[dict]:
    selected = []
    for record in iter_records(start, end):
        if str(record.get("model", "")) != model:
            continue
        selected.append(record)
    selected.sort(key=lambda row: (row["_timestamp"], str(row.get("request_id", "")), row["_source_file"]))
    return selected


def query_models() -> list[str]:
    models = {str(record["model"]) for record in iter_records() if record.get("model") is not None}
    return sorted(models)


def query_target_models() -> list[str]:
    if not LITELLM_API_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY 未配置，无法获取 LiteLLM 可用模型")
    req = Request(f"{LITELLM_URL}/v1/models", headers={"Authorization": f"Bearer {LITELLM_API_KEY}"})
    try:
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        # Some upstream proxies include non-Latin-1 characters in HTTP
        # headers, which can make urllib fail while constructing HTTPError.
        # Preserve a useful diagnostic without exposing headers or secrets.
        raise RuntimeError(f"LiteLLM /v1/models 返回 HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"无法连接 LiteLLM ({LITELLM_URL}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("LiteLLM /v1/models 返回的不是有效 JSON") from exc
    return sorted({str(item["id"]) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")})


def prom_query_range(model: str, start: datetime, end: datetime, step: str = "15s") -> dict:
    metric = os.environ.get("LITELLM_PROMETHEUS_METRIC", "litellm_requests_metric")
    if not metric.replace("_", "").isalnum():
        raise ValueError("LITELLM_PROMETHEUS_METRIC must be a Prometheus metric identifier")
    escaped = model.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    template = os.environ.get("LITELLM_PROMETHEUS_QUERY_TEMPLATE") or 'sum(rate({metric}{{model="{model}"}}[1m]))'
    query = template.format(metric=metric, model=escaped)
    params = "query=" + quote(query) + "&start=" + quote(str(start.timestamp())) + "&end=" + quote(str(end.timestamp())) + "&step=" + quote(step)
    req = Request(f"{PROMETHEUS_URL}/api/v1/query_range?{params}")
    with urlopen(req, timeout=10) as response:
        result = json.loads(response.read())
    if result.get("status") != "success":
        raise RuntimeError("Prometheus range query failed")
    return result["data"]


def update_job(run_id: str, **values):
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job.update(values)


def increment_sent(run_id: str):
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job["sent_count"] += 1


def run_replay(run_id: str, source_model: str, target_model: str, start: datetime, end: datetime, speed: float):
    global active_runs
    try:
        records = select_records(source_model, start, end)
        if not records:
            raise RuntimeError("选定时间段内没有可回放请求")
        prepared = []
        missing = []
        for record in records:
            body = extract_body(record)
            if body is None:
                missing.append(record.get("request_id") or f"{record['_source_file']} 第 {record['_line_number']} 行")
                continue
            try:
                endpoint = endpoint_for_record(record)
            except RuntimeError as exc:
                missing.append(f"{record.get('request_id')}: {exc}")
                continue
            prepared.append((record, body, endpoint))
        if missing:
            preview = "、".join(map(str, missing[:3]))
            more = f" 等 {len(missing)} 条" if len(missing) > 3 else ""
            raise RuntimeError(
                f"选中 {len(records)} 条记录，其中 {len(missing)} 条缺少可回放的 request/messages 内容（例如 {preview}{more}）。"
                "SpendLogs 元数据行不能还原原始 prompt；请确保 JSONL 保存 proxy_server_request 或完整 request_body。"
            )
        if not LITELLM_API_KEY:
            raise RuntimeError("LITELLM_MASTER_KEY 未配置，无法调用目标 LiteLLM")

        cancel_event = cancel_events[run_id]
        t0 = time.monotonic()
        wall_start = datetime.now(timezone.utc)
        first = prepared[0][0]["_timestamp"]
        update_job(run_id, status="running", total=len(prepared), started_at=wall_start.isoformat())

        def send_one(record: dict, body: dict, endpoint: str, planned: datetime):
            # sent_count represents requests handed to the HTTP client, not replies.
            request_body = dict(body)
            request_body["model"] = target_model
            request = Request(
                f"{LITELLM_URL}{endpoint}",
                data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {LITELLM_API_KEY}",
                    "Content-Type": "application/json",
                    **({"anthropic-version": "2023-06-01"} if endpoint == "/v1/messages" else {}),
                },
                method="POST",
            )
            increment_sent(run_id)
            try:
                with urlopen(request, timeout=120) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"LiteLLM 返回 HTTP {response.status}")
            except HTTPError as exc:
                raise RuntimeError(f"LiteLLM 返回 HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc

        executor = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(prepared)))
        futures = []
        try:
            for record, body, endpoint in prepared:
                if cancel_event.is_set():
                    break
                offset = (record["_timestamp"] - first).total_seconds() / speed
                delay = offset - (time.monotonic() - t0)
                if delay > 0 and cancel_event.wait(delay):
                    break
                scheduled = wall_start.timestamp() + offset
                planned = datetime.fromtimestamp(scheduled, timezone.utc)
                futures.append(executor.submit(send_one, record, body, endpoint, planned))
                update_job(run_id, last_scheduled_at=planned.isoformat())
            update_job(run_id, dispatch_finished=True)
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(str(exc))
                    update_job(run_id, error_count=len(errors), last_error=str(exc)[:500])
            if cancel_event.is_set():
                update_job(run_id, status="cancelled", finished_at=datetime.now(timezone.utc).isoformat())
            elif errors:
                update_job(run_id, status="failed", error=f"{len(errors)} 个请求发送失败", finished_at=datetime.now(timezone.utc).isoformat())
            else:
                update_job(run_id, status="completed", finished_at=datetime.now(timezone.utc).isoformat())
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    except Exception as exc:
        update_job(run_id, status="failed", error=str(exc), finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        with jobs_lock:
            active_runs = False


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        # Keep the HTTP reason phrase ASCII/Latin-1 safe. Error details belong
        # in the UTF-8 JSON body, not in the status line.
        self.send_response(status, "OK" if status < 400 else "Error")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/models":
                self.send_json(200, {"source_models": query_models(), "target_models": query_target_models()})
            elif parsed.path == "/api/preview":
                model = params.get("model", [""])[0]
                start, end = parse_time(params.get("start", [""])[0]), parse_time(params.get("end", [""])[0])
                if not model or start >= end:
                    raise ValueError("模型和有效时间范围为必填项")
                rows = select_records(model, start, end)
                replayable = sum(extract_body(row) is not None for row in rows)
                self.send_json(200, {"count": len(rows), "replayable_count": replayable, "missing_payload_count": len(rows) - replayable})
            elif parsed.path == "/api/series":
                model = params.get("model", [""])[0]
                start, end = parse_time(params.get("start", [""])[0]), parse_time(params.get("end", [""])[0])
                self.send_json(200, prom_query_range(model, start, end))
            elif parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.rsplit("/", 1)[-1]
                with jobs_lock:
                    job = jobs.get(run_id)
                self.send_json(200, job) if job is not None else self.send_json(404, {"error": "run not found"})
            else:
                self.path = "/index.html"
                return super().do_GET()
        except (ValueError, RuntimeError, URLError, OSError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def do_POST(self):
        global active_runs
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/runs/") and path.endswith("/cancel"):
                run_id = path.split("/")[-2]
                with jobs_lock:
                    job = jobs.get(run_id)
                    if job is None:
                        return self.send_json(404, {"error": "run not found"})
                    if job["status"] not in ("queued", "running"):
                        return self.send_json(409, {"error": f"run is {job['status']}"})
                    job["status"] = "cancelling"
                    cancel_events[run_id].set()
                return self.send_json(202, {"run_id": run_id, "status": "cancelling"})
            if path != "/api/runs":
                return self.send_json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("请求体不能为空且不得超过 1 MB")
            data = json.loads(self.rfile.read(length))
            source_model, target_model = str(data["source_model"]), str(data["target_model"])
            start, end = parse_time(data["start"]), parse_time(data["end"])
            try:
                speed = float(data.get("speed", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("speed 必须是大于 0 的数字") from exc
            if not (speed > 0 and speed < float("inf")):
                raise ValueError("speed 必须是大于 0 的有限数字")
            if not source_model or not target_model or start >= end:
                raise ValueError("请提供有效模型和时间范围")
            records = select_records(source_model, start, end)
            if not records:
                raise ValueError("选定时间段内没有可回放请求")
            if any(extract_body(row) is None for row in records):
                raise ValueError("所选 JSONL 记录缺少可回放的 request/proxy_server_request 内容，或属于流式请求")
            if target_model not in query_target_models():
                raise ValueError("目标模型当前未在 LiteLLM 中配置或不可用")
            with jobs_lock:
                if active_runs:
                    raise ValueError("已有回放任务处于活动状态，请等待任务完成或失败后再启动")
                active_runs = True
                run_id = str(uuid.uuid4())
                cancel_events[run_id] = threading.Event()
                jobs[run_id] = {
                    "run_id": run_id, "status": "queued", "source_model": source_model,
                    "target_model": target_model, "start": start.isoformat(), "end": end.isoformat(),
                    "speed": speed, "total": len(records), "sent_count": 0, "error_count": 0,
                }
            threading.Thread(target=run_replay, args=(run_id, source_model, target_model, start, end, speed), daemon=True).start()
            self.send_json(202, {"run_id": run_id, "status": "queued", "total": len(records), "speed": speed})
        except (KeyError, ValueError, RuntimeError, json.JSONDecodeError, URLError, OSError) as exc:
            self.send_json(400, {"error": str(exc)})


if __name__ == "__main__":
    Handler.directory = os.path.join(BASE_DIR, "web")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
