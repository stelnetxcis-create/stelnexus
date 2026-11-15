# StelNexus security notes

Production checklist
1. `.env`: ENV=prod, real SECRET_KEY + PII_KEY (`python -m app.cli gen-keys`), COOKIE_SAMESITE=lax,
   TRUSTED_HOSTS=["your.domain"], TRUSTED_PROXIES=["127.0.0.1"], FRAME_ANCESTORS='self', DEBUG=false.
   List settings are JSON in .env.
2. Serve only behind HTTPS (deploy/Caddyfile). Run one worker (deploy/stelnexus.service).
3. Existing plaintext player data: `python -m app.cli encrypt-pii` (after PII_KEY is set).
4. Back up the database nightly (deploy/backup.sh) and keep PII_KEY backed up separately.
5. Run `scripts/security_check.sh` before each deploy.
6. Rotate any credential that ever lived in a shared .env / dev.db / SQL dump.

Known limits: no two-factor login yet; non-login rate limits (bets, cash desk) are per-process memory.
