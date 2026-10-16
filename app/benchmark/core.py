from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# LiteLLM SpendLogs rows contain provider-specific/dynamic fields, so keep the
# row open-ended while naming the shapes passed between replay functions.
SpendLogRecord = dict[str, Any]
RequestBody = dict[str, Any]
ReplayRequest = tuple[SpendLogRecord, RequestBody, str]
Job = dict[str, Any]


def required_env(name: str, allow_empty: bool = False) -> str:
    if name not in os.environ:
        raise RuntimeError(f"Required environment variable {name} is not set")
    value = os.environ[name]
    if not allow_empty and not value.strip():
        raise RuntimeError(f"Required environment variable {name} is empty")
    return value


# All runtime configuration must be declared in the environment (run.sh loads
# the project's .env). Optional values must still be present and explicitly
# empty when unused.
SPENDLOGS_JSONL = required_env("SPENDLOGS_JSONL")
SPENDLOGS_MANIFEST = required_env("SPENDLOGS_MANIFEST", allow_empty=True)
POSTGRES_HOST = required_env("POSTGRES_HOST")
POSTGRES_PORT = int(required_env("POSTGRES_PORT"))
POSTGRES_DB = required_env("POSTGRES_DB")
POSTGRES_USER = required_env("POSTGRES_USER")
POSTGRES_PASSWORD = required_env("POSTGRES_PASSWORD", allow_empty=True)
POSTGRES_DSN = required_env("POSTGRES_DSN", allow_empty=True)
LITELLM_URL = required_env("LITELLM_URL").rstrip("/")
LITELLM_API_KEY = required_env("LITELLM_MASTER_KEY")
BENCHMARK_API_KEY = required_env("BENCHMARK_API_KEY")
PORT = int(required_env("BENCHMARK_PORT"))
HOST = required_env("BENCHMARK_HOST")
MAX_WORKERS = max(1, int(required_env("BENCHMARK_MAX_WORKERS")))
RESPONSE_READ_TIMEOUT = max(1.0, float(required_env("BENCHMARK_RESPONSE_READ_TIMEOUT")))
STATE_DB_PATH = os.path.abspath(os.path.expanduser(required_env("BENCHMARK_STATE_DB")))
logger = logging.getLogger(__name__)

jobs: dict[str, Job] = {}
cancel_events: dict[str, threading.Event] = {}
jobs_lock = threading.Lock()


