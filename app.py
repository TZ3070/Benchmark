from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


# LiteLLM SpendLogs rows contain provider-specific/dynamic fields, so keep the
# row open-ended while naming the shapes passed between replay functions.
SpendLogRecord = dict[str, Any]
RequestBody = dict[str, Any]
ReplayRequest = tuple[SpendLogRecord, RequestBody, str]
Job = dict[str, Any]


# Runtime configuration: point SPENDLOGS_JSONL at a JSONL file or archive
# directory; LiteLLM is required for target model discovery and request replay.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPENDLOGS_JSONL = os.environ.get("SPENDLOGS_JSONL", os.path.join(BASE_DIR, "LiteLLM_SpendLogs.jsonl"))
SPENDLOGS_MANIFEST = os.environ.get("SPENDLOGS_MANIFEST", "")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "32100"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "litellm")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "litellm")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", os.environ.get("PGPASSWORD", ""))
POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").rstrip("/")
LITELLM_API_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
PORT = int(os.environ.get("BENCHMARK_PORT", "8088"))
HOST = os.environ.get("BENCHMARK_HOST", "127.0.0.1")
MAX_WORKERS = max(1, int(os.environ.get("BENCHMARK_MAX_WORKERS", "128")))
RESPONSE_READ_TIMEOUT = max(1.0, float(os.environ.get("BENCHMARK_RESPONSE_READ_TIMEOUT", "600")))
logger = logging.getLogger(__name__)

jobs: dict[str, Job] = {}
cancel_events: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()
active_runs = False


