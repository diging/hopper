#!/bin/bash

mkdir -p /usr/src/app/logs
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
