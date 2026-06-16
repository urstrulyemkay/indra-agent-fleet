"""job_hunter — Apify LinkedIn → ranked jobs + tailored CVs.

Flow:
  1. POST to Apify's run-sync-get-dataset-items endpoint for a LinkedIn
     jobs scraper actor; block until results return.
  2. Read master-profile.json from the sibling cv-builder project.
  3. Single Claude call: rank every scraped job, then write a 2-page
     tailored CV markdown for the top `tailor_limit` matches.
  4. Parse the markdown into per-job folders under
     `<cv builder>/jobs/<slug>/` (jd.md + tailored-cv.md). Save the
     full run as an artifact on the Indra side too.

No auto-apply. Drafts only. Honors the hard CV rules in
`cv builder/CLAUDE.md` via the system prompt.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from core import langfuse_integration as lf
from core.base_agent import BaseAgent, WorkflowStep

from .prompts import build_system_prompt, build_user_message


OUTPUTS_DIR = Path(os.getenv("OUTPUTS_DIR", "./outputs")) / "job_hunter"

CV_BUILDER_DIR = Path(os.getenv("CV_BUILDER_DIR", "./cv-builder"))

DEFAULT_APIFY_ACTOR = "curious_coder~linkedin-jobs-scraper"
APIFY_ACTOR_ID = os.getenv("APIFY_LINKEDIN_ACTOR_ID", DEFAULT_APIFY_ACTOR)
APIFY_BASE = "https://api.apify.com/v2"

DEFAULT_WELLFOUND_ACTOR = "orgupdate~wellfound-jobs-scraper"
WELLFOUND_ACTOR_ID = os.getenv("APIFY_WELLFOUND_ACTOR_ID", DEFAULT_WELLFOUND_ACTOR)

ALLOWED_REGIONS = ("india", "europe")

# Sources the agent knows how to crawl. TrueUp is intentionally excluded —
# their robots.txt explicitly disallows AI scrapers (GPTBot, anthropic-ai,
# ChatGPT-User, PerplexityBot) and their /jobs.json endpoint returns 403.
# TrueUp aggregates from LinkedIn + ATS feeds (Greenhouse, Lever, Ashby) which
# we can reach directly, so the content overlap is already covered.
SOURCES = ("linkedin", "wellfound")

# Daily output budget — operator opted on 2026-05-21 for a 10-jobs/day cap
# split 7 India / 3 Outside. Within each geo, both sources (LinkedIn + Wellfound)
# are queried, deduped, then capped. This keeps the dashboard scannable and
# Apify spend trivial (~$0.20/day even on dual-source dual-geo runs).
DAILY_JOB_CAP = 10
DAILY_INDIA_CAP = 7
DAILY_OUTSIDE_CAP = 3
DEFAULT_OUTSIDE_LOCATION = "European Union"
PER_SOURCE_DEFAULT = 5  # used inside each geo before dedup


# ---------- Apify ----------


def _build_linkedin_search_url(target_role: str, location: str, keywords: str) -> str:
    """LinkedIn jobs search URL — what curious_coder~linkedin-jobs-scraper expects.

    Combines role + extra keywords into the `keywords` query param. f_TPR=r604800
    limits to "posted in the last week" so the same actor run doesn't return
    stale postings we've already seen.
    """
    parts = [p for p in [target_role.strip(), keywords.strip()] if p]
    q = " ".join(parts) or "product manager"
    params = {"keywords": q, "f_TPR": "r604800"}
    if location.strip():
        params["location"] = location.strip()
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def _apify_run_linkedin_scraper(
    *,
    target_role: str,
    location: str,
    keywords: str,
    scrape_limit: int,
    token: str,
    timeout: float = 240.0,
) -> list[dict]:
    """Block until the LinkedIn jobs scraper finishes; return dataset items.

    Uses run-sync-get-dataset-items so we don't poll. Default actor
    `curious_coder~linkedin-jobs-scraper` is pay-per-result ($0.001/job) and
    accepts {urls[], count, scrapeCompany}. If you set APIFY_LINKEDIN_ACTOR_ID
    to a different actor with a different input shape, swap the payload here.
    """
    search_url = _build_linkedin_search_url(target_role, location, keywords)
    payload: dict[str, Any] = {
        "urls": [search_url],
        "count": max(1, min(int(scrape_limit), 25)),
        "scrapeCompany": False,
    }

    url = f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?token={token}"
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Apify actor {APIFY_ACTOR_ID} returned {r.status_code}: {r.text[:300]}"
            )
        items = r.json()
        if not isinstance(items, list):
            raise RuntimeError(
                f"Apify dataset returned non-list payload: {type(items).__name__}"
            )
        return items


def _normalize_job(raw: dict, source: str = "linkedin") -> dict:
    """Normalize a raw job dict from any of the supported Apify actors.

    Handles field-name variation across LinkedIn (curious_coder, bebity, voyager)
    and Wellfound (orgupdate) actors. Returns a flat dict with the fields the
    prompt and downstream parsing need, plus a `source` discriminator.
    """
    def first(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                # bebity nests company under {"name": ..., "url": ...}
                name = v.get("name") or v.get("title")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return ""

    return {
        "source": source,
        "title": first("title", "jobTitle", "job_title", "position"),
        "company": first("companyName", "company", "company_name", "companyTitle"),
        "location": first("location", "jobLocation", "place"),
        "link": first("link", "jobUrl", "url", "URL", "applyUrl"),
        "description": first(
            "descriptionText", "description", "jobDescription", "snippet", "descriptionHtml"
        ),
        "posted": first("postedAt", "postedTime", "publishedAt", "datePosted", "date"),
        "seniority": first("seniorityLevel", "experienceLevel"),
        "employment_type": first("employmentType", "jobType"),
        "salary": first("salary", "salaryRange", "compensation"),
    }


def _apify_run_wellfound_scraper(
    *,
    target_role: str,
    location: str,
    keywords: str,
    scrape_limit: int,
    token: str,
    timeout: float = 240.0,
) -> list[dict]:
    """Scrape Wellfound (startup-jobs platform).

    Uses orgupdate/wellfound-jobs-scraper by default — PRICE_PER_DATASET_ITEM
    at $0.015/job, returns rich fields (title, company, location, URL,
    description, salary). Input shape is country + city + keyword + pagesToFetch.
    The actor over-returns by design (one page can yield 40-60 items); we
    let the caller slice the result down to `scrape_limit`.
    """
    # Wellfound expects city-level locationName + countryName separately.
    # Crude geo-split: if location looks like a known Indian city, set
    # countryName="India"; otherwise pass it through as-is.
    loc = (location or "").strip()
    indian_cities = ("bangalore", "bengaluru", "mumbai", "delhi", "gurugram",
                     "gurgaon", "noida", "pune", "hyderabad", "chennai", "kolkata")
    country = "India" if loc.lower() in indian_cities else (loc or "United States")
    city = loc if loc.lower() in indian_cities else loc

    title_parts = [p for p in [target_role.strip(), keywords.strip()] if p]
    keyword_query = " ".join(title_parts) or "product manager"

    payload: dict[str, Any] = {
        "countryName": country,
        "locationName": city or country,
        "includeKeyword": keyword_query,
        "pagesToFetch": 1,  # 1 page returns 40-60 results; cheap and enough
    }

    url = f"{APIFY_BASE}/acts/{WELLFOUND_ACTOR_ID}/run-sync-get-dataset-items?token={token}"
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Apify actor {WELLFOUND_ACTOR_ID} returned {r.status_code}: {r.text[:300]}"
            )
        items = r.json()
        if not isinstance(items, list):
            raise RuntimeError(
                f"Wellfound dataset returned non-list payload: {type(items).__name__}"
            )
        # Cap to scrape_limit on this side (the actor returns whole pages).
        return items[: max(1, min(int(scrape_limit), 50))]


def _dedupe_jobs(jobs: list[dict]) -> list[dict]:
    """Drop duplicate postings across sources by (company, title) lowercased.

    Keep first occurrence (so LinkedIn wins over Wellfound on ties if LinkedIn
    runs first, which it should — LinkedIn descriptions tend to be longer and
    standardized). Returns the deduped list in original order.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for j in jobs:
        key = (j.get("company", "").lower().strip(), j.get("title", "").lower().strip())
        if not any(key) or key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def scrape_daily_run(
    *,
    india_role: str,
    outside_role: str,
    keywords: str,
    token: str,
    india_location: str = "Bangalore",
    outside_location: str = DEFAULT_OUTSIDE_LOCATION,
    india_cap: int = DAILY_INDIA_CAP,
    outside_cap: int = DAILY_OUTSIDE_CAP,
    sources: tuple[str, ...] = SOURCES,
) -> dict:
    """One day's worth: 7 India + 3 outside, both geos hitting both sources.

    Each geo's results are deduped within-geo, capped, then combined into a
    single list. Returns:
      {"jobs": [...], "by_geo": {"india": [...], "outside": [...]},
       "by_source_geo": {("linkedin", "india"): n, ...}}
    """
    india_jobs = scrape_both_sources(
        target_role=india_role,
        location=india_location,
        keywords=keywords,
        token=token,
        per_source=PER_SOURCE_DEFAULT,
        daily_cap=india_cap,
        sources=sources,
    )
    outside_jobs = scrape_both_sources(
        target_role=outside_role,
        location=outside_location,
        keywords=keywords,
        token=token,
        per_source=PER_SOURCE_DEFAULT,
        daily_cap=outside_cap,
        sources=sources,
    )
    # Tag geo on each job
    for j in india_jobs:
        j["geo"] = "india"
    for j in outside_jobs:
        j["geo"] = "outside"

    combined = _dedupe_jobs(india_jobs + outside_jobs)
    breakdown: dict[tuple[str, str], int] = {}
    for j in combined:
        key = (j.get("source", "?"), j.get("geo", "?"))
        breakdown[key] = breakdown.get(key, 0) + 1

    return {
        "jobs": combined[:DAILY_JOB_CAP],
        "by_geo": {"india": india_jobs, "outside": outside_jobs},
        "by_source_geo": breakdown,
    }


