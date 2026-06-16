"""startup_lookup — India-funded startups × senior PM openings.

Flow (no Claude API call — all free):
  1. Pull RSS: Entrackr `/rss` (primary) + YourStory funding feed (secondary).
  2. Parse titles → extract company, stage, amount, date.
  3. Filter: India + last N days + selected stages.
  4. One Apify LinkedIn search for senior PM roles in India.
  5. Cross-reference: jobs at funded companies. Score + write markdown.

The artifact is template-driven (no LLM rationale). For an LLM-quality
"why you're a fit" rewrite, the operator runs `rank startup lookup latest`
inside a Claude Code session — that does the ranking from this run's raw data
without burning Anthropic API credit.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import certifi
import httpx

from core.base_agent import BaseAgent, WorkflowStep


# Some India news sites (entrackr.com) serve an incomplete TLS chain that the
# stdlib default context rejects; certifi's bundle resolves it. Pin it on all
# outbound calls from this agent.
_CA = certifi.where()


OUTPUTS_DIR = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "startup_lookup"

# Apify LinkedIn scraper — same actor job_hunter uses.
DEFAULT_APIFY_ACTOR = "curious_coder~linkedin-jobs-scraper"
APIFY_ACTOR_ID = os.getenv("APIFY_LINKEDIN_ACTOR_ID", DEFAULT_APIFY_ACTOR)
APIFY_BASE = "https://api.apify.com/v2"

# India funding feeds (free, no key). Entrackr is the primary signal —
# it has the cleanest deal-titles format ("X raises $Y Series Z").
RSS_SOURCES = (
    ("entrackr", "https://entrackr.com/rss"),
    ("yourstory", "https://yourstory.com/category/funding/feed"),
)

# Stage detection from title text. Order matters — match longer phrases first
# so "pre-seed" doesn't get caught by the bare "seed" pattern.
STAGE_PATTERNS = [
    ("pre-seed", re.compile(r"\bpre[\s-]?seed\b", re.I)),
    ("series_a", re.compile(r"\bseries\s*a\b", re.I)),
    ("series_b", re.compile(r"\bseries\s*b\b", re.I)),
    ("series_c", re.compile(r"\bseries\s*c\b", re.I)),
    ("seed", re.compile(r"\bseed\s*(round|funding)?\b", re.I)),
]

ALLOWED_STAGES = {"pre-seed", "seed", "series_a", "series_b", "series_c"}

# Senior PM role keywords for the LinkedIn search + post-hoc job filter.
SENIOR_PM_KEYWORDS = [
    "Group Product Manager",
    "Product Lead",
    "Lead Product Manager",
    "Director of Product",
    "Head of Product",
    "Principal Product Manager",
    "AVP Product",
]

# Compiled regex to spot a senior PM title in scraped job text.
SENIOR_PM_TITLE_RE = re.compile(
    r"\b(group\s+product\s+manager|product\s+lead|lead\s+product\s+manager|"
    r"director\s+of\s+product|head\s+of\s+product|principal\s+product\s+manager|"
    r"avp\s+product|associate\s+vice\s+president\s+product)\b",
    re.I,
)

# Titles that look like funding but are actually VC fund launches / closes —
# the "company" in these is a fund manager, not a startup we'd target.
VC_FUND_NOISE_RE = re.compile(
    r"(\blaunch(?:es|ed)?\b.{0,40}\bfund\b|"
    r"\bto\s+launch\b.{0,40}\bfund\b|"
    r"\bfinal\s+close\b|"
    r"\bcorpus\b|"
    r"\bunveils?\b.{0,30}\bfund\b|"
    r"\bmaiden\s+fund\b|"
    r"\bVC\s+fund\b|"
    r"\bventure\s+(fund|capital)\b)",
    re.I,
)

# Acronym/sector descriptor tokens that prefix a company name in headlines
# ("D2C footwear brand Yoho raises…"). Used to trim leading noise.
DESCRIPTOR_TOKENS = {
    "d2c", "b2b", "b2c", "b2b2c", "saas", "ai", "ml", "genai", "fintech", "edtech",
    "healthtech", "medtech", "insurtech", "spacetech", "cleantech", "climatetech",
    "deeptech", "agritech", "proptech", "legaltech", "hrtech", "foodtech", "retailtech",
    "crypto", "web3", "gaming", "content", "media", "mobility", "logistics", "ev",
    "platform", "brand", "startup", "startups", "firm", "company", "app", "player",
    "maker", "provider", "venture", "quick", "commerce", "e-commerce", "ecommerce",
    "q-commerce", "marketplace", "lab", "labs",
}


# ---------- RSS ----------


def _strip_cdata(s: str) -> str:
    s = s.strip()
    if s.startswith("<![CDATA[") and s.endswith("]]>"):
        return s[9:-3].strip()
    return s


def _parse_rss_items(xml: str) -> list[dict]:
    """Best-effort RSS item extraction — title, link, description, pubDate."""
    items = []
    for raw in re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.S | re.I):
        def field(tag: str) -> str:
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", raw, re.S | re.I)
            return unescape(_strip_cdata(m.group(1))) if m else ""

        items.append({
            "title": field("title"),
            "link": field("link"),
            "description": re.sub(r"<[^>]+>", " ", field("description"))[:500],
            "pub_date": field("pubDate"),
        })
    return items


def _parse_pub_date(s: str) -> datetime | None:
    """RFC822 ('Tue, 26 May 2026 19:14:37 +0000') → UTC datetime."""
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_rss(url: str, timeout: float = 10.0) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; IndraStartupLookup/1.0)"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True, verify=_CA) as c:
        r = c.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"RSS {url} → HTTP {r.status_code}")
        return _parse_rss_items(r.text)


# ---------- Title parsing ----------


# Money in title: "$15 Mn", "$1.5 Mn", "$300 Mn valuation", "Rs 3,000 Cr"
MONEY_RE = re.compile(
    r"(\$\s?\d+(?:\.\d+)?\s*(?:Mn|Million|M|Bn|Billion|B)\b|"
    r"Rs\.?\s?\d[\d,]*\s*(?:Cr|Crore|Lakh|L|Mn|Million)\b)",
    re.I,
)


def _extract_stage(text: str) -> str:
    for stage, pat in STAGE_PATTERNS:
        if pat.search(text):
            return stage
    return ""


def _extract_amount(text: str) -> str:
    m = MONEY_RE.search(text)
    return m.group(0).strip() if m else ""


def _extract_company(title: str) -> str:
    """Heuristic: the company name is usually the subject of '<company> raises'
    or '<verb> ... <stage>... in <company>' patterns. Returns best guess."""
    t = re.sub(r"^Exclusive:\s*", "", title.strip(), flags=re.I)

    # Pattern 1: "Company raises $X..."  → take everything before the verb
    m = re.match(
        r"^(.+?)\s+(?:raises|raised|secures|secured|bags|bagged|nets|netted|"
        r"closes|extends|picks\s+up|gets|lands|scores|mops\s+up)\b",
        t, re.I,
    )
    if m:
        return _clean_company(m.group(1).strip(" .,:"))

    # Pattern 2: "... in <Company>" or "... led by ... in <Company>"
    m = re.search(r"\bin\s+([A-Z][A-Za-z0-9.&\-\s]{1,40}?)(?:[.,;]|$)", t)
    if m:
        cand = m.group(1).strip()
        if len(cand.split()) <= 5:
            return _clean_company(cand)

    # Pattern 3: "<Company> set to raise..." (Entrackr leak pattern)
    m = re.match(r"^(.+?)\s+set\s+to\s+raise\b", t, re.I)
    if m:
        return _clean_company(m.group(1).strip(" .,:"))

    # Fallback: chunk before the first verb-ish word.
    return _clean_company(t.split(" raises")[0].split(" set to ")[0].strip(" .,:")[:60])


def _clean_company(cand: str) -> str:
    """Drop leading sector/descriptor tokens so 'D2C footwear brand Yoho' → 'Yoho',
    while keeping lowercase-styled brands ('slice', 'upGrad') and multiword names
    ('Leverage Edu'). Never strips the final token."""
    tokens = cand.split()
    if len(tokens) <= 1:
        return cand
    i = 0
    while i < len(tokens) - 1:
        tok = tokens[i].strip(".,").lower()
        # Drop if it's a known descriptor, or a non-capitalized connective word.
        if tok in DESCRIPTOR_TOKENS or (tokens[i][:1].islower() and tok not in ("of", "the", "and")):
            i += 1
        else:
            break
    return " ".join(tokens[i:]).strip(" .,:")


def parse_deal(item: dict) -> dict | None:
    """Map an RSS item → deal dict, or None if it's not a funding story."""
    title = item.get("title", "")
    # Skip VC-fund-launch / fund-close announcements — those are fund managers, not startups.
    if VC_FUND_NOISE_RE.search(title):
        return None
    stage = _extract_stage(title + " " + item.get("description", ""))
    if not stage:
        return None
    amount = _extract_amount(title) or _extract_amount(item.get("description", ""))
    company = _extract_company(title)
    if not company or len(company) < 2:
        return None
    return {
        "company": company,
        "stage": stage,
        "amount": amount,
        "title": title,
        "link": item.get("link", ""),
        "pub_date_raw": item.get("pub_date", ""),
        "pub_date": _parse_pub_date(item.get("pub_date", "")),
    }


