# syntax=docker/dockerfile:1

# The octoreview server image (docs/deployment.md). Modeled on octonomy's Dockerfile.
#
# The base image is pinned by digest on a literal `FROM`, and both real stages descend from this
# `base` stage, so the pin lives in one place. Keep it out of an ARG: Dependabot's docker
# ecosystem does not resolve `FROM ${VAR}` and would never bump it (octonomy #132).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

# --- Builder -----------------------------------------------------------------------------------
# Install only the locked runtime dependencies, plus every storage extra (spec section 7.1: the
# published image carries them all), into /opt/venv. No dev tools.
FROM base AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Pin uv so the build tool cannot drift between builds.
RUN pip install --no-cache-dir uv==0.9.5

# The manifests first, so this layer stays cached across source edits.
COPY pyproject.toml uv.lock README.md ./

# The venv lives at /opt/venv, not /app/.venv, so a compose bind mount of the source over /app
# cannot shadow it.
RUN uv sync --frozen --no-install-project --no-dev --extra s3

# --- Runtime -----------------------------------------------------------------------------------
FROM base AS runtime

# DJANGO_DEBUG is false in the image: a container must never fall into debug mode (development
# secret fallbacks, tracebacks) because a deployment omitted the variable. Compose sets it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=false \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# A fixed unprivileged user, for clusters that enforce runAsNonRoot. It writes only under
# /var/lib/octoreview: `media` is the local backend's OCTOREVIEW_MEDIA_ROOT (mount a volume
# there), and `dev` holds the compose stack's .octoreview-dev.env. A named volume mounted on
# either inherits this ownership.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app \
    && install -d -o app -g app /var/lib/octoreview/media /var/lib/octoreview/dev

COPY --from=builder /opt/venv /opt/venv
# Source stays root-owned and read-only to the app user (.dockerignore keeps local state out).
COPY . .

# The admin console's bundled static files, served by WhiteNoise (docs/operations.md "Admin
# console"). Settings need a location id and the local backend's paths to load at all; these
# values exist only for this build step, and DEBUG lets the development secret defaults apply.
RUN DJANGO_DEBUG=true OCTOREVIEW_STORAGE_LOCATION_ID=build \
    OCTOREVIEW_PUBLIC_BASE_URL=http://build.invalid OCTOREVIEW_MEDIA_ROOT=/tmp/build-media \
    python manage.py collectstatic --noinput >/dev/null

USER app

EXPOSE 8000

# docker-entrypoint.sh runs `manage.py check` before exec'ing the command, so a misconfigured
# container exits instead of serving. Migrations are a separate deploy step (docs/deployment.md).
ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application"]
