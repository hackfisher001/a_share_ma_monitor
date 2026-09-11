"""Fetch public RSS/Atom headlines for topic matching."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests

from src.news_topics import NewsSource

log = logging.getLogger("ma_monitor")

_TIMEOUT = 12
_UA = (
    "Mozilla/5.0 (compatible; a_share_ma_monitor/1.0; "
    "+https://github.com/local/a_share_ma_monitor)"
)


@dataclass(frozen=True)
class NewsItem:
    title: str
    summary: str
    link: str
    published: datetime | None
    source: str


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            fixed = text.replace("Z", "+0000") if fmt.endswith("%z") else text
            dt = datetime.strptime(fixed[: len("2020-01-01T00:00:00+0000")], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(node: ET.Element, names: Iterable[str]) -> str:
    wanted = {n.lower() for n in names}
    for child in list(node):
        if _local(child.tag).lower() in wanted:
            return (child.text or "").strip()
    return ""


def _parse_feed_xml(xml_text: str, source_name: str) -> list[NewsItem]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("RSS 解析失败 %s: %s", source_name, exc)
        return []

    items: list[NewsItem] = []
    # RSS 2.0: channel/item ; Atom: feed/entry
    nodes = [n for n in root.iter() if _local(n.tag).lower() in {"item", "entry"}]
    for node in nodes:
        title = _strip_html(_child_text(node, ("title",)))
        if not title:
            continue
        summary = _strip_html(
            _child_text(node, ("description", "summary", "content"))
        )
        link = _child_text(node, ("link",))
        if not link:
            for child in list(node):
                if _local(child.tag).lower() == "link":
                    link = (child.attrib.get("href") or child.text or "").strip()
                    if link:
                        break
        published = _parse_date(
            _child_text(node, ("pubDate", "published", "updated", "date"))
        )
        items.append(
            NewsItem(
                title=title,
                summary=summary[:400],
                link=link,
                published=published,
                source=source_name,
            )
        )
    return items


def fetch_source(source: NewsSource) -> list[NewsItem]:
    try:
        resp = requests.get(
            source.url,
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        )
        resp.raise_for_status()
        # Prefer declared charset; fall back to apparent encoding so CN feeds
        # (often UTF-8 without a header) don't show mojibake titles.
        if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "ascii"}:
            resp.encoding = resp.apparent_encoding or "utf-8"
        return _parse_feed_xml(resp.text, source.name)
    except Exception as exc:
        log.warning("RSS 拉取失败 %s: %s", source.name, exc)
        return []


def fetch_all_sources(
    sources: tuple[NewsSource, ...] | list[NewsSource],
    *,
    lookback_hours: int,
) -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen: set[str] = set()
    out: list[NewsItem] = []
    for source in sources:
        for item in fetch_source(source):
            if item.published is not None and item.published < cutoff:
                continue
            # Undated items keep if title is new; better to include than miss.
            key = item.title.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out
