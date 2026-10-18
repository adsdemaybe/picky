# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Download selected top-level folders of a HF dataset without listing the whole repo.

usage: python -I hf_folders.py <repo_id> <ids.txt> <out_dir> [--exclude regex ...]
Lists each folder with the tree API (recursive, paginated), then fetches files via /resolve/main
in parallel. Skips files already present with the right size. Uses HF_TOKEN if set.
"""

import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://huggingface.co/api/datasets/{repo}/tree/main/{path}?recursive=true&expand=false"
RES = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def req(url):
    h = {"User-Agent": "picky-assets/1.0"}
    tok = os.environ.get("HF_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return urllib.request.Request(url, headers=h)


def get_json(url):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req(url), timeout=60) as r:
                link = r.headers.get("Link")
                return json.load(r), link
        except Exception as ex:  # 429 / transient
            time.sleep(2 + attempt * 3)
            last = ex
    raise last


def list_folder(repo, folder):
    url = API.format(repo=repo, path=urllib.parse.quote(folder))
    out = []
    while url:
        data, link = get_json(url)
        out += [d for d in data if d["type"] == "file"]
        url = None
        if link and 'rel="next"' in link:
            url = link.split(";")[0].strip("<>")
    return out


def fetch(repo, f, out_dir):
    dst = os.path.join(out_dir, f["path"])
    if os.path.isfile(dst) and os.path.getsize(dst) == f["size"]:
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    url = RES.format(repo=repo, path=urllib.parse.quote(f["path"]))
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req(url), timeout=300) as r, open(dst + ".part", "wb") as w:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    w.write(b)
            os.replace(dst + ".part", dst)
            return f["size"]
        except Exception as ex:
            time.sleep(2 + attempt * 3)
            last = ex
    raise last


def main():
    repo, ids_file, out_dir = sys.argv[1:4]
    excl = [re.compile(x) for i, x in enumerate(sys.argv) if i > 0 and sys.argv[i - 1] == "--exclude"]
    ids = [x.strip() for x in open(ids_file) if x.strip()]
    t0 = time.time()
    files = []
    with cf.ThreadPoolExecutor(6) as ex:
        for fl in ex.map(lambda i: list_folder(repo, i), ids):
            files += [f for f in fl if not any(r.search(f["path"]) for r in excl)]
    tot = sum(f["size"] for f in files)
    print(f"listed {len(ids)} folders, {len(files)} files, {tot / 1e6:.1f} MB in {time.time() - t0:.0f}s", flush=True)
    done = 0
    with cf.ThreadPoolExecutor(12) as ex:
        for n, b in enumerate(ex.map(lambda f: fetch(repo, f, out_dir), files)):
            done += b
            if n % 200 == 0:
                print(f"  {n}/{len(files)} files, {done / 1e6:.0f} MB, {time.time() - t0:.0f}s", flush=True)
    print(f"done {len(files)} files {tot / 1e6:.1f} MB in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
