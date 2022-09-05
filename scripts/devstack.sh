#!/usr/bin/env bash

source /edx/app/analytics_api/analytics_api_env
COMMAND=$1

case $COMMAND in
    start)
        /edx/app/supervisor/venvs/supervisor/bin/supervisord -n --configuration /edx/app/supervisor/supervisord.conf
        ;;
    open)
        . /edx/app/analytics_api/nodeenvs/analytics_api/bin/activate
        . /edx/app/analytics_api/venvs/analytics_api/bin/activate
        cd /edx/app/analytics_api/analytics_api

        /bin/bash
        ;;
    exec)
        shift

        . /edx/app/analytics_api/nodeenvs/analytics_api/bin/activate
        . /edx/app/analytics_api/venvs/analytics_api/bin/activate
        cd /edx/app/analytics_api/analytics_api

        "$@"
        ;;
    *)
        "$@"
        ;;
esac
