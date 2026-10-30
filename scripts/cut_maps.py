"""Cuts 5 degree map squares out of the Protomaps planet build for the app's
offline map, one PMTiles file per square, reading only the byte ranges each
square needs.

Each square is slimmed on the way: layers stop at the zoom in LAST and the
app draws deeper tiles from that parent, translated names go, and the z15
tiles keep only buildings (house numbers). Outside towns (a z14 tile with
fewer than TOWN_ADDRESSES house numbers under it) the square stops at z13;
paths are already there. That is what keeps a square a fraction of the full
map's size. LAST must match OfflineMapStyle.LAST_ZOOM in the app.

usage: cut_maps.py <planet.pmtiles url> <out dir> <square> [<square> ...]
       squares are named like the routing tiles: E10_N55, W5_S5
"""
import gzip
import math
import re
import struct
import sys
import urllib.request

LAST = {"earth": 13, "water": 13, "landuse": 10, "boundaries": 10,
        "roads": 14, "pois": 14, "places": 14, "buildings": 15}
DROP_KEYS = ("sort_rank", "wikidata", "population_rank", "sort_key")
TOWN_ADDRESSES = 50
HDR = "<7sBQQQQQQQQQQQBBBBBBiiiiBii"


def get(url, off, ln):
    req = urllib.request.Request(url, headers={"Range": f"bytes={off}-{off + ln - 1}", "User-Agent": "eucplanet-maps"})
    for _ in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) == ln:
                return data
        except Exception:
            pass
    raise IOError(f"range {off}+{ln} failed")


def rv(b, i):
    v = s = 0
    while True:
        c = b[i]; i += 1
        v |= (c & 0x7F) << s; s += 7
        if c < 0x80:
            return v, i


def wv(out, v):
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80); v >>= 7
    out.append(v)


def read_dir(b):
    n, i = rv(b, 0)
    ids, runs, lens, offs = [], [], [], []
    last = 0
    for _ in range(n):
        d, i = rv(b, i); last += d; ids.append(last)
    for _ in range(n):
        v, i = rv(b, i); runs.append(v)
    for _ in range(n):
        v, i = rv(b, i); lens.append(v)
    for k in range(n):
        v, i = rv(b, i)
        offs.append(offs[k - 1] + lens[k - 1] if v == 0 and k > 0 else v - 1)
    return list(zip(ids, runs, offs, lens))


def write_dir(entries):
    out = bytearray(); wv(out, len(entries)); last = 0
    for e in entries:
        wv(out, e[0] - last); last = e[0]
    for e in entries:
        wv(out, e[1])
    for e in entries:
        wv(out, e[3])
    for k, e in enumerate(entries):
        if k > 0 and e[2] == entries[k - 1][2] + entries[k - 1][3]:
            wv(out, 0)
        else:
            wv(out, e[2] + 1)
    return gzip.compress(bytes(out), mtime=0)


def zxy_id(z, x, y):
    acc = ((1 << (2 * z)) - 1) // 3
    n = 1 << z; d = 0; s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x; y = n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def id_zoom(tid):
    z = 0; acc = 0
    while acc + (1 << (2 * z)) <= tid:
        acc += 1 << (2 * z); z += 1
    return z


def tile_range(z, w, s, e, n):
    def tx(lon):
        return int((lon + 180) / 360 * (1 << z))

    def ty(lat):
        lat = max(-85.0511, min(85.0511, lat))
        r = math.radians(lat)
        return int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))
    m = (1 << z) - 1
    return max(0, tx(w)), min(m, tx(e - 1e-9)), max(0, ty(n)), min(m, ty(s + 1e-9))


# --- vector tile slimming ---------------------------------------------------

def pb(b):
    i = 0
    while i < len(b):
        key, i = rv(b, i)
        f, t = key >> 3, key & 7
        if t == 0:
            v, i = rv(b, i); yield f, t, v
        elif t == 2:
            ln, i = rv(b, i); yield f, t, b[i:i + ln]; i += ln
        elif t == 5:
            yield f, t, b[i:i + 4]; i += 4
        elif t == 1:
            yield f, t, b[i:i + 8]; i += 8
        else:
            raise ValueError("bad wire type")


def field(out, f, payload):
    wv(out, (f << 3) | 2); wv(out, len(payload)); out += payload


