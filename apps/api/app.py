# LUMI — FastAPI application
# Endpoint groups: /health, /api/v1/*, /webhooks/telegram/<opaque_path>, /events/stream (SSE)
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text

from memory.service import MemoryService
from observability import __version__, models
from observability.config import settings
from observability.db import async_session_factory
from observability.security import redact

log = logging.getLogger("lumi.api")


# --- Lifespan: seed admin user ---
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Fail-closed in production: require real secrets
    settings.validate_production()
    if settings.ADMIN_PASSWORD_HASH:
        async with async_session_factory() as s:
            res = await s.execute(select(models.User).where(models.User.username == settings.ADMIN_EMAIL))
            u = res.scalar_one_or_none()
            if u is None:
                s.add(models.User(username=settings.ADMIN_EMAIL, display_name="Admin",
                                  role="admin", is_active=True, password_hash=settings.ADMIN_PASSWORD_HASH))
                await s.commit()
                log.info("admin user seeded: %s", settings.ADMIN_EMAIL)
            elif u.password_hash != settings.ADMIN_PASSWORD_HASH:
                u.password_hash = settings.ADMIN_PASSWORD_HASH
                await s.commit()
    # Telegram Application singleton: build+initialize+start ONCE (for webhook)
    _tg = None
    if settings.TELEGRAM_BOT_TOKEN:
        try:
            from agent_core.telegram import get_service
            _tg = get_service()
            await _tg.initialize()
            log.info("Telegram Application initialized")
        except Exception as e:
            log.warning("Telegram initialize skipped (dev): %s", type(e).__name__)
    yield
    if _tg is not None:
        await _tg.shutdown()


app = FastAPI(title="LUMI Agentic Observatory", version=__version__, lifespan=_lifespan)

# --- Fail-fast: do not boot with missing/placeholder secrets in production (P58) ---
import os as _os

if _os.getenv("APP_ENV") == "production":
    _missing = []
    for _k in ("JWT_SECRET", "SESSION_ENCRYPTION_MASTER_KEY", "TELEGRAM_WEBHOOK_SECRET"):
        _v = getattr(settings, _k.lower(), "") or _os.getenv(_k, "")
        if not _v or _v in ("CHANGE_ME", "dev-only-change-me") or len(str(_v)) < 16:
            _missing.append(_k)
    if _missing:
        raise RuntimeError(f"LUMI fail-fast: missing/placeholder secret: {', '.join(_missing)}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Local dev + gateway (configurable via ALLOWED_ORIGINS env if you expose a public domain)
        "http://127.0.0.1:3525",
        "http://localhost:3525",
        *([o.strip() for o in __import__("os").getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]),
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Cf-Access-Jwt-Assertion", "X-Telegram-Bot-Api-Secret-Token"],
)

# --- Local auth (no Cloudflare Access) + rate limit + body size ---
from observability.auth import (
    create_session_token,
    get_current_user,
    rate_limiter,
    require_role,
    verify_password,
)

_PUBLIC_PATHS = {"/health/live", "/health/ready", "/api/health/live", "/api/health/ready", "/api/v1/auth/login", "/api/v1/auth/status"}


@app.middleware("http")
async def _guard(request: Request, call_next):
    path = request.url.path
    # Telegram webhook + health + login are exempt (protected by webhook secret header)
    if path.startswith("/webhooks/telegram/") or path in _PUBLIC_PATHS:
        return await call_next(request)
    # Body size limit (Content-Length + streaming guard)
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > settings.MAX_REQUEST_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    # Global rate limit — real client IP (behind Cloudflare Tunnel request.client.host=127.0.0.1)
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    ip = cf_ip or xff or (request.client.host if request.client else "unknown")
    if not await rate_limiter.check(f"rl:global:{ip}", settings.RATE_LIMIT_PER_MINUTE, 60):
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic schemas — secret fields are NOT in response models
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    title: str
    prompt: str
    scope: dict = {}
    budget: dict = {}
    idempotency_key: str | None = None


class ApprovalDecision(BaseModel):
    decision: str  # approve | reject
    approval_id: str


