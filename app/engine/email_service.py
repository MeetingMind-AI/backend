from __future__ import annotations

import json
import os
from typing import Any

import httpx


def _parse_json_field(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(clean)
        except (json.JSONDecodeError, IndexError):
            return None
    return None


def _badge(text: str, color: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:10px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;'
        f'color:{color};background:{bg};">{text}</span>'
    )


def _suggestion_badge() -> str:
    return _badge("Suggestion", "#92400e", "#fef3c7")


def _approved_badge() -> str:
    return _badge("Approved", "#065f46", "#d1fae5")


def _action_rows(actions: list[dict]) -> str:
    if not actions:
        return '<p style="color:#9ca3af;font-size:13px;margin:0;">None.</p>'
    rows = []
    for a in actions:
        badge = _approved_badge() if a.get("status") == "accepted" else _suggestion_badge()
        assignee_html = ""
        if a.get("assignee") and a["assignee"].get("name"):
            assignee_html = (
                f'<span style="color:#6b7280;font-size:12px;margin-left:8px;">'
                f'&#8594; {a["assignee"]["name"]}</span>'
            )
        rows.append(
            f'<div style="display:flex;align-items:flex-start;gap:10px;padding:12px 14px;'
            f'background:#f9fafb;border-radius:8px;border:1px solid #e5e7eb;margin-bottom:8px;">'
            f'<div style="flex-shrink:0;width:6px;height:6px;border-radius:50%;'
            f'background:#4f8ef7;margin-top:7px;"></div>'
            f'<div style="flex:1;">'
            f'<div style="margin-bottom:5px;">{badge}{assignee_html}</div>'
            f'<span style="font-size:14px;color:#111827;line-height:1.5;">{a["content"]}</span>'
            f'</div>'
            f'</div>'
        )
    return "".join(rows)


def _section(title: str, svg_path: str, content: str, accent: str) -> str:
    return (
        f'<div style="margin-bottom:32px;">'
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;'
        f'padding-bottom:12px;border-bottom:2px solid {accent}30;">'
        f'<div style="width:28px;height:28px;border-radius:8px;background:{accent}18;'
        f'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
        f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="{accent}" stroke-width="2">'
        f'{svg_path}</svg></div>'
        f'<h2 style="margin:0;font-size:16px;font-weight:700;color:#111827;letter-spacing:-0.2px;">'
        f'{title}</h2>'
        f'</div>'
        f'{content}'
        f'</div>'
    )


def build_email_html(meeting: dict, actions: dict) -> str:
    raw_title = meeting.get("title", "Meeting")
    title = ":".join(raw_title.split(":")[1:]) if ":" in raw_title else raw_title

    from datetime import datetime, timezone
    date_str = ""
    created_at = meeting.get("created_at", "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            date_str = dt.strftime("%B %d, %Y")
        except (ValueError, AttributeError):
            date_str = created_at

    summary_obj = meeting.get("summary") or {}
    sm = _parse_json_field(summary_obj.get("scrum_master"))
    tl = _parse_json_field(summary_obj.get("tech_lead"))
    pm = _parse_json_field(summary_obj.get("product_manager"))

    sections = ""

    # Executive Summary
    if sm and sm.get("summary"):
        sections += _section(
            "Executive Summary",
            '<polyline points="9 11 12 14 22 4"/>'
            '<path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
            f'<p style="font-size:15px;color:#374151;line-height:1.7;margin:0;">{sm["summary"]}</p>',
            "#4f8ef7",
        )

    # Technical Decisions
    if tl:
        items = []
        for d in (tl.get("technical_decisions") or []):
            text = d.get("decision", "") if isinstance(d, dict) else str(d)
            note = d.get("rationale", "") if isinstance(d, dict) else ""
            if text:
                sub = f'<span style="color:#6b7280;font-size:12px;"> — {note}</span>' if note else ""
                items.append(
                    f'<li style="margin-bottom:8px;font-size:14px;color:#111827;line-height:1.5;">'
                    f'{text}{sub}</li>'
                )
        for a in (tl.get("architecture") or []):
            text = a if isinstance(a, str) else str(a)
            if text:
                items.append(
                    f'<li style="margin-bottom:8px;font-size:14px;color:#111827;line-height:1.5;">{text}</li>'
                )
        if items:
            sections += _section(
                "Technical Decisions",
                '<polygon points="12 2 2 7 12 22 22 7 12 2"/>',
                f'<ul style="margin:0;padding-left:20px;">{"".join(items)}</ul>',
                "#bc8cff",
            )

    # Business Decisions
    if pm:
        items = []
        for f in (pm.get("feature_requests") or []):
            text = f.get("feature", "") if isinstance(f, dict) else str(f)
            req = f.get("requester", "") if isinstance(f, dict) else ""
            if text:
                sub = f'<span style="color:#6b7280;font-size:12px;"> — requested by {req}</span>' if req else ""
                items.append(
                    f'<li style="margin-bottom:8px;font-size:14px;color:#111827;line-height:1.5;">'
                    f'{text}{sub}</li>'
                )
        for u in (pm.get("ux_topics") or []):
            text = u.get("topic", "") if isinstance(u, dict) else str(u)
            desc = u.get("description", "") if isinstance(u, dict) else ""
            if text:
                sub = f'<span style="color:#6b7280;font-size:12px;"> — {desc}</span>' if desc else ""
                items.append(
                    f'<li style="margin-bottom:8px;font-size:14px;color:#111827;line-height:1.5;">'
                    f'{text}{sub}</li>'
                )
        for r in (pm.get("roadmap_alignment") or []):
            text = r.get("task", "") if isinstance(r, dict) else str(r)
            owner = r.get("owner", "") if isinstance(r, dict) else ""
            if text:
                sub = f'<span style="color:#6b7280;font-size:12px;"> — {owner}</span>' if owner else ""
                items.append(
                    f'<li style="margin-bottom:8px;font-size:14px;color:#111827;line-height:1.5;">'
                    f'{text}{sub}</li>'
                )
        if items:
            sections += _section(
                "Business Decisions",
                '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
                f'<ul style="margin:0;padding-left:20px;">{"".join(items)}</ul>',
                "#3fb950",
            )

    # Action Items (to_do, non-rejected)
    todo = (
        (actions.get("to_do") or {}).get("accepted", [])
        + (actions.get("to_do") or {}).get("pending", [])
    )
    if todo:
        sections += _section(
            "Action Items",
            '<polyline points="9 11 12 14 22 4"/>'
            '<path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
            _action_rows(todo),
            "#4f8ef7",
        )

    # Parking Lot
    parking = (
        (actions.get("parking_lot") or {}).get("accepted", [])
        + (actions.get("parking_lot") or {}).get("pending", [])
    )
    if parking:
        sections += _section(
            "Parking Lot",
            '<circle cx="12" cy="12" r="10"/>'
            '<line x1="12" y1="8" x2="12" y2="16"/>'
            '<line x1="8" y1="12" x2="16" y2="12"/>',
            _action_rows(parking),
            "#d29922",
        )

    # To Schedule
    schedule = (
        (actions.get("to_schedule") or {}).get("accepted", [])
        + (actions.get("to_schedule") or {}).get("pending", [])
    )
    if schedule:
        sections += _section(
            "To Schedule",
            '<rect x="3" y="4" width="18" height="18" rx="2"/>'
            '<line x1="16" y1="2" x2="16" y2="6"/>'
            '<line x1="8" y1="2" x2="8" y2="6"/>'
            '<line x1="3" y1="10" x2="21" y2="10"/>',
            _action_rows(schedule),
            "#e3884c",
        )

    date_line = f" &middot; {date_str}" if date_str else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Meeting Report &mdash; {title}</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
<tr><td align="center" style="padding:32px 16px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;">

  <!-- Header -->
  <tr><td style="background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);border-radius:16px 16px 0 0;padding:32px 40px;">
    <table cellpadding="0" cellspacing="0" width="100%"><tr>
      <td style="padding-bottom:20px;">
        <table cellpadding="0" cellspacing="0"><tr>
          <td style="width:36px;height:36px;background:#4f8ef7;border-radius:10px;text-align:center;vertical-align:middle;padding:0 9px;">
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
              <polygon points="10,1 19,5.5 19,14.5 10,19 1,14.5 1,5.5" fill="none" stroke="white" stroke-width="1.5"/>
              <circle cx="10" cy="10" r="2.5" fill="white"/>
            </svg>
          </td>
          <td style="padding-left:12px;font-size:18px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">MeetingMind</td>
        </tr></table>
      </td>
    </tr><tr>
      <td>
        <h1 style="margin:0 0 8px 0;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;line-height:1.3;">{title}</h1>
        <p style="margin:0;font-size:13px;color:#94a3b8;font-weight:500;">Meeting Report{date_line}</p>
      </td>
    </tr></table>
  </td></tr>

  <!-- Body -->
  <tr><td style="background:#ffffff;padding:40px;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;">
    <p style="margin:0 0 32px 0;font-size:15px;color:#374151;line-height:1.7;">
      Dear team,<br/><br/>
      Here is the AI-generated report for <strong>{title}</strong>{(f' ({date_str})') if date_str else ''}.
      Please review the summary, decisions, and action items below.
    </p>
    {sections if sections else '<p style="color:#9ca3af;font-size:14px;margin:0;">No summary available yet.</p>'}
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9fafb;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 16px 16px;padding:20px 40px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6;">
      Generated by <strong style="color:#4f8ef7;">MeetingMind AI</strong>
      &nbsp;&middot;&nbsp;Self-hosted, offline-first meeting assistant
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


async def send_meeting_email(to_emails: list[str], subject: str, html: str) -> None:
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY not configured — add it to .env")

    from_addr = os.getenv("EMAIL_FROM", "MeetingMind <onboarding@resend.dev>")

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": to_emails, "subject": subject, "html": html},
        )
        if not resp.is_success:
            raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")
