"""Mirror the BRouter routing tiles (rd5) into GitHub releases.

Reads the tile list from brouter.de, uploads every tile that is new or
changed upstream to a dated release, and writes routing/index.json, the
list the EUC Planet app reads. Tiles that did not change keep their old
download link, so a month with few changes uploads little.

A GitHub release holds at most 1000 files, so a run's uploads are split
into releases of CHUNK files: routing-YYYYMMDD-1, -2, ...

Releases no longer listed in the new index or the previous one are
deleted. Keeping one generation lets a download that started before the
switch finish.
"""
import concurrent.futures as cf
import datetime as dt
import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

SRC = "https://brouter.de/brouter"
INDEX = "routing/index.json"
CHUNK = 900
WORKERS = 4
REPO = os.environ.get("GITHUB_REPOSITORY", "eried/eucplanet-maps")
UA = {"User-Agent": "eucplanet-maps-sync (+https://github.com/%s)" % REPO}

ROW = re.compile(r'href="([EW]\d+_[NS]\d+\.rd5)">[^<]*</a>\s+(\d{2}-\w{3}-\d{4} \d{2}:\d{2})\s+(\d+)')


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def listing():
    """name -> (bytes, date ms). The server lists times in CET."""
    out = {}
    for name, when, size in ROW.findall(get(SRC + "/segments4/")):
        t = dt.datetime.strptime(when, "%d-%b-%Y %H:%M").replace(tzinfo=dt.timezone(dt.timedelta(hours=1)))
        out[name[:-4]] = (int(size), int(t.timestamp() * 1000))
    return out


def lookups_date():
    req = urllib.request.Request(SRC + "/profiles2/lookups.dat", headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        lm = r.headers.get("Last-Modified")
        size = int(r.headers.get("Content-Length", "0"))
    return size, int(email.utils.parsedate_to_datetime(lm).timestamp() * 1000) if lm else 0


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def download(url, path):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)


def asset_url(tag, file):
    return "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, file)


def ensure_release(tag, made):
    if tag in made:
        return
    gh("release", "create", tag, "--repo", REPO, "--latest=false",
       "--title", "Routing tiles " + tag,
       "--notes", "BRouter routing tiles (rd5) mirrored from brouter.de. "
                  "Data (c) OpenStreetMap contributors, ODbL. See routing/index.json.")
    made.add(tag)


def upload(tag, src_url, file, size):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, file)
        download(src_url, path)
        got = os.path.getsize(path)
        if size and got != size:
            raise IOError("%s: got %d bytes, listed %d" % (file, got, size))
        gh("release", "upload", tag, path, "--repo", REPO, "--clobber")
    return asset_url(tag, file)


def tags_in(index):
    tags = set()
    for t in index.get("tiles", []) + [index.get("lookups") or {}]:
        m = re.search(r"/releases/download/([^/]+)/", t.get("url", ""))
        if m:
            tags.add(m.group(1))
    return tags


def main():
    old = json.load(open(INDEX)) if os.path.exists(INDEX) else {}
    old_tiles = {t["name"]: t for t in old.get("tiles", [])}
    remote = listing()
    if len(remote) < 500:
        sys.exit("only %d tiles listed upstream; not touching the mirror" % len(remote))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    made = set()
    tiles, todo = [], []
    for name in sorted(remote):
        size, date = remote[name]
        prev = old_tiles.get(name)
        if prev and prev["bytes"] == size and prev["date"] == date:
            tiles.append(prev)
        else:
            todo.append((name, size, date))

    lsize, ldate = lookups_date()
    prev_l = old.get("lookups")
    lookups = prev_l if prev_l and prev_l.get("date") == ldate and prev_l.get("bytes") == lsize else None

    print("%d tiles upstream, %d to upload, lookups %s" % (len(remote), len(todo), "kept" if lookups else "new"))
    jobs = {}
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        for i, (name, size, date) in enumerate(todo):
            tag = "routing-%s-%d" % (stamp, i // CHUNK + 1)
            ensure_release(tag, made)
            jobs[pool.submit(upload, tag, "%s/segments4/%s.rd5" % (SRC, name), name + ".rd5", size)] = (name, size, date)
        if lookups is None:
            tag = "routing-%s-1" % stamp
            ensure_release(tag, made)
            jobs[pool.submit(upload, tag, SRC + "/profiles2/lookups.dat", "lookups.dat", lsize)] = ("lookups", lsize, ldate)
        failed = 0
        for fut in cf.as_completed(jobs):
            name, size, date = jobs[fut]
            try:
                url = fut.result()
            except Exception as e:
                failed += 1
                print("FAILED %s: %s" % (name, e))
                # Keep last month's copy rather than drop the square.
                if name == "lookups":
                    lookups = prev_l
                elif name in old_tiles:
                    tiles.append(old_tiles[name])
                continue
            entry = {"name": name, "bytes": size, "date": date, "url": url}
            if name == "lookups":
                lookups = entry
            else:
                tiles.append(entry)
    if failed > len(todo) // 10 + 5:
        sys.exit("%d uploads failed; index not updated" % failed)
    if lookups is None:
        sys.exit("no lookups.dat; index not updated")

    tiles.sort(key=lambda t: t["name"])
    # The map squares are synced by sync_maps.py: keep each square's map.
    for t in tiles:
        prev = old_tiles.get(t["name"])
        if prev and "map" in prev:
            t["map"] = prev["map"]
    new = {
        "version": 1,
        "source": SRC,
        "license": "Data (c) OpenStreetMap contributors, ODbL 1.0. Tiles built by BRouter (brouter.de).",
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "lookups": lookups,
        "tiles": tiles,
    }
    for key in ("maps_license", "maps_retired"):
        if key in old:
            new[key] = old[key]
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    with open(INDEX, "w") as f:
        json.dump(new, f, indent=1)
        f.write("\n")

    keep = tags_in(new) | tags_in(old)
    for tag in gh("release", "list", "--repo", REPO, "--limit", "200", "--json", "tagName",
                  "--jq", ".[].tagName").stdout.split():
        if tag.startswith("routing-") and tag not in keep:
            print("deleting " + tag)
            gh("release", "delete", tag, "--repo", REPO, "--yes", "--cleanup-tag", check=False)
    print("index: %d tiles, %d failed" % (len(tiles), failed))


if __name__ == "__main__":
    main()