def scrape_both_sources(
    *,
    target_role: str,
    location: str,
    keywords: str,
    token: str,
    per_source: int = PER_SOURCE_DEFAULT,
    daily_cap: int = DAILY_JOB_CAP,
    sources: tuple[str, ...] = SOURCES,
) -> list[dict]:
    """Scrape both LinkedIn and Wellfound, normalize, dedupe, cap to daily_cap.

    LinkedIn's curious_coder actor requires count >= 10 — we call with 10 then
    slice down to `per_source`. Wellfound returns whole pages (40-60) — sliced
    inside `_apify_run_wellfound_scraper`.

    Each returned job dict carries a `source` field ("linkedin" or "wellfound")
    so the dashboard / consumer can show which feed it came from.
    """
    all_jobs: list[dict] = []

    if "linkedin" in sources:
        # Min count is 10 for curious_coder; ask for 10, slice down.
        raw_li = _apify_run_linkedin_scraper(
            target_role=target_role,
            location=location,
            keywords=keywords,
            scrape_limit=10,
            token=token,
        )
        li_jobs = [_normalize_job(it, source="linkedin") for it in raw_li if isinstance(it, dict)]
        li_jobs = [j for j in li_jobs if j.get("title") and j.get("description")]
        all_jobs.extend(li_jobs[:per_source])

    if "wellfound" in sources:
        try:
            raw_wf = _apify_run_wellfound_scraper(
                target_role=target_role,
                location=location,
                keywords=keywords,
                scrape_limit=per_source * 3,  # over-fetch so dedup has headroom
                token=token,
            )
            wf_jobs = [_normalize_job(it, source="wellfound") for it in raw_wf if isinstance(it, dict)]
            wf_jobs = [j for j in wf_jobs if j.get("title") and j.get("description")]
            all_jobs.extend(wf_jobs[:per_source])
        except Exception as e:
            # Wellfound is the secondary source — if it fails (rate limit, actor
            # outage), don't kill the whole run. Log and continue with LinkedIn.
            print(f"[job_hunter] Wellfound scrape failed: {e}. Continuing with LinkedIn only.")

    deduped = _dedupe_jobs(all_jobs)
    return deduped[:daily_cap]