class RunControl(BaseModel):
    action: str  # pause | resume | stop


def _tg_allowed() -> bool:
    return bool(settings.allowed_user_ids)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health/live")
async def health_live():
    return {"status": "live", "time": datetime.now(UTC).isoformat()}


@app.get("/health/ready")
async def health_ready():
    async with async_session_factory() as s:
        await s.execute(text("SELECT 1"))
    return {"status": "ready", "db": True}


@app.get("/api/health/live")
async def api_health_live():
    return {"status": "live", "time": datetime.now(UTC).isoformat()}


@app.get("/api/health/ready")
async def api_health_ready():
    async with async_session_factory() as s:
        await s.execute(text("SELECT 1"))
    return {"status": "ready", "db": True}


# ---------------------------------------------------------------------------
# Auth (local session)
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/v1/auth/login")
async def login(req: LoginRequest):
    async with async_session_factory() as s:
        res = await s.execute(select(models.User).where(models.User.username == req.email))
        u = res.scalar_one_or_none()
        if u is None or not u.is_active or not u.password_hash or not verify_password(req.password, u.password_hash):
            raise HTTPException(401, "invalid email or password")
        token = create_session_token(str(u.id), u.role, settings.SESSION_TTL_SECONDS)
        resp = JSONResponse({"token": token, "role": u.role, "email": u.username, "display_name": u.display_name})
        return resp


@app.post("/api/v1/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("lumi_session", path="/")
    return resp


@app.get("/api/v1/auth/status")
async def auth_status():
    return {"auth": "local", "admin_email": settings.ADMIN_EMAIL,
            "roles": ["admin", "operator", "viewer"],
            "session_ttl_seconds": settings.SESSION_TTL_SECONDS}


@app.get("/api/v1/auth/me")
async def auth_me(user: dict = Depends(get_current_user)):
    async with async_session_factory() as s:
        res = await s.execute(select(models.User).where(models.User.id == user["user_id"]))
        u = res.scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "user not found")
    return {"user_id": str(u.id), "username": u.username, "role": u.role,
            "display_name": u.display_name, "is_active": u.is_active}


