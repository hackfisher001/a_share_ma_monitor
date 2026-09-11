"""Load topic monitors: keywords → watchlist codes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NewsTopic:
    id: str
    title: str
    keywords: tuple[str, ...]
    codes: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsSource:
    name: str
    url: str


@dataclass(frozen=True)
class NewsTopicsConfig:
    enabled: bool
    max_topics: int
    lookback_hours: int
    cluster_jaccard: float
    sources: tuple[NewsSource, ...]
    topics: tuple[NewsTopic, ...]


def _codes(raw: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in raw or []:
        code = str(item).strip()
        if not code:
            continue
        # Keep letter tickers upper; leave digit A-share codes as-is.
        out.append(code.upper() if any(c.isalpha() for c in code) else code)
    return tuple(dict.fromkeys(out))


def load_news_topics(path: Path) -> NewsTopicsConfig:
    if not path.exists():
        return NewsTopicsConfig(
            enabled=False,
            max_topics=5,
            lookback_hours=36,
            cluster_jaccard=0.55,
            sources=(),
            topics=(),
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = tuple(
        NewsSource(name=str(s.get("name") or "src"), url=str(s.get("url") or "").strip())
        for s in (data.get("sources") or [])
        if str(s.get("url") or "").strip()
    )
    topics: list[NewsTopic] = []
    for raw in data.get("topics") or []:
        tid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or tid).strip()
        keywords = tuple(
            str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()
        )
        if not tid or not keywords:
            continue
        topics.append(
            NewsTopic(
                id=tid,
                title=title,
                keywords=keywords,
                codes=_codes(raw.get("codes")),
                exclude=tuple(
                    str(k).strip()
                    for k in (raw.get("exclude") or [])
                    if str(k).strip()
                ),
            )
        )
    return NewsTopicsConfig(
        enabled=bool(data.get("enabled", True)),
        max_topics=max(1, int(data.get("max_topics", 5))),
        lookback_hours=max(1, int(data.get("lookback_hours", 36))),
        cluster_jaccard=float(data.get("cluster_jaccard", 0.55)),
        sources=sources,
        topics=tuple(topics),
    )