def _format_jobs_for_claude(jobs: list[dict]) -> str:
    if not jobs:
        return "(no jobs returned)"
    blocks: list[str] = []
    for i, j in enumerate(jobs, start=1):
        desc = j["description"]
        if len(desc) > 1800:
            desc = desc[:1800].rstrip() + "..."
        blocks.append(
            f"## Posting {i}\n"
            f"- title: {j['title'] or '(unknown)'}\n"
            f"- company: {j['company'] or '(unknown)'}\n"
            f"- location: {j['location'] or '(unknown)'}\n"
            f"- seniority: {j['seniority'] or '(unspecified)'}\n"
            f"- employment_type: {j['employment_type'] or '(unspecified)'}\n"
            f"- posted: {j['posted'] or '(unknown)'}\n"
            f"- link: {j['link'] or '(none)'}\n\n"
            f"{desc or '(no description text)'}"
        )
    return "\n\n---\n\n".join(blocks)


# ---------- Master profile ----------


def _load_master_profile() -> tuple[str, Path | None]:
    """Return (json_text, source_path). Falls back to a compact placeholder if missing."""
    candidate = CV_BUILDER_DIR / "profile" / "master-profile.json"
    if not candidate.exists():
        placeholder = json.dumps({
            "_warning": (
                f"master-profile.json not found at {candidate}. "
                "Set CV_BUILDER_DIR env var or place the file. Using minimal fallback."
            ),
            "identity": {
                "full_name": "Mani Kumar Jami",
                "current_title": "AVP of CX and AI Products",
                "current_employer": "TVS Group (DriveX)",
                "location": "Bengaluru",
                "years_experience": "10+",
            },
        }, indent=2)
        return placeholder, None
    raw = candidate.read_text()
    # Validate it parses; if not, hand the raw text to Claude anyway and warn
    try:
        parsed = json.loads(raw)
        return json.dumps(parsed, indent=2), candidate
    except json.JSONDecodeError:
        return raw, candidate


