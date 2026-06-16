"""Email templates for digital product delivery.

All product-specific copy is driven by env vars so these templates work
for any digital product without code changes:

  PRODUCT_NAME         — e.g. "Python Crash Course PDF"
  PRODUCT_TAGLINE      — one-line description for the email header
  PRODUCT_DESCRIPTION  — 1-2 sentence body copy about what they're getting
  SITE_BASE_URL        — your site (used in footer unsubscribe link)
  DELIVERY_TOKEN_HOURS — how long the link is valid (default: 48)
"""

from __future__ import annotations

import os


PRODUCT_NAME        = os.getenv("PRODUCT_NAME", "Your Digital Product")
PRODUCT_TAGLINE     = os.getenv("PRODUCT_TAGLINE", "The resource you requested")
PRODUCT_DESCRIPTION = os.getenv(
    "PRODUCT_DESCRIPTION",
    "Click the button below to download your copy. The link is personal to you.",
)
SITE_BASE           = os.getenv("SITE_BASE_URL", "https://yoursite.com")
TOKEN_HOURS         = os.getenv("TOKEN_TTL_HOURS", "48")


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
          <p style="margin:0;font-size:12px;color:#4fc3f7;font-weight:700;letter-spacing:1px;text-transform:uppercase;">Digital Delivery</p>
          <h1 style="margin:8px 0 0;font-size:24px;font-weight:900;color:#ffffff;">{PRODUCT_TAGLINE} 🎁</h1>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.6;">Hi there,</p>
          <p style="margin:0 0 16px;color:#374151;font-size:15px;line-height:1.6;">
            <strong>{PRODUCT_NAME}</strong> is ready for you.
            {PRODUCT_DESCRIPTION}
          </p>

          <!-- CTA -->
          <table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0;">
            <tr><td align="center">
              <a href="{download_url}" style="display:inline-block;background:linear-gradient(135deg,#00e676,#00bcd4);color:#000;font-weight:800;font-size:15px;padding:14px 36px;border-radius:8px;text-decoration:none;letter-spacing:.3px;">
                ↓ Download Now
              </a>
            </td></tr>
          </table>

          <p style="margin:0 0 8px;color:#6b7280;font-size:12px;text-align:center;">
            This link is personal to you and expires in <strong>{TOKEN_HOURS} hours</strong>.
          </p>
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#f9fafb;padding:20px 32px;border-top:1px solid #e5e7eb;">
          <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.6;">
            Sent to {email} via <a href="{SITE_BASE}" style="color:#6b7280;">{SITE_BASE}</a><br>
            You requested this resource. To unsubscribe from future emails, reply "unsubscribe".
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_delivery_email_text(email: str, download_url: str) -> str:
    return f"""{PRODUCT_NAME} is ready.

Download here (single-use link, expires in {TOKEN_HOURS} hours):
{download_url}

{PRODUCT_DESCRIPTION}

--
{SITE_BASE}
Sent to {email}
"""
