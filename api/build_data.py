"""
Generates the four bundled database files under data/:
    dataset_flat.json, dataset_nested.json   -- JSON source of truth
    dataset_flat.toon, dataset_nested.toon   -- TOON source of truth

Generates 100,000 records per structure (up from 10,000 in earlier versions)
to support the full payload-size matrix (100 / 1,000 / 10,000 / 100,000),
per the "identify a payload-size crossover point" research goal.

Run once (already run -- files are checked into the repo):
    python3 build_data.py

Both formats are generated from the SAME fixed-seed values so the two
databases contain identical underlying data -- but after this script runs,
they are two independent files. main.py never converts one to the other at
runtime; it loads each file directly.
"""
import json
import random
import string
from pathlib import Path

from toon_codec import encode_toon, decode_toon

CITIES = ["Delhi", "Mumbai", "Chennai", "Pune", "Jaipur", "Kolkata", "Nagpur", "Indore", "Bhopal", "Surat"]
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
RECORD_COUNT = 100000


def gen_flat(n, seed=42):
    random.seed(seed)
    return [{"id": i, "name": "".join(random.choices(string.ascii_uppercase, k=6)),
             "age": random.randint(18, 65), "city": random.choice(CITIES)}
            for i in range(1, n + 1)]


def gen_nested(n, seed=42):
    random.seed(seed)
    out = []
    for i in range(1, n + 1):
        rec = {"id": i, "name": "".join(random.choices(string.ascii_uppercase, k=6)),
               "address": {"city": random.choice(CITIES), "zip": random.randint(100000, 999999)}}
        if i % 3 == 0:
            rec["tags"] = random.sample(["vip", "new", "flagged", "trial"], k=2)
        out.append(rec)
    return out


if __name__ == "__main__":
    flat = gen_flat(RECORD_COUNT)
    nested = gen_nested(RECORD_COUNT)

    (DATA_DIR / "dataset_flat.json").write_text(json.dumps(flat))
    (DATA_DIR / "dataset_nested.json").write_text(json.dumps(nested))

    flat_toon = encode_toon(flat, "flat")
    nested_toon = encode_toon(nested, "nested")
    (DATA_DIR / "dataset_flat.toon").write_text(flat_toon)
    (DATA_DIR / "dataset_nested.toon").write_text(nested_toon)

    # sanity check before trusting these files
    assert decode_toon(flat_toon, "flat") == flat, "flat TOON round-trip mismatch!"
    assert decode_toon(nested_toon, "nested") == nested, "nested TOON round-trip mismatch!"

    print(f"Generated and verified ({RECORD_COUNT:,} records each):")
    print(f"  dataset_flat.json    {len(json.dumps(flat)):>10,} bytes")
    print(f"  dataset_flat.toon    {len(flat_toon):>10,} bytes")
    print(f"  dataset_nested.json  {len(json.dumps(nested)):>10,} bytes")
    print(f"  dataset_nested.toon  {len(nested_toon):>10,} bytes")
