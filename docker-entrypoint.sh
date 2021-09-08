#!/bin/bash
set -em

chives init
tail --retry --follow --lines=0 /root/.chives/*/log/debug.log &
chives run_daemon &
sleep 2
"$@"
fg
