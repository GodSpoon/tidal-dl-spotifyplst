#!/bin/bash
# Show aggregate download progress across all backends.
# Usage: ./progress.sh [refresh_seconds]  (default: 0 = once and exit)
#   ./progress.sh         # snapshot
#   ./progress.sh 5       # refresh every 5 seconds

MUSIC=/mnt/storage/music
RESOLVER_LOG=/tmp/qobuz_resolver.log
REFRESH="${1:-0}"

_fmt() {
    # Format bytes as human-readable (portable across macOS/Linux)
    numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null \
        || awk -v b="${1:-0}" 'BEGIN{u[1]="K";u[2]="M";u[3]="G";u[4]="T";n=b;i=0;while(n>=1024&&i<4){n/=1024;i++};printf "%.1f%s\n",n,u[i]}'
}

_snapshot() {
    clear 2>/dev/null || true
    echo "=========================================="
    echo " SPOTIFY->DOWNLOAD PROGRESS  $(date +%H:%M:%S)"
    echo "=========================================="

    total_n=0
    total_s=0
    for entry in "tiddl:M4A 320 from Tidal" "squidwtf:CD FLAC from qobuz.squid.wtf" "qobuz:MP3 320 from Qobuz"; do
        name="${entry%%:*}"
        desc="${entry#*:}"
        if [ -d "$MUSIC/$name" ]; then
            n=$(find "$MUSIC/$name" -type f \( -name '*.flac' -o -name '*.m4a' -o -name '*.mp3' \) 2>/dev/null | wc -l)
            s=$(du -sb "$MUSIC/$name" 2>/dev/null | awk '{print $1}')
            printf "  %-9s %6d files  %10s  (%s)\n" "$name" "$n" "$(_fmt "$s")" "$desc"
            total_n=$((total_n + n))
            total_s=$((total_s + ${s:-0}))
        else
            printf "  %-9s %6s  %10s  (%s)\n" "$name" "—" "—" "$desc"
        fi
    done
    echo "  --------"
    printf "  %-9s %6d files  %10s\n" "TOTAL" "$total_n" "$(_fmt "$total_s")"

    # resolver progress
    if [ -f "$RESOLVER_LOG" ]; then
        total=$(grep -oE 'Resolving qobuz IDs for [0-9]+' "$RESOLVER_LOG" | tail -1 | grep -oE '[0-9]+')
        if [ -n "$total" ]; then
            done=$(grep -oE '\.\.\. [0-9]+ / '"$total" "$RESOLVER_LOG" | tail -1 | awk '{print $2}')
            pct=$(awk "BEGIN{printf \"%.1f\", ${done:-0}/$total*100}")
            printf "\n  resolver: %s / %s qobuz_ids (%s%%)\n" "${done:-0}" "$total" "$pct"
        fi
    fi

    # duplicates across backends
    if [ -d "$MUSIC/tiddl" ] && [ -d "$MUSIC/squidwtf" ]; then
        find "$MUSIC/tiddl" -type f -exec basename {} \; 2>/dev/null | sort -u > /tmp/__t_names
        find "$MUSIC/squidwtf" -type f -exec basename {} \; 2>/dev/null | sort -u > /tmp/__s_names
        dup=$(comm -12 /tmp/__t_names /tmp/__s_names | wc -l)
        printf "  duplicates (tiddl ∩ squidwtf): %d\n" "$dup"
    fi

    # active processes
    echo ""
    procs=$(pgrep -fa 'spotify_to_tidal|fetch_qobuz|auto_reschedule' 2>/dev/null | wc -l)
    echo "  active processes: $procs"
    pgrep -fa 'spotify_to_tidal|fetch_qobuz_ids|auto_reschedule' 2>/dev/null \
        | cut -d' ' -f2- | head -8 | sed 's/^/    - /'

    if [ -f /tmp/auto_reschedule.log ]; then
        last=$(tail -1 /tmp/auto_reschedule.log)
        echo ""
        echo "  last re-schedule: $last"
    fi
}

if [ "$REFRESH" = "0" ]; then
    _snapshot
else
    while true; do
        _snapshot
        sleep "$REFRESH"
    done
fi