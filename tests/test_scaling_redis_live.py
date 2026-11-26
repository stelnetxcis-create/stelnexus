"""Against a REAL Redis. Skipped unless REDIS_TEST_URL is set, e.g.
    redis-server --port 6390 --save "" --appendonly no &
    REDIS_TEST_URL=redis://127.0.0.1:6390/0 python -m pytest tests/test_scaling_redis_live.py -q
"""
import os
import threading

import pytest

from app.core import ratelimit, redisbus
from app.core.errors import RateLimited

URL = os.environ.get("REDIS_TEST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="REDIS_TEST_URL not set")


@pytest.fixture
def real_redis(monkeypatch):
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", URL)
    monkeypatch.setattr(redisbus._cfg, "REDIS_PREFIX", "stelnexus-test")
    redisbus.reset_for_tests(None)
    redisbus.call(lambda r: [r.delete(k) for k in r.scan_iter("stelnexus-test:*")])
    ratelimit._hits.clear()
    yield
    redisbus.call(lambda r: [r.delete(k) for k in r.scan_iter("stelnexus-test:*")])
    redisbus.reset_for_tests(None)


def test_a_burst_from_many_threads_admits_exactly_the_limit(real_redis):
    admitted, lock = [0], threading.Lock()

    def worker():
        for _ in range(40):
            try:
                ratelimit.check("burst", 100, 60)
                with lock:
                    admitted[0] += 1
            except RateLimited:
                pass
    threads = [threading.Thread(target=worker) for _ in range(8)]       # 320 attempts against a limit of 100
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert admitted[0] == 100


def test_keys_expire_by_themselves(real_redis):
    ratelimit.check("ttl", 5, 30)
    ttl = redisbus.call(lambda r: r.ttl(redisbus.key("rl", "ttl")))
    assert 0 < ttl <= 35


def test_typing_and_publish_round_trip(real_redis):
    assert redisbus.typing_touch(1, "Alice", 5.0)
    assert redisbus.typing_list(exclude_user_id=2) == ["Alice"]
    assert redisbus.call(lambda r: r.publish(redisbus.key("wake"), "{}")) == 0       # no subscribers: still no error
