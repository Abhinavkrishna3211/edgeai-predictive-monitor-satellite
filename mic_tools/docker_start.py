#!/usr/bin/env python3
"""
docker_start.py — Docker entrypoint for recv_verify.py.

Reads configuration from environment variables and calls recv_verify.main()
directly, avoiding shell word-splitting issues with passwords containing spaces.

Environment variables:
  FACTORY_NAME      — displayed in dashboard header (default: "EPM Factory")
  GATEWAY_PORT      — TCP port for satellite connections (default: 5100)
  DASHBOARD_PORT    — HTTP dashboard port (default: 8080)
  AUTH              — "user:pass" for HTTP Basic Auth (optional)
  NOTIFY_WEBHOOK    — Discord/Slack/Teams webhook URL (optional)
  NOTIFY_EMAIL      — SMTP config FROM:TO:HOST[:PORT[:USER:PASS]] (optional)
"""

import os
import sys

args = ['recv_verify']
args += ['--no-plot']
args += ['--factory-name', os.environ.get('FACTORY_NAME', 'EPM Factory')]
args += ['--port',          os.environ.get('GATEWAY_PORT', '5100')]
args += ['--dashboard-port', os.environ.get('DASHBOARD_PORT', '8080')]

if os.environ.get('AUTH'):
    args += ['--auth', os.environ['AUTH']]
if os.environ.get('NOTIFY_WEBHOOK'):
    args += ['--notify-webhook', os.environ['NOTIFY_WEBHOOK']]
if os.environ.get('NOTIFY_EMAIL'):
    args += ['--notify-email', os.environ['NOTIFY_EMAIL']]

sys.argv = args
import recv_verify
recv_verify.main()