# ---------------------------------------------------------------------------
# Tasks / Runs — outbox pattern + Redis Streams
# ---------------------------------------------------------------------------
@app.post("/api/v1/tasks", status_code=201)
async def create_task(t: TaskCreate, user: dict = Depends(require_role("operator"))):
    from sqlalchemy.exc import IntegrityError
    outbox_key = f"task:{t.idempotency_key}" if t.idempotency_key else f"auto:{uuid.uuid4().hex}"
    async with async_session_factory() as s:
        # idempotency pre-check (optimistic)
        if t.idempotency_key:
            existing = await s.execute(
                select(models.Task).where(models.Task.idempotency_key == t.idempotency_key)
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                # return existing task's latest run
                rres = await s.execute(select(models.Run).where(models.Run.task_id == row.id).order_by(models.Run.created_at.desc()).limit(1))
                rr = rres.scalar_one_or_none()
                raise HTTPException(409, detail={"error": "idempotency key already used", "task_id": str(row.id), "run_id": str(rr.id) if rr else None})
        task = models.Task(
            title=t.title, prompt=t.prompt, scope=t.scope, budget=t.budget,
            idempotency_key=t.idempotency_key,
        )
        s.add(task)
        await s.flush()
        run = models.Run(task_id=task.id, status=models.RunStatus.QUEUED.value,
                         token_budget=settings.RUN_MAX_TOKEN_BUDGET,
                         cost_budget=settings.RUN_MAX_COST_BUDGET)
        s.add(run)
        await s.flush()
        # outbox transactional
        ob = models.OutboxMessage(
            topic="lumi.run_queued",
            payload={"run_id": str(run.id), "task_id": str(task.id)},
            idempotency_key=outbox_key,
            processed=False,
        )
        s.add(ob)
        try:
            await s.commit()
        except IntegrityError as ie:
            await s.rollback()
            msg = str(ie.orig) if hasattr(ie, "orig") else str(ie)
            if "ix_tasks_idempotency_key" in msg or "uq_" in msg or "idempotency" in msg.lower():
                raise HTTPException(409, "idempotency key already used (race)") from ie
            raise HTTPException(500, f"commit error: {type(ie).__name__}") from ie
        # outbox is sole reliable publish path; scheduler/reconciler publishes to Redis
        return {"task_id": str(task.id), "run_id": str(run.id), "status": run.status}


@app.get("/api/v1/runs")
async def list_runs(limit: int = 20, offset: int = 0, user: dict = Depends(get_current_user)):
    async with async_session_factory() as s:
        res = await s.execute(
            select(models.Run).order_by(models.Run.created_at.desc()).limit(limit).offset(offset)
        )
        runs = res.scalars().all()
        return [{"id": str(r.id), "status": r.status, "iteration": r.iteration,
                 "created_at": r.created_at.isoformat(), "error": r.error} for r in runs]


@app.get("/api/v1/runs/{run_id}/events")
async def run_events(run_id: str, user: dict = Depends(get_current_user)):
    try:
        uid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id") from None
    async with async_session_factory() as s:
        res = await s.execute(
            select(models.RunEvent).where(models.RunEvent.run_id == uid).order_by(models.RunEvent.seq)
        )
        return [{"seq": e.seq, "global_seq": e.global_seq, "event_type": e.event_type, "payload": e.payload, "ts": e.ts.isoformat()}
                for e in res.scalars().all()]


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(get_current_user)):
    try:
        uid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id") from None
    async with async_session_factory() as s:
        run = await s.get(models.Run, uid)
        if run is None:
            raise HTTPException(404, "run not found")
        return {"id": str(run.id), "task_id": str(run.task_id), "status": run.status,
                "iteration": run.iteration, "token_used": run.token_used, "cost_used": run.cost_used,
                "worker_id": run.worker_id, "control_request": run.control_request,
                "error": run.error, "created_at": run.created_at.isoformat(),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                # deprecated alias for backward compatibility
                "completed_at": run.finished_at.isoformat() if run.finished_at else None}


@app.post("/api/v1/runs/{run_id}/control")
async def control_run(run_id: str, c: RunControl, user: dict = Depends(require_role("operator"))):
    if c.action not in ("pause", "resume", "stop"):
        raise HTTPException(400, "action must be pause|resume|stop")
    try:
        uid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id") from None
    async with async_session_factory() as s:
        run = await s.get(models.Run, uid)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status not in (models.RunStatus.EXECUTING.value, models.RunStatus.QUEUED.value):
            raise HTTPException(409, f"run is in terminal state ({run.status}), cannot be controlled")
        run.control_request = c.action if c.action != "resume" else None
        await s.commit()
        return {"ok": True, "run_id": str(run.id), "control_request": run.control_request}


