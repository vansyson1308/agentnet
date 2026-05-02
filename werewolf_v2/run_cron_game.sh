#!/bin/bash
set -a
source /opt/agentnet/.env
set +a
cd /opt/agentnet/werewolf_v2
exec python3 -u cron_game.py
