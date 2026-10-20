"""Download pinned official public development data; never execute upstream code."""

import concurrent.futures
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "data" / "stage6"


def fetch(url):
    with urlopen(url, timeout=90) as response:
        return response.read()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    # Reviewed immutable revisions; refreshing sources requires a deliberate new snapshot.
    infi = "3d6c4a70198e0a41fadf539f5b43c88b8c1a2d9c"
    dab = "e0265538267d9e56f9d773bb6a8cafa136f8c843"
    bird = "f65faf4ae3b638c1fa6df1d3370c8d92c8366301"
    tree = json.loads(
        fetch(f"https://api.github.com/repos/InfiAgent/InfiAgent/git/trees/{infi}?recursive=1")
    )["tree"]
    jobs = []
    prefix = "examples/DA-Agent/data/"
    for item in tree:
        if item["type"] == "blob" and item["path"].startswith(prefix):
            relative = item["path"][len(prefix) :]
            if relative.endswith((".csv", ".jsonl")):
                jobs.append(
                    (
                        "infi/" + relative,
                        f"https://raw.githubusercontent.com/InfiAgent/InfiAgent/{infi}/"
                        + quote(item["path"]),
                        infi,
                    )
                )
    jobs.append(
        (
            "infi/eval_closed_form.py.txt",
            f"https://raw.githubusercontent.com/InfiAgent/InfiAgent/{infi}/examples/DA-Agent/eval_closed_form.py",
            infi,
        )
    )
    for filename in [
        "payments.csv",
        "fees.json",
        "manual.md",
        "merchant_data.json",
        "acquirer_countries.csv",
        "merchant_category_codes.csv",
        "payments-readme.md",
    ]:
        jobs.append(
            (
                "dabstep/" + filename,
                f"https://huggingface.co/datasets/adyen/DABstep/resolve/{dab}/data/context/{filename}",
                dab,
            )
        )
    jobs.append(
        (
            "dabstep/dev.jsonl",
            f"https://huggingface.co/datasets/adyen/DABstep/resolve/{dab}/data/tasks/dev.jsonl",
            dab,
        )
    )
    jobs.append(
        (
            "bird/mini_dev_sqlite.json",
            f"https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/{bird}/data/mini_dev_sqlite-00000-of-00001.json",
            bird,
        )
    )

    def download(job):
        relative, url, revision = job
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = fetch(url)
        if len(data) > 100_000_000:
            raise ValueError("source exceeds 100 MB quota")
        path.write_bytes(data)
        return dict(
            path=relative,
            url=url,
            revision=revision,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(download, jobs))
    manifest = {
        "sources": sorted(records, key=lambda x: x["path"]),
        "licenses": {
            "infi": "CC-BY-NC-4.0",
            "dabstep": "see upstream LICENSE; research use",
            "bird": "CC-BY-SA-4.0",
        },
    }
    (ROOT / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "files": len(records),
                "bytes": sum(x["bytes"] for x in records),
                "revisions": {"infi": infi, "dabstep": dab, "bird": bird},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
