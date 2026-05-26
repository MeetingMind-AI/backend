"""Email notification service for meeting summaries.

Sends a styled HTML email with the meeting summary to every member
of the associated team.  Configured entirely through environment
variables:

  SMTP_HOST      – SMTP server hostname  (required to enable email)
  SMTP_PORT      – SMTP server port      (default 587)
  SMTP_USER      – SMTP login username
  SMTP_PASSWORD  – SMTP login password
  SMTP_FROM      – "From" address        (falls back to SMTP_USER)
  SMTP_USE_TLS   – "1" / "true" to use STARTTLS (default true)
"""

from __future__ import annotations

import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiosmtplib
from sqlalchemy import select

from app.db.models import TeamMembership, User
from app.db.session import SessionLocal


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _smtp_config() -> dict[str, Any] | None:
    """Return SMTP settings from env, or *None* when email is disabled."""
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return None
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_addr = os.getenv("SMTP_FROM", "").strip() or user
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "use_tls": use_tls,
    }


# ---------------------------------------------------------------------------
# Team-member email lookup
# ---------------------------------------------------------------------------

def _get_team_member_emails(team_id: int) -> list[str]:
    """Return a de-duplicated list of email addresses for every team member."""
    with SessionLocal() as db:
        rows = db.execute(
            select(User.email)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .where(TeamMembership.team_id == team_id)
        ).scalars().all()
        return list({e for e in rows if e})


# ---------------------------------------------------------------------------
# HTML email builder
# ---------------------------------------------------------------------------

def _build_summary_html(
    meeting_title: str,
    summary: dict[str, str],
) -> str:
    """Render a styled HTML email body from the meeting summary dict."""
    scrum_master = summary.get("scrum_master", "")
    tech_lead = summary.get("tech_lead", "")
    product_manager = summary.get("product_manager", "")

    def _format_section(raw: str) -> str:
        """Convert raw text to safe HTML paragraphs."""
        if not raw:
            return "<p style='color:#999;'>No content available.</p>"
        escaped = (
            raw.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        paragraphs = escaped.split("\n")
        return "".join(f"<p style='margin:4px 0;line-height:1.6;'>{p}</p>" for p in paragraphs if p.strip())

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:32px 40px;">
            <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">📋 Meeting Summary</h1>
            <p style="margin:8px 0 0;color:#e0e7ff;font-size:15px;">{meeting_title}</p>
          </td>
        </tr>

        <!-- Scrum Master Synthesis -->
        <tr>
          <td style="padding:28px 40px 12px;">
            <h2 style="margin:0 0 12px;font-size:17px;color:#4338ca;border-bottom:2px solid #e0e7ff;padding-bottom:8px;">
              🎯 Scrum Master Synthesis
            </h2>
            <div style="font-size:14px;color:#374151;">
              {_format_section(scrum_master)}
            </div>
          </td>
        </tr>

        <!-- Tech Lead Findings -->
        <tr>
          <td style="padding:20px 40px 12px;">
            <h2 style="margin:0 0 12px;font-size:17px;color:#4338ca;border-bottom:2px solid #e0e7ff;padding-bottom:8px;">
              🔧 Tech Lead Findings
            </h2>
            <div style="font-size:14px;color:#374151;">
              {_format_section(tech_lead)}
            </div>
          </td>
        </tr>

        <!-- Product Manager Findings -->
        <tr>
          <td style="padding:20px 40px 12px;">
            <h2 style="margin:0 0 12px;font-size:17px;color:#4338ca;border-bottom:2px solid #e0e7ff;padding-bottom:8px;">
              📊 Product Manager Findings
            </h2>
            <div style="font-size:14px;color:#374151;">
              {_format_section(product_manager)}
            </div>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:24px 40px 32px;">
            <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
              This is an automated summary from MeetingMind AI.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_plain_text(
    meeting_title: str,
    summary: dict[str, str],
) -> str:
    """Render a plain-text fallback for email clients that don't support HTML."""
    scrum_master = summary.get("scrum_master", "N/A")
    tech_lead = summary.get("tech_lead", "N/A")
    product_manager = summary.get("product_manager", "N/A")
    return (
        f"Meeting Summary: {meeting_title}\n"
        f"{'=' * 50}\n\n"
        f"SCRUM MASTER SYNTHESIS\n{'-' * 30}\n{scrum_master}\n\n"
        f"TECH LEAD FINDINGS\n{'-' * 30}\n{tech_lead}\n\n"
        f"PRODUCT MANAGER FINDINGS\n{'-' * 30}\n{product_manager}\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_meeting_summary_email(
    meeting_id: int,
    team_id: int,
    summary: dict[str, str],
    meeting_title: str,
) -> None:
    """Send the meeting summary to every member of *team_id*.

    - No-ops silently when SMTP is not configured.
    - Logs errors but **never** raises — the caller's flow must not break.
    """
    print(f"[Email] Starting email notification for meeting {meeting_id} (team_id={team_id})")

    cfg = _smtp_config()
    if cfg is None:
        print("[Email] SMTP not configured — skipping email notifications.")
        return

    print(f"[Email] SMTP config loaded: host={cfg['host']}:{cfg['port']}, from={cfg['from_addr']}, tls={cfg['use_tls']}")

    recipients = _get_team_member_emails(team_id)
    if not recipients:
        print(f"[Email] No team members found for team_id={team_id} — skipping.")
        return

    print(f"[Email] Found {len(recipients)} recipient(s): {', '.join(recipients)}")

    subject = f"Meeting Summary: {meeting_title}"
    html_body = _build_summary_html(meeting_title, summary)
    plain_body = _build_plain_text(meeting_title, summary)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    print(f"[Email] Email composed — subject=\"{subject}\", html={len(html_body)} bytes, plain={len(plain_body)} bytes")
    print(f"[Email] Connecting to SMTP server {cfg['host']}:{cfg['port']}...")

    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg["host"],
            port=cfg["port"],
            username=cfg["user"] or None,
            password=cfg["password"] or None,
            start_tls=cfg["use_tls"],
            recipients=recipients,
        )
        print(
            f"[Email] ✅ Summary for meeting {meeting_id} sent successfully to "
            f"{len(recipients)} recipient(s): {', '.join(recipients)}"
        )
    except Exception as exc:
        print(f"[Email] ❌ Failed to send summary for meeting {meeting_id}: {type(exc).__name__}: {exc}")

