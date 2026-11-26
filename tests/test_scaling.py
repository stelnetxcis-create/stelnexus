"""Several workers: Redis-backed rate limits, cross-worker wake-ups and cache clears, shared typing dots, the startup lock,
PgBouncer settings, Cloudflare client-IP handling and the deployment files."""
import asyncio
import ipaddress
import os
import re
import time
from types import SimpleNamespace

import fakeredis
import pytest

from app.core import cloudflare, ratelimit, redisbus, ttlcache
from app.core.errors import RateLimited

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def bus(server, monkeypatch):
    """Redis switched on, backed by an in-memory fake."""
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", "redis://fake:6379/0")
    redisbus.reset_for_tests(fakeredis.FakeStrictRedis(server=server, decode_responses=True))
    ratelimit._hits.clear()
    yield
    redisbus.reset_for_tests(None)
    ratelimit._hits.clear()


def _other_worker():
    """What a second worker looks like to the limiter: it has never seen this process's memory."""
    ratelimit._hits.clear()


# ----------------------------------------------------------------------------- rate limiting

def test_the_limit_holds_across_workers(bus):
    for _ in range(3):
        ratelimit.check("login:1.2.3.4:bob", 3, 60)
        _other_worker()                                          # every call lands on a different worker
    with pytest.raises(RateLimited) as err:
        ratelimit.check("login:1.2.3.4:bob", 3, 60)
    assert "Try again in" in str(err.value)
    ratelimit.check("login:1.2.3.4:alice", 3, 60)                # another key is unaffected


def test_window_slides_and_reset_clears(bus, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock[0])
    for _ in range(2):
        ratelimit.check("k", 2, 10)
    with pytest.raises(RateLimited):
        ratelimit.check("k", 2, 10)
    clock[0] += 11                                               # the old hits leave the window
    ratelimit.check("k", 2, 10)
    ratelimit.reset("k")
    ratelimit.check("k", 2, 10)
    ratelimit.check("k", 2, 10)


