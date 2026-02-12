#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.config import get_settings
from backend.app.db import Database
from backend.app.kb import IngestionService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest sources into local Sales Concierge knowledge base")
    parser.add_argument("--pdf", action="append", default=[], help="Path to a PDF. Can be used multiple times.")
    parser.add_argument("--pdf-dir", default="data/raw", help="Directory to scan for PDFs")
    parser.add_argument("--scan-pdf-dir", action="store_true", help="Also scan --pdf-dir for PDFs")
    parser.add_argument("--web", help="Website start URL for same-domain crawl")
    parser.add_argument("--max-pages", type=int, default=25, help="Max pages to crawl")
    parser.add_argument("--text-file", help="Path to a UTF-8 text file to ingest")
    parser.add_argument("--text-title", default="manual_text", help="Title label for raw text source")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()

    db = Database(settings.database_file)
    db.init()
    ingestion = IngestionService(db, settings)

    total_chunks = 0

    pdf_paths = [Path(p) for p in args.pdf]
    if args.scan_pdf_dir:
        pdf_dir = Path(args.pdf_dir)
        if pdf_dir.exists():
            pdf_paths.extend(pdf_dir.glob("*.pdf"))

    seen: set[Path] = set()
    for path in pdf_paths:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        chunks = ingestion.ingest_pdf(str(resolved))
        total_chunks += chunks
        print(f"[ingest] pdf={resolved} chunks={chunks}")

    if args.web:
        chunks = ingestion.ingest_website(args.web, max_pages=args.max_pages)
        total_chunks += chunks
        print(f"[ingest] web={args.web} chunks={chunks}")

    if args.text_file:
        text_path = Path(args.text_file)
        text = text_path.read_text(encoding="utf-8")
        chunks = ingestion.ingest_raw_text(text, title=args.text_title)
        total_chunks += chunks
        print(f"[ingest] text={text_path} chunks={chunks}")

    print(f"[done] total chunks added: {total_chunks}")


if __name__ == "__main__":
    main()