def keep_key(k):
    return k == "name:en" or not (k.startswith("name:") or k in DROP_KEYS)


def slim_layer(v):
    parts = list(pb(v))
    keys = [w.decode() for g, t, w in parts if g == 3]
    remap = {}
    for i, k in enumerate(keys):
        if keep_key(k):
            remap[i] = len(remap)
    out = bytearray()
    for g, t, w in parts:
        if g == 3:
            continue
        if g == 2:
            feat = bytearray()
            for h, ht, x in pb(w):
                if h == 2:
                    vals = []; i = 0
                    while i < len(x):
                        val, i = rv(x, i); vals.append(val)
                    pk = bytearray()
                    for a in range(0, len(vals), 2):
                        if vals[a] in remap:
                            wv(pk, remap[vals[a]]); wv(pk, vals[a + 1])
                    field(feat, 2, pk)
                elif ht == 0:
                    wv(feat, h << 3); wv(feat, x)
                else:
                    field(feat, h, x)
            field(out, 2, feat)
        elif t == 0:
            wv(out, g << 3); wv(out, w)
        else:
            field(out, g, w)
    for i, k in enumerate(keys):
        if i in remap:
            field(out, 3, k.encode())
    return bytes(out)


def addresses(tile):
    """House numbers in a tile's buildings layer."""
    for f, t, v in pb(tile):
        if f != 3:
            continue
        parts = list(pb(v))
        if next((w for g, gt, w in parts if g == 1), b"") != b"buildings":
            continue
        keys = [w for g, gt, w in parts if g == 3]
        if b"addr_housenumber" not in keys:
            return 0
        k = keys.index(b"addr_housenumber"); n = 0
        for g, gt, w in parts:
            if g != 2:
                continue
            for a, at, b in pb(w):
                if a == 2:
                    i = 0; vals = []
                    while i < len(b):
                        val, i = rv(b, i); vals.append(val)
                    if k in vals[0::2]:
                        n += 1
        return n
    return 0


