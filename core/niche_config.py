"""Content pillars and per-niche voice/source configuration.

Three pillars for the personal brand:
- behavioral_psychology: most mature pipeline (reuses content 2026/psychology format)
- technology: AI x society x daily life angle
- legal_tech: newer pillar; higher accuracy bar (citations required downstream)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Niche:
    key: str
    label: str
    default_tone: str
    voice_profile: str
    hook_archetypes: tuple[str, ...]
    research_sources: tuple[str, ...]
    accuracy_bar: str


NICHES: dict[str, Niche] = {
    "behavioral_psychology": Niche(
        key="behavioral_psychology",
        label="Behavioral Psychology",
        default_tone="Storytelling-first, protective framing for dark psych, evidence-aware",
        voice_profile=(
            "Speaks like a smart friend revealing hidden mechanics of the mind. "
            "Uses concrete scenarios over abstractions. Calls out manipulation to "
            "protect, never to teach manipulation. Empathetic but unflinching."
        ),
        hook_archetypes=(
            "Bold claim that reframes a familiar behavior",
            "Identity threat ('they're not your friend, they're your handler')",
            "Specific scenario you've lived ('if your partner does X...')",
            "Contradiction of common wisdom",
        ),
        research_sources=(
            "reddit.com/r/psychology",
            "reddit.com/r/socialskills",
            "Behavioral Scientist (RSS)",
            "BPS Research Digest",
            "Choiceology podcast",
        ),
        accuracy_bar="Evidence-aware; cite mechanism when claiming one. No fabricated studies.",
    ),
    "technology": Niche(
        key="technology",
        label="Technology",
        default_tone="Curiosity-driven, slightly contrarian, insider POV without jargon",
        voice_profile=(
            "Speaks like a builder who's seen the inside. Trades hype for "
            "first-principles. Names the thing nobody else is naming. Concrete "
            "examples over abstractions. Never sells; always reveals."
        ),
        hook_archetypes=(
            "Insider observation framed as a public secret",
            "Counter-intuitive technical truth",
            "Specific scenario ('the moment my AI agent stopped working...')",
            "Implication-first ('this changes how X works forever')",
        ),
        research_sources=(
            "Hacker News front page",
            "ArXiv cs.AI and cs.CL recent",
            "Lobsters",
            "GitHub trending",
            "Latent Space podcast",
        ),
        accuracy_bar="Technical claims must be verifiable. Distinguish opinion from fact.",
    ),
    "legal_tech": Niche(
        key="legal_tech",
        label="Legal Tech",
        default_tone="Precise, authoritative, practical. Plain English over legalese.",
        voice_profile=(
            "Speaks like a practitioner translating the law for builders. Names "
            "specific risks and specific tools. Never gives legal advice; explains "
            "frameworks. Cites jurisdictions when relevant. Skeptical of hype."
        ),
        hook_archetypes=(
            "Specific risk most builders don't know about",
            "Tool capability that changes practitioner workflow",
            "Regulation moment with concrete consequences",
            "Misconception correction ('AI contracts don't actually...')",
        ),
        research_sources=(
            "Above the Law",
            "LawSites blog",
            "Legaltech News",
            "Artificial Lawyer",
            "Stanford CodeX",
        ),
        accuracy_bar=(
            "HIGH: Every legal claim cites jurisdiction and source. "
            "Never legal advice. Use 'generally' and 'in many jurisdictions' liberally. "
            "Flag any claim that needs a lawyer review."
        ),
    ),
}


def get_niche(key: str) -> Niche:
    if key not in NICHES:
        raise ValueError(f"Unknown niche '{key}'. Valid: {list(NICHES)}")
    return NICHES[key]
