"""Download pinned public inference/annotation files; never call a model."""
import hashlib
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REV = "5b256b5a9c17478f06ed6912612e9a14d17065c8"
BASE = f"https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/{REV}/"
FILES = [
    ("data/mini_dev_sqlite.jsonl", "mini_dev_sqlite_2026.jsonl",
     "926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606"),
    ("baseline_inference/mini_dev_sqlite.jsonl", "official_baseline_input_sqlite_2026.jsonl",
     "b060ee9cb69be62a052f5e40367afeb2cf08f345f15ca318fd9a0f9bf52d53ef"),
]


def main():
    directory = ROOT / "data"
    directory.mkdir(exist_ok=True)
    for remote, name, expected in FILES:
        target = directory / name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Existing file has unexpected hash: {target}")
            print(f"Verified existing {name}")
            continue
        with urllib.request.urlopen(BASE + remote, timeout=120) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"Downloaded file has unexpected hash: {name}")
        target.write_bytes(data)
        print(f"Downloaded and verified {name}")


if __name__ == "__main__":
    main()
