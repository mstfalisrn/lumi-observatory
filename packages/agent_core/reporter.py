# LUMI — Reporter (short human summary + machine-readable result package)
from __future__ import annotations

from datetime import UTC, datetime


class Reporter:
    def human_summary(self, run: dict) -> str:
        status = run.get("status", "UNKNOWN")
        summary = {
            "COMPLETED": f"✅ Completed — {run.get('id','')[:8]}",
            "FAILED": f"❌ Failed — {run.get('error','error')}",
            "WAITING_APPROVAL": "🕐 Approval pending",
            "PAUSED": "⏸️ Paused",
            "CANCELLED": "🚫 Cancelled",
        }.get(status, f"ℹ️ {status}")
        return summary

    def machine_result(self, *, run_id: str, status: str, claim: str = "",
                       evidence: list | None = None, confidence: float = 0.0,
                       reports: list | None = None, error: str | None = None) -> dict:
        return {
            "run_id": run_id,
            "status": status,
            "claim": claim,
            "evidence": evidence or [],
            "confidence": confidence,
            "reports": reports or [],
            "error": error,
            "generated_at": datetime.now(UTC).isoformat(),
        }


def build_public_report(
    *, report_type: str, observed_at: str, subject: str, change: str,
    evidence: str, confidence: float, schema_version: int = 1,
) -> dict:
    """Public report schema: type, observed_at, subject, change, evidence, confidence, schema_version."""
    return {
        "type": report_type,
        "observed_at": observed_at,
        "subject": subject,
        "change": change,
        "evidence": evidence,
        "confidence": confidence,
        "schema_version": schema_version,
    }