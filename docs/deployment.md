# Deployment

How to run the octoreview server image. [`spec.md`](spec.md) is the authority on behavior;
[`.env.example`](../.env.example) lists every setting; [`storage.md`](storage.md) covers the
bucket or media directory.

## The image

Build it with `make docker-build` (tag `octoreview:local`). Published images arrive with the
publish workflow (epic #3).

- Python 3.12 slim, pinned by digest; the locked runtime dependencies plus every storage extra;
  no dev tools.
- Runs as the unprivileged user `app` (uid and gid 1001). The source under `/app` is read-only to
  it; `/var/lib/octoreview/media` is the only path it writes.
- `DJANGO_DEBUG=false` by default, so development secret fallbacks never apply.
- The entrypoint runs `manage.py check` and exits on a configuration error instead of serving.
  Every boot error names the variable and the fix.
- `make docker-smoke` (the CI `docker` job) checks all of the above, starts the container, and
  asserts that both health endpoints answer and every log line is JSON.

## Running it

The minimum set, beyond your storage settings (`.env.example` explains each):

```sh
docker run -d --name octoreview -p 8000:8000 \
  -e DJANGO_SECRET_KEY=... \
  -e OCTOREVIEW_SERVICE_TOKEN_PEPPER=... \
  -e OCTOREVIEW_ERASURE_HMAC_KEY=... \
  -e DATABASE_URL=postgres://octoreview:...@db:5432/octoreview \
  -e DJANGO_ALLOWED_HOSTS=reviews.example.com \
  -e OCTOREVIEW_STORAGE_LOCATION_ID=aws-ap-southeast-3-reviews \
  -e OCTOREVIEW_STORAGE_BACKEND=s3 \
  -e OCTOREVIEW_STORAGE_S3_BUCKET=reviews \
  -e OCTOREVIEW_STORAGE_S3_REGION=ap-southeast-3 \
  octoreview:local
```

Generate each secret with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. `OCTOREVIEW_ERASURE_HMAC_KEY`
never rotates (erasure markers depend on it).

**Migrations are a deploy step.** The container does not migrate on start. Run them once per
release, before the new pods take traffic (a Kubernetes Job or init container):

```sh
docker run --rm <same environment> octoreview:local python manage.py migrate --noinput
```

**Health.** `GET /api/v1/health/live` answers while the process serves.
`GET /api/v1/health/ready` also checks the database; it never calls storage, so a storage blip
does not take every pod out of rotation.

**The local backend in a container.** Mount a volume at `/var/lib/octoreview/media`, owned by uid
1001, and set `OCTOREVIEW_MEDIA_ROOT` to it. Only one replica can serve those files; use the `s3`
backend for anything larger (Kubernetes requires a cloud backend).

## Gunicorn

`gunicorn.conf.py` reads these variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `WEB_CONCURRENCY` | 2 | worker processes; about 2 x cores + 1 |
| `GUNICORN_WORKER_CLASS` | `gthread` | keeps the worker heartbeat alive while a thread waits on storage |
| `GUNICORN_THREADS` | 8 | per worker; keep it above `OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS` (4), since each completion holds a thread for its copy |
| `GUNICORN_TIMEOUT` | 30 | worker heartbeat timeout, not a request limit under `gthread` |
| `GUNICORN_GRACEFUL_TIMEOUT` | 25 | keep it under the pod's `terminationGracePeriodSeconds` (30) |
| `GUNICORN_KEEPALIVE` | 5 | seconds |
| `GUNICORN_MAX_REQUESTS`, `GUNICORN_MAX_REQUESTS_JITTER` | 1000, 100 | recycles workers to bound memory |
| `GUNICORN_LOG_LEVEL` | `info` | |
| `GUNICORN_ACCESS_LOG` | off | the app already logs each request with its route template; when on, the access log is scrubbed JSON too |
| `GUNICORN_BIND` | `0.0.0.0:8000` | |

Logs go to stderr as JSON. Media token paths and signed query parameters (`X-Amz-*` and the
like) are scrubbed from every message.

## Reverse proxy and load balancer

- **Idle timeouts and `complete`.** `POST /api/v1/reviews/{id}/media/{media_id}/complete` copies
  the upload inside the request, for up to `OCTOREVIEW_COMPLETE_LEASE_SECONDS` (900). Ingress and
  load-balancer idle timeouts are often 60 seconds. A proxy 504 does not stop the copy: a retry
  answers 409 `complete_in_progress` with `Retry-After` until the first attempt finishes, and the
  SDKs handle that. Raise the idle timeout on that path if your largest videos take longer than
  the default to copy.
- **Upload bodies on the local backend.** Uploads stream through the server on
  `PUT /api/v1/media-uploads/{token}`. Allow bodies up to your largest `max_video_bytes` there
  (500 MiB at most), and stream them instead of buffering. For nginx:

  ```nginx
  location /api/v1/media-uploads/ {
      client_max_body_size 500m;
      proxy_request_buffering off;
      proxy_pass http://octoreview;
  }
  ```

  On `s3`, bytes go straight to the bucket and never reach the proxy.
- **A separate media host for the local backend.** Serve `/api/v1/media-files/` from its own
  hostname (for example `media.reviews.example.com`), so user-uploaded bytes never share an origin
  with anything that holds cookies. The responses already send `nosniff`, a sandbox CSP and
  `Cross-Origin-Resource-Policy`.
- **Logs.** The two token paths carry bearer tokens in the URL. Redact
  `/api/v1/media-uploads/<token>` and `/api/v1/media-files/<token>` in proxy and CDN logs, as the
  server does in its own.

Kubernetes manifests and CronJobs (`cleanup_media`, the outbox dispatcher, the nightly
`recompute_ratings --check`) arrive with `deploy/kubernetes`.
