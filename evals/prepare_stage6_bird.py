"""Fetch the official BIRD Mini-Dev database archive and extract only 11 SQLite assets."""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "data/stage6/bird"
URL = (
    "https://drive.usercontent.google.com/download?"
    "id=13VLWIwpw5E3d5DUkMvzw7hvHE67a4XkG&export=download&confirm=t"
)
ARCHIVE_SHA256 = "aeb211c0e39010bbdae3838bb5e8bd27dc446ed77495b1709f85ccc9bf67f2be"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / "minidev_0703.zip"
    if not archive.exists():
        part = archive.with_suffix(".partial")
        with urlopen(URL, timeout=120) as response, part.open("wb") as output:
            if "text/html" in response.headers.get("Content-Type", ""):
                raise RuntimeError("official download did not return an archive")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 1_000_000_000:
                    raise ValueError("archive exceeds quota")
                output.write(chunk)
        part.rename(archive)
    if sha(archive) != ARCHIVE_SHA256:
        raise ValueError("official archive changed; review and pin a new version")
    records = []
    expected = {x["db_id"] for x in json.loads((ROOT / "mini_dev_sqlite.json").read_text())}
    with zipfile.ZipFile(archive) as source:
        for name in sorted(expected):
            member = f"minidev/MINIDEV/dev_databases/{name}/{name}.sqlite"
            info = source.getinfo(member)
            if info.file_size > 1_000_000_000:
                raise ValueError("database exceeds quota")
            target = ROOT / "databases" / name / (name + ".sqlite")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as input_stream, target.open("wb") as output:
                shutil.copyfileobj(input_stream, output)
            records.append(
                {
                    "path": str(target.relative_to(ROOT)),
                    "sha256": sha(target),
                    "bytes": target.stat().st_size,
                }
            )
    manifest = {
        "url": URL,
        "archive_sha256": ARCHIVE_SHA256,
        "bytes": archive.stat().st_size,
        "databases": records,
    }
    (ROOT / "database-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"databases": len(records), "archive_sha256": ARCHIVE_SHA256}))


if __name__ == "__main__":
    main()