def test_a_refused_hit_does_not_extend_the_lockout(bus, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(ratelimit.time, "time", lambda: clock[0])
    ratelimit.check("k2", 1, 10)
    for _ in range(5):                                           # hammering while blocked must not keep it blocked
        with pytest.raises(RateLimited):
            ratelimit.check("k2", 1, 10)
        clock[0] += 1
    clock[0] += 6
    ratelimit.check("k2", 1, 10)


def test_redis_down_falls_back_to_this_process(monkeypatch):
    class Broken:
        def pipeline(self, *a, **k):
            raise ConnectionError("down")
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", "redis://fake:6379/0")
    redisbus.reset_for_tests(Broken())
    ratelimit._hits.clear()
    try:
        ratelimit.check("fb", 2, 60)
        ratelimit.check("fb", 2, 60)
        with pytest.raises(RateLimited):                         # still limited, by the local counters
            ratelimit.check("fb", 2, 60)
        assert redisbus.client() is None                         # the breaker is open: no hammering of a dead Redis
    finally:
        redisbus.reset_for_tests(None)
        ratelimit._hits.clear()


def test_without_redis_nothing_changes():
    assert not redisbus.enabled() and redisbus.client() is None
    ratelimit._hits.clear()
    ratelimit.check("plain", 1, 60)
    with pytest.raises(RateLimited):
        ratelimit.check("plain", 1, 60)
    ratelimit._hits.clear()


# ----------------------------------------------------------------------------- wake-ups and cache clears

def _subscribe(topics):
    from app.services.realtime import hub

    async def go():
        return hub.subscribe(987654, set(topics))
    return hub, asyncio.run(go())


def test_a_broadcast_from_another_worker_wakes_local_streams(bus, server):
    import json
    from app.services.realtime import hub
    hub_obj, sub = _subscribe({"chat"})
    try:
        sub.event.clear()
        redisbus.handle_message(redisbus.key("wake"), json.dumps({"n": "someone-else", "t": ["chat"]}))
        assert sub.event.is_set()
        sub.event.clear()
        redisbus.handle_message(redisbus.key("wake"), json.dumps({"n": redisbus.NODE, "t": ["chat"]}))   # my own echo
        assert not sub.event.is_set()
        redisbus.handle_message(redisbus.key("wake"), "not json")                                         # garbage is ignored
    finally:
        hub.unsubscribe(sub)


def test_publishing_reaches_a_real_subscriber_loop(server, monkeypatch):
    """End to end with a fake Redis: worker B's listener hears worker A's publish and wakes its own stream."""
    import fakeredis.aioredis
    from app.services.realtime import Hub
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", "redis://fake:6379/0")
    redisbus.reset_for_tests(fakeredis.FakeStrictRedis(server=server, decode_responses=True))

    async def scenario():
        hub_b = Hub()
        sub = hub_b.subscribe(5, {"fight:7"})
        sub.event.clear()
        monkeypatch.setattr(redisbus, "_async_factory", lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
        monkeypatch.setattr("app.services.realtime.hub", hub_b)
        task = redisbus.start_listener()
        await asyncio.sleep(0.3)                                  # let it subscribe
        import json
        publisher = fakeredis.FakeStrictRedis(server=server, decode_responses=True)       # worker A, a different process
        publisher.publish(redisbus.key("wake"), json.dumps({"n": "worker-a", "t": ["fight:7"]}))
        woke = await sub.wait(3.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return woke
    try:
        assert asyncio.run(scenario()) is True
    finally:
        redisbus.reset_for_tests(None)


def test_a_commit_clears_the_cache_in_every_worker(bus):
    import json
    ttlcache.clear()
    assert ttlcache.cached(("live", 1), 30, lambda: {"v": 1}) == {"v": 1}
    assert ttlcache.cached(("live", 1), 30, lambda: {"v": 2}) == {"v": 1}      # cached
    redisbus.handle_message(redisbus.key("inv"), json.dumps({"n": "other-worker", "p": ["live"]}))
    assert ttlcache.cached(("live", 1), 30, lambda: {"v": 3}) == {"v": 3}      # the other worker's commit cleared it
    ttlcache.clear()


def test_clear_publishes_once_and_without_redis_is_silent(bus, server):
    r = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    ps = r.pubsub()
    ps.subscribe(redisbus.key("inv"))
    ps.get_message(timeout=1)
    ttlcache.clear("dashboard")
    msg = ps.get_message(timeout=1)
    assert msg and '"p": ["dashboard"]' in msg["data"] and redisbus.NODE in msg["data"]


# ----------------------------------------------------------------------------- typing dots

def test_typing_dots_are_shared_between_workers(bus):
    assert redisbus.typing_touch(1, "Alice", 4.0) and redisbus.typing_touch(2, "Bob", 4.0)
    assert redisbus.typing_list(exclude_user_id=1) == ["Bob"]       # worker B sees A's typist, not itself
    redisbus.typing_clear(2)
    assert redisbus.typing_list(exclude_user_id=1) == []


def test_typing_expires(bus, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(redisbus.time, "time", lambda: clock[0])
    redisbus.typing_touch(3, "Cy", 2.0)
    assert redisbus.typing_list(99) == ["Cy"]
    clock[0] += 3
    assert redisbus.typing_list(99) == []


def test_typing_without_redis_reports_unavailable():
    assert redisbus.typing_list(1) is None and redisbus.typing_touch(1, "x", 1.0) is False


# ----------------------------------------------------------------------------- startup lock

class _Conn:
    def __init__(self, answers):
        self.answers, self.sql = list(answers), []

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def begin(self): return self
    def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params))
        return SimpleNamespace(scalar=lambda: self.answers.pop(0) if self.answers else False)


def _engine(dialect, conn):
    return SimpleNamespace(dialect=SimpleNamespace(name=dialect), connect=lambda: conn)


def test_startup_lock_is_a_no_op_on_sqlite():
    from app.db.startlock import startup_lock
    with startup_lock(_engine("sqlite", None)) as got:
        assert got is True


def test_startup_lock_waits_for_the_other_worker_then_runs():
    from app.db.startlock import lock_key, startup_lock
    conn = _Conn([False, False, True])                                # two refusals, then it is ours
    with startup_lock(_engine("postgresql", conn), poll=0.0) as got:
        assert got is True
    assert len(conn.sql) == 3 and all("pg_try_advisory_xact_lock" in s for s, _ in conn.sql)
    assert conn.sql[0][1] == {"k": lock_key("stelnexus-startup")} and 0 < lock_key("stelnexus-startup") < 2 ** 63


def test_startup_lock_gives_up_after_the_deadline_but_still_continues(caplog):
    from app.db.startlock import startup_lock
    conn = _Conn([])
    with startup_lock(_engine("postgresql", conn), wait_seconds=0.05, poll=0.01) as got:
        assert got is False                                           # a stuck sibling must not keep the site down
    assert "startup lock not acquired" in caplog.text


# ----------------------------------------------------------------------------- database + deployment settings

def test_pgbouncer_turns_off_prepared_statements():
    from app.db.session import pg_connect_args
    assert pg_connect_args(SimpleNamespace(DB_PGBOUNCER=True)) == {"prepare_threshold": None}
    assert pg_connect_args(SimpleNamespace(DB_PGBOUNCER=False)) == {}


def test_deployment_warnings(monkeypatch):
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", "")
    monkeypatch.setattr(redisbus._cfg, "DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setattr(redisbus._cfg, "DB_PGBOUNCER", False)
    assert redisbus.deployment_warnings({"WEB_CONCURRENCY": "1"}) == []
    w = redisbus.deployment_warnings({"WEB_CONCURRENCY": "4"})
    assert any("REDIS_URL is empty" in x for x in w) and any("max_connections" in x for x in w)
    monkeypatch.setattr(redisbus._cfg, "REDIS_URL", "redis://x/0")
    monkeypatch.setattr(redisbus._cfg, "DB_PGBOUNCER", True)
    assert redisbus.deployment_warnings({"WEB_CONCURRENCY": "4"}) == []
    assert redisbus.deployment_warnings({"WEB_CONCURRENCY": "garbage"}) == []


# ----------------------------------------------------------------------------- real visitor IP behind Cloudflare

def _resolver(nets):
    from app.core import middleware
    old = middleware._TRUSTED_NETS
    middleware._TRUSTED_NETS = nets
    return middleware, old


def test_visitor_ip_behind_cloudflare():
    from app.core import middleware
    xff = "203.0.113.9, 172.64.10.1"                                  # visitor, Cloudflare edge (as Caddy forwards it)
    mw, old = _resolver(middleware.build_trusted_nets(["127.0.0.1"], trust_cloudflare=True))
    try:
        assert mw.resolve_client_ip("127.0.0.1", xff) == "203.0.113.9"
        assert mw.resolve_client_ip("127.0.0.1", "203.0.113.9") == "203.0.113.9"          # Caddy sent just the visitor
        assert mw.resolve_client_ip("198.51.100.5", xff) == "198.51.100.5"                # a direct hit cannot spoof it
        mw._TRUSTED_NETS = middleware.build_trusted_nets(["127.0.0.1"], trust_cloudflare=False)
        assert mw.resolve_client_ip("127.0.0.1", xff) == "172.64.10.1"                    # the misconfiguration this guards against
    finally:
        mw._TRUSTED_NETS = old


def test_cloudflare_ranges_are_valid_and_match_the_caddy_snippet():
    nets = [ipaddress.ip_network(n) for n in cloudflare.ALL]
    assert len(nets) >= 20 and ipaddress.ip_address("104.16.0.1") in nets[cloudflare.ALL.index("104.16.0.0/13")]
    snippet = open(os.path.join(ROOT, "deploy", "cloudflare.caddy")).read()
    listed = re.search(r"trusted_proxies static (.+)", snippet).group(1).split()
    assert listed == list(cloudflare.ALL) and "client_ip_headers CF-Connecting-IP" in snippet


def test_deployment_files_are_ready_for_workers():
    svc = open(os.path.join(ROOT, "deploy", "stelnexus.service")).read()
    assert "--workers 4" in svc and "WEB_CONCURRENCY=4" in svc and "LimitNOFILE=65536" in svc
    assert "redis-server.service" in svc and "--proxy-headers" in svc and "--forwarded-allow-ips 127.0.0.1" in svc
    assert re.search(r"^redis>=", open(os.path.join(ROOT, "requirements.txt")).read(), re.M)
    caddy = open(os.path.join(ROOT, "deploy", "Caddyfile")).read()
    assert "import cloudflare.caddy" in caddy and "flush_interval -1" in caddy      # the SSE handling is still there
    assert os.access(os.path.join(ROOT, "deploy", "update-cloudflare-ips.sh"), os.X_OK)
