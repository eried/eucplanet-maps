"""Check the mirror the way the app uses it.

Downloads every tile in routing/index.json from its GitHub link, checks
the length, and runs BRouter's own integrity check on it, which walks
every checksum in the file. A few tiles and lookups.dat are also compared
byte for byte with brouter.de; the rest are not, to spare that volunteer
server another full download.

Usage: verify_routing.py <brouter jar> [sample count]
"""
import concurrent.futures as cf
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request

INDEX = "routing/index.json"
SRC = "https://brouter.de/brouter"
WORKERS = 6
UA = {"User-Agent": "eucplanet-maps-verify (+https://github.com/%s)"
      % os.environ.get("GITHUB_REPOSITORY", "eried/eucplanet-maps")}

JAR = sys.argv[1]
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 12
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(url, path, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
                    f.write(b)
            return h.hexdigest()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def integrity(path):
    out = subprocess.run(["java", "-cp", JAR + os.pathsep + HERE, "CheckTiles", path],
                         capture_output=True, text=True)
    line = (out.stdout.strip().splitlines() or ["BAD no output " + out.stderr[-300:]])[-1]
    return None if out.returncode == 0 else line


def check(t, tmp, upstream):
    path = os.path.join(tmp, t["name"] + ".rd5")
    try:
        sha = fetch(t["url"], path)
        if os.path.getsize(path) != t["bytes"]:
            return "%s: %d bytes, index says %d" % (t["name"], os.path.getsize(path), t["bytes"])
        bad = integrity(path)
        if bad:
            return "%s: %s" % (t["name"], bad)
        if upstream:
            if fetch("%s/segments4/%s.rd5" % (SRC, t["name"]), path) != sha:
                return "%s: differs from brouter.de" % t["name"]
        return None
    except Exception as e:
        return "%s: %r" % (t["name"], e)
    finally:
        if os.path.exists(path):
            os.remove(path)


def main():
    idx = json.load(open(INDEX))
    tiles = idx["tiles"]
    # Small ones plus the biggest: the sample costs brouter.de little.
    by_size = sorted(tiles, key=lambda t: t["bytes"])
    rnd = random.Random(idx["updated"])
    sample = {t["name"] for t in rnd.sample(by_size[: len(by_size) // 2], SAMPLE)} | {by_size[-1]["name"]}

    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        lk = idx["lookups"]
        if fetch(lk["url"], os.path.join(tmp, "a")) != fetch(SRC + "/profiles2/lookups.dat", os.path.join(tmp, "b")):
            problems.append("lookups.dat differs from brouter.de")
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            futs = {ex.submit(check, t, tmp, t["name"] in sample): t for t in tiles}
            for n, f in enumerate(cf.as_completed(futs), 1):
                r = f.result()
                if r:
                    problems.append(r)
                    print("PROBLEM", r, flush=True)
                if n % 100 == 0:
                    print("checked %d of %d" % (n, len(tiles)), flush=True)

    total = sum(t["bytes"] for t in tiles)
    summary = ["## Mirror check", "",
               "- %d tiles (%.1f GB) downloaded from GitHub and passed BRouter's integrity check: %d" % (
                   len(tiles), total / 1e9, len(tiles) - len(problems)),
               "- Compared byte for byte with brouter.de: lookups.dat and %d tiles (%s)" % (
                   len(sample), ", ".join(sorted(sample))),
               "- Problems: %d" % len(problems)] + ["  - " + p for p in problems]
    print("\n".join(summary))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("\n".join(summary) + "\n")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
