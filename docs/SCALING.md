# Scaling StelNexus: more workers, Redis, PgBouncer, Cloudflare

Written for a 4-core VPS running Caddy, PostgreSQL and uvicorn. The numbers below are starting points to measure
against, not promises: run `scripts/bola_loadtest.py` (or your own bet-burst test) before and after each step.

| Step | What | Code change |
|---|---|---|
| 1 | PostgreSQL tuning, PgBouncer, Cloudflare in front of Caddy | settings only (`DB_PGBOUNCER`, `TRUST_CLOUDFLARE`) |
| 2 | `--workers 4` + Redis | rate limiter, cache clears, typing dots and startup lock are shared / serialised |
| 3 | Live chat across workers | every commit wakes the chat streams of **all** workers through Redis |

## Before you touch anything: connection maths

Each worker has its own database pool of `DB_POOL_SIZE + DB_MAX_OVERFLOW` (default 10 + 20 = 30).
With 4 workers that is up to **120** connections, above PostgreSQL's default `max_connections = 100`. Pick one:

* **Without PgBouncer:** `max_connections = 200` in `postgresql.conf` (4 x 30 + room for the timers, `psql`, backups),
  or lower the pool: `DB_POOL_SIZE=8`, `DB_MAX_OVERFLOW=8` (4 x 16 = 64).
* **With PgBouncer (recommended):** the app opens many cheap client connections, PgBouncer keeps about 20 real ones.

The app warns at startup when `WEB_CONCURRENCY` > 1 and the numbers do not fit.

## Step 1: no code, big effect

### PostgreSQL (starting points for 8 GB RAM; adjust to your VPS)
```
shared_buffers = 2GB
effective_cache_size = 5GB
work_mem = 16MB
maintenance_work_mem = 256MB
max_connections = 100          # 200 if you do not use PgBouncer
```
Run `python scripts/index_audit.py` after a busy day to see missing indexes.

### PgBouncer (transaction pooling)
`/etc/pgbouncer/pgbouncer.ini`
```
[databases]
stelnexus = host=127.0.0.1 port=5432 dbname=stelnexus
[pgbouncer]
listen_addr = 127.0.0.1
listen_port = 6432
pool_mode = transaction
default_pool_size = 20
reserve_pool_size = 5
max_client_conn = 1000
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
```
`.env`
```
DATABASE_URL=postgresql+psycopg://stelnexus:...@127.0.0.1:6432/stelnexus
DB_PGBOUNCER=true          # turns off psycopg's server-side prepared statements (they break under transaction pooling)
DB_POOL_SIZE=8
DB_MAX_OVERFLOW=8
```
Checked in the code: nothing uses session-level features (`SET`, `LISTEN`, session advisory locks), so transaction pooling is
safe. The startup lock uses a *transaction-level* advisory lock for exactly this reason.

### Cloudflare in front of Caddy
1. DNS: orange cloud on the site name. SSL/TLS mode **Full (strict)** (Caddy already has a real certificate).
2. **Tell Caddy and the app who the visitor is.** Behind Cloudflare, the connection comes from Cloudflare, not the visitor.
   If you skip this, every login attempt and every IP ban lands on a Cloudflare address, so 30 logins in 5 minutes from
   *anyone* would lock *everyone* out.
   * Run `sudo deploy/update-cloudflare-ips.sh` (writes `deploy/cloudflare.caddy`), then follow the comment at the top of
     `deploy/Caddyfile` (an `import cloudflare.caddy` in the global options block).
   * `.env`: `TRUSTED_PROXIES=["127.0.0.1"]` and `TRUST_CLOUDFLARE=true`, then restart the app and `systemctl reload caddy`.
3. **Close the back door:** allow ports 80/443 only from Cloudflare (the script prints the `ufw` commands). Otherwise anyone who
   learns your server's IP can bypass Cloudflare and its protection.
4. Caching: hashed files under `/static/dist/` are sent with a one-year `immutable` header, so Cloudflare caches them by default.
   Do not add "Cache Everything" rules: pages are per-user. Leave `/sw.js` alone.
