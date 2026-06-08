#!/usr/bin/env python3
"""Partition manifest into per-backend filtered manifests.

Each track goes to exactly one backend. Priority: squidwtf > tiddl > qobuz.
"""
import json
import sys
from pathlib import Path

BACKENDS = ("squidwtf", "tiddl", "qobuz")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/manifest.json")
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(src) as f:
        m = json.load(f)

    out = {b: [] for b in BACKENDS}
    for pl in m.get("playlists", []):
        buckets = {b: [] for b in BACKENDS}
        for t in pl.get("tracks", []):
            if not t.get("matched"):
                continue
            if t.get("qobuz_id"):
                buckets["squidwtf"].append(t)
            elif t.get("tidal_id"):
                buckets["tiddl"].append(t)
        for b, tracks in buckets.items():
            if tracks:
                out[b].append({**pl, "tracks": tracks, "track_count": len(tracks)})

    for b, pls in out.items():
        path = out_dir / f"manifest_{b}.json"
        with open(path, "w") as f:
            json.dump({**m, "playlists": pls}, f, separators=(",", ":"))
        n = sum(len(p["tracks"]) for p in pls)
        print(f"{b:9s} {len(pls):4d} playlists, {n:6d} tracks -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
