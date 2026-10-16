from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from .core import (
    BENCHMARK_API_KEY,
    Job,
    cancel_events,
    extract_body,
    jobs,
    jobs_lock,
    parse_time,
    parse_unix_time,
    prepare_and_run,
    query_models,
    query_target_models,
    register_job,
    request_exception_detail,
    save_job,
    select_records,
    select_time_range,
)

ACTIVE_STATUSES = {"preparing", "queued", "running", "draining", "cancelling"}
CANCELLABLE_STATUSES = ACTIVE_STATUSES - {"cancelling"}
logger = logging.getLogger(__name__)


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

    def api_authorized(self) -> bool:
        """Require the dedicated Benchmark key on API routes."""
        authorization = self.headers.get("Authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential.strip():
            logger.warning("event=api_auth_rejected method=%s path=%s reason=missing_bearer_token", self.command, urlparse(self.path).path)
            self.send_json(401, {"error": "Authorization bearer token is required"})
            return False
        if not hmac.compare_digest(credential.strip(), BENCHMARK_API_KEY):
            logger.warning("event=api_auth_rejected method=%s path=%s reason=invalid_bearer_token", self.command, urlparse(self.path).path)
            self.send_json(401, {"error": "Invalid Benchmark API token"})
            return False
        return True

    @staticmethod
    def api_path_requires_auth(path: str) -> bool:
        # The unchanged frontend loads model names and time bounds without
        # credentials. Protect replay data and all operations that inspect it.
        return path not in {"/api/models", "/api/time-range"}

    @staticmethod
    def public_job(job: Job) -> dict[str, Any]:
        total = job.get("request_count")
        sent = int(job.get("sent_count", 0))
        completed = int(job.get("completed_count", 0))
        skipped = int(job.get("skipped_count", 0))
        progress_percent = (
            min(100, int(completed * 100 / total))
            if isinstance(total, int) and total > 0
            else None
        )
        sent_percent = (
            min(100, int(sent * 100 / total))
            if isinstance(total, int) and total > 0
            else None
        )
        result: dict[str, Any] = {
            "replay_id": job["run_id"],
            "status": job["status"],
            "cancellable": job["status"] in CANCELLABLE_STATUSES,
            "model_id": job.get("target_model"),
            "source_model": job.get("source_model"),
            "target_model": job.get("target_model"),
            "dispatch_mode": job.get("dispatch_mode", "scheduled"),
            "speed": job.get("public_speed", job.get("speed", 1.0)),
            "start_time": job.get("start"),
            "end_time": job.get("end"),
            "created_at": job.get("created_at"),
            "sent_count": sent,
            "completed_count": completed,
            "selected_count": job.get("selected_count"),
            "failed_count": job["failed_count"],
            "skipped_count": skipped,
            "progress": {
                "ready": total is not None,
                "total": total,
                "sent": sent,
                "completed": completed,
                "skipped": skipped,
                "percent": progress_percent,
                "sent_percent": sent_percent,
            },
            "results": list(job.get("results", [])),
            "warnings": list(job.get("warnings", [])),
            "target": {"metrics_labels": {"model_name": job["target_model"]}},
        }
        if job.get("request_count") is not None:
            result["request_count"] = job["request_count"]
        if job.get("started_at") is not None:
            result["started_at"] = job["started_at"]
        if job.get("cancel_requested_at") is not None:
            result["cancel_requested_at"] = job["cancel_requested_at"]
        if job.get("finished_at") is not None:
            result["finished_at"] = job["finished_at"]
        if job.get("error"):
            result["error"] = job["error"][:500]
        return result

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self.api_path_requires_auth(parsed.path) and not self.api_authorized():
            return
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/models":
                self.send_json(200, {"source_models": query_models(), "target_models": query_target_models()})
            elif parsed.path == "/api/time-range":
                self.send_json(200, select_time_range())
            elif parsed.path == "/api/replays/active":
                with jobs_lock:
                    active_job = next(
                        (
                            dict(job)
                            for job in jobs.values()
                            if job.get("status") in ACTIVE_STATUSES
                        ),
                        None,
                    )
                self.send_json(200, {
                    "active": active_job is not None,
                    "task": self.public_job(active_job) if active_job is not None else None,
                })
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
            elif parsed.path.startswith("/api/replays/"):
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
        path = urlparse(self.path).path
        if path.startswith("/api/") and self.api_path_requires_auth(path) and not self.api_authorized():
            return
        try:
            if path.startswith("/api/replays/") and path.endswith("/cancel"):
                run_id = path.split("/")[-2]
                with jobs_lock:
                    job = jobs.get(run_id)
                    if job is None:
                        return self.send_json(404, {"error": "replay not found"})
                    if job["status"] == "cancelling":
                        return self.send_json(202, self.public_job(dict(job)))
                    if job["status"] not in CANCELLABLE_STATUSES:
                        return self.send_json(409, {
                            "error": f"Replay is already {job['status']} and cannot be cancelled",
                            "task": self.public_job(dict(job)),
                        })
                    cancel_event = cancel_events.get(run_id)
                    if cancel_event is None:
                        return self.send_json(409, {
                            "error": "Replay cancellation is unavailable for this task",
                            "task": self.public_job(dict(job)),
                        })
                    job["status"] = "cancelling"
                    job["cancel_requested_at"] = time.time()
                    save_job(job)
                    cancel_event.set()
                    snapshot = dict(job)
                return self.send_json(202, self.public_job(snapshot))
            if path != "/api/replays":
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
            if "model_id" in data:
                model_id = str(data["model_id"])
                source_model = model_id
                target_model = model_id
            else:
                source_model = str(data["source_model"])
                target_model = str(data["target_model"])
                model_id = source_model
            if "start_time" in data and "end_time" in data:
                start = parse_unix_time(data["start_time"], "start_time")
                end = parse_unix_time(data["end_time"], "end_time")
            else:
                # Accept the earlier ISO-8601 start/end payload shape.
                start, end = parse_time(data["start"]), parse_time(data["end"])
            raw_speed = data.get("speed", 1.0)
            requested_mode = str(data.get("dispatch_mode", "")).strip().lower()
            speed_text = str(raw_speed).strip().lower()
            if requested_mode not in {"", "scheduled", "burst"}:
                raise ValueError("dispatch_mode must be 'scheduled' or 'burst'")
            burst_requested = requested_mode == "burst" or speed_text in {"burst", "突发"}
            if requested_mode == "scheduled" and speed_text in {"burst", "突发"}:
                raise ValueError("speed 'burst' cannot be used with scheduled dispatch")
            dispatch_mode = "burst" if burst_requested else "scheduled"
            if dispatch_mode == "burst":
                speed = 1.0
                public_speed: float | str = "burst"
            else:
                if speed_text.startswith("*"):
                    speed_text = speed_text[1:].strip()
                try:
                    speed = float(speed_text)
                except (TypeError, ValueError) as exc:
                    raise ValueError("speed must be a positive number, '*N', or 'burst'") from exc
                if not (speed > 0 and speed < float("inf")):
                    raise ValueError("speed must be a finite number greater than 0")
                public_speed = speed
            if not source_model or not target_model or start >= end:
                raise ValueError("A valid model and time range are required")
            idempotency_key = self.headers.get("Idempotency-Key", "").strip()
            if not idempotency_key or len(idempotency_key) > 256:
                raise ValueError("Idempotency-Key header is required and must be no longer than 256 characters")
            fingerprint_input = json.dumps(
                {
                    "source_model": source_model,
                    "target_model": target_model,
                    "start_time": start.timestamp(),
                    "end_time": end.timestamp(),
                    "dispatch_mode": dispatch_mode,
                    "speed": speed,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            request_fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
            run_id = str(uuid.uuid4())
            job = {
                "run_id": run_id,
                "status": "preparing",
                "source_model": source_model,
                "target_model": target_model,
                "created_at": time.time(),
                "start": start.timestamp(),
                "end": end.timestamp(),
                "dispatch_mode": dispatch_mode,
                "speed": speed,
                "public_speed": public_speed,
                "request_count": None,
                "sent_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "results": [],
                "warnings": [],
            }
            disposition, registered_job = register_job(job, idempotency_key, request_fingerprint)
            if disposition == "conflict":
                logger.warning("event=replay_create_conflict reason=idempotency_key_reused")
                self.send_json(409, {"error": "Idempotency-Key was already used with a different request"})
                return
            if disposition == "active":
                logger.warning(
                    "event=replay_create_conflict reason=active_task active_run_id=%s",
                    registered_job.get("run_id") if registered_job else None,
                )
                self.send_json(409, {
                    "error": "A replay task is already active; wait for it to finish before starting another",
                    "active_task": self.public_job(registered_job) if registered_job is not None else None,
                })
                return
            if disposition == "existing":
                assert registered_job is not None
                self.send_json(200, self.public_job(registered_job))
                return

            cancel_events[run_id] = threading.Event()
            initial_response = self.public_job(dict(job))
            threading.Thread(
                target=prepare_and_run,
                args=(run_id, source_model, target_model, start, end, speed, dispatch_mode),
                daemon=True,
            ).start()
            logger.info(
                "event=replay_create_accepted run_id=%s source_model=%r target_model=%r mode=%s speed=%s start=%s end=%s",
                run_id,
                source_model,
                target_model,
                dispatch_mode,
                public_speed,
                start.isoformat(),
                end.isoformat(),
            )
            self.send_json(202, initial_response)
        except KeyError as exc:
            logger.warning("event=replay_create_rejected error_type=KeyError field=%r", exc.args[0] if exc.args else "")
            self.send_json(400, {"error": f"Missing required field: {exc.args[0]}"})
        except sqlite3.Error as exc:
            logger.error("event=replay_create_failed error_type=%s detail=%r", type(exc).__name__, request_exception_detail(exc))
            self.send_json(500, {"error": "Replay state storage is unavailable"})
        except (KeyError, ValueError, RuntimeError, json.JSONDecodeError, URLError, OSError) as exc:
            logger.warning(
                "event=replay_create_rejected error_type=%s detail=%r",
                type(exc).__name__,
                request_exception_detail(exc),
            )
            self.send_json(400, {"error": str(exc)})
