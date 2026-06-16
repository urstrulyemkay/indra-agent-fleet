"""Email templates for MAPC report delivery."""

from __future__ import annotations


def build_delivery_email_html(email: str, download_url: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#131623,#1a1d2e);padding:28px 32px;">
          <p style="margin:0;font-size:12px;color:#4fc3f7;font-weight:700;letter-spacing:1px;text-transform:uppercase;">MAPC Exam Prep</p>
          <h1 style="margin:8px 0 0;font-size:24px;font-weight:900;color:#ffffff;">Your Study Guide is Ready 🎓</h1>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.6;">Hi there,</p>
          <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.6;">
            Your <strong>MAPC 2nd Year Exam Prep Guide</strong> is ready to download. It covers all 4 subjects
            — MPCE-021, 022, 023, and 046 — with questions ranked by repeat frequency across 37 official
            IGNOU papers (2018–2025).
          </p>

          <!-- Stats -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0;background:#f0fdf4;border-radius:8px;padding:16px;border:1px solid #d1fae5;">
            <tr>
              <td align="center" style="padding:8px;"><strong style="font-size:20px;color:#059669;">37</strong><br><span style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;">Papers Analysed</span></td>
              <td align="center" style="padding:8px;"><strong style="font-size:20px;color:#059669;">2018–2025</strong><br><span style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;">Date Range</span></td>
              <td align="center" style="padding:8px;"><strong style="font-size:20px;color:#059669;">4</strong><br><span style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;">Subjects</span></td>
              <td align="center" style="padding:8px;"><strong style="font-size:20px;color:#059669;">2×+</strong><br><span style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;">Only Repeated Qs</span></td>
            </tr>
          </table>

          <!-- CTA -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0;">
            <tr><td align="center">
              <a href="{download_url}" style="display:inline-block;background:linear-gradient(135deg,#00e676,#00bcd4);color:#000;font-weight:800;font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none;letter-spacing:.3px;">
                ↓ Download Your Guide
              </a>
            </td></tr>
          </table>

          <p style="margin:0 0 8px;color:#6b7280;font-size:12px;text-align:center;">
            This link is personal to you and works <strong>once</strong>. It expires in 48 hours.
          </p>

          <!-- What's inside -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin:20px 0;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
            <tr style="background:#f9fafb;"><td style="padding:10px 16px;font-weight:700;font-size:13px;color:#111;">What's inside</td></tr>
            <tr><td style="padding:12px 16px;">
              <p style="margin:0 0 6px;font-size:13px;color:#374151;">✅ <strong>MPCE-021</strong> — Counselling Psychology (11 papers)</p>
              <p style="margin:0 0 6px;font-size:13px;color:#374151;">✅ <strong>MPCE-022</strong> — Assessment in Counselling &amp; Guidance (11 papers)</p>
              <p style="margin:0 0 6px;font-size:13px;color:#374151;">✅ <strong>MPCE-023</strong> — Interventions in Counselling (11 papers)</p>
              <p style="margin:0 0 6px;font-size:13px;color:#374151;">✅ <strong>MPCE-046</strong> — Applied Positive Psychology (4 papers)</p>
              <p style="margin:4px 0 0;font-size:13px;color:#374151;">✅ Questions ranked by repeat frequency · Confidence % · Exact question text per year</p>
            </td></tr>
          </table>

          <p style="margin:0;color:#374151;font-size:14px;line-height:1.6;">
            Coming next: <strong>answer guides</strong> with exact source references (Block → Unit → Section).
            Stay subscribed — those land in your inbox soon.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:20px 32px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
            Sent to {email} · <a href="https://manikumarjami.com/mapc" style="color:#6b7280;">manikumarjami.com/mapc</a><br>
            You subscribed to get this guide. To unsubscribe from future emails, reply "unsubscribe".
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_delivery_email_text(email: str, download_url: str) -> str:
    return f"""Your MAPC Study Guide is ready.

Download here (single-use link, expires in 48 hours):
{download_url}

What's inside:
- MPCE-021: Counselling Psychology (11 papers, 2018-2024)
- MPCE-022: Assessment in Counselling & Guidance (11 papers)
- MPCE-023: Interventions in Counselling (11 papers)
- MPCE-046: Applied Positive Psychology (4 papers, 2023-2025)

Questions ranked by repeat frequency. Only topics repeated 2+ times included.

Coming soon: answer guides with exact source references (Block > Unit > Section).

--
manikumarjami.com/mapc
Sent to {email}
"""
