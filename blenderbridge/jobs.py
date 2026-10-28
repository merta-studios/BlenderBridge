"""Job manager: queue, approvals, timeouts, snapshots and result bookkeeping.

A *job* is one tool call.  Lifecycle::

    submit -> (pending_approval) -> queued -> running -> succeeded | failed
                     |                              -> timed_out | cancelled
                     +-> denied / approval_timeout

Only one job talks to Blender at a time (Blender data is single threaded), so a
single worker drains a FIFO queue.  Everything a job produces (result JSON,
warnings, artifacts) is stored in ``jobs/<job_id>/`` so it can be reviewed later
and so the UI can show a history.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import protocol, security, tools
from .protocol import ErrorCode, JobState, ProtocolError

TERMINAL = protocol.TERMINAL_STATES

#: Risk levels that always get a safety snapshot before they run.
SNAPSHOT_REQUIRED = {"high", "critical"}


@dataclass
class ApprovalInfo:
    job_id: str
    tool: str
    risk: str
    reason: str
    summary: str
    params_preview: Any
    created_at: float
    expires_at: float
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "tool": self.tool,
            "risk": self.risk,
            "reason": self.reason,
            "summary": self.summary,
            "params_preview": self.params_preview,
            "created_at": protocol.now_iso(self.created_at),
            "expires_at": protocol.now_iso(self.expires_at),
            "seconds_left": max(0, int(self.expires_at - time.time())),
            "note": self.note,
        }


class JobManager:
    """Owns the job store, the worker thread and the approval queue."""

    def __init__(self, *,
                 config,
                 policy: security.ApprovalPolicy,
                 blender_call: Callable[..., Dict[str, Any]],
                 blender_status: Callable[[], Dict[str, Any]],
                 audit=None,
                 job_dir: Optional[Path] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.config = config
        self.policy = policy
        self._blender_call = blender_call
        self._blender_status = blender_status
        self.audit = audit
        self.job_dir = Path(job_dir) if job_dir else Path("jobs")
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event

        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._approvals: Dict[str, ApprovalInfo] = {}
        self._request_index: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._current_job: Optional[str] = None
        self._cancel_requested: set = set()
        self._stats = {"submitted": 0, "succeeded": 0, "failed": 0, "denied": 0}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="bb-jobs", daemon=True)
        self._worker.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="bb-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait("__stop__")
        except queue.Full:
            pass
        for t in (self._worker, self._watchdog):
            if t and t.is_alive():
                t.join(timeout=3.0)

    # -- submission --------------------------------------------------------
    def submit(self, payload: Dict[str, Any], *, actor: str = "agent") -> Dict[str, Any]:
        """Validate and enqueue a job. Returns the public job record."""
        try:
            req = protocol.validate_job_request(payload)
        except ProtocolError as exc:
            raise

        if req["tool"] == "script.run" and not self.config.allow_script_mode:
            raise ProtocolError(ErrorCode.FORBIDDEN,
                                "script.run is disabled in the connector settings")

        spec = tools.get(req["tool"])
        if spec is None:
            raise ProtocolError(ErrorCode.TOOL_UNKNOWN,
                                f"unknown tool: {req['tool']}", tool=req["tool"])

        # idempotency: same request_id returns the same job
        if req["request_id"]:
            with self._lock:
                existing = self._request_index.get(req["request_id"])
                if existing and existing in self._jobs:
                    return self.public(existing)

        risk = spec.risk
        needs_approval = self.policy.requires_approval(risk)
        if spec.name == "script.run" and self.config.script_requires_approval:
            needs_approval = True

        job = protocol.new_job_record(
            req["tool"], req["params"],
            risk=risk,
            requires_approval=needs_approval,
            approval_reason=self.policy.describe(risk) if needs_approval else "",
            note=req["note"],
            request_id=req["request_id"],
        )
        job["dry_run"] = req["dry_run"]
        job["timeout_s"] = req["timeout_s"]
        job["actor"] = actor
        job["events"] = []
        job_id = job["job_id"]

        with self._lock:
            self._jobs[job_id] = job
            if req["request_id"]:
                self._request_index[req["request_id"]] = job_id
            self._stats["submitted"] += 1

        if needs_approval:
            approval = ApprovalInfo(
                job_id=job_id,
                tool=req["tool"],
                risk=risk,
                reason=self.policy.describe(risk),
                summary=security.summary_of(req["params"]),
                params_preview=security.redact(req["params"]),
                created_at=time.time(),
                expires_at=time.time() + max(30, int(self.config.approval_timeout_s)),
                note=req["note"],
            )
            with self._lock:
                self._approvals[job_id] = approval
            self._event({"type": "approval_requested", "job": job_id,
                         "approval": approval.to_dict()})
            self._audit("approval_requested", job)
        else:
            self._enqueue(job_id)

        return self.public(job_id)

    def _enqueue(self, job_id: str) -> None:
        try:
            self._queue.put_nowait(job_id)
        except queue.Full:
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = JobState.FAILED
                job["error"] = protocol.error(ErrorCode.BLENDER_BUSY,
                                              "job queue is full, please retry later")
                job["finished_at"] = protocol.now_iso()
            self._audit("queue_full", self._jobs[job_id])
            return
        with self._lock:
            self._jobs[job_id]["status"] = JobState.QUEUED
        self._event({"type": "job_queued", "job": job_id})

    # -- approvals ---------------------------------------------------------
    def approve(self, job_id: str, approved: bool, reason: str = "",
                actor: str = "user") -> Dict[str, Any]:
        with self._lock:
            approval = self._approvals.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job is None:
                raise ProtocolError(ErrorCode.NOT_FOUND, "job not found", job=job_id)
            if job["status"] != JobState.PENDING_APPROVAL or approval is None:
                raise ProtocolError(ErrorCode.CONFLICT,
                                    f"job is not waiting for approval (state: {job['status']})",
                                    job=job_id)
            if approved:
                job["status"] = JobState.QUEUED
                job["approval_reason"] += f" | approved by {actor}"
            else:
                job["status"] = JobState.DENIED
                job["finished_at"] = protocol.now_iso()
                job["error"] = protocol.error(ErrorCode.APPROVAL_DENIED,
                                              reason or "Der Benutzer hat die Aktion abgelehnt.")
                self._stats["denied"] += 1
        if approved:
            self._event({"type": "job_approved", "job": job_id})
            self._audit("approved", job, detail=f"by {actor}")
            self._enqueue(job_id)
        else:
            self._event({"type": "job_denied", "job": job_id})
            self._audit("denied", job, detail=reason or f"by {actor}")
            self._persist(job)
        return self.public(job_id)

    def pending_approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in sorted(self._approvals.values(),
                                                key=lambda a: a.created_at)]

    # -- cancel ------------------------------------------------------------
    def cancel(self, job_id: str, actor: str = "user") -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ProtocolError(ErrorCode.NOT_FOUND, "job not found", job=job_id)
            if protocol.is_terminal(job["status"]):
                return self.public(job_id)
            if job["status"] == JobState.PENDING_APPROVAL:
                self._approvals.pop(job_id, None)
                job["status"] = JobState.CANCELLED
                job["finished_at"] = protocol.now_iso()
                job["error"] = protocol.error(ErrorCode.CANCELLED, "cancelled before execution")
            elif job["status"] == JobState.RUNNING:
                self._cancel_requested.add(job_id)
                # ask Blender to stop cooperatively
                try:
                    self._blender_call("_control.cancel", {"target": self._current_job or job_id},
                                       timeout=2.0)
                except Exception:  # noqa: BLE001 - cancellation must never raise
                    pass
            else:
                job["status"] = JobState.CANCELLED
                job["finished_at"] = protocol.now_iso()
        self._event({"type": "job_cancel", "job": job_id})
        self._audit("cancelled", job, detail=f"by {actor}")
        return self.public(job_id)

    def cancel_all(self, actor: str = "user") -> int:
        count = 0
        with self._lock:
            ids = [j for j, rec in self._jobs.items() if not protocol.is_terminal(rec["status"])]
        for job_id in ids:
            try:
                self.cancel(job_id, actor=actor)
                count += 1
            except ProtocolError:
                pass
        return count

    # -- reads -------------------------------------------------------------
    def get(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise ProtocolError(ErrorCode.NOT_FOUND, "job not found", job=job_id)
            return self.public(job_id)

    def list_jobs(self, limit: int = 50, state: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
        if state:
            jobs = [j for j in jobs if j["status"] == state]
        return [self.public(j["job_id"]) for j in jobs[:limit]]

    def wait(self, job_id: str, timeout: float) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.get(job_id)
            if protocol.is_terminal(rec["status"]):
                return rec
            time.sleep(0.05)
        return self.get(job_id)

    def public(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs[job_id]
            out = json.loads(json.dumps(job, default=str))
        out.pop("events", None)
        out["artifacts"] = [a for a in out.get("artifacts", [])]
        with self._lock:
            approval = self._approvals.get(job_id)
        if approval:
            out["approval"] = approval.to_dict()
        return out

    def snapshot_state(self) -> Dict[str, Any]:
        with self._lock:
            running = self._current_job
            counts: Dict[str, int] = {}
            for job in self._jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {
            "queue_length": self._queue.qsize(),
            "running": running,
            "pending_approvals": len(self._approvals),
            "states": counts,
            "stats": dict(self._stats),
        }

    # -- worker ------------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id == "__stop__":
                break
            try:
                self._run_job(job_id)
            except Exception:  # noqa: BLE001 - never kill the worker
                self._fail(job_id, ErrorCode.INTERNAL,
                           "Unerwarteter Fehler im Job-Worker",
                           trace=traceback.format_exc()[-1500:])

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] == JobState.CANCELLED:
                return
            job["status"] = JobState.RUNNING
            job["started_at"] = protocol.now_iso()
            job["updated_at"] = protocol.now_iso()
            self._current_job = job_id
            self._cancel_requested.discard(job_id)
        self._event({"type": "job_started", "job": job_id, "tool": job["tool"]})
        self._audit("started", job)

        started = time.time()
        spec = tools.get(job["tool"])
        artifact_dir = self.job_dir / job_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        # 1) safety snapshot before destructive work
        if spec and spec.mutating and self.config.snapshot_before_risky:
            risk = spec.risk
            try:
                snap = self._blender_call(
                    "file.snapshot",
                    {"label": f"before {job['tool']}", "include_preview": False},
                    timeout=60.0, meta={"job_id": job_id, "internal": "snapshot"},
                )
                if snap.get("ok"):
                    job["snapshot"] = (snap.get("result") or {}).get("snapshot")
                else:
                    job["warnings"].append(
                        f"Sicherung fehlgeschlagen: {(snap.get('error') or {}).get('message', 'unbekannt')}"
                    )
                    if risk in SNAPSHOT_REQUIRED and not job.get("dry_run"):
                        self._fail(job_id, ErrorCode.PRECONDITION_FAILED,
                                   "Sicherung vor riskanter Aktion fehlgeschlagen - Aktion abgebrochen.",
                                   details={"tool": job["tool"]})
                        return
            except Exception as exc:  # noqa: BLE001
                job["warnings"].append(f"Sicherung konnte nicht erstellt werden: {security.redact_text(str(exc))}")
                if (spec.risk in SNAPSHOT_REQUIRED) and not job.get("dry_run"):
                    self._fail(job_id, ErrorCode.PRECONDITION_FAILED,
                               "Sicherung fehlgeschlagen - riskante Aktion abgebrochen.",
                               details={"error": security.redact_text(str(exc))})
                    return

        # 2) dry run: keep only the inspection part
        params = dict(job["params"])
        meta = {"job_id": job_id, "artifact_dir": str(artifact_dir), "actor": job.get("actor", "")}
        timeout = float(job.get("timeout_s") or protocol.DEFAULT_JOB_TIMEOUT)
        timeout = min(timeout, float(self.config.max_job_timeout_s))

        try:
            response = self._blender_call(job["tool"], params, timeout=timeout,
                                          dry_run=bool(job.get("dry_run")), meta=meta)
        except ProtocolError as exc:
            self._fail(job_id, exc.code, exc.message, details=exc.details)
            return
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, ErrorCode.INTERNAL, "Kommunikationsfehler mit Blender",
                       details={"error": security.redact_text(str(exc))})
            return

        duration_ms = int((time.time() - started) * 1000)
        with self._lock:
            if job_id in self._cancel_requested:
                job["status"] = JobState.CANCELLED
                job["finished_at"] = protocol.now_iso()
                job["error"] = protocol.error(ErrorCode.CANCELLED, "vom Benutzer abgebrochen")
                self._cancel_requested.discard(job_id)
                self._current_job = None
                self._audit("cancelled_after_run", job)
                self._persist(job)
                self._event({"type": "job_cancelled", "job": job_id})
                return
            job["duration_ms"] = duration_ms
            job["finished_at"] = protocol.now_iso()
            job["updated_at"] = job["finished_at"]
            if response.get("ok"):
                job["status"] = JobState.SUCCEEDED
                job["result"] = response.get("result")
                job["warnings"] = job["warnings"] + list(response.get("warnings") or [])
                job["assumptions"] = list(response.get("assumptions") or [])
                job["artifacts"] = self._collect_artifacts(artifact_dir, response, timeout_ms=duration_ms)
                self._stats["succeeded"] += 1
            else:
                err = response.get("error") or {}
                job["status"] = JobState.FAILED
                job["error"] = err or protocol.error(ErrorCode.INTERNAL, "unbekannter Fehler")
                job["warnings"] = job["warnings"] + list(response.get("warnings") or [])
                self._stats["failed"] += 1
            self._current_job = None

        self._persist(job)
        self._audit("finished", job)
        self._event({"type": "job_finished", "job": job_id, "status": job["status"]})

    def _collect_artifacts(self, artifact_dir: Path, response: Dict[str, Any],
                           timeout_ms: int = 0) -> List[Dict[str, Any]]:
        """Trust only files inside this job's artifact directory."""
        found: List[Dict[str, Any]] = []
        listed = response.get("artifacts") or []
        candidates = []
        for item in listed:
            if isinstance(item, str):
                candidates.append({"path": item})
            elif isinstance(item, dict) and item.get("path"):
                candidates.append(item)
        result = response.get("result")
        if isinstance(result, dict):
            for item in result.get("artifacts") or []:
                if isinstance(item, str):
                    candidates.append({"path": item})
                elif isinstance(item, dict) and item.get("path"):
                    candidates.append(item)
        for item in candidates:
            path = Path(str(item["path"]))
            try:
                resolved = path.resolve()
                base = artifact_dir.resolve()
                if base not in resolved.parents and resolved != base:
                    continue
                if not resolved.is_file():
                    continue
                found.append({
                    "name": resolved.name,
                    "kind": item.get("kind", self._guess_kind(resolved.name)),
                    "bytes": resolved.stat().st_size,
                    "url": f"/v1/jobs/{resolved.parent.name}/artifacts/{resolved.name}",
                    "description": item.get("description", ""),
                })
            except OSError:
                continue
        return found

    @staticmethod
    def _guess_kind(name: str) -> str:
        lower = name.lower()
        for ext, kind in ((".png", "image"), (".jpg", "image"), (".jpeg", "image"),
                          (".blend", "blend"), (".glb", "model"), (".gltf", "model"),
                          (".fbx", "model"), (".json", "json"), (".txt", "text")):
            if lower.endswith(ext):
                return kind
        return "file"

    def _fail(self, job_id: str, code: str, message: str,
              details: Optional[Dict[str, Any]] = None, trace: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if protocol.is_terminal(job["status"]):
                return
            job["status"] = JobState.FAILED
            job["finished_at"] = protocol.now_iso()
            job["error"] = protocol.error(code, message, **(details or {}))
            if trace:
                job["error"]["details"] = {**(job["error"].get("details") or {}), "trace": trace}
            self._stats["failed"] += 1
            self._current_job = None
        self._persist(job)
        self._audit("failed", job)
        self._event({"type": "job_finished", "job": job_id, "status": job["status"]})

    # -- timeouts ----------------------------------------------------------
    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            now = time.time()
            expired: List[str] = []
            with self._lock:
                for job_id, approval in list(self._approvals.items()):
                    if now >= approval.expires_at:
                        expired.append(job_id)
                for job_id in expired:
                    self._approvals.pop(job_id, None)
                    job = self._jobs.get(job_id)
                    if job and job["status"] == JobState.PENDING_APPROVAL:
                        job["status"] = JobState.DENIED
                        job["finished_at"] = protocol.now_iso()
                        job["error"] = protocol.error(
                            ErrorCode.APPROVAL_TIMEOUT,
                            "Keine Freigabe innerhalb der Frist - Aktion wurde nicht ausgeführt.",
                            timeout_s=int(self.config.approval_timeout_s),
                        )
                        self._stats["denied"] += 1
            for job_id in expired:
                self._audit("approval_timeout", self._jobs[job_id])
                self._event({"type": "job_denied", "job": job_id, "reason": "timeout"})
                self._persist(self._jobs[job_id])
            self._prune()

    def _prune(self) -> None:
        keep = max(50, int(self.config.keep_jobs))
        with self._lock:
            if len(self._jobs) <= keep:
                return
            finished = [j for j in self._jobs.values() if protocol.is_terminal(j["status"])]
            finished.sort(key=lambda j: j.get("created_at", 0))
            for job in finished[: max(0, len(self._jobs) - keep)]:
                self._jobs.pop(job["job_id"], None)
                if job.get("request_id"):
                    self._request_index.pop(job["request_id"], None)

    # -- persistence / events ---------------------------------------------
    def _persist(self, job: Dict[str, Any]) -> None:
        try:
            directory = self.job_dir / job["job_id"]
            directory.mkdir(parents=True, exist_ok=True)
            public = {k: v for k, v in job.items() if k != "events"}
            (directory / "job.json").write_text(
                json.dumps(public, indent=2, default=str), encoding="utf-8")
        except OSError:
            pass

    def _audit(self, event: str, job: Dict[str, Any], detail: str = "") -> None:
        if not self.audit or not job:
            return
        if not self.config.log_payloads and event in ("approval_requested", "approved"):
            params = "<disabled>"
        else:
            params = security.redact(job.get("params", {}))
        self.audit.write({
            "event": event,
            "job_id": job.get("job_id"),
            "tool": job.get("tool"),
            "risk": job.get("risk"),
            "status": job.get("status"),
            "params": params,
            "error": security.redact(job.get("error")),
            "detail": detail,
        })

    def _event(self, payload: Dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(payload)
            except Exception:  # noqa: BLE001
                pass
