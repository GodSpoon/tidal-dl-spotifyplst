#!/bin/bash
# Show aggregate download progress across all backends with per-backend speed.
# Usage: ./progress.sh [refresh_seconds]  (default: 0 = once and exit)
#   ./progress.sh         # snapshot
#   ./progress.sh 5       # refresh every 5 seconds (live monitor)

MUSIC=/mnt/storage/music
RESOLVER_LOG=/tmp/qobuz_resolver.log
STATE=/tmp/progress_state.json
REFRESH="${1:-0}"

# ---- Color helpers (auto-disable if not a TTY) ----
if [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_MAGENTA=$'\033[35m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""
    C_YELLOW=""; C_BLUE=""; C_MAGENTA=""; C_CYAN=""
fi

_fmt() {
    numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null \
        || awk -v b="${1:-0}" 'BEGIN{u[1]="K";u[2]="M";u[3]="G";u[4]="T";n=b;i=0;while(n>=1024&&i<4){n/=1024;i++};printf "%.1f%s\n",n,u[i]}'
}

# Backend color: tiddl=blue, squidwtf=green, qobuz=magenta
_bcolor() {
    case "$1" in
        tiddl)    echo "$C_BLUE" ;;
        squidwtf) echo "$C_GREEN" ;;
        qobuz)    echo "$C_MAGENTA" ;;
        *)        echo "$C_RESET" ;;
    esac
}

_now() { date +%s; }

# Persist state as JSON via python
_save_state() {
    STATE="$STATE" MUSIC="$MUSIC" python3 - <<'PYEOF' 2>/dev/null
import json, os, time
from pathlib import Path
music = os.environ["MUSIC"]
state = os.environ["STATE"]
out = {"time": int(time.time())}
for b in ("tiddl","squidwtf","qobuz"):
    p = Path(music) / b
    if not p.exists():
        out[b] = {"n": 0, "s": 0}
        continue
    n = 0; s = 0
    for f in p.rglob("*"):
        if f.is_file() and f.suffix in (".flac",".m4a",".mp3"):
            n += 1
            try: s += f.stat().st_size
            except OSError: pass
    out[b] = {"n": n, "s": s}
json.dump(out, open(state, "w"))
PYEOF
}

# Load state into bash globals using python's json
_load_state() {
    [ -f "$STATE" ] || return
    STATE="$STATE" _load_state_inner=1 python3 - <<'PYEOF' 2>/dev/null > /tmp/__progress_loaded
import json, os
d = json.load(open(os.environ["STATE"]))
print(f"_prev_time={d.get('time',0)}")
for b in ("tiddl","squidwtf","qobuz"):
    print(f"prev_n_{b}={d.get(b,{}).get('n',0)}")
    print(f"prev_s_{b}={d.get(b,{}).get('s',0)}")
PYEOF
    [ -s /tmp/__progress_loaded ] && . /tmp/__progress_loaded
}

# Speed: returns "<fpm> files/min, <mpm> MB/min" or "—"
_speed() {
    local b="$1" n="$2" s="$3"
    if [ -z "$_prev_time" ] || [ "$_prev_time" = "0" ]; then echo "—"; return; fi
    # Use indirect variable names since assoc arrays don't survive subshell
    local pn_var="prev_n_${b}" ps_var="prev_s_${b}"
    local pn="${!pn_var:-0}" ps="${!ps_var:-0}"
    local dn=$((n - pn)) ds=$((s - ps))
    local now=$(_now)
    local dt=$((now - _prev_time))
    if [ "$dt" -le 0 ] || [ "$dn" -le 0 ]; then echo "—"; return; fi
    local fpm=$(awk "BEGIN{printf \"%.1f\", $dn/$dt*60}")
    local mpm=$(awk "BEGIN{printf \"%.1f\", $ds/1048576/$dt*60}")
    echo "$fpm files/min, $mpm MB/min"
}

