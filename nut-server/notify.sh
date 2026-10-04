#!/bin/sh
# Runs INSIDE the nut-server container, invoked by upsmon via NOTIFYCMD.
# upsmon sets $NOTIFYTYPE (ONLINE, ONBATT, LOWBATT, FSD, SHUTDOWN, ...).
# On a critical event, write "1" to the flag file the host-side
# ups-monitor.service is polling.
case "$NOTIFYTYPE" in
  LOWBATT|FSD|SHUTDOWN)
    echo 1 > /tmp/nut-flag/monitor
    ;;
esac
