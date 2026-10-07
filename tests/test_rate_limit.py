"""#246a: the per-host rate limit is shared by every client, thread and process, not kept per client instance.

Before #246 each PoliteClient spaced only its own requests, so N live NSE clients made N x 2 req/s (~28 req/s seen).
"""

from __future__ import annotations

import asyncio
import itertools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from finresearch.adapters.http import PoliteClient, SharedSlots, UnsafeURLError

RATE = 10.0  # req/s for the test host: 100 ms apart (wide enough that scheduling jitter cannot hide a burst)
GAP = 1 / RATE
URL = "https://www.nseindia.com/api/x"


def _gaps(stamps: list[float]) -> list[float]:
    s = sorted(stamps)
    return [b - a for a, b in itertools.pairwise(s)]


def _client(stamps: list[float], lock: threading.Lock) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            stamps.append(time.time())
        return httpx.Response(200, json={})

    return PoliteClient(
        transport=httpx.MockTransport(handler), host_rates={"nseindia.com": RATE}, cache_dir=None
    )


def test_concurrent_clients_in_threads_share_one_rate():
    stamps: list[float] = []
    lock = threading.Lock()

    async def one_loop():
        clients = [_client(stamps, lock) for _ in range(2)]
        await asyncio.gather(*(c.get(URL) for c in clients for _ in range(3)))
        for c in clients:
            await c.aclose()

    # three threads, each with its own event loop (like sync API handlers in the threadpool), two clients each
    threads = [threading.Thread(target=lambda: asyncio.run(one_loop())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(stamps) == 18
    # per-instance limiters would let 6 clients fire at once (gaps ~0); shared, every gap is ~100 ms (less only by
    # scheduling jitter on a busy machine, which can delay a request past its slot but never move it earlier)
    assert min(_gaps(stamps)) >= GAP * 0.3, _gaps(stamps)
    assert max(stamps) - min(stamps) >= 17 * GAP * 0.9


def test_hosts_under_one_suffix_share_its_budget():
    stamps: list[float] = []
    c = _client(stamps, threading.Lock())

    async def go():
        await asyncio.gather(
            *(c.get(u) for u in ["https://www.nseindia.com/a", "https://archives.nseindia.com/b"] * 3)
        )
        await c.aclose()

    asyncio.run(go())
    assert max(stamps) - min(stamps) >= 5 * GAP * 0.95


_WORKER = """
import asyncio, sys, time, httpx
from finresearch.adapters.http import PoliteClient
start = float(sys.argv[1])
def h(r):
    print(repr(time.time()), flush=True)
    return httpx.Response(200, json={})
async def go():
    c = PoliteClient(transport=httpx.MockTransport(h), host_rates={"nseindia.com": RATE}, cache_dir=None)
    while time.time() < start:
        await asyncio.sleep(0.005)
    await asyncio.gather(*(c.get("https://www.nseindia.com/api/x") for _ in range(5)))
    await c.aclose()
asyncio.run(go())
""".replace("RATE", repr(RATE))


def test_separate_processes_share_one_rate(tmp_path):
    env = {**os.environ, "FINRESEARCH_STATE_DIR": str(tmp_path / "state")}
    env.pop("PYTEST_CURRENT_TEST", None)
    start = time.time() + 3.0  # both processes are up and waiting before the first request
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, repr(start)], stdout=subprocess.PIPE, text=True,
                              env=env) for _ in range(2)]  # fmt: skip
    stamps = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        stamps += [float(x) for x in out.split()]
    assert len(stamps) == 10
    # per-process limiters would finish in ~4 intervals (5 requests each, side by side); shared ones need 9. A busy
    # machine can only delay a request past its slot, so the span is the robust check; gaps get a scheduling margin
    assert max(stamps) - min(stamps) >= 9 * GAP * 0.95
    assert min(_gaps(stamps)) >= GAP * 0.3, _gaps(stamps)
    assert any(Path(tmp_path / "state" / "ratelimit").glob("nseindia.com.slot"))


def test_slots_reserve_in_order_and_a_429_holds_off_every_client(tmp_path):
    now = [1000.0]
    a = SharedSlots(tmp_path, clock=lambda: now[0])
    b = SharedSlots(tmp_path, clock=lambda: now[0])  # another process: same file, own memory
    assert [a.reserve("h", 0.5), b.reserve("h", 0.5), a.reserve("h", 0.5)] == [0.0, 0.5, 1.0]
    now[0] += 10
    assert b.reserve("h", 0.5) == 0.0
    a.hold_off("h", 4.0)
    assert b.reserve("h", 0.5) == pytest.approx(4.0)
    now[0] += 5000  # a slot far behind (or a corrupt file) never blocks
    (tmp_path / "h.slot").write_text("not a number")
    assert a.reserve("h", 0.5) == 0.0


async def test_a_redirect_to_this_machine_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.sebi.gov.in":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8710/api/export/all.zip"})
        return httpx.Response(200, content=b"personal data")

    async def no_sleep(_s: float) -> None:
        return None

    c = PoliteClient(transport=httpx.MockTransport(handler), cache_dir=None, sleep=no_sleep, max_retries=0)
    with pytest.raises(UnsafeURLError):
        await c.get("https://www.sebi.gov.in/x")
    await c.aclose()
