#!/bin/sh
# Fail-closed startup (copied from octonomy): run Django's system checks before serving.
#
# Gunicorn loads the WSGI app directly, which runs django.setup() but not the system checks that
# `manage.py runserver` runs. `manage.py check` reads settings only (no database), so it is safe
# before migrations. Its "no issues" banner goes to /dev/null so the log stream stays JSON; a
# failure reaches stderr and `set -e` stops the container. `exec` hands PID 1 to the real
# process so it receives SIGTERM directly.
set -e
python manage.py check >/dev/null
exec "$@"