# ---------- Apify LinkedIn search ----------


def _build_search_url(keywords: str, location: str) -> str:
    """LinkedIn search URL — past 30 days, sorted by date."""
    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": "r2592000",  # 30 days
        "sortBy": "DD",        # date descending
    }
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def search_linkedin_jobs(*, keywords: str, location: str, count: int, token: str) -> list[dict]:
    payload: dict[str, Any] = {
        "urls": [_build_search_url(keywords, location)],
        "count": max(1, min(int(count), 50)),
        "scrapeCompany": False,
    }
    url = f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?token={token}"
    with httpx.Client(timeout=240.0, verify=_CA) as c:
        r = c.post(url, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Apify {APIFY_ACTOR_ID} → {r.status_code}: {r.text[:300]}"
            )
        items = r.json()
        if not isinstance(items, list):
            raise RuntimeError(f"Apify returned non-list: {type(items).__name__}")
        return items


def _normalize_job(raw: dict) -> dict:
    def first(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                name = v.get("name") or v.get("title")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return ""

    return {
        "title": first("title", "jobTitle", "position"),
        "company": first("companyName", "company", "companyTitle"),
        "location": first("location", "jobLocation"),
        "link": first("link", "jobUrl", "url"),
        "description": first("descriptionText", "description", "snippet")[:1200],
        "posted": first("postedAt", "postedTime", "datePosted"),
        "seniority": first("seniorityLevel", "experienceLevel"),
    }


# ---------- Cross-reference + scoring ----------


def _norm_company(s: str) -> str:
    s = s.lower().strip()
    # Strip common corp suffixes for matching
    s = re.sub(r"\b(pvt\.?|private|ltd\.?|limited|inc\.?|llc|llp|technologies|labs|labs\.?)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _company_title_regex(name: str) -> re.Pattern | None:
    """Word-boundary phrase regex for spotting a funded company's name inside a
    job title (covers recruiter posts like 'Yes Madam - Director' where the
    LinkedIn company field is the agency, not the startup). Skips names with
    fewer than 4 alphanumerics to avoid false positives on short tokens."""
    core = re.sub(r"[^A-Za-z0-9 ]", " ", name).strip()
    core = re.sub(r"\s+", " ", core)
    if len(core.replace(" ", "")) < 4:
        return None
    pat = r"\b" + r"\s+".join(re.escape(w) for w in core.split()) + r"\b"
    return re.compile(pat, re.I)


def cross_reference(deals: list[dict], jobs: list[dict]) -> list[dict]:
    """Per-deal match list. A senior-PM job matches a funded company when the
    job's company field normalizes to the same name, OR the company's name
    appears as a phrase in the job title."""
    senior_jobs = [j for j in jobs if SENIOR_PM_TITLE_RE.search(j.get("title", ""))]

    by_norm: dict[str, list[dict]] = {}
    for j in senior_jobs:
        key = _norm_company(j.get("company", ""))
        if key:
            by_norm.setdefault(key, []).append(j)

    matched: list[dict] = []
    for d in deals:
        key = _norm_company(d["company"])
        if not key or len(key) < 4:
            continue
        hits: list[dict] = list(by_norm.get(key, []))

        title_re = _company_title_regex(d["company"])
        if title_re:
            for j in senior_jobs:
                if title_re.search(j.get("title", "")):
                    hits.append(j)

        # Dedupe hits by link (title-match + company-match can overlap)
        seen: set[str] = set()
        uniq: list[dict] = []
        for j in hits:
            lk = j.get("link") or j.get("title") or ""
            if lk in seen:
                continue
            seen.add(lk)
            uniq.append(j)

        if uniq:
            matched.append({**d, "jobs": uniq})
    return matched


STAGE_WEIGHT = {
    "pre-seed": 10,
    "seed": 15,
    "series_a": 30,
    "series_b": 35,
    "series_c": 30,
}


def score_match(match: dict) -> int:
    score = STAGE_WEIGHT.get(match["stage"], 5)
    # More jobs = more signal company is hiring product leadership broadly
    score += min(15, 5 * len(match["jobs"]))
    # Recency boost — funded in last 30 days
    pd = match.get("pub_date")
    if pd and (datetime.now(timezone.utc) - pd).days <= 30:
        score += 10
    # Title strength — count of "senior" markers
    senior_markers = ("director", "head of", "principal", "avp", "group")
    has_senior = any(
        m in (j.get("title", "").lower())
        for j in match["jobs"]
        for m in senior_markers
    )
    if has_senior:
        score += 15
    return score


# ---------- Markdown writers ----------


def _slugify(s: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:max_len] or "run"


def _stage_label(stage: str) -> str:
    return {
        "pre-seed": "Pre-Seed",
        "seed": "Seed",
        "series_a": "Series A",
        "series_b": "Series B",
        "series_c": "Series C",
    }.get(stage, stage)


def render_markdown(
    *,
    timestamp: str,
    window_days: int,
    deals_count: int,
    jobs_scanned: int,
    matched: list[dict],
    company_cap: int,
    unmatched_deals: list[dict],
) -> str:
    parts = [
        f"# Startup Lookup — {timestamp}",
        "",
        f"- **Window:** last {window_days} days (RSS coverage)",
        f"- **Funding deals found:** {deals_count}",
        f"- **LinkedIn senior PM jobs scanned:** {jobs_scanned}",
        f"- **Companies with funding × senior PM match:** {len(matched)}",
        f"- **Top N rendered:** {min(company_cap, len(matched))}",
        "",
        "> Each match below is a recently-funded Indian startup currently hiring at",
        "> Group PM, Product Lead, Director/Head of Product, or Principal/AVP Product level.",
        "> The 'why you're a fit' line is heuristic — for an LLM-quality rewrite,",
        "> run `rank startup lookup latest` in a Claude Code session.",
        "",
        "---",
        "",
    ]

    if not matched:
        parts.append(
            "## No matches this run\n\n"
            "Either no funded companies are publicly hiring senior PM roles right now, "
            "or the company-name match didn't connect (LinkedIn often uses a parent-brand "
            "name where Entrackr uses the product name, e.g. 'Slice' vs 'Garagepreneurs').\n\n"
            "Unmatched fresh raises (for manual review):\n"
        )
        for d in unmatched_deals[:15]:
            parts.append(
                f"- **{d['company']}** · {_stage_label(d['stage'])}"
                f"{(' · ' + d['amount']) if d['amount'] else ''}"
                f"{(' · ' + d['pub_date_raw'][:16]) if d.get('pub_date_raw') else ''}"
                f"{('  ' + d['link']) if d.get('link') else ''}"
            )
        return "\n".join(parts) + "\n"

    for i, m in enumerate(matched[:company_cap], 1):
        date_str = m["pub_date_raw"][:25] if m.get("pub_date_raw") else "unknown date"
        parts.append(f"## {i}. {m['company']} — {_stage_label(m['stage'])}")
        parts.append("")
        parts.append(f"- **Funding:** {_stage_label(m['stage'])}"
                     f"{(' · ' + m['amount']) if m['amount'] else ''} · {date_str}")
        parts.append(f"- **Score:** {m['score']}/100")
        parts.append(f"- **Source:** {m['link'] or '(no link)'}")
        parts.append("")
        parts.append("### Open senior PM roles")
        for j in m["jobs"]:
            posted = j.get("posted", "") or "recent"
            parts.append(
                f"- **{j['title']}** — {j.get('location', 'India')} · _{posted}_  \n"
                f"  {j.get('link', '')}"
            )
        parts.append("")
        parts.append("### Why this might be a fit (heuristic)")
        parts.append(_heuristic_fit_blurb(m))
        parts.append("")
        parts.append("---")
        parts.append("")

    if unmatched_deals:
        parts.append("## Other recent raises (no senior PM role detected on LinkedIn)")
        parts.append("")
        for d in unmatched_deals[:20]:
            parts.append(
                f"- **{d['company']}** · {_stage_label(d['stage'])}"
                f"{(' · ' + d['amount']) if d['amount'] else ''}"
                f"{(' · ' + d['pub_date_raw'][:16]) if d.get('pub_date_raw') else ''}"
            )

    return "\n".join(parts) + "\n"


def _heuristic_fit_blurb(match: dict) -> str:
    stage = _stage_label(match["stage"])
    n_roles = len(match["jobs"])
    titles = ", ".join(sorted({j["title"] for j in match["jobs"]}))
    return (
        f"{match['company']} is at **{stage}** — typically the stage where a "
        f"product-leadership hire defines the next 12-18 months of roadmap. "
        f"They have {n_roles} open senior PM role(s): _{titles}_. Fit depends on "
        f"sector (read the JD), but stage + role-level alignment is strong."
    )


# ---------- Agent ----------


class StartupLookupAgent(BaseAgent):
    name = "startup_lookup"
    description = (
        "Pulls India funding RSS (Entrackr + YourStory), filters by stage + window, "
        "runs ONE Apify LinkedIn search for senior PM roles in India, and cross-references "
        "the two lists. No Claude API call — pure data work. Output is a markdown bundle "
        "with each funded-company × open-role match, plus unmatched raises for manual review."
    )
    workflow_steps = [
        WorkflowStep(
            "fetch_funding",
            "Fetch India funding RSS",
            "Entrackr + YourStory funding feeds, no API key.",
        ),
        WorkflowStep(
            "parse_deals",
            "Parse + filter deals",
            "Extract company/stage/amount/date from titles; filter by stage + window.",
        ),
        WorkflowStep(
            "search_jobs",
            "Apify LinkedIn search",
            "One broad search for senior PM roles in India (past 30d).",
        ),
        WorkflowStep(
            "match_and_write",
            "Cross-reference + write bundle",
            "Match funded companies × open roles, score, write markdown artifact.",
        ),
    ]

    def _run(
        self,
        task: str,
        window_days: int = 14,
        stages: tuple[str, ...] = ("pre-seed", "seed", "series_a", "series_b", "series_c"),
        company_cap: int = 10,
        jobs_count: int = 40,
        location: str = "India",
    ) -> dict:
        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            raise RuntimeError("APIFY_TOKEN not set in .env.")

        stages_set = set(stages) & ALLOWED_STAGES
        if not stages_set:
            raise ValueError(f"No valid stages selected; allowed: {sorted(ALLOWED_STAGES)}")

        # 1. Fetch RSS
        self.step("fetch_funding", detail=f"sources={len(RSS_SOURCES)}")
        raw_items: list[dict] = []
        for src_name, src_url in RSS_SOURCES:
            try:
                items = fetch_rss(src_url)
                self.log(f"  {src_name}: {len(items)} items")
                for it in items:
                    it["_source"] = src_name
                raw_items.extend(items)
            except Exception as exc:
                self.log(f"  {src_name} failed: {exc}", level="warning")

        # 2. Parse + filter
        self.step("parse_deals", detail=f"raw={len(raw_items)}")
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        deals: list[dict] = []
        for it in raw_items:
            d = parse_deal(it)
            if not d or d["stage"] not in stages_set:
                continue
            if d["pub_date"] and d["pub_date"] < cutoff:
                continue
            deals.append(d)
        # Dedupe by (company, stage) — same raise reported by multiple feeds
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for d in deals:
            key = (_norm_company(d["company"]), d["stage"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(d)
        deals = sorted(deduped, key=lambda d: d.get("pub_date") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        self.log(f"Parsed {len(deals)} fresh India deals across {sorted(stages_set)}")

        # 3. Apify LinkedIn search — ONE broad query
        self.step("search_jobs", detail=f"location={location} count={jobs_count}")
        keywords = " OR ".join(f'"{k}"' for k in SENIOR_PM_KEYWORDS)
        try:
            raw_jobs = search_linkedin_jobs(
                keywords=keywords,
                location=location,
                count=jobs_count,
                token=token,
            )
        except Exception as exc:
            self.log(f"Apify search failed: {exc}", level="error")
            raw_jobs = []
        jobs = [_normalize_job(j) for j in raw_jobs if isinstance(j, dict)]
        jobs = [j for j in jobs if j.get("title")]
        self.log(f"Apify returned {len(jobs)} jobs (post-normalize).")

        # 4. Match + score + write
        self.step("match_and_write", detail=f"deals={len(deals)} jobs={len(jobs)}")
        matched = cross_reference(deals, jobs)
        for m in matched:
            m["score"] = score_match(m)
        matched.sort(key=lambda m: m["score"], reverse=True)

        matched_companies = {_norm_company(m["company"]) for m in matched}
        unmatched = [d for d in deals if _norm_company(d["company"]) not in matched_companies]

        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slugify(f"india-{'-'.join(sorted(stages_set))}")
        out_path = OUTPUTS_DIR / f"{timestamp}-{slug}.md"
        md = render_markdown(
            timestamp=timestamp,
            window_days=window_days,
            deals_count=len(deals),
            jobs_scanned=len(jobs),
            matched=matched,
            company_cap=company_cap,
            unmatched_deals=unmatched,
        )
        out_path.write_text(md)
        self.log(f"Wrote {out_path.name} ({len(md)} chars).")

        artifact_id = self.record_artifact(
            kind="startup_lookup_run",
            title=f"Startups — {len(matched)} match(es) · {len(deals)} deal(s)",
            path=str(out_path),
            meta={
                "window_days": window_days,
                "stages": sorted(stages_set),
                "deals_count": len(deals),
                "jobs_scanned": len(jobs),
                "matched_count": len(matched),
                "unmatched_count": len(unmatched),
                "company_cap": company_cap,
                "actor": APIFY_ACTOR_ID,
                "top_matches": [
                    {"company": m["company"], "stage": m["stage"], "score": m["score"], "n_jobs": len(m["jobs"])}
                    for m in matched[:company_cap]
                ],
            },
        )
        return {
            "path": str(out_path),
            "artifact_id": artifact_id,
            "deals": len(deals),
            "jobs": len(jobs),
            "matched": len(matched),
        }