# ---------- Output parsing ----------


_JOB_HEADER_RE = re.compile(r"^##\s+Job\s+(\d+):\s*(.+?)\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"\*\*Slug:\*\*\s*`?([a-z0-9][a-z0-9\-]*)`?", re.IGNORECASE)
_FIT_RE = re.compile(r"\*\*Fit score:\*\*\s*(\d+)\s*/\s*100", re.IGNORECASE)


def _extract_fenced(after: str, lang: str | None = None) -> str:
    """Pull the body of the first ```lang ... ``` fence after position 0 of `after`.

    If `lang` is None, accepts any opening fence. Returns "" if no fence found.
    """
    if lang:
        fence_open = re.search(rf"```{re.escape(lang)}\s*\n", after)
    else:
        fence_open = re.search(r"```[a-zA-Z]*\s*\n", after)
    if not fence_open:
        return ""
    start = fence_open.end()
    close = after.find("```", start)
    if close == -1:
        return ""
    return after[start:close].rstrip()


def _section(chunk: str, *headings: str) -> tuple[int, int]:
    """Return (start, end) byte-offsets for the section under the first heading
    that exists in `chunk`. End is the next ###-heading start, or len(chunk).
    """
    for h in headings:
        pos = chunk.find(h)
        if pos != -1:
            # End at next "### " heading
            next_h = re.search(r"\n###\s+", chunk[pos + len(h):])
            end = (pos + len(h) + next_h.start()) if next_h else len(chunk)
            return pos, end
    return -1, -1


def _split_job_blocks(text: str) -> list[dict]:
    """Extract per-job blocks from the markdown output.

    Per block returned: {header, slug, fit, jd_md, tailored_cv_md,
                         patches_json, fit_report_md, referral_md}
    """
    matches = list(_JOB_HEADER_RE.finditer(text))
    blocks: list[dict] = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunk = text[start:end]

        slug_m = _SLUG_RE.search(chunk)
        fit_m = _FIT_RE.search(chunk)
        header_title = m.group(2).strip()
        slug = (slug_m.group(1).lower() if slug_m else _slugify(header_title))[:60]
        fit = int(fit_m.group(1)) if fit_m else 0

        # JD analysis: everything between "### JD Analysis" and the next ### heading
        jd_md = ""
        jd_pos, jd_end = _section(chunk, "### JD Analysis")
        if jd_pos != -1:
            jd_md = chunk[jd_pos:jd_end].rstrip()
        else:
            # Fallback: everything before first fenced block
            first_fence = chunk.find("```")
            jd_md = chunk[:first_fence].rstrip() if first_fence != -1 else chunk

        # Tailored CV: ```markdown ... ``` under "### Tailored CV"
        tailored_cv = ""
        cv_pos, cv_end = _section(chunk, "### Tailored CV")
        if cv_pos != -1:
            tailored_cv = _extract_fenced(chunk[cv_pos:cv_end], lang="markdown") \
                or _extract_fenced(chunk[cv_pos:cv_end])

        # CV Patches: ```json ... ``` under "### CV Patches"
        patches_json = ""
        cp_pos, cp_end = _section(chunk, "### CV Patches")
        if cp_pos != -1:
            patches_json = _extract_fenced(chunk[cp_pos:cp_end], lang="json") \
                or _extract_fenced(chunk[cp_pos:cp_end])

        # Fit Report: ```markdown ... ``` under "### Fit Report"
        fit_report_md = ""
        fr_pos, fr_end = _section(chunk, "### Fit Report")
        if fr_pos != -1:
            fit_report_md = _extract_fenced(chunk[fr_pos:fr_end], lang="markdown") \
                or _extract_fenced(chunk[fr_pos:fr_end])

        # Cold Note to Hiring Manager: ```markdown ... ``` under "### Cold Note to Hiring Manager"
        cold_note_md = ""
        cn_pos, cn_end = _section(
            chunk, "### Cold Note to Hiring Manager", "### Cold Note", "### Referral Message",
        )
        if cn_pos != -1:
            cold_note_md = _extract_fenced(chunk[cn_pos:cn_end], lang="markdown") \
                or _extract_fenced(chunk[cn_pos:cn_end])

        # Link line in the per-job header: "- **Link:** https://..."
        link_m = re.search(r"\*\*Link:\*\*\s*(\S+)", chunk)
        link = link_m.group(1).strip().strip("`<>") if link_m else ""

        blocks.append({
            "header": header_title,
            "slug": slug,
            "fit": fit,
            "link": link,
            "jd_md": jd_md,
            "tailored_cv_md": tailored_cv,
            "patches_json": patches_json,
            "fit_report_md": fit_report_md,
            "cold_note_md": cold_note_md,
        })
    return blocks


# ---------- data.json patch application ----------


def _load_sample_data(region: str) -> tuple[dict | None, Path | None]:
    """Return (sample-data.json contents, source path) for the given region.

    Falls back to india/ if region-specific sample doesn't exist (europe template
    is still on roadmap per cv builder/CLAUDE.md §5).
    """
    candidates = [
        CV_BUILDER_DIR / "templates" / region / "sample-data.json",
        CV_BUILDER_DIR / "templates" / "india" / "sample-data.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text()), p
            except json.JSONDecodeError:
                continue
    return None, None


def _apply_patches(sample: dict, patches: dict) -> dict:
    """Merge a small patches dict onto a copy of sample-data.json.

    Patch keys (all optional, agent tolerates missing ones):
      identity_title       → identity.title
      summary_primary      → summary.primary
      summary_accent       → summary.accent
      drivex_role_title    → experience[0].title  (DriveX is always experience[0])
      drivex_groups        → experience[0].groups (replaces wholesale)
    """
    out = copy.deepcopy(sample)

    if patches.get("identity_title"):
        out.setdefault("identity", {})["title"] = patches["identity_title"]
    if patches.get("summary_primary"):
        out.setdefault("summary", {})["primary"] = patches["summary_primary"]
    if patches.get("summary_accent"):
        out.setdefault("summary", {})["accent"] = patches["summary_accent"]

    exp = out.get("experience") or []
    if exp:
        if patches.get("drivex_role_title"):
            exp[0]["title"] = patches["drivex_role_title"]
        if isinstance(patches.get("drivex_groups"), list) and patches["drivex_groups"]:
            exp[0]["groups"] = patches["drivex_groups"]
    return out


def _slugify(s: str) -> str:
    s = s.lower().replace("—", "-").replace("–", "-")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "job"


# ---------- Agent ----------


class JobHunterAgent(BaseAgent):
    name = "job_hunter"
    description = (
        "Pulls fresh LinkedIn job postings via Apify, ranks them against the "
        "master profile in the sibling cv-builder project, and writes a tailored "
        "2-page CV markdown per top match directly into jobs/<slug>/. Drafts only — "
        "the operator ports each to data.json + renders the PDF manually."
    )
    workflow_steps = [
        WorkflowStep(
            "scrape_jobs",
            "Pull jobs via Apify",
            "Calls the LinkedIn jobs Apify actor with the operator's role + filters.",
        ),
        WorkflowStep(
            "load_profile",
            "Load master profile",
            "Reads master-profile.json from the cv-builder project.",
        ),
        WorkflowStep(
            "rank_and_tailor",
            "Rank jobs + draft CVs",
            "Single Claude call: scores every job, writes a full CV for the top N.",
        ),
        WorkflowStep(
            "write_artifacts",
            "Push files to cv-builder",
            "Writes jd.md + tailored-cv.md per top job into cv builder/jobs/<slug>/.",
        ),
    ]

    def _run(
        self,
        task: str,
        target_role: str,
        location: str = "",
        keywords: str = "",
        region: str = "india",
        scrape_limit: int = 15,
        tailor_limit: int = 5,
    ) -> dict:
        if region not in ALLOWED_REGIONS:
            raise ValueError(f"region must be one of {ALLOWED_REGIONS}, got {region!r}")
        scrape_limit = max(1, min(int(scrape_limit), 25))
        tailor_limit = max(1, min(int(tailor_limit), 10))
        if tailor_limit > scrape_limit:
            tailor_limit = scrape_limit

        token = os.getenv("APIFY_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "APIFY_TOKEN not set in .env. Get a free-tier token at apify.com and add it."
            )

        # 1. Scrape
        self.step(
            "scrape_jobs",
            detail=f"role={target_role!r} loc={location!r} n={scrape_limit}",
        )
        raw_items = _apify_run_linkedin_scraper(
            target_role=target_role,
            location=location,
            keywords=keywords,
            scrape_limit=scrape_limit,
            token=token,
        )
        jobs = [_normalize_job(it) for it in raw_items if isinstance(it, dict)]
        # Drop entries with neither title nor description — those are unusable
        jobs = [j for j in jobs if j["title"] or j["description"]]
        self.log(
            f"Apify returned {len(raw_items)} raw items → {len(jobs)} usable.",
            data={"actor": APIFY_ACTOR_ID},
        )
        if not jobs:
            self.log(
                "No usable jobs returned — finishing run with empty artifact.",
                level="warning",
            )

        # 2. Load profile
        self.step("load_profile")
        profile_json, profile_path = _load_master_profile()
        if profile_path is None:
            self.log(
                f"master-profile.json not found under {CV_BUILDER_DIR}/profile/ — "
                "using minimal fallback. Set CV_BUILDER_DIR if your cv-builder is elsewhere.",
                level="warning",
            )
        else:
            self.log(f"Loaded master profile from {profile_path}")

        # 3. Rank + tailor
        self.step("rank_and_tailor", detail=f"{len(jobs)} jobs → top {tailor_limit}")
        jobs_block = _format_jobs_for_claude(jobs)
        system, src, prompt_obj = lf.get_prompt(
            "job_hunter.system",
            fallback=build_system_prompt(),
        )
        self.log(f"System prompt source: {src}")
        user_msg = build_user_message(
            target_role=target_role,
            location=location,
            keywords=keywords,
            region=region,
            tailor_limit=tailor_limit,
            master_profile_json=profile_json,
            jobs_block=jobs_block,
        )
        result = self.claude.complete(
            system=system,
            user_message=user_msg,
            max_tokens=8000,
            temperature=0.7,
            trace_name="job_hunter.run",
            agent_name=self.name,
            trace_metadata={
                "target_role": target_role,
                "location": location,
                "region": region,
                "jobs_scraped": len(jobs),
                "tailor_limit": tailor_limit,
                "actor": APIFY_ACTOR_ID,
                "prompt_source": src,
            },
            trace_tags=["intelligence", "cv"],
            trace_prompt=prompt_obj,
        )
        self.log(
            f"Claude drafted {len(result.text)} chars · "
            f"in={result.input_tokens} (cached={result.cache_read_tokens}) "
            f"out={result.output_tokens}"
        )

        # 4. Write artifacts
        self.step("write_artifacts")
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        query_slug = _slugify(f"{target_role}-{location}-{region}")[:50] or "run"
        out_path = OUTPUTS_DIR / f"{timestamp}-{query_slug}.md"

        header = (
            f"# Job Hunter Run — {timestamp}\n\n"
            f"- **Target role:** {target_role or '(any)'}\n"
            f"- **Location:** {location or '(any)'}\n"
            f"- **Keywords:** {keywords or '(none)'}\n"
            f"- **Region template:** {region}\n"
            f"- **Apify actor:** `{APIFY_ACTOR_ID}`\n"
            f"- **Jobs scraped:** {len(jobs)}\n"
            f"- **CVs tailored:** up to {tailor_limit}\n"
            f"- **CV builder dir:** `{CV_BUILDER_DIR}`\n\n"
            "> Per-job folders written under `cv builder/jobs/<slug>/`.\n"
            "> Each has `jd.md` (analysis + ATS keywords) and `tailored-cv.md`.\n"
            "> Port to `data.json` and run `templates/render.py` for the PDF.\n\n"
            "---\n\n"
        )
        out_path.write_text(header + result.text)

        # Per-job files into the cv-builder project
        job_blocks = _split_job_blocks(result.text)
        jobs_dir = CV_BUILDER_DIR / "jobs"
        written: list[dict] = []
        skipped_no_cv = 0
        data_written = 0
        data_skipped_bad_json = 0

        sample_data, sample_path = _load_sample_data(region)
        if sample_data is None:
            self.log(
                f"sample-data.json not found under {CV_BUILDER_DIR}/templates/ — "
                "skipping data.json generation. Other files still written.",
                level="warning",
            )
        else:
            self.log(f"Using {sample_path.relative_to(CV_BUILDER_DIR)} as data.json base.")

        if jobs_dir.parent.exists():
            jobs_dir.mkdir(exist_ok=True)
            for jb in job_blocks:
                if not jb["tailored_cv_md"].strip():
                    skipped_no_cv += 1
                    continue
                target = jobs_dir / jb["slug"]
                target.mkdir(parents=True, exist_ok=True)

                # 1. jd.md (analysis + fit + source)
                (target / "jd.md").write_text(
                    f"# {jb['header']}\n\n"
                    f"- **Fit:** {jb['fit']}/100\n"
                    f"- **Source run:** `{out_path.name}`\n"
                    f"- **Apify actor:** `{APIFY_ACTOR_ID}`\n\n"
                    f"{jb['jd_md']}\n"
                )

                # 2. tailored-cv.md (markdown preview)
                (target / "tailored-cv.md").write_text(jb["tailored_cv_md"] + "\n")

                # 3. data.json (sample-data + Claude patches → render-ready)
                data_ok = False
                if sample_data is not None and jb["patches_json"].strip():
                    try:
                        patches = json.loads(jb["patches_json"])
                        merged = _apply_patches(sample_data, patches)
                        (target / "data.json").write_text(
                            json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
                        )
                        data_written += 1
                        data_ok = True
                    except json.JSONDecodeError as e:
                        data_skipped_bad_json += 1
                        self.log(
                            f"  ✗ {jb['slug']}: patches JSON malformed ({e}); "
                            "data.json not written. Markdown CV still saved.",
                            level="warning",
                        )

                # 4. fit-report.md (optional — only if Claude produced one)
                if jb["fit_report_md"].strip():
                    (target / "fit-report.md").write_text(jb["fit_report_md"] + "\n")

                # 5. cold-note.md (cold outreach to the hiring manager)
                if jb["cold_note_md"].strip():
                    (target / "cold-note.md").write_text(jb["cold_note_md"] + "\n")

                written.append({
                    "slug": jb["slug"],
                    "fit": jb["fit"],
                    "header": jb["header"],
                    "link": jb["link"],
                    "data_json": data_ok,
                    "fit_report": bool(jb["fit_report_md"].strip()),
                    "cold_note": bool(jb["cold_note_md"].strip()),
                })
                self.log(
                    f"  → cv builder/jobs/{jb['slug']}/ (fit {jb['fit']}"
                    f"{'· data.json' if data_ok else ''}"
                    f"{'· fit-report' if jb['fit_report_md'].strip() else ''}"
                    f"{'· cold-note' if jb['cold_note_md'].strip() else ''})",
                    data={"slug": jb["slug"]},
                )
        else:
            self.log(
                f"cv-builder dir not found at {CV_BUILDER_DIR} — skipping per-job file writes. "
                f"Set CV_BUILDER_DIR or symlink the project. Run artifact still saved.",
                level="warning",
            )

        if skipped_no_cv:
            self.log(
                f"{skipped_no_cv} job(s) in the markdown had no tailored-CV block — skipped per-job write.",
                level="warning",
            )
        if data_skipped_bad_json:
            self.log(
                f"{data_skipped_bad_json} job(s) had malformed patches JSON — data.json missing for those.",
                level="warning",
            )

        artifact_id = self.record_artifact(
            kind="job_hunter_run",
            title=f"Jobs — {target_role or 'any'} · {location or 'any'} · {len(written)} CV(s)",
            path=str(out_path),
            meta={
                "target_role": target_role,
                "location": location,
                "keywords": keywords,
                "region": region,
                "jobs_scraped": len(jobs),
                "cvs_written": len(written),
                "data_json_written": data_written,
                "tailor_limit": tailor_limit,
                "cv_builder_dir": str(CV_BUILDER_DIR),
                "actor": APIFY_ACTOR_ID,
                "model": result.model,
                "output_tokens": result.output_tokens,
                "top_jobs": written[:tailor_limit],
                "prompt_source": src,
            },
        )
        self.log(
            f"Saved run summary → {out_path.name} · {len(written)} CV(s) "
            f"· {data_written} data.json written.",
            data={"artifact_id": artifact_id},
        )

        return {
            "path": str(out_path),
            "artifact_id": artifact_id,
            "jobs_scraped": len(jobs),
            "cvs_written": len(written),
            "data_json_written": data_written,
            "top_jobs": written,
        }
