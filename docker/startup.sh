#! /bin/bash

source .app_env

cd hospexplorer
uv run python manage.py migrate
uv run python manage.py runserver 0.0.0.0:8000
