"""Gunicorn configuration for the container image (docs/deployment.md), copied from octonomy.

Gunicorn loads this file from the working directory (/app) when the image runs
`gunicorn config.wsgi:application`. Every value is overridable through the environment, so one
image is tuned per deployment without a rebuild. Differences from octonomy: `gthread` with 8
threads by default (docs/spec.md section 7.4: a `complete` can hold a thread for the copy, and
each process allows OCTOREVIEW_MAX_CONCURRENT_COMPLETIONS of them, 4 by default, so the thread
pool must sit above that cap), and the app formatter comes from `octoreview.core.logging`.
"""

from __future__ import annotations

import os
import sys

# Gunicorn loads this file by path, so /app is not guaranteed on sys.path when logconfig_dict is
# processed; without it the formatter import fails with "Unable to configure root logger".
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from octoreview.core.logging import JsonFormatter  # noqa: E402  (needs the path insert above)

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# Worker processes; size to the pod's CPU, for example 2 * cores + 1.
workers = int(os.getenv("WEB_CONCURRENCY", "2"))

# gthread keeps the worker heartbeat alive while a thread waits on storage (a completion copy, a
# streamed local upload), so long requests do not trip `timeout`.
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.getenv("GUNICORN_THREADS", "8"))

# Recycle workers to bound memory growth; jitter avoids a restart herd.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "100"))

# graceful_timeout stays a few seconds under the pod's terminationGracePeriodSeconds (30 s by
# default) so in-flight requests drain before the kubelet sends SIGKILL.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "30"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "25"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))

# Logs stay JSON: gunicorn's own records go through the app's JsonFormatter, which also scrubs
# media token paths and signed query parameters (docs/spec.md section 16). The per-request
# access log is off by default (the app's request log already records the route template, never
# the raw path); GUNICORN_ACCESS_LOG=1 turns it on, still as scrubbed JSON.
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
_access_on = os.getenv("GUNICORN_ACCESS_LOG", "").strip().lower() in {"1", "true", "yes", "on"}
accesslog = "-" if _access_on else None
_access_handlers = ["json_stderr"] if _access_on else ["null"]

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {
        "json_stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "json",
        },
        "null": {"class": "logging.NullHandler"},
    },
    # Gunicorn shallow-merges this over its defaults, so "root" must be supplied too.
    "root": {"handlers": ["json_stderr"], "level": loglevel.upper()},
    "loggers": {
        "gunicorn.error": {
            "handlers": ["json_stderr"],
            "level": loglevel.upper(),
            "propagate": False,
        },
        "gunicorn.access": {
            "handlers": _access_handlers,
            "level": loglevel.upper(),
            "propagate": False,
        },
    },
}