5. Switch **off** Rocket Loader, Auto Minify and Email Obfuscation (they rewrite HTML/JS and break the strict CSP).
6. Live chat is server-sent events. They pass through Cloudflare as long as nothing buffers them: the app sends
   `no-store, no-transform` and a ping every 20 s, comfortably under Cloudflare's 100 s idle limit.

**Verify the real IP:** from your phone on mobile data, sign in, then open Admin > Audit and compare the IP on that entry with
the address shown by "what is my IP". They must match. If you see a Cloudflare address (104.x, 172.64.x, 162.158.x) or
`127.0.0.1`, step 2 is not complete.

## Step 2: four workers

1. Redis (it holds only counters and wake-ups, nothing that must survive a restart):
   ```
   sudo apt install redis-server
   # /etc/redis/redis.conf
   bind 127.0.0.1 ::1
   maxmemory 256mb
   maxmemory-policy allkeys-lru
   save ""
   appendonly no
   ```
2. `.env`: `REDIS_URL=redis://127.0.0.1:6379/0`
3. `deploy/stelnexus.service` already has `--workers 4`, `Environment=WEB_CONCURRENCY=4`, `LimitNOFILE=65536` and starts after Redis.
   Copy it to `/etc/systemd/system/`, then `sudo systemctl daemon-reload && sudo systemctl restart stelnexus`.
4. Check the log: `journalctl -u stelnexus -n 50` must not contain "REDIS_URL is empty". `redis-cli monitor` shows `rl:` keys and
   `PUBLISH stelnexus:wake` when someone logs in or chats.

What is shared through Redis: login / chat / registration rate limits (one limit for all workers), chat wake-ups, "someone is
typing", and live-cache clearing after a commit.

What each worker still keeps for itself, each safe because it expires within seconds: the live cache (2 s), the IP-ban list and
the stream-host (CSP) list (10 s). A change made in worker A is therefore seen by worker B within those few seconds at worst
(immediately for the live cache when Redis is on).

**If Redis stops:** nothing fails. For 5 seconds at a time each worker falls back to its own memory (rate limits become about
4x looser, chat falls back to the 20 s heartbeat) and then retries Redis. It logs one warning every 30 s.

**Startup:** four workers boot at once. A PostgreSQL advisory lock makes one run the schema upgrade and team seed while the
others wait a moment, so they cannot race.

**Rollback:** set `--workers 1` and `WEB_CONCURRENCY=1` and restart. Redis can stay.

## Step 3: live chat at scale

Done: a message committed through any worker (or the CLI / timers) wakes the chat streams on every worker at once. Capacity is
`MAX_TOTAL = 800` streams per worker, so 4 workers hold about 3,200 open chats. Raise `LimitNOFILE` if you raise that number.

Not done, and the real cost to watch: each wake-up makes every subscribed stream run one short database read. With 800 streams
on a worker, one busy chat message means up to 800 reads queued on the worker's thread pool (40 threads) and database pool.
Signs you have reached it: chat messages arrive in bursts of a few seconds, or `journalctl` shows pool timeouts. Options, in
order of effort:
1. Lower `MAX_TOTAL` (or `MAX_PER_USER`) in `services/realtime.py`: a worker then refuses new streams earlier and those
   browsers fall back to ordinary polling (cheap, and chat still works).
2. Build one snapshot per topic per wake-up and hand it to all streams of that topic (a code change in `services/realtime.py`).
3. A separate small chat service, or Pusher / Ably, if you need many thousands of chat viewers.

(`aioredis` was merged into `redis-py` as `redis.asyncio`; that is what the subscriber uses.)

## Checklist

- [ ] Postgres `max_connections` / PgBouncer match the connection maths
- [ ] Cloudflare: real-IP check passes, firewall allows Cloudflare only
- [ ] `REDIS_URL` set, `redis-cli ping` answers `PONG`
- [ ] `--workers 4`, `WEB_CONCURRENCY=4`, `LimitNOFILE=65536`
- [ ] Load test before / after; keep the numbers
