#!/bin/bash

NAME="hopper"                               # Name of the application
DJANGODIR=/usr/src/app/hospexplorer               # Django project directory
NUM_WORKERS=3                                     # Number of Gunicorn workers
DJANGO_SETTINGS_MODULE=hospexplorer.settings      # Django settings module
DJANGO_WSGI_MODULE=hospexplorer.wsgi              # WSGI module name
TIMEOUT=360

echo "Starting $NAME as `whoami`"

# Activate the virtual environment
cd $DJANGODIR
export DJANGO_SETTINGS_MODULE=$DJANGO_SETTINGS_MODULE
export PYTHONPATH=$DJANGODIR:$PYTHONPATH
mkdir -p /usr/src/app/logs

# Install dependencies and run migrations
cd $DJANGODIR
source ../.app_env
source ../.docker-env

uv run python manage.py migrate
uv run python manage.py collectstatic --noinput

# Start your Django Unicorn
# Programs meant to be run under supervisor should not daemonize themselves (do not use --daemon)
exec uv run gunicorn ${DJANGO_WSGI_MODULE}:application \
  --name $NAME \
  --workers $NUM_WORKERS \
  --timeout $TIMEOUT \
  --bind=0.0.0.0:8000 \
  --log-level=info \
  --log-file /usr/src/app/logs/hopper_app.log
