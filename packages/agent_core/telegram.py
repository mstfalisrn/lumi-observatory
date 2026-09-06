# LUMI — Telegram bot
# Only respond to approved numeric user IDs; '*' / allow-all is FORBIDDEN.
# Polling (initial setup) + webhook (production). Idempotent by update_id (BIGINT).
# 11+ commands + approval inline callback + getMe verification + DB allowlist + BIGINT.
from __future__ import annotations

import hashlib
import logging
import uuid

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackContext, CallbackQueryHandler, CommandHandler

from observability import models
from observability.config import settings
from observability.db import async_session_factory
from observability.security import redact

log = logging.getLogger("lumi.telegram")

# Opaque path: sha256(secret)[:32] — webhook URL resistant to brute-force
def webhook_opaque_path(secret: str) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode()).hexdigest()[:32]


class TelegramService:
    def __init__(self) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN
        self._app: Application | None = None
        self._started = False

    # --- lifecycle (singleton; initialized once during lifespan) ---
    async def initialize(self) -> None:
        if self._app is None:
            await self.build()
        await self._app.initialize()
        await self._app.start()
        self._started = True
        log.info("Telegram Application initialize+start OK")

    async def shutdown(self) -> None:
        if self._app is not None and self._started:
            try:
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass
            self._started = False

    # --- allowlist: env + DB (BIGINT) ---
    def allowed(self, user_id: int) -> bool:
        # Highest priority: if env allowlist is empty, DB fallback; if non-empty, OR
        env_allowed = settings.allowed_user_ids
        if user_id in env_allowed:
            return True
        # DB check synchronous fallback not possible here — async version below
        # This sync method is for env only; DB check is inside _require
        return bool(env_allowed) and user_id in env_allowed

    async def allowed_with_db(self, user_id: int) -> bool:
        if user_id in settings.allowed_user_ids:
            return True
        # DB allowlist: telegram_identities.is_allowed + BIGINT
        try:
            async with async_session_factory() as s:
                from sqlalchemy import select
                res = await s.execute(
                    select(models.TelegramIdentity).where(
                        models.TelegramIdentity.telegram_user_id == int(user_id),
                        models.TelegramIdentity.is_allowed == True,  # noqa: E712
                    ).limit(1)
                )
                row = res.scalar_one_or_none()
                if row is not None:
                    return True
        except Exception as e:
            log.warning("allowlist DB check error: %s", redact(str(e)))
        return False

    async def send_to_allowed(self, text: str) -> bool:
        """Send message to all allowed users (risk alert etc.). False if no token."""
        if not self.token or not text:
            log.warning("send_to_allowed: no token or empty text")
            return False
        if self._app is None:
            try:
                await self.build()
            except Exception as e:
                log.warning("send_to_allowed build error: %s", e)
                return False
        recipients: set[int] = set()
        try:
            recipients.update(settings.allowed_user_ids)
        except Exception:
            pass
        try:
            async with async_session_factory() as s:
                from sqlalchemy import select as _select

                res = await s.execute(_select(models.TelegramIdentity.telegram_user_id).where(models.TelegramIdentity.is_allowed == True))  # noqa: E712
                for row in res.scalars().all():
                    try:
                        recipients.add(int(row))
                    except Exception:
                        pass
        except Exception as e:
            log.warning("send_to_allowed DB collection error: %s", redact(str(e)))
        if not recipients:
            log.warning("send_to_allowed: no recipients (allowlist empty)")
            return False
        bot = None
        try:
            bot = self._app.bot if self._app else None
        except Exception:
            bot = None
        if bot is None:
            log.warning("send_to_allowed: bot unavailable")
            return False
        ok_any = False
        for uid in recipients:
            try:
                await bot.send_message(chat_id=int(uid), text=text, parse_mode="Markdown", disable_web_page_preview=True)
                ok_any = True
            except Exception as e:
                log.warning("send_to_allowed uid=%s error: %s", uid, redact(str(e)))
        return ok_any

    async def _require(self, update: Update, ctx) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        if not uid:
            return False
        # BIGINT safe cast
        try:
            uid_int = int(uid)
        except Exception:
            return False
        ok = await self.allowed_with_db(uid_int)
        if not ok:
            try:
                await update.effective_message.reply_text("⛔ You don't have access. You are not an authorized user.")
            except Exception:
                pass
            # audit
            log.info("telegram unauthorized uid=%s chat=%s", uid_int, getattr(update.effective_chat, 'id', None))
            return False
        return True

    # --- getMe verification (is token actually valid) ---
    async def verify_token_via_getme(self) -> dict | None:
        if not self.token:
            log.warning("TELEGRAM_BOT_TOKEN empty — getMe skipped (dev/mock)")
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"https://api.telegram.org/bot{self.token}/getMe")
                data = r.json()
                if not data.get("ok"):
                    raise RuntimeError(f"getMe failed: {data}")
                bot = data.get("result", {})
                log.info("Telegram getMe OK: @%s id=%s", bot.get("username"), bot.get("id"))
                return bot
        except Exception as e:
            msg = redact(str(e))
            log.exception("Telegram getMe error: %s", msg)
            if settings.is_production:
                raise RuntimeError(f"Telegram token verification failed: {msg}") from e
            return None

    # --- handlers ---
    async def cmd_start(self, update: Update, context: CallbackContext) -> None:
        text = (
            "🐦 LUMI Observatory — command center\n"
            "/help — command list\n"
            "/status — system status\n"
            "/ask <question> — quickly create a question or task\n"
            "/task <title> | <prompt> — create a task\n"
            "/watch <run_id> — monitor a run\n"
            "/runs — recent runs\n"
            "/last — latest run details and approval buttons\n"
            "/approve <id> — approve an action\n"
            "/reject <id> — reject an action\n"
            "/pause <run_id> — pause a run\n"
            "/resume <run_id> — resume a run\n"
            "/stop <run_id> — cancel a run\n"
            "/memory [query] — search or list memory"
        )
        await update.effective_message.reply_text(text)

    async def cmd_help(self, update: Update, context: CallbackContext) -> None:
        await self.cmd_start(update, context)

    async def cmd_status(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        from observability.config import get_settings
        s = get_settings()
        async with async_session_factory() as sess:
            from sqlalchemy import func, select
            runs_cnt = (await sess.execute(select(func.count()).select_from(models.Run))).scalar() or 0
            appr_cnt = (await sess.execute(select(func.count()).select_from(models.Approval).where(models.Approval.status == models.ApprovalStatus.PENDING.value))).scalar() or 0
        await update.effective_message.reply_text(
            f"ℹ️ LUMI status\n"
            f"Provider: {s.LLM_PROVIDER}\n"
            f"LLM: {s.LLM_MODEL or '-'}\n"
            f"Env allowlist: {len(s.allowed_user_ids)} users\n"
            f"DB runs: {runs_cnt} · pending approvals: {appr_cnt}\n"
            f"Telegram: {'configured' if self.token else 'TOKEN MISSING'}"
        )

    async def cmd_runs(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        async with async_session_factory() as s:
            from sqlalchemy import select
            res = await s.execute(select(models.Run).order_by(models.Run.created_at.desc()).limit(8))
            runs = res.scalars().all()
        if not runs:
            await update.effective_message.reply_text("No runs yet.")
            return
        lines = [f"`{str(r.id)[:8]}` · {r.status} · iter {r.iteration}" for r in runs]
        await update.effective_message.reply_text("📋 Recent runs:\n" + "\n".join(lines), parse_mode="Markdown")

    async def cmd_last(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        async with async_session_factory() as s:
            from sqlalchemy import select
            res = await s.execute(select(models.Run).order_by(models.Run.created_at.desc()).limit(1))
            run = res.scalar_one_or_none()
            if not run:
                await update.effective_message.reply_text("Run not found.")
                return
            appr = None
            if run.status == models.RunStatus.WAITING_APPROVAL.value:
                res2 = await s.execute(select(models.Approval).where(models.Approval.run_id == run.id).order_by(models.Approval.created_at.desc()).limit(1))
                appr = res2.scalar_one_or_none()
            text = f"🔎 Latest run\nid: `{run.id}`\nstatus: {run.status}\niter: {run.iteration}\nerror: {run.error or '-'}"
            if appr:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{appr.id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{appr.id}"),
                ]])
                await update.effective_message.reply_text(text + f"\nApproval: {appr.action_class} · {redact(appr.target)[:80]}", parse_mode="Markdown", reply_markup=kb)
            else:
                await update.effective_message.reply_text(text, parse_mode="Markdown")

    async def cmd_ask(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        prompt = " ".join(context.args) if context.args else ""
        if not prompt:
            await update.effective_message.reply_text("Usage: /ask <question/prompt>")
            return
        # Quick task creation + queue
        async with async_session_factory() as s:
            task = models.Task(title=prompt[:80], prompt=prompt, scope={}, budget={})
            s.add(task)
            await s.flush()
            run = models.Run(task_id=task.id, status=models.RunStatus.QUEUED.value,
                             token_budget=settings.RUN_MAX_TOKEN_BUDGET,
                             cost_budget=settings.RUN_MAX_COST_BUDGET)
            s.add(run)
            await s.commit()
            run_id = str(run.id)
        # Redis Streams (outbox/stream — not LPUSH)
        try:
            import redis as redis_lib

            from observability.queue import publish_to_stream
            r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            publish_to_stream(r, {"run_id": run_id}, idempotency_key=f"task:{run_id}")
        except Exception:
            pass
        await update.effective_message.reply_text(f"✅ Task created\nrun: `{run_id}`\nwatch with /status", parse_mode="Markdown")

    async def cmd_task(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        raw = " ".join(context.args) if context.args else ""
        if not raw:
            await update.effective_message.reply_text("Usage: /task <title> | <prompt>")
            return
        if "|" in raw:
            title, prompt = [p.strip() for p in raw.split("|", 1)]
        else:
            title, prompt = raw[:80], raw
        if not prompt:
            await update.effective_message.reply_text("Prompt cannot be empty.")
            return
        async with async_session_factory() as s:
            task = models.Task(title=title or "Telegram task", prompt=prompt, scope={}, budget={})
            s.add(task)
            await s.flush()
            run = models.Run(task_id=task.id, status=models.RunStatus.QUEUED.value,
                             token_budget=settings.RUN_MAX_TOKEN_BUDGET,
                             cost_budget=settings.RUN_MAX_COST_BUDGET)
            s.add(run)
            await s.commit()
            run_id = str(run.id)
        try:
            import redis as redis_lib

            from observability.queue import publish_to_stream
            r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            publish_to_stream(r, {"run_id": run_id}, idempotency_key=f"task:{run_id}")
        except Exception:
            pass
        await update.effective_message.reply_text(f"✅ Task created\nrun `{run_id}` QUEUED", parse_mode="Markdown")

    async def cmd_watch(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        run_id = context.args[0] if context.args else ""
        if not run_id:
            await update.effective_message.reply_text("Usage: /watch <run_id>")
            return
        try:
            uid = uuid.UUID(run_id)
        except ValueError:
            await update.effective_message.reply_text("run_id invalid UUID")
            return
        async with async_session_factory() as s:
            run = await s.get(models.Run, uid)
            if not run:
                await update.effective_message.reply_text("Run not found.")
                return
            from sqlalchemy import select
            res = await s.execute(select(models.RunEvent).where(models.RunEvent.run_id == uid).order_by(models.RunEvent.seq.desc()).limit(6))
            evs = list(res.scalars().all())
        lines = [f"{e.seq}: {e.event_type}" for e in reversed(evs)] or ["no events"]
        await update.effective_message.reply_text(f"👁️ Watch `{run.id}` — {run.status}\n" + "\n".join(lines), parse_mode="Markdown")

    async def _decide_approval(self, approval_id: str, decision: str, user_id: int) -> tuple[bool, str]:
        try:
            uuid.UUID(approval_id)
        except ValueError:
            return False, "invalid approval_id"
        from policy.approval import ApprovalService
        try:
            async with async_session_factory() as s:
                # Telegram + Web should use the same transaction-safe continuation service
                svc = ApprovalService(s)
                # Map telegram_user_id → TelegramIdentity → user_id (rather than an anonymous empty value)
                resolved_user_id = ""
                try:
                    from sqlalchemy import select as _select
                    _res = await s.execute(
                        _select(models.TelegramIdentity).where(
                            models.TelegramIdentity.telegram_user_id == int(user_id)
                        ).limit(1)
                    )
                    _ident = _res.scalar_one_or_none()
                    if _ident is not None and _ident.user_id is not None:
                        resolved_user_id = str(_ident.user_id)
                except Exception:
                    resolved_user_id = ""
                try:
                    _ = await svc.decide_with_continuation(approval_id, decision, resolved_user_id)
                except ValueError as e:
                    msg = str(e)
                    if "expired" in msg or "EXPIRED" in msg:
                        try:
                            await s.commit()
                        except Exception:
                            await s.rollback()
                        return False, msg
                    if "already" in msg.lower():
                        return False, msg
                    if "not found" in msg:
                        return False, "no approval record"
                    return False, msg
                await s.commit()
            return True, decision
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, f"error: {type(e).__name__}"

    async def cmd_approve(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        aid = context.args[0] if context.args else ""
        if not aid:
            await update.effective_message.reply_text("Usage: /approve <approval_id>")
            return
        ok, msg = await self._decide_approval(aid, "approve", update.effective_user.id)
        await update.effective_message.reply_text(f"{'✅' if ok else '⛔'} {msg} — {aid[:8]}" if ok else f"⛔ {msg}")

    async def cmd_reject(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        aid = context.args[0] if context.args else ""
        if not aid:
            await update.effective_message.reply_text("Usage: /reject <approval_id>")
            return
        ok, msg = await self._decide_approval(aid, "reject", update.effective_user.id)
        await update.effective_message.reply_text(f"{'✅' if ok else '⛔'} {msg} — {aid[:8]}" if ok else f"⛔ {msg}")

    async def cmd_pause(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        rid = context.args[0] if context.args else ""
        if not rid:
            await update.effective_message.reply_text("Usage: /pause <run_id>")
            return
        try:
            uid = uuid.UUID(rid)
        except ValueError:
            await update.effective_message.reply_text("invalid run_id")
            return
        async with async_session_factory() as s:
            run = await s.get(models.Run, uid)
            if not run:
                await update.effective_message.reply_text("Run not found")
                return
            # Actually pause the active run: coordinator reads from DB every iteration
            run.control_request = "pause"
            await s.commit()
        await update.effective_message.reply_text(f"⏸️ Pause requested for {rid[:8]} (takes effect at the next iteration)")

    async def cmd_resume(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        rid = context.args[0] if context.args else ""
        if not rid:
            await update.effective_message.reply_text("Usage: /resume <run_id>")
            return
        try:
            uid = uuid.UUID(rid)
        except ValueError:
            await update.effective_message.reply_text("invalid run_id")
            return
        async with async_session_factory() as s:
            run = await s.get(models.Run, uid)
            if not run:
                await update.effective_message.reply_text("Run not found")
                return
            if run.status != models.RunStatus.PAUSED.value and run.control_request != "pause":
                await update.effective_message.reply_text(f"Run status {run.status} — cannot be resumed")
                return
            run.control_request = None
            if run.status == models.RunStatus.PAUSED.value:
                run.status = models.RunStatus.QUEUED.value
            await s.commit()
            run_id = str(run.id)
        try:
            import redis as redis_lib

            from observability.queue import publish_to_stream
            r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            publish_to_stream(r, {"run_id": run_id}, idempotency_key=f"resume:{run_id}")
        except Exception:
            pass
        await update.effective_message.reply_text(f"▶️ Resumed {rid[:8]} → QUEUED")

    async def cmd_stop(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        rid = context.args[0] if context.args else ""
        if not rid:
            await update.effective_message.reply_text("Usage: /stop <run_id>")
            return
        try:
            uid = uuid.UUID(rid)
        except ValueError:
            await update.effective_message.reply_text("invalid run_id")
            return
        async with async_session_factory() as s:
            run = await s.get(models.Run, uid)
            if not run:
                await update.effective_message.reply_text("Run not found")
                return
            # Actually stop the active run: coordinator reads from DB every iteration
            run.control_request = "stop"
            await s.commit()
        await update.effective_message.reply_text(f"⏹️ Stop requested for {rid[:8]} (cancels at the next iteration)")

    async def cmd_memory(self, update: Update, context: CallbackContext) -> None:
        if not await self._require(update, context):
            return
        q = " ".join(context.args) if context.args else ""
        async with async_session_factory() as s:
            from sqlalchemy import select
            stmt = select(models.MemoryItem).order_by(models.MemoryItem.created_at.desc()).limit(6)
            if q:
                stmt = select(models.MemoryItem).where(models.MemoryItem.content.ilike(f"%{q}%")).order_by(models.MemoryItem.created_at.desc()).limit(6)
            res = await s.execute(stmt)
            items = list(res.scalars().all())
        if not items:
            await update.effective_message.reply_text("No memory record." + (f" (query: {q})" if q else ""))
            return
        lines = [f"`{str(m.id)[:8]}` [{m.status}] {m.content[:120]}" for m in items]
        await update.effective_message.reply_text("🧠 Memory — last 6:\n" + "\n".join(lines), parse_mode="Markdown")

    # --- approval inline callback ---
    async def on_callback(self, update: Update, context: CallbackContext) -> None:
        q = update.callback_query
        if not q:
            return
        # allowlist check
        uid = q.from_user.id if q.from_user else None
        if not uid or not await self.allowed_with_db(int(uid)):
            try:
                await q.answer("⛔ Not authorized", show_alert=True)
            except Exception:
                pass
            return
        data = q.data or ""
        # format: approve:<uuid> or reject:<uuid>
        try:
            action, aid = data.split(":", 1)
        except ValueError:
            await q.answer("Invalid callback")
            return
        if action not in ("approve", "reject"):
            await q.answer("Unknown operation")
            return
        ok, msg = await self._decide_approval(aid, action, int(uid))
        try:
            await q.answer(f"{'✅ Approved' if ok and action=='approve' else '❌ Rejected' if ok else msg}", show_alert=False)
        except Exception:
            pass
        try:
            await q.edit_message_text(f"{'✅ APPROVED' if action=='approve' and ok else '❌ REJECTED' if ok else '⛔ Error'} — {aid[:8]}\n{msg}")
        except Exception:
            try:
                await context.bot.send_message(chat_id=q.message.chat_id, text=f"{'✅' if ok else '⛔'} {msg} — {aid[:8]}")
            except Exception:
                pass

    # --- tokens are never logged ---
    def _redact_error(self, exc: Exception) -> str:
        return redact(str(exc))

    async def build(self) -> Application:
        if not self.token:
            log.warning("TELEGRAM_BOT_TOKEN missing — build skipped (bot off)")
            # Test-only application instance for operation without a token
            self._app = Application.builder().token("123456789:" + ("T" * 35)).build()
        else:
            self._app = Application.builder().token(self.token).build()
        # 11+ commands (13 commands + callback)
        self._app.add_handler(CommandHandler("start", self.cmd_start))
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("ask", self.cmd_ask))
        self._app.add_handler(CommandHandler("task", self.cmd_task))
        self._app.add_handler(CommandHandler("watch", self.cmd_watch))
        self._app.add_handler(CommandHandler("runs", self.cmd_runs))
        self._app.add_handler(CommandHandler("last", self.cmd_last))
        self._app.add_handler(CommandHandler("approve", self.cmd_approve))
        self._app.add_handler(CommandHandler("reject", self.cmd_reject))
        self._app.add_handler(CommandHandler("pause", self.cmd_pause))
        self._app.add_handler(CommandHandler("resume", self.cmd_resume))
        self._app.add_handler(CommandHandler("stop", self.cmd_stop))
        self._app.add_handler(CommandHandler("memory", self.cmd_memory))
        # approval inline callback
        self._app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(approve|reject):"))
        return self._app

    # webhook raw update processing (except dedup — app.py does dedup)
    async def handle_raw_update(self, body: dict) -> None:
        if not self._app:
            await self.build()
        try:
            upd = Update.de_json(body, self._app.bot)
            await self._app.process_update(upd)
        except Exception as e:
            log.warning("handle_raw_update error: %s", self._redact_error(e))


def ensure_user_id_in_allowlist(user_id: int):
    """Adds a Telegram user (approved candidate) to the allowlist. Takes effect on application restart."""
    from observability.config import get_settings
    s = get_settings()
    current = set(s.allowed_user_ids)
    current.add(int(user_id))
    s.TELEGRAM_ALLOWED_USER_IDS = ",".join(str(x) for x in sorted(current))


_singleton: TelegramService | None = None


def get_service() -> TelegramService:
    """Singleton TelegramService — a new instance is not created on every webhook."""
    global _singleton
    if _singleton is None:
        _singleton = TelegramService()
    return _singleton
