#!/bin/sh
# The compose stack's server command (compose.yaml): migrate, seed the demo key when asked, then
# serve with gunicorn. Production runs migrations as a separate deploy step (docs/deployment.md).
set -e
python manage.py migrate --noinput
if [ "${OCTOREVIEW_DEMO_SEED:-0}" = 1 ]; then
  python manage.py seed --dev-env /var/lib/octoreview/dev/.octoreview-dev.env --copy-hint
fi
exec gunicorn config.wsgi:application
