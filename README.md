## Railway web and Celery worker

The application uses one environment-driven Redis URL for Django and Celery:

```text
REDIS_URL=redis://127.0.0.1:6379/0
```

The local value above is only a development fallback. In Railway, configure the
Redis service and reference its generated URL from both application services:

```text
REDIS_URL=${{Redis.REDIS_URL}}
```

Set the Railway worker's browser mode to headless:

```text
PLAYWRIGHT_HEADLESS=true
```

The web service (`virtugadgets_webapp`) keeps its existing Gunicorn start
command and must not start a Celery worker. The separate worker service
(`virtugadgets_worker`) uses the same image and Django/PostgreSQL/API
environment variables, with this start command:

```text
/app/worker-entrypoint.sh celery -A config worker --loglevel=INFO --concurrency=2
```

All scraping launches headless Chromium by default, so Railway does not need
an X server. The wrapper remains compatible with an explicit local
`PLAYWRIGHT_HEADLESS=false` override and does not start Redis or Gunicorn. The
underlying worker command is:

```text
celery -A config worker --loglevel=INFO --concurrency=2
```

Provide Railway's `DATABASE_URL` to both services when available. The project
also supports the existing `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and
`DB_PORT` variables as a local or explicit deployment fallback.

Recommended local verification:

```sh
redis-cli ping
REDIS_URL=redis://127.0.0.1:6379/0 .venv/bin/celery -A config inspect ping
```

Do not place Railway Redis URLs, passwords, database credentials, or API keys
in this repository. Use Railway variables and references instead.