@app.post("/api/v1/runs/{run_id}/retry")
async def retry_run(request: Request, run_id: str, user: dict = Depends(require_role("operator"))):
    try:
        uid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "invalid run_id") from None
    raw_key = request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key") or request.query_params.get("idempotency_key") or ""
    client_key = raw_key.strip()[:128] if raw_key else "default"
    from sqlalchemy.exc import IntegrityError
    async with async_session_factory() as s:
        run = await s.get(models.Run, uid)
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status not in (models.RunStatus.FAILED.value, models.RunStatus.COMPLETED.value,
                              models.RunStatus.CANCELLED.value):
            raise HTTPException(409, f"run is not terminal ({run.status}), cannot retry")
        # composite idempotency: same (source_run_id, key) -> same retry
        existing = await s.execute(
            select(models.Run).where(models.Run.source_run_id == uid, models.Run.retry_idempotency_key == client_key).limit(1)
        )
        er = existing.scalar_one_or_none()
        if er is not None:
            return {"ok": True, "run_id": str(er.id), "source_run_id": str(run.id), "retry_count": er.retry_count, "dedup": True}
        idem_key = f"retry:{run.id}:{client_key}"
        new_run = models.Run(
            task_id=run.task_id,
            source_run_id=uid,
            retry_idempotency_key=client_key,
            status=models.RunStatus.QUEUED.value,
            retry_count=(run.retry_count or 0) + 1,
            token_budget=run.token_budget,
            cost_budget=run.cost_budget,
        )
        s.add(new_run)
        # flush to get id; outbox add must be inside same transaction and commit together
        await s.flush()
        out = models.OutboxMessage(
            topic="lumi.run_queued",
            payload={"run_id": str(new_run.id), "source_run_id": str(run.id)},
            idempotency_key=idem_key,
        )
        s.add(out)
        try:
            await s.commit()
        except IntegrityError as ie:
            await s.rollback()
            # No error-string matching: re-query exact (source_run_id, client_key) under race.
            async with async_session_factory() as s2:
                res2 = await s2.execute(
                    select(models.Run).where(models.Run.source_run_id == uid, models.Run.retry_idempotency_key == client_key).limit(1)
                )
                er2 = res2.scalar_one_or_none()
                if er2 is not None:
                    return {"ok": True, "run_id": str(er2.id), "source_run_id": str(run.id), "retry_count": er2.retry_count, "dedup": True}
            # if no matching run, this IntegrityError is not a retry dedup — raise safe 500 with original cause
            raise HTTPException(500, "retry commit error") from ie
        return {"ok": True, "run_id": str(new_run.id), "source_run_id": str(run.id), "retry_count": new_run.retry_count}


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
@app.get("/api/v1/approvals")
async def list_approvals(limit: int = 20, user: dict = Depends(get_current_user)):
    async with async_session_factory() as s:
        res = await s.execute(
            select(models.Approval).order_by(models.Approval.created_at.desc()).limit(limit)
        )
        return [{"id": str(a.id), "action_class": a.action_class, "target": redact(a.target),
                 "status": a.status, "impact_summary": a.impact_summary,
                 "expires_at": a.expires_at.isoformat() if a.expires_at else None}
                for a in res.scalars().all()]


@app.post("/api/v1/approvals/{approval_id}/decision")
async def decide_approval(approval_id: str, d: ApprovalDecision, user: dict = Depends(require_role("operator"))):
    if d.approval_id != approval_id:
        raise HTTPException(400, "path approval_id does not match body approval_id")
    from policy.approval import ApprovalService
    async with async_session_factory() as s:
        svc = ApprovalService(s)
        try:
            a = await svc.decide_with_continuation(approval_id, d.decision, user.get("user_id"))
            await s.commit()
            return {"id": str(a.id), "status": a.status, "decision": a.decision}
        except ValueError as e:
            msg = str(e)
            if "expired" in msg or "EXPIRED" in msg:
                # expiry was flushed inside service; persist it even though we raise 410
                try:
                    await s.commit()
                except Exception:
                    await s.rollback()
                raise HTTPException(410, msg) from None
            await s.rollback()
            if "not found" in msg:
                raise HTTPException(404, msg) from None
            if "already" in msg:
                raise HTTPException(409, msg) from None
            raise HTTPException(400, msg) from None


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
class MemoryCreate(BaseModel):
    content: str
    source: str = ""
    confidence: float = 0.5
    category: str | None = None
    ttl_seconds: int | None = None