class NoReplayableRequestsError(RuntimeError):
    def __init__(self, skipped_count: int):
        super().__init__("No replayable requests were found in the selected range")
        self.skipped_count = skipped_count


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid time format. Use timezone-aware ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Time must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_unix_time(value: Any, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a Unix timestamp in seconds")
    if not (float("-inf") < float(value) < float("inf")):
        raise ValueError(f"{field} must be a finite Unix timestamp")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{field} is outside the supported time range") from exc


def record_time(record: SpendLogRecord) -> datetime:
    value = record.get("startTime", record.get("timestamp"))
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ValueError("Missing startTime/timestamp")
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
        raise ValueError("Invalid JSONL startTime/timestamp format") from exc
    # LiteLLM's UTC SpendLogs timestamps are timezone-naive in the supplied export.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_body(record: SpendLogRecord) -> RequestBody | None:
    """Return the raw request body stored in a SpendLogs row."""
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
        return dict(body)
    elif body is None and isinstance(record.get("messages"), list):
        return {"messages": record["messages"]}
    elif body is None and isinstance(record.get("prompt"), str):
        return {"messages": [{"role": "user", "content": record["prompt"]}]}
    return None


def extract_body(record: SpendLogRecord) -> RequestBody | None:
    """Return a sanitized, replayable request body."""
    body = _row_body(record)
    if body is None:
        return None
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        return None
    # SpendLogs proxy_server_request can contain LiteLLM-enriched metadata with
    # credentials, old routing state, and IDs. Replay API/model parameters only.
    body = {
        key: value for key, value in body.items()
        if not key.lower().startswith("litellm_")
        and key.lower() not in {"drop_params", "api_key", "authorization", "headers", "metadata"}
    }
    return body


def endpoint_for_record(record: SpendLogRecord) -> str:
    body = _row_body(record) or {}
    anthropic_body_keys = {"system", "thinking", "context_management"}
    if any(key in body for key in anthropic_body_keys):
        return "/v1/messages"
    call_type = str(record.get("call_type", ""))
    if call_type == "anthropic_messages":
        return "/v1/messages"
    if call_type in {"acompletion", "completion", "chat_completion"}:
        return "/v1/chat/completions"
    raise RuntimeError(f"Unsupported call_type for replay: {call_type or '(empty)'}")


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
    raise RuntimeError(f"JSONL file or archive directory does not exist: {path}")


def jsonl_files(required: bool = True) -> list[str]:
    files = []
    if SPENDLOGS_JSONL and os.path.exists(SPENDLOGS_JSONL):
        files.extend(_expand_jsonl_source(SPENDLOGS_JSONL))
    elif not SPENDLOGS_MANIFEST and required:
        raise RuntimeError(f"JSONL file or archive directory does not exist: {SPENDLOGS_JSONL}")
    if SPENDLOGS_MANIFEST:
        manifest = os.path.abspath(os.path.expanduser(SPENDLOGS_MANIFEST))
        if not os.path.isfile(manifest):
            raise RuntimeError(f"JSONL manifest does not exist: {manifest}")
        with open(manifest, "r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                entry_path = entry if os.path.isabs(entry) else os.path.join(os.path.dirname(manifest), entry)
                try:
                    files.extend(_expand_jsonl_source(entry_path))
                except RuntimeError as exc:
                    raise RuntimeError(f"Manifest line {line_number}: {exc}") from exc
    files = sorted({os.path.realpath(path) for path in files})
    if not files and required:
        raise RuntimeError("No .jsonl files were found in the data source")
    return files


def iter_records(
    start: datetime | None = None,
    end: datetime | None = None,
    allow_empty: bool = False,
) -> Iterator[SpendLogRecord]:
    seen_request_ids: set[str] = set()
    for path in jsonl_files(required=not allow_empty):
        with open(path, "r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON at {path} line {line_number}: {exc.msg}") from exc
                if not isinstance(record, dict):
                    raise RuntimeError(f"Expected a JSON object at {path} line {line_number}")
                timestamp = record_time(record)
                if start is not None and end is not None and not start <= timestamp < end:
                    continue
                request_id = record.get("request_id")
                if request_id is not None and str(request_id).strip():
                    dedupe_key = str(request_id).strip()
                    if dedupe_key in seen_request_ids:
                        continue
                    seen_request_ids.add(dedupe_key)
                record["_timestamp"] = timestamp
                record["_line_number"] = line_number
                record["_source_file"] = path
                record["_data_source"] = "jsonl"
                yield record


def postgres_connect() -> Any:
    """Connect to the LiteLLM Postgres from the Python host process."""
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        try:
            import psycopg2 as psycopg  # type: ignore[import-not-found,no-redef]
        except ImportError as exc:
            raise RuntimeError(
                "Reading Postgres requires psycopg (recommended: psycopg[binary]) or psycopg2; "
                "no driver is installed in this Python environment"
            ) from exc

    options = {"connect_timeout": 10}
    if POSTGRES_DSN:
        return psycopg.connect(POSTGRES_DSN, **options)
    options.update({
        "host": POSTGRES_HOST,
        "port": POSTGRES_PORT,
        "dbname": POSTGRES_DB,
        "user": POSTGRES_USER,
        "password": POSTGRES_PASSWORD or None,
    })
    return psycopg.connect(**options)


def postgres_error_detail(exc: Exception) -> str:
    """Return useful database diagnostics while redacting connection secrets."""
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    detail = re.sub(r"(?i)(password\s*=\s*)[^\s]+", r"\1[REDACTED]", detail)
    detail = re.sub(
        r"(?i)(postgres(?:ql)?://[^:/@\s]+:)[^@/\s]+(@)",
        r"\1[REDACTED]\2",
        detail,
    )
    if POSTGRES_PASSWORD:
        detail = detail.replace(POSTGRES_PASSWORD, "[REDACTED]")
    if POSTGRES_DSN:
        detail = detail.replace(POSTGRES_DSN, "[REDACTED_DSN]")
    return detail[:300]


def read_postgres_records(model: str, start: datetime, end: datetime) -> list[SpendLogRecord]:
    """Fetch complete SpendLogs rows in the requested half-open interval."""
    connection = None
    try:
        connection = postgres_connect()
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute(
                'SELECT row_to_json(s)::text '
                'FROM "LiteLLM_SpendLogs" AS s '
                'WHERE s."model" = %s AND s."startTime" >= %s AND s."startTime" < %s '
                'ORDER BY s."startTime", s."request_id"',
                (model, start, end),
            )
            records: list[SpendLogRecord] = []
            for row in cursor:
                record = json.loads(row[0])
                if not isinstance(record, dict):
                    raise RuntimeError('Postgres "LiteLLM_SpendLogs" returned a non-object row')
                timestamp = record_time(record)
                record["_timestamp"] = timestamp
                record["_data_source"] = "postgres"
                records.append(record)
            return records
    except RuntimeError:
        raise
    except Exception as exc:
        # Keep the driver's useful reason (e.g. password authentication failed)
        # while redacting any connection secrets before it reaches logs/API.
        raise RuntimeError(
            f'Failed to read Postgres "LiteLLM_SpendLogs" '
            f"({type(exc).__name__}: {postgres_error_detail(exc)})"
        ) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                logger.warning("Failed to close Postgres connection (%s)", type(exc).__name__)


def record_identity(record: SpendLogRecord) -> str | None:
    request_id = record.get("request_id")
    if request_id is None or not str(request_id).strip():
        return None
    return str(request_id).strip()


def select_records(
    model: str,
    start: datetime,
    end: datetime,
    diagnostics: list[str] | None = None,
) -> list[SpendLogRecord]:
    # Database rows are authoritative. The weekly JSONL exports only fill keys
    # that are no longer present in the live 15-day table.
    merged: dict[str, SpendLogRecord] = {}
    selected_without_id: list[SpendLogRecord] = []
    try:
        postgres_records = read_postgres_records(model, start, end)
    except Exception as exc:
        warning = (
            "Postgres SpendLogs query failed; using JSONL only "
            f"({type(exc).__name__}): {postgres_error_detail(exc)}"
        )
        logger.warning(
            "%s",
            warning,
        )
        if diagnostics is not None:
            diagnostics.append(warning)
        postgres_records = []

    for record in postgres_records:
        identity = record_identity(record)
        if identity is None:
            selected_without_id.append(record)
        elif identity not in merged:
            merged[identity] = record

    for record in iter_records(start, end, allow_empty=True):
        if str(record.get("model", "")) != model:
            continue
        identity = record_identity(record)
        if identity is None:
            selected_without_id.append(record)
        elif identity not in merged:
            merged[identity] = record

    selected = [*merged.values(), *selected_without_id]
    selected.sort(key=lambda row: (row["_timestamp"], str(row.get("request_id", ""))))
    return selected


def query_models() -> list[str]:
    models = set()
    connection = None
    try:
        connection = postgres_connect()
        with connection.cursor() as cursor:
            cursor.execute('SELECT DISTINCT "model" FROM "LiteLLM_SpendLogs" WHERE "model" IS NOT NULL')
            models.update(str(row[0]) for row in cursor if row[0] is not None)
    except Exception as exc:
        logger.warning(
            'Postgres model lookup failed; using JSONL models only (%s: %s)',
            type(exc).__name__,
            postgres_error_detail(exc),
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                logger.warning("Failed to close Postgres model connection (%s)", type(exc).__name__)
    models.update(
        str(record["model"])
        for record in iter_records(allow_empty=True)
        if record.get("model") is not None
    )
    return sorted(models)


def query_target_models() -> list[str]:
    if not LITELLM_API_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY is not configured; cannot query LiteLLM models")
    req = Request(f"{LITELLM_URL}/v1/models", headers={"Authorization": f"Bearer {LITELLM_API_KEY}"})
    try:
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        # Some upstream proxies include non-Latin-1 characters in HTTP
        # headers, which can make urllib fail while constructing HTTPError.
        # Preserve a useful diagnostic without exposing headers or secrets.
        raise RuntimeError(f"LiteLLM /v1/models returned HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"Could not connect to LiteLLM ({LITELLM_URL}): {type(exc).__name__}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("LiteLLM /v1/models returned invalid JSON") from exc
    return sorted({str(item["id"]) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")})


def prom_query_range(
    model: str,
    start: datetime,
    end: datetime,
    step: str = "15s",
) -> dict[str, Any]:
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


def update_job(run_id: str, **values: Any) -> None:
    global active_runs
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job.update(values)
            if values.get("status") in ("succeeded", "failed", "cancelled"):
                active_runs = False


def increment_sent(run_id: str) -> None:
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job["sent_count"] += 1


def append_result(run_id: str, result: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job.setdefault("results", []).append(result)
            job["completed_count"] += 1
            if result.get("error") or (result.get("status_code") is not None and result["status_code"] >= 400):
                job["failed_count"] += 1
                job["last_error"] = str(result.get("error") or "Request failed")[:500]


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type", "text") == "text"
        )
    return ""


def usage_counts(payload: Any) -> tuple[int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    if not isinstance(usage, dict) and isinstance(payload.get("message"), dict):
        usage = payload["message"].get("usage")
    if not isinstance(usage, dict):
        return None, None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
    )


def parse_response(raw: bytes, streaming: bool) -> tuple[str, int | None, int | None, str | None]:
    text = raw.decode("utf-8", errors="replace")
    output_parts: list[str] = []
    input_tokens = output_tokens = None
    stream_error = None

    if not streaming:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, None, None, None
        input_tokens, output_tokens = usage_counts(payload)
        if isinstance(payload, dict):
            if isinstance(payload.get("content"), list):
                output = content_to_text(payload["content"])
            else:
                output = ""
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                message = choice.get("message")
                if isinstance(message, dict):
                    output = content_to_text(message.get("content"))
                elif isinstance(choice.get("text"), str):
                    output = choice["text"]
            return output, input_tokens, output_tokens, None
        return "", None, None, None

    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        found_input, found_output = usage_counts(payload)
        if found_input is not None:
            input_tokens = found_input
        if found_output is not None:
            output_tokens = found_output
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "error":
            stream_error = "The upstream stream reported an error"
        delta = payload.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            output_parts.append(delta["text"])
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                fragment = choice.get("delta") or choice.get("message") or {}
                if isinstance(fragment, dict):
                    content = fragment.get("content")
                    if isinstance(content, str):
                        output_parts.append(content)
                    elif isinstance(content, list):
                        output_parts.append(content_to_text(content))
    return "".join(output_parts), input_tokens, output_tokens, stream_error


def prepare_replay_records(records: list[SpendLogRecord]) -> tuple[list[ReplayRequest], int]:
    """Prepare replayable rows and count rows that should be skipped."""
    prepared: list[ReplayRequest] = []
    skipped_count = 0
    for record in records:
        try:
            body = extract_body(record)
        except (AttributeError, TypeError, ValueError):
            body = None
        if body is None:
            skipped_count += 1
            continue
        try:
            endpoint = endpoint_for_record(record)
        except RuntimeError:
            skipped_count += 1
            continue
        prepared.append((record, body, endpoint))
    if not prepared:
        raise NoReplayableRequestsError(skipped_count)
    return prepared, skipped_count


def run_replay(
    run_id: str,
    target_model: str,
    prepared: list[ReplayRequest],
    speed: float,
) -> None:
    try:
        if not prepared:
            raise RuntimeError("No replay requests are ready")
        if not LITELLM_API_KEY:
            raise RuntimeError("LITELLM_MASTER_KEY is not configured; cannot send replay requests")

        cancel_event = cancel_events[run_id]
        t0 = time.monotonic()
        first = prepared[0][0]["_timestamp"]
        update_job(run_id, request_count=len(prepared))
        if cancel_event.is_set():
            update_job(run_id, status="cancelled", finished_at=time.time())
            return

        def send_one(
            record: SpendLogRecord,
            body: RequestBody,
            endpoint: str,
            planned: datetime,
        ) -> dict[str, Any]:
            started = time.monotonic()
            status_code = None
            input_tokens = output_tokens = None
            output_text = ""
            error_text = None
            try:
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
                with urlopen(request, timeout=RESPONSE_READ_TIMEOUT) as response:
                    status_code = response.status
                    read_chunk = getattr(response, "read1", response.read)
                    chunks = []
                    while True:
                        chunk = read_chunk(64 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    output_text, input_tokens, output_tokens, stream_error = parse_response(
                        b"".join(chunks), body.get("stream") is True
                    )
                    if stream_error:
                        error_text = stream_error
            except HTTPError as exc:
                status_code = exc.code
                try:
                    error_body = exc.read(64 * 1024)
                    error_body.decode("utf-8", errors="replace")
                    error_text = f"Upstream returned HTTP {exc.code}"
                except Exception:
                    error_text = f"Upstream returned HTTP {exc.code}"
            except Exception as exc:
                error_text = f"{type(exc).__name__}: Request failed"
            result = {
                "request_id": record.get("request_id"),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "status_code": status_code,
                "output": output_text,
            }
            if error_text:
                result["error"] = error_text
            append_result(run_id, result)
            return result

        executor = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(prepared)))
        futures = []
        dispatch_started = False
        dispatch_epoch = None
        try:
            for record, body, endpoint in prepared:
                if cancel_event.is_set():
                    break
                offset = (record["_timestamp"] - first).total_seconds() / speed
                delay = offset - (time.monotonic() - t0)
                if delay > 0 and cancel_event.wait(delay):
                    break
                if cancel_event.is_set():
                    break
                if not dispatch_started:
                    dispatch_started = True
                    dispatch_epoch = time.time()
                    update_job(
                        run_id,
                        status="running",
                        started_at=dispatch_epoch,
                    )
                assert dispatch_epoch is not None
                planned = datetime.fromtimestamp(dispatch_epoch + offset, timezone.utc)
                increment_sent(run_id)
                futures.append(executor.submit(send_one, record, body, endpoint, planned))
                update_job(run_id, last_scheduled_at=planned.timestamp())
            if not cancel_event.is_set():
                update_job(run_id, status="draining", dispatch_finished=True)
            else:
                update_job(run_id, dispatch_finished=True)
            errors = []
            for future in futures:
                try:
                    result = future.result()
                    if result.get("error") or (result.get("status_code") is not None and result["status_code"] >= 400):
                        errors.append(result.get("error") or f"HTTP {result['status_code']}")
                except Exception as exc:
                    errors.append(str(exc))
                    update_job(run_id, error_count=len(errors), last_error="Request worker failed")
            if cancel_event.is_set():
                update_job(run_id, status="cancelled", finished_at=time.time())
            elif errors:
                update_job(run_id, status="failed", error=f"{len(errors)} request(s) failed", finished_at=time.time())
            else:
                update_job(run_id, status="succeeded", finished_at=time.time())
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    except Exception as exc:
        update_job(run_id, status="failed", error=str(exc), finished_at=time.time())


def prepare_and_run(run_id: str, source_model: str, target_model: str, start: datetime, end: datetime, speed: float) -> None:
    """Load and merge all source records off the HTTP request thread."""
    try:
        diagnostics: list[str] = []
        records = select_records(source_model, start, end, diagnostics)
        if diagnostics:
            update_job(run_id, warnings=diagnostics)
        try:
            prepared, skipped_count = prepare_replay_records(records)
        except NoReplayableRequestsError as exc:
            update_job(run_id, request_count=0, skipped_count=exc.skipped_count)
            raise
        update_job(
            run_id,
            request_count=len(prepared),
            skipped_count=skipped_count,
            total=len(prepared),
        )
        if cancel_events[run_id].is_set():
            update_job(run_id, status="cancelled", finished_at=time.time())
            return
        if target_model not in query_target_models():
            raise RuntimeError("The target model is not configured or available in LiteLLM")
        with jobs_lock:
            job = jobs.get(run_id)
            if job is None:
                return
            if cancel_events[run_id].is_set():
                job.update(status="cancelled", finished_at=time.time())
                return
            job.update(status="queued")
        run_replay(run_id, target_model, prepared, speed)
    except Exception as exc:
        update_job(run_id, status="failed", error=str(exc), finished_at=time.time())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        # Keep the HTTP reason phrase ASCII/Latin-1 safe. Error details belong
        # in the UTF-8 JSON body, not in the status line.
        self.send_response(status, "OK" if status < 400 else "Error")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def public_job(job: Job) -> dict[str, Any]:
        result: dict[str, Any] = {
            "replay_id": job["run_id"],
            "status": job["status"],
            "sent_count": job["sent_count"],
            "completed_count": job["completed_count"],
            "failed_count": job["failed_count"],
            "skipped_count": job.get("skipped_count", 0),
            "results": list(job.get("results", [])),
            "warnings": list(job.get("warnings", [])),
            "target": {"metrics_labels": {"model_name": job["target_model"]}},
        }
        if job.get("request_count") is not None:
            result["request_count"] = job["request_count"]
        if job.get("started_at") is not None:
            result["started_at"] = job["started_at"]
        if job.get("finished_at") is not None:
            result["finished_at"] = job["finished_at"]
        if job.get("error"):
            result["error"] = job["error"][:500]
        return result

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/models":
                self.send_json(200, {"source_models": query_models(), "target_models": query_target_models()})
            elif parsed.path == "/api/preview":
                model = params.get("model", [""])[0]
                start, end = parse_time(params.get("start", [""])[0]), parse_time(params.get("end", [""])[0])
                if not model or start >= end:
                    raise ValueError("A model and valid time range are required")
                warnings: list[str] = []
                rows = select_records(model, start, end, warnings)
                replayable = sum(extract_body(row) is not None for row in rows)
                self.send_json(200, {
                    "count": len(rows),
                    "replayable_count": replayable,
                    "missing_payload_count": len(rows) - replayable,
                    "warnings": warnings,
                })
            elif parsed.path == "/api/series":
                model = params.get("model", [""])[0]
                start, end = parse_time(params.get("start", [""])[0]), parse_time(params.get("end", [""])[0])
                self.send_json(200, prom_query_range(model, start, end))
            elif parsed.path.startswith("/api/replays/") or parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.rsplit("/", 1)[-1]
                with jobs_lock:
                    job = jobs.get(run_id)
                    snapshot = dict(job) if job is not None else None
                self.send_json(200, self.public_job(snapshot)) if snapshot is not None else self.send_json(404, {"error": "replay not found"})
            else:
                self.send_json(404, {"error": "not found"})
        except (ValueError, RuntimeError, URLError, OSError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        global active_runs
        path = urlparse(self.path).path
        try:
            if (path.startswith("/api/runs/") or path.startswith("/api/replays/")) and path.endswith("/cancel"):
                run_id = path.split("/")[-2]
                with jobs_lock:
                    job = jobs.get(run_id)
                    if job is None:
                        return self.send_json(404, {"error": "replay not found"})
                    if job["status"] not in ("queued", "preparing", "running", "draining"):
                        return self.send_json(409, {"error": f"run is {job['status']}"})
                    job["status"] = "cancelling"
                    cancel_events[run_id].set()
                return self.send_json(202, {"replay_id": run_id, "status": "cancelling"})
            if path not in ("/api/runs", "/api/replays"):
                return self.send_json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Request body must be non-empty and no larger than 1 MB")
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValueError("Request body must contain valid JSON") from exc
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            model_id = str(data["model_id"])
            source_model = model_id
            target_model = model_id
            if "start_time" in data and "end_time" in data:
                start = parse_unix_time(data["start_time"], "start_time")
                end = parse_unix_time(data["end_time"], "end_time")
            else:
                # Keep the previous local API usable while the frontend moves
                # to the documented Unix-seconds contract.
                start, end = parse_time(data["start"]), parse_time(data["end"])
            try:
                speed = float(data.get("speed", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("speed must be a number greater than 0") from exc
            if not (speed > 0 and speed < float("inf")):
                raise ValueError("speed must be a finite number greater than 0")
            if not source_model or not target_model or start >= end:
                raise ValueError("A valid model and time range are required")
            with jobs_lock:
                if active_runs:
                    self.send_json(409, {"error": "A replay task is already active; wait for it to finish before starting another"})
                    return
                run_id = str(uuid.uuid4())
                active_runs = True
                cancel_events[run_id] = threading.Event()
                jobs[run_id] = {
                    "run_id": run_id,
                    "status": "preparing",
                    "source_model": source_model,
                    "target_model": target_model,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "speed": speed,
                    "request_count": None,
                    "sent_count": 0,
                    "completed_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "results": [],
                }
                initial_response = self.public_job(dict(jobs[run_id]))
            threading.Thread(
                target=prepare_and_run,
                args=(run_id, source_model, target_model, start, end, speed),
                daemon=True,
            ).start()
            self.send_json(202, initial_response)
        except KeyError as exc:
            self.send_json(400, {"error": f"Missing required field: {exc.args[0]}"})
        except (KeyError, ValueError, RuntimeError, json.JSONDecodeError, URLError, OSError) as exc:
            self.send_json(400, {"error": str(exc)})


if __name__ == "__main__":
    # API-only HTTP server. Bind address and port are controlled by the host;
    # use a reverse proxy or SSH tunnel if remote access is required.
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
