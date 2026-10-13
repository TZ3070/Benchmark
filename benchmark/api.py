from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from .core import (
    BENCHMARK_API_KEY,
    PROMETHEUS_STEP,
    Job,
    cancel_events,
    extract_body,
    jobs,
    jobs_lock,
    parse_time,
    parse_unix_time,
    prepare_and_run,
    prom_query_range,
    query_models,
    query_target_models,
    register_job,
    save_job,
    select_records,
    select_time_range,
)


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
            self.send_json(401, {"error": "Authorization bearer token is required"})
            return False
        if not hmac.compare_digest(credential.strip(), BENCHMARK_API_KEY):
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
        if parsed.path.startswith("/api/") and self.api_path_requires_auth(parsed.path) and not self.api_authorized():
            return
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/models":
                self.send_json(200, {"source_models": query_models(), "target_models": query_target_models()})
            elif parsed.path == "/api/time-range":
                self.send_json(200, select_time_range())
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
                self.send_json(200, prom_query_range(model, start, end, PROMETHEUS_STEP))
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
        path = urlparse(self.path).path
        if path.startswith("/api/") and self.api_path_requires_auth(path) and not self.api_authorized():
            return
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
                    save_job(job)
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
                # Accept the earlier ISO-8601 /api/runs contract as a
                # compatibility path; the current UI sends Unix seconds.
                start, end = parse_time(data["start"]), parse_time(data["end"])
            try:
                speed = float(data.get("speed", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("speed must be a number greater than 0") from exc
            if not (speed > 0 and speed < float("inf")):
                raise ValueError("speed must be a finite number greater than 0")
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
                "start": start.timestamp(),
                "end": end.timestamp(),
                "speed": speed,
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
                self.send_json(409, {"error": "Idempotency-Key was already used with a different request"})
                return
            if disposition == "active":
                self.send_json(409, {"error": "A replay task is already active; wait for it to finish before starting another"})
                return
            if disposition == "existing":
                assert registered_job is not None
                self.send_json(200, self.public_job(registered_job))
                return

            cancel_events[run_id] = threading.Event()
            initial_response = self.public_job(dict(job))
            threading.Thread(
                target=prepare_and_run,
                args=(run_id, source_model, target_model, start, end, speed),
                daemon=True,
            ).start()
            self.send_json(202, initial_response)
        except KeyError as exc:
            self.send_json(400, {"error": f"Missing required field: {exc.args[0]}"})
        except sqlite3.Error:
            self.send_json(500, {"error": "Replay state storage is unavailable"})
        except (KeyError, ValueError, RuntimeError, json.JSONDecodeError, URLError, OSError) as exc:
            self.send_json(400, {"error": str(exc)})
