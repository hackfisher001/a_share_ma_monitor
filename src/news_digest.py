"""Match headlines to topics, cluster duplicates, optionally ask DeepSeek."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests

from src.fetch_quotes import QuoteBundle
from src.news_fetch import NewsItem, fetch_all_sources
from src.news_topics import NewsTopic, NewsTopicsConfig, load_news_topics

log = logging.getLogger("ma_monitor")

_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)


@dataclass
class TopicHit:
    topic: NewsTopic
    items: list[NewsItem] = field(default_factory=list)
    fact: str = ""
    matched_keywords: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(_normalize(text)) if len(t) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def item_matches_topic(item: NewsItem, topic: NewsTopic) -> list[str]:
    blob = f"{item.title} {item.summary}"
    blob_l = blob.lower()
    for ex in topic.exclude:
        if ex.lower() in blob_l:
            return []
    hits: list[str] = []
    for kw in topic.keywords:
        if kw.lower() in blob_l:
            hits.append(kw)
    return hits


def cluster_items(items: list[NewsItem], threshold: float) -> list[list[NewsItem]]:
    """Greedy clustering by title Jaccard; first item is the cluster head."""
    clusters: list[list[NewsItem]] = []
    for item in items:
        placed = False
        for cluster in clusters:
            if jaccard(item.title, cluster[0].title) >= threshold:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    # Prefer larger clusters first, then longer titles as proxy for substance.
    clusters.sort(key=lambda c: (-len(c), -len(c[0].title)))
    return clusters


def match_topics(
    items: list[NewsItem],
    config: NewsTopicsConfig,
) -> list[TopicHit]:
    by_id: dict[str, TopicHit] = {}
    for item in items:
        for topic in config.topics:
            kws = item_matches_topic(item, topic)
            if not kws:
                continue
            hit = by_id.get(topic.id)
            if hit is None:
                hit = TopicHit(topic=topic, matched_keywords=[])
                by_id[topic.id] = hit
            hit.items.append(item)
            for kw in kws:
                if kw not in hit.matched_keywords:
                    hit.matched_keywords.append(kw)

    ranked: list[TopicHit] = []
    for hit in by_id.values():
        clusters = cluster_items(hit.items, config.cluster_jaccard)
        # Keep one representative per cluster, capped.
        reps = [c[0] for c in clusters[:8]]
        hit.items = reps
        hit.fact = _default_fact(hit)
        ranked.append(hit)

    ranked.sort(key=lambda h: (-len(h.items), h.topic.title))
    return ranked[: config.max_topics]


def _default_fact(hit: TopicHit) -> str:
    if not hit.items:
        return hit.topic.title
    head = hit.items[0].title.strip()
    extra = len(hit.items) - 1
    if extra > 0:
        return f"{head}（另有 {extra} 条相近报道）"
    return head


def _code_key(market: str, code: str) -> str:
    c = str(code).strip()
    if any(ch.isalpha() for ch in c):
        c = c.upper()
    return f"{market}:{c}"


def _bundle_map(bundles: Iterable[QuoteBundle]) -> dict[str, QuoteBundle]:
    out: dict[str, QuoteBundle] = {}
    for b in bundles:
        out[_code_key(b.market, b.code)] = b
        out[str(b.code).upper()] = b
        out[str(b.code)] = b
    return out


def _day_change(bundle: QuoteBundle) -> float | None:
    from src.perf import change_by_trading_days

    return change_by_trading_days(bundle.hist, bundle.price, 1)


def _impact_line(hit: TopicHit, bundles: list[QuoteBundle]) -> str:
    index = _bundle_map(bundles)
    parts: list[str] = []
    moves: list[float] = []
    for code in hit.topic.codes:
        b = index.get(str(code).upper()) or index.get(str(code))
        if b is None:
            parts.append(code)
            continue
        chg = _day_change(b)
        if chg is None:
            parts.append(f"{b.name}({b.code})")
        else:
            parts.append(f"{b.name}({b.code}) {chg:+.2f}%")
            moves.append(chg)
    if not parts:
        return "可能影响：—"
    direction = ""
    if moves:
        up = sum(1 for m in moves if m > 0.15)
        down = sum(1 for m in moves if m < -0.15)
        if up and not down:
            direction = "｜今日多数同向上涨"
        elif down and not up:
            direction = "｜今日多数同向下跌"
        elif up and down:
            direction = "｜今日涨跌分化"
        else:
            direction = "｜今日变动不大"
    return "可能影响：" + "、".join(parts) + direction


def polish_facts_with_deepseek(hits: list[TopicHit]) -> None:
    """Optional: rewrite each topic fact into one Chinese sentence. Mutates hits."""
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key or not hits:
        return
    base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()
    # Keep prompt small: titles only.
    lines = []
    for i, hit in enumerate(hits, 1):
        titles = " | ".join(it.title for it in hit.items[:3])
        lines.append(f"{i}. 话题={hit.topic.title}; 标题={titles}")
    user = (
        "你是投资助理。根据下列已按话题归类的新闻标题，为每个话题写一句中文事实摘要"
        "（不超过40字，不要投资建议，不要表情）。\n"
        "只输出 JSON 数组，元素形如 "
        '{"id":"话题序号从1开始","fact":"..."}\n\n'
        + "\n".join(lines)
    )
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "只输出合法 JSON 数组，不要 Markdown。",
                    },
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                # Non-thinking flash is enough for daily digest.
            },
            timeout=45,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        import json

        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        rows = json.loads(text)
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                idx = int(row.get("id")) - 1
                fact = str(row.get("fact") or "").strip()
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(hits) and fact:
                hits[idx].fact = fact
        log.info("DeepSeek 已润色 %d 条话题摘要（model=%s）", len(hits), model)
    except Exception as exc:
        log.warning("DeepSeek 摘要失败，回退标题原文: %s", exc)


def build_news_markdown(
    hits: list[TopicHit],
    bundles: list[QuoteBundle],
    *,
    heading: str = "相关要闻",
) -> str | None:
    if not hits:
        return None
    blocks = [f"**{heading}**（按持仓话题筛选，最多 {len(hits)} 条）"]
    for hit in hits:
        blocks.append(
            f"**[{hit.topic.title}]** {hit.fact}\n{_impact_line(hit, bundles)}"
        )
    return "\n\n".join(blocks)


def run_news_digest(
    bundles: list[QuoteBundle],
    *,
    kind: str = "daily",
    topics_path: Path | None = None,
    use_llm: bool = True,
) -> str | None:
    """Fetch → match → cluster → optional DeepSeek → markdown for Feishu card."""
    path = topics_path or Path("news_topics.yaml")
    config = load_news_topics(path)
    if not config.enabled or not config.topics or not config.sources:
        return None

    lookback = config.lookback_hours
    if kind == "weekly":
        lookback = max(lookback, 24 * 7)
    elif kind == "monthly":
        lookback = max(lookback, 24 * 14)

    items = fetch_all_sources(config.sources, lookback_hours=lookback)
    if not items:
        log.info("相关要闻：未拉到可用新闻（信源失败或窗口内无条目）")
        return None

    hits = match_topics(items, config)
    if not hits:
        log.info("相关要闻：%d 条新闻均未命中话题", len(items))
        return None

    if use_llm:
        polish_facts_with_deepseek(hits)

    heading = {
        "daily": "相关要闻 · 日报",
        "weekly": "相关要闻 · 周报",
        "monthly": "相关要闻 · 月报",
    }.get(kind, "相关要闻")
    return build_news_markdown(hits, bundles, heading=heading)
