"""Mirror the offline map squares into GitHub releases, beside the routing
tiles, and add each square's map to routing/index.json.

Three steps, one per workflow job:

  sync_maps.py plan <batches>       picks the newest Protomaps build and
                                    splits the routing squares into batches;
                                    prints JSON for the job matrix
  sync_maps.py cut <build> <stamp> <batch> <names...>
                                    cuts each square, uploads it to
                                    map-<stamp>-<n> and writes
                                    maps-<batch>.json with what it uploaded
  sync_maps.py index <stamp> <dir>  merges the batch files into the index
                                    and deletes map releases no longer used

A release holds at most 1000 files, so squares are spread over releases of
CHUNK. A square that fails keeps last month's map.
"""
import datetime as dt
import glob
import json
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
CHUNK = 900


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def plan(batches):
    req = urllib.request.Request(BUILDS, headers={"User-Agent": "eucplanet-maps"})
    builds = json.load(urllib.request.urlopen(req, timeout=60))
    build = sorted(b["key"] for b in builds if b["key"].endswith(".pmtiles"))[-1]
    names = sorted(t["name"] for t in json.load(open(INDEX))["tiles"])
    size = -(-len(names) // batches)
    matrix = [{"batch": i, "names": " ".join(names[i * size:(i + 1) * size])}
              for i in range(batches) if names[i * size:(i + 1) * size]]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    print(json.dumps({"build": build, "stamp": stamp, "batches": matrix}))


def cut(build, stamp, batch, names):
    planet = cut_maps.Planet(PLANET + build)
    date = int(dt.datetime.strptime(build[:8], "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    order = sorted(t["name"] for t in json.load(open(INDEX))["tiles"])
    done = []
    for name in names:
        path = name + ".pmtiles"
        try:
            size = cut_maps.cut(planet, name, path)
            if size == 0:
                continue
            tag = "map-%s-%d" % (stamp, order.index(name) // CHUNK + 1)
            gh("release", "create", tag, "--repo", REPO, "--latest=false", "--title", tag,
               "--notes", "Offline map squares, cut from the Protomaps OpenStreetMap build %s. "
                          "Data (c) OpenStreetMap contributors, ODbL. See routing/index.json." % build, check=False)
            # Jobs create the shared release at the same time: retry a
            # moment later when it is not there yet.
            for attempt in range(5):
                if gh("release", "upload", tag, path, "--repo", REPO, "--clobber", check=False).returncode == 0:
                    break
                time.sleep(15 * (attempt + 1))
            else:
                raise IOError("upload failed")
            done.append({"name": name, "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, path),
                         "bytes": size, "date": date})
            print("%s %d" % (name, size), flush=True)
        except Exception as e:
            print("FAILED %s: %s" % (name, e), flush=True)
        finally:
            if os.path.exists(path):
                os.remove(path)
    with open("maps-%s.json" % batch, "w") as f:
        json.dump(done, f)


def tags(index):
    out = set()
    for t in index.get("tiles", []):
        m = re.search(r"/releases/download/([^/]+)/", (t.get("map") or {}).get("url", ""))
        if m:
            out.add(m.group(1))
    return out


def index(stamp, folder):
    idx = json.load(open(INDEX))
    old = tags(idx)
    maps = {}
    for f in glob.glob(os.path.join(folder, "**", "maps-*.json"), recursive=True):
        for m in json.load(open(f)):
            maps[m.pop("name")] = m
    tiles = idx["tiles"]
    missing = [t["name"] for t in tiles if t["name"] not in maps]
    if len(maps) < len(tiles) // 2:
        sys.exit("only %d of %d squares cut; index not updated" % (len(maps), len(tiles)))
    for t in tiles:
        if t["name"] in maps:
            t["map"] = maps[t["name"]]
    idx["maps_license"] = "Map data (c) OpenStreetMap contributors, ODbL 1.0. Vector tiles by Protomaps."
    with open(INDEX, "w") as f:
        json.dump(idx, f, indent=1)
        f.write("\n")
    keep = tags(idx) | old
    for tag in gh("release", "list", "--repo", REPO, "--limit", "200", "--json", "tagName",
                  "--jq", ".[].tagName").stdout.split():
        if tag.startswith("map-") and tag not in keep:
            print("deleting " + tag)
            gh("release", "delete", tag, "--repo", REPO, "--yes", "--cleanup-tag", check=False)
    print("maps: %d squares, %d kept from before or without a map" % (len(maps), len(missing)))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "plan":
        plan(int(sys.argv[2]))
    elif cmd == "cut":
        cut(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
    elif cmd == "index":
        index(sys.argv[2], sys.argv[3])
