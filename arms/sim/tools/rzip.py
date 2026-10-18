# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Remote zip: list / extract selected members over HTTP Range (stdlib only).
usage: python -I rzip.py list URL [prefix_regex]
       python -I rzip.py extract URL OUTDIR regex [regex...]
"""

import sys, re, struct, zlib, os, urllib.request


def resolve(url):
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        final = r.geturl()
        cr = r.headers.get("Content-Range")
    size = int(cr.split("/")[-1])
    return final, size


def rget(url, start, end):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def central_dir(url, size):
    tail = rget(url, max(0, size - 65536 - 22), size - 1)
    i = tail.rfind(b"PK\x05\x06")
    _, _, _, _, n, cdsize, cdoff, _ = struct.unpack("<IHHHHIIH", tail[i : i + 22])
    # zip64?
    j = tail.rfind(b"PK\x06\x07")
    if cdoff == 0xFFFFFFFF or n == 0xFFFF or j != -1:
        loc = tail[j : j + 20]
        _, _, z64off, _ = struct.unpack("<IIQI", loc)
        rec = rget(url, z64off, z64off + 56 - 1)
        (_, _, _, _, _, _, _, n, cdsize, cdoff) = struct.unpack("<IQHHIIQQQQ", rec)
    cd = rget(url, cdoff, cdoff + cdsize - 1)
    ents = []
    p = 0
    while p < len(cd) and cd[p : p + 4] == b"PK\x01\x02":
        (_, _, _, _, method, _, _, crc, csize, usize, fnl, exl, cml, _, _, _, lho) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", cd[p : p + 46]
        )
        name = cd[p + 46 : p + 46 + fnl].decode("utf-8", "replace")
        extra = cd[p + 46 + fnl : p + 46 + fnl + exl]
        # zip64 extra
        q = 0
        while q + 4 <= len(extra):
            hid, hsz = struct.unpack("<HH", extra[q : q + 4])
            if hid == 1:
                vals = extra[q + 4 : q + 4 + hsz]
                k = 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack("<Q", vals[k : k + 8])[0]
                    k += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack("<Q", vals[k : k + 8])[0]
                    k += 8
                if lho == 0xFFFFFFFF:
                    lho = struct.unpack("<Q", vals[k : k + 8])[0]
                    k += 8
            q += 4 + hsz
        ents.append((name, method, csize, usize, lho))
        p += 46 + fnl + exl + cml
    return ents


def extract(url, ent, outdir):
    name, method, csize, usize, lho = ent
    if name.endswith("/"):
        return
    hdr = rget(url, lho, lho + 30 - 1)
    fnl, exl = struct.unpack("<HH", hdr[26:30])
    data = rget(url, lho + 30 + fnl + exl, lho + 30 + fnl + exl + csize - 1) if csize else b""
    if method == 8:
        data = zlib.decompress(data, -15)
    elif method != 0:
        raise SystemExit(f"unsupported method {method} for {name}")
    safe = os.path.normpath(name).lstrip("/")
    if safe.startswith(".."):
        raise SystemExit(f"unsafe path {name}")
    dst = os.path.join(outdir, safe)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    cmd, url = sys.argv[1], sys.argv[2]
    final, size = resolve(url)
    ents = central_dir(final, size)
    if cmd == "list":
        rx = re.compile(sys.argv[3]) if len(sys.argv) > 3 else None
        print(f"# size={size} entries={len(ents)}", file=sys.stderr)
        for e in ents:
            if rx is None or rx.search(e[0]):
                print(e[0], e[2], e[3])
    else:
        outdir = sys.argv[3]
        rxs = [re.compile(r) for r in sys.argv[4:]]
        sel = [e for e in ents if any(r.search(e[0]) for r in rxs)]
        tot = sum(e[2] for e in sel)
        print(f"# extracting {len(sel)} members, {tot / 1e6:.1f} MB compressed", file=sys.stderr)
        import concurrent.futures as cf

        state = {"final": final}

        def job(e):
            for attempt in range(5):
                try:
                    extract(state["final"], e, outdir)
                    return e[0]
                except Exception as ex:
                    if attempt == 4:
                        raise
                    state["final"], _ = resolve(url)

        with cf.ThreadPoolExecutor(8) as ex:
            for n in ex.map(job, sel):
                pass
        print("# done", file=sys.stderr)
