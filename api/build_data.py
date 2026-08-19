"""
Generates the four bundled database files under data/:
    dataset_flat.json, dataset_nested.json            -- JSON source of truth
    dataset_flat_official.toon, dataset_nested_official.toon
        -- TOON source of truth, encoded with the OFFICIAL toon-format
           package (github.com/toon-format/toon-python, pinned commit
           e475c82e9da03dfaf88c0b277dee6b5d17100b13, v0.9.0-beta.1)

Both formats are generated from the SAME fixed-seed values, then verified
to round-trip exactly via the official decoder before being trusted.

Run once (already run -- files are checked into the repo):
    pip install "git+https://github.com/toon-format/toon-python.git@e475c82e9da03dfaf88c0b277dee6b5d17100b13"
    python3 build_data.py
"""
import json
import random
import string
from pathlib import Path

import toon_format

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

    flat_toon = toon_format.encode(flat)
    nested_toon = toon_format.encode(nested)
    (DATA_DIR / "dataset_flat_official.toon").write_text(flat_toon)
    (DATA_DIR / "dataset_nested_official.toon").write_text(nested_toon)

    # sanity check before trusting these files -- full dataset, not a sample
    assert toon_format.decode(flat_toon) == flat, "flat TOON round-trip mismatch!"
    assert toon_format.decode(nested_toon) == nested, "nested TOON round-trip mismatch!"

    print(f"Generated and verified ({RECORD_COUNT:,} records each), official codec v{toon_format.__version__ if hasattr(toon_format, '__version__') else '?'}:")
    print(f"  dataset_flat.json              {len(json.dumps(flat)):>10,} bytes")
    print(f"  dataset_flat_official.toon     {len(flat_toon.encode('utf-8')):>10,} bytes")
    print(f"  dataset_nested.json            {len(json.dumps(nested)):>10,} bytes")
    print(f"  dataset_nested_official.toon   {len(nested_toon.encode('utf-8')):>10,} bytes")
