"""Keep the offline map squares on the mirror fresh, a few at a time.

A square's map hardly changes month to month, so instead of re-uploading
the world, each weekly run looks at the squares checked longest ago (with
no map first), cuts them from the newest Protomaps build and uploads only
those that changed: more than CHANGED of their size, or older than MAX_AGE.
The rest only get their "checked" date moved. Every square comes round
about every ROUND weeks; a manual run with a bigger count fills the mirror
the first time.

Three steps, one per workflow job:

  sync_maps.py plan <count> <batches>   picks this run's squares and the
                                        build; prints JSON for the matrix
  sync_maps.py cut <build> <stamp> <batch> <names...>
                                        cuts, compares and uploads; writes
                                        maps-<batch>.json
  sync_maps.py index <dir>              merges the batches into the index,
                                        retires replaced files and deletes
                                        those retired RETIRE_DAYS ago

A replaced file stays up RETIRE_DAYS so a phone with an older index can
still fetch it. A square that fails keeps its current map.
"""
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import cut_maps  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "eried/eucplanet-maps")
INDEX = "routing/index.json"
BUILDS = "https://build-metadata.protomaps.dev/builds.json"
PLANET = "https://build.protomaps.com/"
CHUNK = 900               # files per release, under GitHub's 1000
CHANGED = 0.01            # size change that counts as a new map
MAX_AGE_DAYS = 182        # re-upload anyway after this long
ROUND = 12                # weeks for every square to come round
RETIRE_DAYS = 14          # how long a replaced file stays up
DAY_MS = 86_400_000


def now_ms():
    return int(time.time() * 1000)


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def load():
    return json.load(open(INDEX))


def order(tiles, week):
    """Squares by how much they need a look: no map first, then the longest
    since checked; ties shuffled by a hash of the week, so runs do not walk
    the map in the same order."""
    def key(t):
        m = t.get("map") or {}
        shuffle = hashlib.sha256(("%s:%s" % (week, t["name"])).encode()).hexdigest()
        return (1 if m else 0, m.get("checked", 0), shuffle)
    return [t["name"] for t in sorted(tiles, key=key)]


def plan(count, batches):
    req = urllib.request.Request(BUILDS, headers={"User-Agent": "eucplanet-maps"})
    builds = json.load(urllib.request.urlopen(req, timeout=60))
    build = sorted(b["key"] for b in builds if b["key"].endswith(".pmtiles"))[-1]
    tiles = load()["tiles"]
    if count <= 0:
        count = math.ceil(len(tiles) / ROUND)
    week = dt.date.today().isocalendar()
    names = order(tiles, "%d-%d" % (week[0], week[1]))[:count]
    size = -(-len(names) // batches)
    matrix = [{"batch": i, "names": " ".join(names[i * size:(i + 1) * size])}
              for i in range(batches) if names[i * size:(i + 1) * size]]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    print(json.dumps({"build": build, "stamp": stamp, "batches": matrix}))


def needs_upload(old, size, today):
    if not old:
        return True
    if abs(size - old["bytes"]) > CHANGED * max(old["bytes"], 1):
        return True
    return today - old.get("date", 0) > MAX_AGE_DAYS * DAY_MS


def upload(tag, path, build):
    gh("release", "create", tag, "--repo", REPO, "--latest=false", "--title", tag,
       "--notes", "Offline map squares cut from the Protomaps OpenStreetMap build %s. "
                  "Data (c) OpenStreetMap contributors, ODbL. See routing/index.json." % build, check=False)
    # Jobs create the shared release at the same time: retry a moment later
    # when it is not there yet.
    for attempt in range(5):
        if gh("release", "upload", tag, path, "--repo", REPO, "--clobber", check=False).returncode == 0:
            return "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, os.path.basename(path))
        time.sleep(15 * (attempt + 1))
    raise IOError("upload failed")


def cut(build, stamp, batch, names):
    planet = cut_maps.Planet(PLANET + build)
    built = int(dt.datetime.strptime(build[:8], "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    current = {t["name"]: t.get("map") for t in load()["tiles"]}
    today = now_ms()
    # One release per batch keeps each under GitHub's file limit.
    tag = "map-%s-%s" % (stamp, batch)
    out = []
    for name in names:
        path = name + ".pmtiles"
        try:
            size = cut_maps.cut(planet, name, path)
            if size == 0:
                continue
            old = current.get(name)
            if needs_upload(old, size, today):
                url = upload(tag, path, build)
                out.append({"name": name, "url": url, "bytes": size, "date": built, "checked": today})
                print("%s uploaded %d" % (name, size), flush=True)
            else:
                out.append({"name": name, "checked": today})
                print("%s unchanged %d" % (name, size), flush=True)
        except Exception as e:
            print("FAILED %s: %s" % (name, e), flush=True)
        finally:
            if os.path.exists(path):
                os.remove(path)
    with open("maps-%s.json" % batch, "w") as f:
        json.dump(out, f)


def asset_of(url):
    m = re.search(r"/releases/download/([^/]+)/([^/]+)$", url or "")
    return (m.group(1), m.group(2)) if m else None


def index(folder):
    idx = load()
    by_name = {t["name"]: t for t in idx["tiles"]}
    retired = idx.get("maps_retired", [])
    today = now_ms()
    n_new = n_checked = 0
    for f in glob.glob(os.path.join(folder, "**", "maps-*.json"), recursive=True):
        for m in json.load(open(f)):
            t = by_name.get(m.pop("name"))
            if t is None:
                continue
            if "url" in m:
                old = t.get("map")
                if old and old.get("url") != m["url"]:
                    retired.append({"url": old["url"], "since": today})
                t["map"] = m
                n_new += 1
            elif t.get("map"):
                t["map"]["checked"] = m["checked"]
                n_checked += 1
    # Delete files replaced long enough ago, then releases left empty.
    keep = []
    for r in retired:
        a = asset_of(r["url"])
        if a and today - r["since"] > RETIRE_DAYS * DAY_MS:
            gh("release", "delete-asset", a[0], a[1], "--repo", REPO, "--yes", check=False)
        else:
            keep.append(r)
    idx["maps_retired"] = keep
    idx["maps_license"] = "Map data (c) OpenStreetMap contributors, ODbL 1.0. Vector tiles by Protomaps."
    with open(INDEX, "w") as f:
        json.dump(idx, f, indent=1)
        f.write("\n")
    used = {asset_of(t["map"]["url"])[0] for t in idx["tiles"] if t.get("map")}
    used |= {asset_of(r["url"])[0] for r in keep if asset_of(r["url"])}
    for tag in gh("release", "list", "--repo", REPO, "--limit", "300", "--json", "tagName",
                  "--jq", ".[].tagName").stdout.split():
        if tag.startswith("map-") and tag not in used:
            print("deleting empty " + tag)
            gh("release", "delete", tag, "--repo", REPO, "--yes", "--cleanup-tag", check=False)
    print("maps: %d uploaded, %d unchanged, %d retired files waiting" % (n_new, n_checked, len(keep)))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "plan":
        plan(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "cut":
        cut(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
    elif cmd == "index":
        index(sys.argv[2])
