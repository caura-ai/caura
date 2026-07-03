#!/bin/sh
# Core service container entrypoint.
#
# Datadog APM (CAURA-0000 observability POC) is OPT-IN and activates only when a
# Datadog API key is supplied at runtime — i.e. the Caura SaaS deploy, which
# injects DD_API_KEY + DD_TRACE_ENABLED. OSS / on-prem / local runs supply no
# key, so they see no Datadog at all: no serverless-init, no ddtrace, no log
# mutation — just the app. The serverless-init binary is only present in images
# built with --build-arg DD_APM_ENABLED=true (see the Dockerfile); the -x check
# below is belt-and-suspenders for any image built without it.
set -e

# DD_TRACE_ENABLED is an explicit kill-switch; honor the common falsy spellings
# (matches ddtrace's own asbool parsing) rather than only exact "false".
case "${DD_TRACE_ENABLED:-true}" in
  false|False|FALSE|0|no|No|NO|off|Off|OFF) dd_trace_on=0 ;;
  *) dd_trace_on=1 ;;
esac

if [ -n "${DD_API_KEY:-}" ] && [ "$dd_trace_on" = "1" ]; then
  if [ -x /app/dd/datadog-init ]; then
    exec /app/dd/datadog-init ddtrace-run "$@"
  else
    echo "[entrypoint] WARNING: DD_API_KEY is set but /app/dd/datadog-init is not present; starting without Datadog APM (was this image built with --build-arg DD_APM_ENABLED=true?)" >&2
  fi
fi

exec "$@"