def open_state_db() -> sqlite3.Connection:
    parent = os.path.dirname(STATE_DB_PATH)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    connection = sqlite3.connect(STATE_DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def save_job(job: Job) -> None:
    serialized = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
    with closing(open_state_db()) as connection:
        connection.execute(
            "UPDATE replay_jobs SET status = ?, job_json = ?, updated_at = ? WHERE run_id = ?",
            (job["status"], serialized, time.time(), job["run_id"]),
        )
        connection.commit()


def initialize_state_db() -> None:
    """Create the durable task/idempotency store and recover interrupted runs."""
    with closing(open_state_db()) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS replay_jobs ("
            "run_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "request_fingerprint TEXT NOT NULL, status TEXT NOT NULL, "
            "job_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        rows = connection.execute("SELECT run_id, status, job_json FROM replay_jobs").fetchall()
        now = time.time()
        for row in rows:
            job = json.loads(row["job_json"])
            if row["status"] in {"queued", "preparing", "running", "draining", "cancelling"}:
                job.update(
                    status="failed",
                    error="Service restarted while replay was active; the task was not resumed",
                    finished_at=now,
                )
                connection.execute(
                    "UPDATE replay_jobs SET status = ?, job_json = ?, updated_at = ? WHERE run_id = ?",
                    (job["status"], json.dumps(job, ensure_ascii=False, separators=(",", ":")), now, row["run_id"]),
                )
            jobs[row["run_id"]] = job
        connection.commit()
    os.chmod(STATE_DB_PATH, 0o600)


def register_job(job: Job, idempotency_key: str, request_fingerprint: str) -> tuple[str, Job | None]:
    """Atomically return a duplicate, reject an active run, or persist a new run."""
    with jobs_lock:
        connection = open_state_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id, request_fingerprint, job_json FROM replay_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["request_fingerprint"] != request_fingerprint:
                    return "conflict", None
                existing_job = json.loads(existing["job_json"])
                jobs[existing["run_id"]] = existing_job
                return "existing", existing_job

            active = connection.execute(
                "SELECT run_id, job_json FROM replay_jobs "
                "WHERE status IN ('queued','preparing','running','draining','cancelling') LIMIT 1"
            ).fetchone()
            if active is not None:
                connection.commit()
                active_job = json.loads(active["job_json"])
                jobs[active["run_id"]] = active_job
                return "active", active_job

            now = time.time()
            connection.execute(
                "INSERT INTO replay_jobs "
                "(run_id, idempotency_key, request_fingerprint, status, job_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job["run_id"], idempotency_key, request_fingerprint, job["status"],
                    json.dumps(job, ensure_ascii=False, separators=(",", ":")), now, now,
                ),
            )
            connection.commit()
            jobs[job["run_id"]] = job
            return "created", job
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


initialize_state_db()


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


def sanitize_upstream_error_body(raw: bytes) -> str:
    """Keep a bounded, readable upstream error while masking credentials."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    sensitive_keys = {
        "authorization", "apikey", "password", "token", "secret",
        "accesstoken", "refreshtoken", "clientsecret", "privatekey",
    }

    def sanitize_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if re.sub(r"[^a-z0-9]", "", str(key).lower()) in sensitive_keys
                else sanitize_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [sanitize_value(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"(?i)\bBearer\s+[^\s\"',]+", "Bearer [REDACTED]", value)
            value = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED_KEY]", value)
            value = re.sub(
                r"(?i)(api[_-]?key|authorization|password|access[_-]?token|refresh[_-]?token|client[_-]?secret)\s*[:=]\s*([^\s,;]+)",
                r"\1=[REDACTED]",
                value,
            )
            for secret in (LITELLM_API_KEY, POSTGRES_PASSWORD):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        return value

    if payload is not None:
        text = json.dumps(sanitize_value(payload), ensure_ascii=False)
    else:
        text = sanitize_value(text)
    return text[:3000]


def request_exception_detail(exc: Exception) -> str:
    """Return bounded transport diagnostics while redacting configured secrets."""
    if isinstance(exc, TimeoutError):
        detail = "socket operation timed out"
    elif isinstance(exc, URLError):
        reason = exc.reason
        detail = f"{type(reason).__name__}: {reason}"
    else:
        detail = str(exc).strip() or type(exc).__name__
    for secret in (LITELLM_API_KEY, POSTGRES_PASSWORD, BENCHMARK_API_KEY, POSTGRES_DSN):
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    detail = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer [REDACTED]", detail)
    return detail.replace("\n", " ")[:300]


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


def select_time_range() -> dict[str, Any]:
    """Return SpendLogs time bounds, preferring Postgres and falling back to JSONL."""
    warnings: list[str] = []
    connection = None
    try:
        connection = postgres_connect()
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute('SELECT MIN("startTime"), MAX("startTime"), COUNT(*) FROM "LiteLLM_SpendLogs"')
            minimum, maximum, count = cursor.fetchone()
        if minimum is not None and maximum is not None and count:
            if minimum.tzinfo is None:
                minimum = minimum.replace(tzinfo=timezone.utc)
            if maximum.tzinfo is None:
                maximum = maximum.replace(tzinfo=timezone.utc)
            return {
                "min": minimum.astimezone(timezone.utc).timestamp(),
                "max": maximum.astimezone(timezone.utc).timestamp(),
                "count": count,
                "source": "postgres",
                "warnings": warnings,
            }
    except Exception as exc:
        warning = f"Postgres SpendLogs time-range query failed; using JSONL only ({type(exc).__name__}): {postgres_error_detail(exc)}"
        logger.warning("%s", warning)
        warnings.append(warning)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:
                logger.warning("Failed to close Postgres time-range connection (%s)", type(exc).__name__)

    minimum_time: datetime | None = None
    maximum_time: datetime | None = None
    count = 0
    try:
        for path in jsonl_files(required=False):
            with open(path, "r", encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            continue
                        timestamp = record_time(record)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
                    minimum_time = timestamp if minimum_time is None else min(minimum_time, timestamp)
                    maximum_time = timestamp if maximum_time is None else max(maximum_time, timestamp)
                    count += 1
    except (OSError, RuntimeError) as exc:
        warnings.append(f"JSONL SpendLogs time-range lookup failed: {exc}")

    if minimum_time is None or maximum_time is None:
        detail = "; ".join(warnings) if warnings else "No SpendLogs records were found in Postgres or JSONL"
        raise RuntimeError(detail)
    return {
        "min": minimum_time.timestamp(),
        "max": maximum_time.timestamp(),
        "count": count,
        "source": "jsonl",
        "warnings": warnings,
    }


def query_models() -> list[str]:
    models = set()
    connection = None
    try:
        connection = postgres_connect()
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT DISTINCT "model" FROM "LiteLLM_SpendLogs" '
                'WHERE "model" IS NOT NULL AND BTRIM("model") <> %s',
                ("",),
            )
            models.update(str(row[0]).strip() for row in cursor if row[0] is not None and str(row[0]).strip())
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
        str(record["model"]).strip()
        for record in iter_records(allow_empty=True)
        if record.get("model") is not None and str(record["model"]).strip()
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


def update_job(run_id: str, **values: Any) -> None:
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job.update(values)
            save_job(job)


def increment_sent(run_id: str) -> None:
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job["sent_count"] += 1
            save_job(job)


def append_result(run_id: str, result: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs.get(run_id)
        if job is not None:
            job.setdefault("results", []).append(result)
            job["completed_count"] += 1
            if result.get("error") or (result.get("status_code") is not None and result["status_code"] >= 400):
                job["failed_count"] += 1
                job["last_error"] = str(result.get("error") or "Request failed")[:500]
            save_job(job)


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
    dispatch_mode: str = "scheduled",
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
        logger.info(
            "event=replay_dispatch_started run_id=%s target_model=%r mode=%s request_count=%d max_in_flight=%d timeout_seconds=%s",
            run_id,
            target_model,
            dispatch_mode,
            len(prepared),
            min(MAX_WORKERS, len(prepared)),
            RESPONSE_READ_TIMEOUT,
        )
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
            error_kind = None
            error_detail = None
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
                        error_kind = "upstream_stream_error"
                        error_detail = "provider reported an error in the response stream"
            except HTTPError as exc:
                status_code = exc.code
                error_kind = "upstream_http_error"
                error_detail = f"upstream returned HTTP {exc.code}"
                try:
                    error_body = exc.read(64 * 1024)
                    detail = sanitize_upstream_error_body(error_body)
                    error_text = f"Upstream returned HTTP {exc.code}"
                    if detail:
                        error_text += f": {detail}"
                except Exception:
                    error_text = f"Upstream returned HTTP {exc.code}"
            except Exception as exc:
                error_kind = type(exc).__name__
                error_detail = request_exception_detail(exc)
                error_text = f"{error_kind}: {error_detail}"
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
            failed = bool(error_text) or (status_code is not None and status_code >= 400)
            if failed:
                logger.error(
                    "event=replay_request_failed run_id=%s request_id=%r target_model=%r endpoint=%s status_code=%s elapsed_seconds=%.6f error_kind=%s error_detail=%r",
                    run_id,
                    record.get("request_id"),
                    target_model,
                    endpoint,
                    status_code,
                    result["elapsed_seconds"],
                    error_kind,
                    error_detail,
                )
            return result

        worker_count = min(MAX_WORKERS, len(prepared))
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures = []
        dispatch_started = False
        dispatch_epoch = None

        def submit_request(
            record: SpendLogRecord,
            body: RequestBody,
            endpoint: str,
            planned: datetime,
        ) -> Any:
            increment_sent(run_id)
            future = executor.submit(send_one, record, body, endpoint, planned)
            update_job(run_id, last_scheduled_at=planned.timestamp())
            return future

        def record_future_error(future: Any, errors: list[str]) -> None:
            try:
                result = future.result()
                if result.get("error") or (result.get("status_code") is not None and result["status_code"] >= 400):
                    errors.append(result.get("error") or f"HTTP {result['status_code']}")
            except Exception as exc:
                detail = request_exception_detail(exc)
                errors.append(f"{type(exc).__name__}: {detail}")
                logger.error(
                    "event=replay_worker_failed run_id=%s error_type=%s detail=%r",
                    run_id,
                    type(exc).__name__,
                    detail,
                )
                update_job(run_id, error_count=len(errors), last_error="Request worker failed")

        try:
            errors: list[str] = []
            if dispatch_mode == "burst":
                next_index = 0
                in_flight = set()
                while next_index < len(prepared) or in_flight:
                    if cancel_event.is_set():
                        break
                    if not dispatch_started and next_index < len(prepared):
                        dispatch_started = True
                        dispatch_epoch = time.time()
                        update_job(run_id, status="running", started_at=dispatch_epoch)
                    while (
                        not cancel_event.is_set()
                        and next_index < len(prepared)
                        and len(in_flight) < worker_count
                    ):
                        record, body, endpoint = prepared[next_index]
                        planned = datetime.now(timezone.utc)
                        in_flight.add(submit_request(record, body, endpoint, planned))
                        next_index += 1
                    if in_flight:
                        completed, in_flight = wait(
                            in_flight,
                            timeout=0.1,
                            return_when=FIRST_COMPLETED,
                        )
                        for future in completed:
                            record_future_error(future, errors)
                # Wait for requests already in flight when cancellation arrives.
                for future in in_flight:
                    record_future_error(future, errors)
            else:
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
                    futures.append(submit_request(record, body, endpoint, planned))
            if not cancel_event.is_set():
                update_job(run_id, status="draining", dispatch_finished=True)
            else:
                update_job(run_id, dispatch_finished=True)
            for future in futures:
                record_future_error(future, errors)
            if cancel_event.is_set():
                final_status = "cancelled"
                update_job(run_id, status=final_status, finished_at=time.time())
            elif errors:
                final_status = "failed"
                update_job(run_id, status=final_status, error=f"{len(errors)} request(s) failed", finished_at=time.time())
            else:
                final_status = "succeeded"
                update_job(run_id, status=final_status, finished_at=time.time())
            with jobs_lock:
                finished_job = jobs.get(run_id, {})
                sent_count = int(finished_job.get("sent_count", 0))
                completed_count = int(finished_job.get("completed_count", 0))
                request_results = list(finished_job.get("results", []))
            succeeded_count = sum(
                1 for item in request_results
                if not item.get("error") and (item.get("status_code") is None or item["status_code"] < 400)
            )
            failed_count = sum(
                1 for item in request_results
                if item.get("error") or (item.get("status_code") is not None and item["status_code"] >= 400)
            )
            elapsed_seconds = round(time.monotonic() - t0, 6)
            logger.info(
                "event=replay_dispatch_finished run_id=%s status=%s sent_count=%d completed_count=%d succeeded_count=%d failed_count=%d elapsed_seconds=%.6f",
                run_id,
                final_status,
                sent_count,
                completed_count,
                succeeded_count,
                failed_count,
                elapsed_seconds,
            )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    except Exception as exc:
        detail = request_exception_detail(exc)
        logger.error(
            "event=replay_dispatch_crashed run_id=%s error_type=%s detail=%r",
            run_id,
            type(exc).__name__,
            detail,
        )
        update_job(run_id, status="failed", error=f"{type(exc).__name__}: {detail}", finished_at=time.time())


def prepare_and_run(
    run_id: str,
    source_model: str,
    target_model: str,
    start: datetime,
    end: datetime,
    speed: float,
    dispatch_mode: str = "scheduled",
) -> None:
    """Load and merge all source records off the HTTP request thread."""
    try:
        if cancel_events[run_id].is_set():
            update_job(run_id, status="cancelled", finished_at=time.time())
            return
        diagnostics: list[str] = []
        logger.info(
            "event=replay_preparation_started run_id=%s source_model=%r start=%s end=%s",
            run_id,
            source_model,
            start.isoformat(),
            end.isoformat(),
        )
        records = select_records(source_model, start, end, diagnostics)
        update_job(run_id, selected_count=len(records), warnings=diagnostics)
        logger.info(
            "event=replay_records_selected run_id=%s selected_count=%d warning_count=%d",
            run_id,
            len(records),
            len(diagnostics),
        )
        if cancel_events[run_id].is_set():
            update_job(run_id, status="cancelled", finished_at=time.time())
            return
        try:
            prepared, skipped_count = prepare_replay_records(records)
        except NoReplayableRequestsError as exc:
            update_job(
                run_id,
                request_count=0,
                skipped_count=exc.skipped_count,
                preparation_phase="complete",
            )
            if cancel_events[run_id].is_set():
                update_job(run_id, status="cancelled", finished_at=time.time())
                return
            raise
        update_job(
            run_id,
            request_count=len(prepared),
            skipped_count=skipped_count,
            total=len(prepared),
            preparation_phase="complete",
        )
        logger.info(
            "event=replay_preparation_finished run_id=%s replayable_count=%d skipped_count=%d",
            run_id,
            len(prepared),
            skipped_count,
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
                save_job(job)
                return
            job.update(status="queued")
            save_job(job)
        run_replay(run_id, target_model, prepared, speed, dispatch_mode)
    except Exception as exc:
        detail = request_exception_detail(exc)
        logger.error(
            "event=replay_preparation_failed run_id=%s error_type=%s detail=%r",
            run_id,
            type(exc).__name__,
            detail,
        )
        update_job(run_id, status="failed", error=f"{type(exc).__name__}: {detail}", finished_at=time.time())
