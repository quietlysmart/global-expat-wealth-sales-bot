#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import get_settings
from backend.app.db import Database
from backend.app.kb import RetrievalService


TOPICS = [
    "independent financial advice for expats in Asia",
    "retirement planning and pension consolidation",
    "beneficiary trust and estate planning",
    "life assurance and critical illness cover",
    "tax planning and wealth preservation",
    "second citizenship and visa planning",
]


def main() -> None:
    settings = get_settings()
    db = Database(settings.database_file)
    db.init()
    retrieval = RetrievalService(db, settings)

    out_lines = ["# Draft Sales Playbook + FAQ Seed", "", "## FAQ Draft"]

    for topic in TOPICS:
        results = retrieval.retrieve(topic, top_k=2)
        if not results:
            continue
        out_lines.append(f"### {topic.title()}")
        out_lines.append("Suggested answer (draft):")
        out_lines.append(
            "- Share a short, general explanation and invite a 30-minute call with Dan for personal guidance."
        )
        out_lines.append("Evidence snippets:")
        for r in results:
            locator = f"page {r.page}" if r.page else r.section or "section"
            out_lines.append(f"- [{r.source_name} | {locator}] {r.text[:220]}...")
        out_lines.append("")

    output = Path("data/research_mode_faq_draft.md")
    output.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"[done] wrote {output}")


if __name__ == "__main__":
    main()
