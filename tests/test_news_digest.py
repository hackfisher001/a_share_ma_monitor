"""Topic matching / clustering for news digest."""

from datetime import datetime, timezone

import pandas as pd

from src.fetch_quotes import QuoteBundle
from src.news_digest import (
    TopicHit,
    build_news_markdown,
    cluster_items,
    item_matches_topic,
    jaccard,
    match_topics,
)
from src.news_fetch import NewsItem
from src.news_topics import NewsSource, NewsTopic, NewsTopicsConfig, load_news_topics


def _item(title: str, summary: str = "") -> NewsItem:
    return NewsItem(
        title=title,
        summary=summary,
        link="https://example.com",
        published=datetime.now(timezone.utc),
        source="test",
    )


def test_load_news_topics_from_repo_yaml():
    from pathlib import Path

    cfg = load_news_topics(Path("news_topics.yaml"))
    assert cfg.enabled
    assert cfg.max_topics >= 1
    assert any(t.id == "spcx_corp" for t in cfg.topics)
    assert any(t.id == "memory_cycle" for t in cfg.topics)
    assert cfg.sources


def test_keyword_match_is_case_insensitive():
    topic = NewsTopic(
        id="fed",
        title="美联储",
        keywords=("Federal Reserve", "加息"),
        codes=("GC=F",),
    )
    assert item_matches_topic(_item("Federal Reserve signals path"), topic)
    assert item_matches_topic(_item("央行意外加息"), topic)
    assert not item_matches_topic(_item("unrelated sports news"), topic)


def test_exclude_keywords_block_match():
    topic = NewsTopic(
        id="aapl",
        title="苹果",
        keywords=("Apple",),
        codes=("AAPL",),
        exclude=("Apple sauce",),
    )
    assert not item_matches_topic(_item("Best Apple sauce recipes"), topic)
    assert item_matches_topic(_item("Apple unveils iPhone"), topic)


def test_jaccard_and_cluster_near_duplicate_titles():
    assert jaccard("Fed cuts rates again", "Fed cuts interest rates again") > 0.5
    items = [
        _item("Fed cuts rates again amid cooling inflation"),
        _item("Fed cuts interest rates again as inflation cools"),
        _item("Completely different gold story"),
    ]
    clusters = cluster_items(items, threshold=0.45)
    assert len(clusters) == 2
    assert len(clusters[0]) == 2 or len(clusters[1]) == 2


def test_match_topics_ranks_and_caps():
    cfg = NewsTopicsConfig(
        enabled=True,
        max_topics=1,
        lookback_hours=36,
        cluster_jaccard=0.5,
        sources=(NewsSource("x", "https://example.com/rss"),),
        topics=(
            NewsTopic("fed", "美联储", ("FOMC",), ("GC=F",)),
            NewsTopic("gold", "黄金", ("gold price",), ("518880",)),
        ),
    )
    items = [
        _item("FOMC holds rates"),
        _item("FOMC minutes released"),
        _item("Gold price jumps overnight"),
    ]
    hits = match_topics(items, cfg)
    assert len(hits) == 1
    assert hits[0].topic.id == "fed"
    assert len(hits[0].items) >= 1


def test_build_news_markdown_includes_day_moves():
    dates = pd.date_range("2025-01-01", periods=5, freq="B")
    hist = pd.DataFrame({"date": dates, "close": [100, 101, 102, 103, 100]})
    bundle = QuoteBundle(
        code="518880",
        name="黄金ETF华安",
        market="cn",
        price=100.0,
        ma30=101.0,
        high_252=110.0,
        as_of="2025-01-07",
        hist=hist,
        theme="macro",
    )
    hit = TopicHit(
        topic=NewsTopic("gold", "黄金涨跌逻辑", ("gold",), ("518880",)),
        items=[_item("Gold price drops sharply")],
        fact="金价显著回落",
    )
    md = build_news_markdown([hit], [bundle])
    assert md is not None
    assert "黄金涨跌逻辑" in md
    assert "金价显著回落" in md
    assert "黄金ETF华安(518880)" in md
    assert "%" in md
