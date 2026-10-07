"""Retention (#247): the HTTP cache is pruned past each entry's TTL, old intraday series past the stated window, logs
rotate by size, and nothing outside those is ever deleted."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from finresearch.adapters.http import IST, PoliteClient
from finresearch.monitor import retention

NOW = datetime(2026, 10, 12, 4, 0, tzinfo=IST)  # Monday 04:00 IST


async def _cache(tmp_path, url: str, ttl: float, fetched_at: datetime) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"body " + url.encode()))
    async with PoliteClient(cache_dir=tmp_path, transport=transport, wall_clock=lambda: fetched_at) as c:
        await c.get(url, cache_ttl=ttl)


async def test_http_cache_entries_are_pruned_past_their_own_ttl(tmp_path):
    cache = tmp_path / "cache" / "http"
    await _cache(
        cache, "https://example.com/short", 3600, NOW - timedelta(hours=2)
    )  # 1 h TTL, 2 h old: expired
    await _cache(cache, "https://example.com/long", 86400, NOW - timedelta(hours=2))  # 1 day TTL: kept
    await _cache(cache, "https://example.com/fresh", 3600, NOW - timedelta(minutes=10))  # kept
    metas = {json.loads(p.read_text())["url"]: p for p in cache.glob("*.meta.json")}
    assert json.loads(metas["https://example.com/short"].read_text())["cache_ttl_s"] == 3600
    # an entry written before #247 has no TTL: kept up to LEGACY_MAX_AGE (60 days > the longest TTL, 45 days)
    legacy_old, legacy_new = cache / "legacyold.meta.json", cache / "legacynew.meta.json"
    for p, age in ((legacy_old, 61), (legacy_new, 59)):
        p.write_text(json.dumps({"url": "https://example.com/" + p.name.split(".")[0], "status": 200, "sha256": "x", "size": 1,
                                 "fetched_at": (NOW - timedelta(days=age)).isoformat()}))  # fmt: skip
        p.with_name(p.name.replace(".meta.json", ".body")).write_bytes(b"x")
    orphan = cache / "orphan.body"  # an interrupted write: a body without meta, two days old
    orphan.write_bytes(b"partial")
    old = (NOW - timedelta(days=2)).timestamp()
    os.utime(orphan, (old, old))
    # files the prune must never touch, inside and next to the cache folder
    keep = [
        cache / "notes.txt",
        tmp_path / "reports" / "pack.pdf",
        tmp_path / "backups" / "finresearch-1.dump",
    ]
    for p in keep:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"keep")
        os.utime(p, (0, 0))

    res = retention.prune_http_cache(cache, NOW)
    left = {json.loads(p.read_text())["url"] for p in cache.glob("*.meta.json")}
    assert left == {"https://example.com/long", "https://example.com/fresh", "https://example.com/legacynew"}
    assert len(list(cache.glob("*.body"))) == 3 and not orphan.exists()
    assert res["entries"] == 2 and res["orphans"] == 1 and res["bytes"] > 0
    assert all(p.exists() for p in keep)


async def test_a_cached_entry_is_still_served_after_the_ttl_is_stored(tmp_path):
    """The extra meta field must not break reading the cache."""
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, content=b"ok")

    async with PoliteClient(cache_dir=tmp_path, transport=httpx.MockTransport(handler)) as c:
        first = await c.get("https://example.com/a", cache_ttl=600)
        second = await c.get("https://example.com/a", cache_ttl=600)
    assert len(calls) == 1 and not first.record.from_cache and second.record.from_cache


def test_intraday_series_older_than_the_window_are_deleted(env):
    from finresearch.db import session_scope
    from finresearch.db.models import IntradaySeriesRow

    today = date(2026, 10, 12)
    days = {"old": today - timedelta(days=401), "edge": today - timedelta(days=400), "new": today}
    with session_scope() as s:
        s.query(IntradaySeriesRow).filter(IntradaySeriesRow.symbol == "RETCO").delete()
        for d in days.values():
            s.add(IntradaySeriesRow(kind="equity", symbol="RETCO", day=d, ticks=[[1, "1.0"]], prev_close=Decimal(1), source="nse",
                                    complete=True))  # fmt: skip
    with session_scope() as s:
        assert retention.prune_intraday(s, today) == 1
    with session_scope() as s:
        left = {r.day for r in s.query(IntradaySeriesRow).filter(IntradaySeriesRow.symbol == "RETCO")}
    assert left == {days["edge"], days["new"]}


async def test_the_weekly_job_runs_once_a_week(env, tmp_path):
    retention._DONE.clear()
    mon_early = datetime(2026, 10, 12, 2, 0, tzinfo=IST)
    assert retention.due_slot(mon_early) is None
    assert retention.due_slot(NOW) == "retention:2026-W42"
    assert retention.due_slot(datetime(2026, 10, 18, 23, 0, tzinfo=IST)) == "retention:2026-W42"  # Sunday
    cache = tmp_path / "http"
    await _cache(cache, "https://example.com/x", 60, NOW - timedelta(hours=1))
    res = await retention.retention_step(NOW, cache_dir=cache)
    assert res["retention"]["http_cache"]["entries"] == 1
    retention._DONE.clear()  # another monitor process: the week's slot is already claimed
    assert await retention.retention_step(NOW + timedelta(hours=5), cache_dir=cache) == {}
    retention._DONE.clear()


def test_log_files_rotate_by_size(tmp_path):
    import io

    from finresearch.logfiles import pipe, uvicorn_log_config

    path = tmp_path / "logs" / "web.log"
    lines = "".join(f"line {i:04d} " + "x" * 40 + "\n" for i in range(200))  # about 10 KB
    assert pipe(io.StringIO(lines), path, max_bytes=1000, backups=2) == 200
    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["web.log", "web.log.1", "web.log.2"]  # capped at 1 + 2 backups
    assert all(p.stat().st_size <= 1000 for p in path.parent.iterdir())
    assert path.read_text().splitlines()[-1].startswith("line 0199")  # the newest lines are in the live file
    cfg = uvicorn_log_config(tmp_path / "serve.log", max_bytes=1000, backups=2)
    assert cfg["handlers"]["file"]["class"] == "logging.handlers.RotatingFileHandler"
    assert cfg["handlers"]["file"]["maxBytes"] == 1000 and cfg["root"]["handlers"] == ["file"]
    assert all(lg["handlers"] == ["file"] for lg in cfg["loggers"].values())


def test_the_policy_is_documented_with_the_code_constants():
    from pathlib import Path

    doc = (Path(__file__).parents[1] / "docs" / "USAGE.md").read_text()
    assert "Retention" in doc and f"{retention.INTRADAY_KEEP_DAYS} days" in doc
