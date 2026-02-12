from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import Settings
from .db import Database
from .models import Citation

TOKEN_RE = re.compile(r"[a-zA-Z0-9']+")


@dataclass
class RetrievalResult:
    text: str
    source_type: str
    source_name: str
    source_ref: str
    page: int | None
    section: str | None
    score: float


class Vectorizer:
    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, text: str) -> dict[str, float]:
        tokens = TOKEN_RE.findall(text.lower())
        counts: dict[int, float] = {}
        for token in tokens:
            h = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            counts[idx] = counts.get(idx, 0.0) + 1.0

        if not counts:
            return {}

        norm_sq = sum(v * v for v in counts.values())
        if norm_sq == 0:
            return {}
        norm = norm_sq**0.5
        return {str(k): v / norm for k, v in counts.items()}

    @staticmethod
    def cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
        if not vec_a or not vec_b:
            return 0.0
        # Iterate on smaller dict for efficiency.
        if len(vec_a) > len(vec_b):
            vec_a, vec_b = vec_b, vec_a
        dot = 0.0
        for key, a_val in vec_a.items():
            b_val = vec_b.get(key)
            if b_val:
                dot += a_val * b_val
        return float(dot)


class Chunker:
    def __init__(self, chunk_size: int, overlap: int) -> None:
        self.chunk_size = chunk_size
        self.overlap = overlap

    def split(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return []

        chunks: list[str] = []
        start = 0
        text_len = len(cleaned)
        while start < text_len:
            end = min(start + self.chunk_size, text_len)
            if end < text_len:
                next_space = cleaned.rfind(" ", start, end)
                if next_space > start + int(self.chunk_size * 0.6):
                    end = next_space

            chunk = cleaned[start:end].strip()
            if chunk:
                chunks.append(chunk)

            if end >= text_len:
                break
            start = max(0, end - self.overlap)

        return chunks


class IngestionService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.vectorizer = Vectorizer(settings.vector_dim)
        self.chunker = Chunker(settings.chunk_size, settings.chunk_overlap)

    def _build_chunk_rows(
        self,
        source_type: str,
        source_name: str,
        source_ref: str,
        text: str,
        section: str | None,
        page: int | None,
    ) -> list[dict[str, Any]]:
        chunks = self.chunker.split(text)
        now = datetime.now(timezone.utc).isoformat()
        out: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            metadata = {
                "source_type": source_type,
                "source_name": source_name,
                "source_ref": source_ref,
                "section": section,
                "page": page,
                "chunk_index": idx,
                "ingestion_ts": now,
            }
            out.append(
                {
                    "source_type": source_type,
                    "source_name": source_name,
                    "source_ref": source_ref,
                    "section": section,
                    "page": page,
                    "chunk_index": idx,
                    "text": chunk,
                    "metadata": metadata,
                    "vector": self.vectorizer.embed(chunk),
                    "ingestion_ts": now,
                }
            )
        return out

    def ingest_pdf(self, pdf_path: str) -> int:
        path = Path(pdf_path)
        reader = PdfReader(str(path))
        rows: list[dict[str, Any]] = []
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            rows.extend(
                self._build_chunk_rows(
                    source_type="pdf",
                    source_name=path.name,
                    source_ref=str(path),
                    text=text,
                    section=f"page_{i}",
                    page=i,
                )
            )
        return self.db.insert_kb_chunks(rows)

    def ingest_raw_text(self, text: str, title: str = "manual_text") -> int:
        rows = self._build_chunk_rows(
            source_type="text",
            source_name=title,
            source_ref=title,
            text=text,
            section="body",
            page=None,
        )
        return self.db.insert_kb_chunks(rows)

    def ingest_website(self, start_url: str, max_pages: int = 25, timeout_sec: int = 12) -> int:
        parsed_start = urlparse(start_url)
        base_netloc = parsed_start.netloc
        scheme = parsed_start.scheme or "https"

        robots = RobotFileParser()
        robots.set_url(f"{scheme}://{base_netloc}/robots.txt")
        try:
            robots.read()
        except Exception:
            # If robots cannot be read, continue with cautious crawl cap.
            pass

        queue: deque[str] = deque([self._normalize_url(start_url)])
        visited: set[str] = set()
        rows: list[dict[str, Any]] = []

        session = requests.Session()
        headers = {"User-Agent": "SalesConciergeMVP/1.0"}

        while queue and len(visited) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            if robots and not robots.can_fetch("*", url):
                continue

            try:
                resp = session.get(url, headers=headers, timeout=timeout_sec)
                if resp.status_code >= 400 or "text/html" not in resp.headers.get("content-type", ""):
                    continue
            except Exception:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "footer"]):
                tag.decompose()

            title = (soup.title.string or "").strip() if soup.title else ""
            body_text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
            if body_text:
                rows.extend(
                    self._build_chunk_rows(
                        source_type="web",
                        source_name=title or url,
                        source_ref=url,
                        text=body_text,
                        section=urlparse(url).path or "/",
                        page=None,
                    )
                )

            for anchor in soup.find_all("a", href=True):
                href = anchor.get("href", "").strip()
                if not href or href.startswith("#"):
                    continue
                next_url = self._normalize_url(urljoin(url, href))
                parsed = urlparse(next_url)
                if parsed.netloc != base_netloc:
                    continue
                if parsed.scheme not in {"http", "https"}:
                    continue
                if next_url not in visited:
                    queue.append(next_url)

        return self.db.insert_kb_chunks(rows)

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        clean = parsed._replace(fragment="", query="")
        return urlunparse(clean)


class RetrievalService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.vectorizer = Vectorizer(settings.vector_dim)

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievalResult]:
        k = top_k or self.settings.top_k
        query_vec = self.vectorizer.embed(query)
        rows = self.db.fetch_kb_chunks()

        scored: list[RetrievalResult] = []
        for row in rows:
            score = self.vectorizer.cosine(query_vec, row["vector"])
            if score <= 0:
                continue
            scored.append(
                RetrievalResult(
                    text=row["text"],
                    source_type=row["source_type"],
                    source_name=row["source_name"],
                    source_ref=row["source_ref"],
                    page=row.get("page"),
                    section=row.get("section"),
                    score=score,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:k]

    def citations_from_results(self, results: list[RetrievalResult], max_items: int = 3) -> list[Citation]:
        citations: list[Citation] = []
        seen: set[tuple[str, str | None]] = set()
        for result in results:
            locator = None
            if result.source_type == "pdf" and result.page:
                locator = f"page {result.page}"
            elif result.source_type == "web":
                locator = urlparse(result.source_ref).path or "/"
            elif result.section:
                locator = result.section

            key = (result.source_name, locator)
            if key in seen:
                continue
            seen.add(key)

            citations.append(
                Citation(
                    source_type=result.source_type,
                    source=result.source_name,
                    locator=locator,
                    excerpt=result.text[:180],
                    score=round(result.score, 4),
                )
            )
            if len(citations) >= max_items:
                break
        return citations
