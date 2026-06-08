#!/bin/bash
# Auto re-schedule: re-partition manifest, restart squidwtf with new track list
# Runs every N minutes via cron or as a background loop

set -e
cd ~/tidal-dl-spotifyplst
source .venv/bin/activate

LOG=/tmp/auto_reschedule.log
echo "[$(date +%H:%M:%S)] Re-scheduling..." >> "$LOG"

# Re-partition manifest
python3 scripts/schedule_downloads.py output/manifest.json output >> "$LOG" 2>&1

# Check if squidwtf is running, restart if needed
if pgrep -f "manifest_squidwtf" > /dev/null; then
    pkill -f "manifest_squidwtf" 2>/dev/null
    sleep 2
fi

# Restart squidwtf with the new manifest
nohup .venv/bin/python -m spotify_to_tidal --manifest output/manifest_squidwtf.json download --downloader squidwtf >> /tmp/download_squidwtf.log 2>&1 &
echo "[$(date +%H:%M:%S)] Squidwtf restarted (PID $!)" >> "$LOG"