_snapshot() {
    _load_state

    clear 2>/dev/null || true
    echo "${C_BOLD}=========================================================${C_RESET}"
    echo "${C_BOLD} SPOTIFY->DOWNLOAD PROGRESS  ${C_DIM}$(date +%H:%M:%S)${C_RESET}"
    echo "${C_BOLD}=========================================================${C_RESET}"

    total_n=0
    total_s=0
    for entry in "tiddl:M4A 320 from Tidal" "squidwtf:CD FLAC from qobuz.squid.wtf" "qobuz:MP3 320 from Qobuz"; do
        name="${entry%%:*}"
        desc="${entry#*:}"
        color=$(_bcolor "$name")

        if [ -d "$MUSIC/$name" ]; then
            n=$(find "$MUSIC/$name" -type f \( -name '*.flac' -o -name '*.m4a' -o -name '*.mp3' \) 2>/dev/null | wc -l)
            s=$(du -sb "$MUSIC/$name" 2>/dev/null | awk '{print $1}')
            speed=$(_speed "$name" "$n" "$s")
            printf "  ${color}%-9s${C_RESET} %6d files  %10s  ${C_DIM}%-30s${C_RESET}  ${C_YELLOW}%s${C_RESET}\n" \
                "$name" "$n" "$(_fmt "$s")" "$desc" "$speed"
            total_n=$((total_n + n))
            total_s=$((total_s + ${s:-0}))
        else
            printf "  ${color}%-9s${C_RESET} %6s  %10s  ${C_DIM}%-30s${C_RESET}\n" \
                "$name" "—" "—" "$desc"
        fi
    done
    echo "  ${C_DIM}---------------------------------------------------------${C_RESET}"
    printf "  ${C_BOLD}%-9s${C_RESET} %6d files  %10s\n" "TOTAL" "$total_n" "$(_fmt "$total_s")"

    # resolver progress with bar
    if [ -f "$RESOLVER_LOG" ]; then
        total=$(grep -oE 'Resolving qobuz IDs for [0-9]+' "$RESOLVER_LOG" | tail -1 | grep -oE '[0-9]+')
        if [ -n "$total" ]; then
            done=$(grep -oE '\.\.\. [0-9]+ / '"$total" "$RESOLVER_LOG" | tail -1 | awk '{print $2}')
            pct=$(awk "BEGIN{printf \"%.1f\", ${done:-0}/$total*100}")
            bar_width=30
            filled=$(awk "BEGIN{f=int(${done:-0}/$total*$bar_width); if(f<0) f=0; print f}")
            empty=$((bar_width - filled))
            bar="${C_GREEN}$(printf '%*s' "$filled" '' | tr ' ' '#')${C_DIM}$(printf '%*s' "$empty" '' | tr ' ' '-')${C_RESET}"
            printf "\n  ${C_CYAN}resolver${C_RESET}  [%s] %s / %s  ${C_DIM}(%s%%)${C_RESET}\n" \
                "$bar" "${done:-0}" "$total" "$pct"
        fi
    fi

    # duplicates
    if [ -d "$MUSIC/tiddl" ] && [ -d "$MUSIC/squidwtf" ]; then
        find "$MUSIC/tiddl" -type f -exec basename {} \; 2>/dev/null | sort -u > /tmp/__t_names
        find "$MUSIC/squidwtf" -type f -exec basename {} \; 2>/dev/null | sort -u > /tmp/__s_names
        dup=$(comm -12 /tmp/__t_names /tmp/__s_names | wc -l)
        if [ "$dup" -gt 0 ]; then
            printf "  ${C_RED}duplicates (tiddl ∩ squidwtf): %d${C_RESET}\n" "$dup"
        else
            printf "  ${C_DIM}duplicates (tiddl ∩ squidwtf): 0${C_RESET}\n"
        fi
    fi

    # active processes
    echo ""
    procs=$(pgrep -fa 'spotify_to_tidal|fetch_qobuz_ids|auto_reschedule' 2>/dev/null | wc -l)
    printf "  ${C_DIM}active processes:${C_RESET} %d\n" "$procs"
    pgrep -fa 'spotify_to_tidal|fetch_qobuz_ids|auto_reschedule' 2>/dev/null \
        | cut -d' ' -f2- | head -8 | sed "s/^/    ${C_DIM}-${C_RESET} /"

    if [ -f /tmp/auto_reschedule.log ]; then
        last=$(tail -1 /tmp/auto_reschedule.log)
        echo ""
        printf "  ${C_DIM}last re-schedule: %s${C_RESET}\n" "$last"
    fi

    # Save state for next snapshot's speed calc
    _save_state
}

if [ "$REFRESH" = "0" ]; then
    _snapshot
else
    while true; do
        _snapshot
        sleep "$REFRESH"
    done
fi
