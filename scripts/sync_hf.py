"""Copy the mirror to Hugging Face, so the app has a second home for it.

Reads routing/index.json (the GitHub mirror), copies every file it links
that the Hugging Face dataset lacks, and publishes a second index there
whose links point at the copies. A file not copied yet keeps its GitHub
link, so that index is always whole.

Each file keeps its release folder (`files/<release>/<file>`), so a new
build never replaces a file a phone may be resuming. A file no index uses
any more stays RETIRE_DAYS, then goes, and the dataset's history is
squashed so the storage is freed.

  sync_hf.py                 needs HF_TOKEN in the environment
"""
import datetime as dt
import json
import os
import shutil
import tempfile
import time
import urllib.request

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

REPO = "eucplanet/eucplanet-maps"
KIND = "dataset"
GH = "https://github.com/eried/eucplanet-maps/releases/download/"
HF = "https://huggingface.co/datasets/%s/resolve/main/" % REPO
INDEX = "routing/index.json"
BATCH = 4 * 1024 ** 3      # bytes per commit; the runner has about 14 GB free
RETIRE_DAYS = 14
DAY_MS = 86_400_000


def path_of(url):
    """`files/<release>/<file>` for a GitHub release link, else None."""
    return "files/" + url[len(GH):] if url and url.startswith(GH) else None


def links(idx):
    """Every file link in an index, current and retired."""
    out = [idx["lookups"]["url"]]
    for t in idx["tiles"]:
        out.append(t["url"])
        if t.get("map"):
            out.append(t["map"]["url"])
    out += [r["url"] for r in idx.get("maps_retired", [])]
    return out


def fetch(url, dest):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "eucplanet-maps-sync"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except Exception as e:
            print("retry %s: %s" % (url, e))
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("could not fetch " + url)


def main():
    api = HfApi(token=os.environ["HF_TOKEN"])
    idx = json.load(open(INDEX, encoding="utf-8"))
    have = set(api.list_repo_files(REPO, repo_type=KIND))
    sizes = {t["url"]: t["bytes"] for t in idx["tiles"]}
    sizes.update({t["map"]["url"]: t["map"]["bytes"] for t in idx["tiles"] if t.get("map")})
    sizes[idx["lookups"]["url"]] = idx["lookups"]["bytes"]

    current = [u for u in links(idx) if path_of(u)]
    missing = [u for u in dict.fromkeys(current) if path_of(u) not in have and u in sizes]
    print("%d files linked, %d to copy (%.1f GB)" % (
        len(current), len(missing), sum(sizes[u] for u in missing) / 1e9))

    # Copy in batches: fetch to disk, one commit, free the disk.
    tmp = tempfile.mkdtemp()
    batch, used, done = [], 0, 0
    for i, url in enumerate(missing):
        local = os.path.join(tmp, str(i))
        fetch(url, local)
        batch.append(CommitOperationAdd(path_in_repo=path_of(url), path_or_fileobj=local))
        used += os.path.getsize(local)
        if used >= BATCH or i == len(missing) - 1:
            api.create_commit(REPO, repo_type=KIND, operations=batch,
                              commit_message="copy %d files" % len(batch))
            have.update(op.path_in_repo for op in batch)
            done += len(batch)
            print("copied %d of %d" % (done, len(missing)))
            for op in batch:
                os.remove(op.path_or_fileobj)
            batch, used = [], 0
    shutil.rmtree(tmp, ignore_errors=True)

    # The second index: the same squares, linked to the copies.
    def here(url):
        p = path_of(url)
        return HF + p if p in have else url

    out = json.loads(json.dumps(idx))
    out["lookups"]["url"] = here(out["lookups"]["url"])
    for t in out["tiles"]:
        t["url"] = here(t["url"])
        if t.get("map"):
            t["map"]["url"] = here(t["map"]["url"])
    out["maps_retired"] = [dict(r, url=here(r["url"])) for r in out.get("maps_retired", [])]

    # Files no index uses: retire, then delete RETIRE_DAYS later.
    old = {}
    if INDEX in have:
        try:
            old = json.load(open(api.hf_hub_download(REPO, INDEX, repo_type=KIND), encoding="utf-8"))
        except Exception as e:
            print("old index unreadable: %s" % e)
    used_paths = {path_of(u) for u in current}
    today = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    retired = {r["path"]: r["since"] for r in old.get("hf_retired", [])}
    gone = []
    for p in sorted(have):
        if not p.startswith("files/") or p in used_paths:
            retired.pop(p, None)
            continue
        since = retired.setdefault(p, today)
        if today - since > RETIRE_DAYS * DAY_MS:
            gone.append(p)
            retired.pop(p)
    out["hf_retired"] = [{"path": p, "since": s} for p, s in sorted(retired.items())]
    out["copy_of"] = "https://raw.githubusercontent.com/eried/eucplanet-maps/main/" + INDEX

    body = json.dumps(out, indent=1).encode("utf-8")
    ops = [CommitOperationAdd(path_in_repo=INDEX, path_or_fileobj=body)]
    ops += [CommitOperationDelete(path_in_repo=p) for p in gone]
    if old != out:
        api.create_commit(REPO, repo_type=KIND, operations=ops,
                          commit_message="index %s, %d files deleted" % (dt.date.today(), len(gone)))
    if gone:
        api.super_squash_history(REPO, repo_type=KIND)
    print("index published; %d waiting to retire, %d deleted" % (len(retired), len(gone)))


if __name__ == "__main__":
    main()