def tid_xy(tid):
    z = id_zoom(tid); d = tid - ((1 << (2 * z)) - 1) // 3
    n = 1 << z; x = y = 0; s = 1
    while s < n:
        rx = 1 & (d // 2); ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x; y = s - 1 - y
            x, y = y, x
        x += s * rx; y += s * ry; d //= 4; s *= 2
    return z, x, y


def slim(tile, z):
    out = bytearray()
    for f, t, v in pb(tile):
        if f != 3:
            continue
        name = next((w.decode() for g, gt, w in pb(v) if g == 1), None)
        if z > LAST.get(name, 15):
            continue
        field(out, 3, slim_layer(v))
    return bytes(out)


# --- one square -------------------------------------------------------------

class Planet:
    def __init__(self, url):
        self.url = url
        h = get(url, 0, 16384)
        f = struct.unpack(HDR, h[:127])
        (_, ver, ro, rl, self.meta_off, self.meta_len, self.leaf_off, _, self.data_off, _,
         _, _, _, _, icomp, self.tcomp, self.ttype, self.minz, self.maxz, *_r) = f
        assert ver == 3 and icomp == 2 and self.tcomp == 2
        self.root = read_dir(gzip.decompress(h[ro:ro + rl]))
        self.meta = gzip.decompress(get(url, self.meta_off, self.meta_len))
        self.leaves = {}

    def find(self, tid):
        def look(entries):
            lo, hi = 0, len(entries) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if entries[mid][0] < tid:
                    lo = mid + 1
                elif entries[mid][0] > tid:
                    hi = mid - 1
                else:
                    return entries[mid]
            if hi >= 0 and (entries[hi][1] == 0 or tid - entries[hi][0] < entries[hi][1]):
                return entries[hi]
            return None
        ent = look(self.root)
        depth = 0
        while ent is not None and ent[1] == 0 and depth < 4:
            if ent[2] not in self.leaves:
                if len(self.leaves) > 400:
                    self.leaves.clear()
                self.leaves[ent[2]] = read_dir(gzip.decompress(get(self.url, self.leaf_off + ent[2], ent[3])))
            ent = look(self.leaves[ent[2]]); depth += 1
        if ent is not None and ent[1] > 0 and tid - ent[0] < ent[1]:
            return ent[2], ent[3]
        return None


def cut(planet, name, out_path):
    m = re.fullmatch(r"([EW])(\d{1,3})_([NS])(\d{1,2})", name)
    w = int(m.group(2)) * (-1 if m.group(1) == "W" else 1)
    s = int(m.group(4)) * (-1 if m.group(3) == "S" else 1)
    e, n = w + 5, s + 5
    found = {}
    for z in range(planet.minz, planet.maxz + 1):
        x0, x1, y0, y1 = tile_range(z, w, s, e, n)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tid = zxy_id(z, x, y)
                hit = planet.find(tid)
                if hit:
                    found[tid] = hit
    uniq = sorted(set(found.values()))
    blobs = {}
    i = 0
    while i < len(uniq):
        start = uniq[i][0]; end = start + uniq[i][1]; j = i + 1
        while j < len(uniq) and uniq[j][0] - end < 128 * 1024 and uniq[j][0] + uniq[j][1] - start < 32 << 20:
            end = max(end, uniq[j][0] + uniq[j][1]); j += 1
        chunk = get(planet.url, planet.data_off + start, end - start)
        for k in range(i, j):
            o, ln = uniq[k]; blobs[(o, ln)] = chunk[o - start:o - start + ln]
        i = j

    # Towns: z14 tiles with enough house numbers in their z15 children.
    town = {}
    for tid, src in found.items():
        z, x, y = tid_xy(tid)
        if z == 15:
            town[(x >> 1, y >> 1)] = town.get((x >> 1, y >> 1), 0) + addresses(gzip.decompress(blobs[src]))
    towns = {k for k, n in town.items() if n >= TOWN_ADDRESSES}

    data = bytearray(); done = {}; byblob = {}; entries = []
    for tid in sorted(found):
        z, x, y = tid_xy(tid)
        if z == 14 and (x, y) not in towns or z == 15 and (x >> 1, y >> 1) not in towns:
            continue
        key = (found[tid], z)
        if key not in done:
            t = slim(gzip.decompress(blobs[found[tid]]), z)
            if not t:
                done[key] = None
            else:
                blob = gzip.compress(t, 9, mtime=0)
                if blob not in byblob:
                    byblob[blob] = (len(data), len(blob)); data += blob
                done[key] = byblob[blob]
        if done[key] is None:
            continue
        off, ln = done[key]
        if entries and entries[-1][0] + entries[-1][1] == tid and entries[-1][2] == off:
            entries[-1] = (entries[-1][0], entries[-1][1] + 1, off, ln)
        else:
            entries.append((tid, 1, off, ln))
    if not entries:
        return 0
    root_b = write_dir(entries); leaf_b = b""
    if len(root_b) > 16384 - 127:
        size = 4096
        while True:
            leaf_b = bytearray(); roots = []
            for k in range(0, len(entries), size):
                c = write_dir(entries[k:k + size])
                roots.append((entries[k][0], 0, len(leaf_b), len(c))); leaf_b += c
            root_b = write_dir(roots)
            if len(root_b) <= 16384 - 127:
                break
            size *= 2
    meta_b = gzip.compress(planet.meta, mtime=0)
    ro = 127; mo = ro + len(root_b); lo = mo + len(meta_b); do = lo + len(leaf_b)
    hdr = struct.pack(HDR, b"PMTiles", 3, ro, len(root_b), mo, len(meta_b), lo, len(leaf_b), do, len(data),
                      len(found), len(entries), len(byblob), 1, 2, 2, planet.ttype, planet.minz, planet.maxz,
                      int(w * 1e7), int(s * 1e7), int(e * 1e7), int(n * 1e7), min(planet.maxz, 10),
                      int((w + e) / 2 * 1e7), int((s + n) / 2 * 1e7))
    with open(out_path, "wb") as fo:
        fo.write(hdr); fo.write(root_b); fo.write(meta_b); fo.write(leaf_b); fo.write(data)
    return do + len(data)


def main():
    url, out_dir, names = sys.argv[1], sys.argv[2], sys.argv[3:]
    planet = Planet(url)
    for name in names:
        size = cut(planet, name, f"{out_dir}/{name}.pmtiles")
        print(f"{name} {size}", flush=True)


if __name__ == "__main__":
    main()