@app.get("/api/v1/memory")
async def list_memory(status: str | None = None, q: str | None = None, limit: int = 50, user: dict = Depends(get_current_user)):
    async with async_session_factory() as s:
        msvc = MemoryService(s)
        items = (await msvc.search(q, status, limit)) if q else (await msvc.list_status(status or "candidate", limit))
        return [{"id": str(i.id), "content": redact(i.content), "status": i.status,
                 "confidence": i.confidence, "source": i.source, "category": i.category,
                 "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                 "created_at": i.created_at.isoformat()} for i in items]


@app.post("/api/v1/memory", status_code=201)
async def create_memory_candidate(m: MemoryCreate, user: dict = Depends(require_role("operator"))):
    async with async_session_factory() as s:
        msvc = MemoryService(s)
        item = await msvc.create_candidate(
            content=m.content, source=m.source, confidence=m.confidence,
            category=m.category, ttl_seconds=m.ttl_seconds,
        )
        await s.commit()
        return {"id": str(item.id), "status": item.status}


@app.post("/api/v1/memory/{memory_id}/decision")
async def decide_memory(memory_id: str, d: dict, user: dict = Depends(require_role("admin"))):
    try:
        uid = uuid.UUID(memory_id)
    except ValueError:
        raise HTTPException(400, "invalid memory id") from None
    action = d.get("decision")  # approve | reject | activate | supersede
    async with async_session_factory() as s:
        msvc = MemoryService(s)
        item = await s.get(models.MemoryItem, uid)
        if item is None:
            raise HTTPException(404, "record not found")
        if action in ("approve", "activate"):
            await msvc.mark_active(uid)
        elif action == "reject":
            await msvc.reject(uid)
        else:
            raise HTTPException(400, "unknown decision")
        await s.commit()
        return {"id": memory_id, "status": action}


# ---------------------------------------------------------------------------
# Sources / Reports / Technocore
# ---------------------------------------------------------------------------
@app.get("/api/v1/sources")
async def list_sources(user: dict = Depends(get_current_user)):
    _ = user
    async with async_session_factory() as s:
        res = await s.execute(select(models.Source).order_by(models.Source.name))
        return [{"id": str(x.id), "name": x.name, "source_type": x.source_type,
                 "is_enabled": x.is_enabled, "last_accessed_at": x.last_accessed_at.isoformat() if x.last_accessed_at else None,
                 "error_series_len": len(x.error_series)} for x in res.scalars().all()]


@app.get("/api/v1/reports")
async def list_reports(limit: int = 20, user: dict = Depends(get_current_user)):
    _ = user
    async with async_session_factory() as s:
        res = await s.execute(select(models.Report).order_by(models.Report.created_at.desc()).limit(limit))
        return [{"id": str(r.id), "report_type": r.report_type, "subject": r.subject,
                 "confidence": r.confidence, "created_at": r.created_at.isoformat(),
                 "summary": r.summary} for r in res.scalars().all()]


@app.get("/api/v1/technocore")
async def technocore_status(user: dict = Depends(get_current_user)):
    _ = user
    return {"base_url": settings.TECHNOCORE_BASE_URL,
            "room_claim": settings.TECHNOCORE_ROOM_CLAIM, "registered": False}


class LLMTestRequest(BaseModel):
    provider: str = "mock"
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@app.post("/api/v1/settings/llm/test")
async def test_llm_settings(req: LLMTestRequest, user: dict = Depends(get_current_user)):
    _ = user
    prov = (req.provider or "mock").strip().lower()
    if prov == "mock":
        return {"ok": True, "provider": "mock", "detail": "mock provider always ok"}
    # normalize alias
    if prov in ("openai", "openai_compatible", "openrouter", "ollama"):
        prov_norm = "openai_compatible"
    else:
        prov_norm = prov
    # basic validation
    if prov_norm == "openai_compatible":
        url = (req.base_url or "").strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(400, "base_url must start with http(s)")
        # ollama can have empty api_key
        is_ollama = "11434" in url or prov == "ollama"
        if not is_ollama and req.api_key and len(req.api_key.strip()) < 8:
            raise HTTPException(400, "api_key too short")
        if url:
            # try live ping with small payload; fail -> return ok false with detail
            try:
                import httpx
                ping_url = url.rstrip("/") + "/chat/completions"
                headers = {}
                if req.api_key:
                    headers["Authorization"] = f"Bearer {req.api_key.strip()}"
                payload = {
                    "model": req.model or "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
                async with httpx.AsyncClient(timeout=6.0) as client:
                    resp = await client.post(ping_url, json=payload, headers=headers)
                    # 401 means key invalid but endpoint reachable
                    if resp.status_code == 401:
                        return {"ok": False, "provider": prov_norm, "detail": f"401 unauthorized — invalid api_key ({resp.text[:200]})"}
                    if resp.status_code >= 200 and resp.status_code < 300:
                        return {"ok": True, "provider": prov_norm, "detail": "connection ok"}
                    # other 4xx/5xx -> treat as reachable but error
                    return {"ok": False, "provider": prov_norm, "detail": f"http {resp.status_code}: {resp.text[:200]}"}
            except HTTPException:
                raise
            except Exception as e:
                # network failure -> report but don't raise 500
                return {"ok": False, "provider": prov_norm, "detail": f"connection failed: {type(e).__name__}: {str(e)[:200]}"}
        # no url -> only format check
        if not url:
            raise HTTPException(400, "base_url required")
        return {"ok": True, "provider": prov_norm, "detail": "format valid (no live ping)"}
    # unknown provider -> format check only
    return {"ok": True, "provider": prov, "detail": "unknown provider — format check only"}


@app.get("/api/v1/settings/non-secret")
async def settings_non_secret(user: dict = Depends(get_current_user)):
    return {
        "app_env": settings.APP_ENV,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "llm_base_url": settings.LLM_BASE_URL,
        "telegram_allowed_user_ids_count": len(settings.allowed_user_ids),
        "telegram_group_enabled": settings.TELEGRAM_GROUP_ENABLED,
        "technocore_base_url": settings.TECHNOCORE_BASE_URL,
        "run_max_iterations": settings.RUN_MAX_ITERATIONS,
        "run_max_wall_seconds": settings.RUN_MAX_WALL_SECONDS,
        # secret VALUES are never returned; only configured/valid status:
        "telegram_token_configured": bool(settings.TELEGRAM_BOT_TOKEN),
        "llm_key_configured": bool(settings.LLM_API_KEY),
    }


# ---------------------------------------------------------------------------
# SSE — real-time event stream (Last-Event-ID supported) — auth required
# Finite helper for testing (no infinite loop)
# ---------------------------------------------------------------------------
def _parse_sse_cursor(request: Request) -> int:
    """Header Last-Event-ID wins over query ?lastEventId / ?last_event_id."""
    header_raw = request.headers.get("Last-Event-ID") or request.headers.get("last-event-id") or ""
    if header_raw.strip().isdigit():
        return int(header_raw.strip())
    q = request.query_params.get("lastEventId") or request.query_params.get("last_event_id") or ""
    if q.strip().isdigit():
        return int(q.strip())
    return 0


async def _fetch_sse_events(after_seq: int, limit: int = 50) -> list:
    async with async_session_factory() as s:
        q = (select(models.RunEvent)
             .where(models.RunEvent.global_seq > after_seq)
             .order_by(models.RunEvent.global_seq.asc())
             .limit(limit))
        res = await s.execute(q)
        return list(res.scalars().all())


@app.get("/api/v1/events/stream")
async def events_stream(request: Request):
    # auth: Bearer-only (query ?token removed, cookie fallback removed — prevents URL/log leakage; web fetch sends Authorization)
    from observability.auth import decode_session_token
    token = None
    auth_h = request.headers.get("Authorization", "")
    if auth_h.startswith("Bearer "):
        token = auth_h[7:].strip()
    if not token:
        raise HTTPException(401, "authentication required")
    try:
        payload = decode_session_token(token)
        user = {"user_id": payload.get("sub"), "role": payload.get("role", "viewer")}
    except Exception:
        raise HTTPException(401, "invalid session token") from None
    _ = user  # auth passed
    last_seq = _parse_sse_cursor(request)

    async def gen():
        import asyncio as _a
        cur = last_seq  # global_seq cursor (global monotonic, not run-local seq)
        yield "retry: 3000\n\n"
        try:
            while True:
                rows = await _fetch_sse_events(cur, limit=50)
                if rows:
                    for e in rows:
                        cur = max(cur, int(e.global_seq))
                        payload = {"global_seq": e.global_seq, "seq": e.seq,
                                   "event_type": e.event_type, "ts": e.ts.isoformat(),
                                   "run_id": str(e.run_id)}
                        yield f"id: {e.global_seq}\ndata: {json.dumps(payload, default=str)}\n\n"
                else:
                    yield f": keepalive {datetime.now(UTC).isoformat()}\n\n"
                await _a.sleep(2)
        except asyncio.CancelledError:
            return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={
                                 "Cache-Control": "no-cache",
                                 "Connection": "keep-alive",
                                 "X-Accel-Buffering": "no",
                             })


# ---------------------------------------------------------------------------
# Telegram webhook (opaque path + mandatory secret header + BIGINT dedup)
# ---------------------------------------------------------------------------
def _expected_opaque_paths() -> set[str]:
    sec = settings.TELEGRAM_WEBHOOK_SECRET or ""
    if not sec:
        return set()
    # accept plain secret and its hash — hash prevents brute-force, plain secret for backward compatibility
    h = hashlib.sha256(sec.encode()).hexdigest()
    return {sec, h[:32], h, hashlib.sha256(sec.encode()).hexdigest()[:16]}


@app.post("/webhooks/telegram/{opaque_path}")
async def telegram_webhook(opaque_path: str, request: Request):
    # 1) opaque path required — even if secret leaks, cannot be hit without path
    expected = _expected_opaque_paths()
    if expected and opaque_path not in expected:
        raise HTTPException(404, "webhook path not found")
    if not expected:
        # if no secret configured, webhook is disabled
        raise HTTPException(404, "webhook not configured")
    # 2) secret header REQUIRED (P58 fail-fast equivalent) — no pass with empty header
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or secret != settings.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(403, "invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON") from None

    update_id = body.get("update_id")
    rec_id = int(update_id) if isinstance(update_id, int) else None
    # BIGINT durable inbox: PENDING record (for dedup + retry)
    if rec_id is not None:
        try:
            async with async_session_factory() as s:
                existing = await s.get(models.TelegramUpdate, rec_id)
                if existing is not None and existing.status == "PROCESSED":
                    return {"ok": True, "dedup": True, "update_id": update_id}
                if existing is None:
                    payload_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:64]
                    s.add(models.TelegramUpdate(update_id=rec_id, payload_hash=payload_hash, status="PENDING"))
                    await s.commit()
        except Exception as e:
            if "uq_tg_update_id" in str(e) or "UniqueViolation" in str(e) or "duplicate" in str(e).lower():
                return {"ok": True, "dedup": True, "update_id": update_id}
            log.warning("telegram dedup DB error: %s", redact(str(e)))

    # actually process (singleton) — not marked PROCESSED until handled
    from agent_core.telegram import get_service
    tg = get_service()
    try:
        await tg.handle_raw_update(body)
    except Exception as e:
        log.warning("telegram handle error: %s", redact(str(e)))
        if rec_id is not None:
            try:
                async with async_session_factory() as s:
                    r2 = await s.get(models.TelegramUpdate, rec_id)
                    if r2 is not None:
                        r2.status = "FAILED"
                        r2.attempt_count = (r2.attempt_count or 0) + 1
                        await s.commit()
            except Exception:
                pass
        # return 500 so Telegram retries
        return JSONResponse({"ok": False, "error": "processing_failed"}, status_code=500)

    # success → PROCESSED
    if rec_id is not None:
        try:
            async with async_session_factory() as s:
                r2 = await s.get(models.TelegramUpdate, rec_id)
                if r2 is not None:
                    r2.status = "PROCESSED"
                    r2.attempt_count = (r2.attempt_count or 0) + 1
                    r2.processed_at = datetime.now(UTC)
                    await s.commit()
        except Exception:
            pass

    return {"ok": True, "update_id": update_id}


# ---------------------------------------------------------------------------
# Root / UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    # serve UI build if present; otherwise a simple status page
    import os as _os
    dist_index = "/srv/lumi/apps/web/dist/index.html"
    candidate = "apps/web/dist/index.html"
    built = _os.path.exists(dist_index) or _os.path.exists(candidate)
    if built:
        try:
            # find dist from the api container's working directory
            from pathlib import Path
            txt = (Path(candidate).read_text() if Path(candidate).exists() else Path(dist_index).read_text())
            return HTMLResponse(txt)
        except Exception:
            pass
    return HTMLResponse("""
    <html><head><title>LUMI Agentic Observatory</title></head>
    <body style="font-family:sans-serif;background:#0d1421;color:#e8eefc;display:flex;align-items:center;justify-content:center;height:100vh">
      <div style="text-align:center">
        <h1>🐦 LUMI Agentic Observatory</h1>
        <p>API is running. UI build is not in dist.</p>
        <p><code>/health/live</code> · <code>/health/ready</code> · <code>/docs</code></p>
      </div>
    </body></html>
    """)


# ---------------------------------------------------------------------------
# M4: Agent Evaluations — auth required, tier/room filter + pagination
# ---------------------------------------------------------------------------
@app.get("/api/v1/agents/evaluations/stats")
async def agent_evaluations_stats(user: dict = Depends(get_current_user)):
    _ = user
    from sqlalchemy import func

    async with async_session_factory() as s:
        total = (await s.execute(select(func.count()).select_from(models.AgentEvaluation))).scalar() or 0
        rows = (await s.execute(select(models.AgentEvaluation.tier, func.count()).group_by(models.AgentEvaluation.tier))).all()
        by_tier = {str(r[0]): int(r[1]) for r in rows}
        # normalize expected tiers
        for k in ("SAFE", "RISKY", "DANGEROUS", "UNKNOWN"):
            by_tier.setdefault(k, 0)
        # include any extra tier keys already counted
        return {"total": int(total), "by_tier": by_tier}


@app.get("/api/v1/agents/evaluations")
async def agent_evaluations(
    tier: str | None = None,
    room: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    _ = user
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    async with async_session_factory() as s:
        stmt = select(models.AgentEvaluation).order_by(models.AgentEvaluation.evaluated_at.desc())
        if tier:
            # case-insensitive tier filter, normalize upper
            t = tier.strip().upper()
            if t:
                stmt = stmt.where(models.AgentEvaluation.tier == t)
        if room:
            r = room.strip()
            if r:
                stmt = stmt.where(models.AgentEvaluation.room == r)
        # count for pagination header
        from sqlalchemy import func

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await s.execute(count_stmt)).scalar() or 0
        stmt = stmt.limit(limit).offset(offset)
        res = await s.execute(stmt)
        items = res.scalars().all()
        return {
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "id": str(e.id),
                    "room": e.room,
                    "seq": e.seq,
                    "global_seq": e.global_seq,
                    "nick": e.nick,
                    "did": e.did,
                    "text": e.text,
                    "score": e.score,
                    "tier": e.tier,
                    "reason": e.reason,
                    "dimensions": e.dimensions,
                    "model": e.model,
                    "evaluated_at": e.evaluated_at.isoformat() if e.evaluated_at else None,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "link": (
                        f"{settings.TECHNOCORE_BASE_URL.rstrip('/')}/r/{e.room}"
                        if settings.TECHNOCORE_BASE_URL
                        else ""
                    ),
                }
                for e in items
            ],
        }


@app.get("/assets/{path:path}")
async def assets(path: str):
    import os as _os

    from fastapi.responses import FileResponse
    for base in ("apps/web/dist", "/srv/lumi/apps/web/dist"):
        p = f"{base}/assets/{path}"
        if _os.path.exists(p):
            return FileResponse(p)
    raise HTTPException(404)


@app.get("/{filename}")
async def root_static(filename: str):
    import os as _os

    from fastapi.responses import FileResponse

    # only serve known static files from dist root (favicon, logos, etc.)
    allowed = {
        "favicon.ico",
        "favicon-16x16.png",
        "favicon-32x32.png",
        "apple-touch-icon.png",
        "icon-192.png",
        "icon-512.png",
        "logo.png",
        "logo-64.png",
        "logo-128.png",
    }
    if filename not in allowed:
        raise HTTPException(404)
    for base in ("apps/web/dist", "/srv/lumi/apps/web/dist"):
        p = f"{base}/{filename}"
        if _os.path.exists(p):
            return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(404)
