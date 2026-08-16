"""
TOON codec: encode Python objects to TOON text, and decode TOON text back to
Python objects, for both structures used in this benchmark:

- flat:   uniform records -> compact tabular form
              items[N]{id,name,age,city}:
              1,Alice,20,Delhi
              ...
- nested: irregular records (nested address, optional tags) -> indented
          per-record blocks:
              item:
                id: 1
                name: Alice
                address:
                  city: Delhi
                  zip: 110001
                tags: [vip,new]
              item:
                ...

Both directions are implemented for both structures, so this module is used
to (a) generate the bundled .toon database files once, and (b) parse those
files back into Python objects at server startup so requests can slice by n
and re-encode without ever touching the JSON database file.
"""


def _esc(v):
    s = str(v)
    return f'"{s}"' if ("," in s or "\n" in s) else s


def encode_toon(rows, structure: str) -> str:
    if structure == "flat":
        header = f"items[{len(rows)}]{{id,name,age,city}}:"
        lines = [f"{r['id']},{_esc(r['name'])},{r['age']},{_esc(r['city'])}" for r in rows]
        return "\n".join([header] + lines)

    blocks = []
    for r in rows:
        b = (f"item:\n  id: {r['id']}\n  name: {_esc(r['name'])}\n"
             f"  address:\n    city: {_esc(r['address']['city'])}\n    zip: {r['address']['zip']}")
        if "tags" in r:
            b += f"\n  tags: [{','.join(r['tags'])}]"
        blocks.append(b)
    return "\n".join(blocks)


def decode_toon(text: str, structure: str):
    if structure == "flat":
        return _decode_flat(text)
    return _decode_nested(text)


def _decode_flat(text: str):
    lines = text.strip("\n").split("\n")
    header = lines[0]
    if not (header.startswith("items[") and "]{" in header):
        raise ValueError("Not a valid flat TOON header: expected 'items[N]{fields}:'")
    n = int(header[header.index("[") + 1:header.index("]")])
    fields = header[header.index("{") + 1:header.index("}")].split(",")
    out = []
    for row in lines[1:1 + n]:
        vals = row.split(",")
        rec = {}
        for k, v in zip(fields, vals):
            rec[k] = int(v) if v.lstrip("-").isdigit() else v
        out.append(rec)
    return out


def _decode_nested(text: str):
    out = []
    lines = text.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip() == "item:":
            rec = {}
            i += 1
            # id
            rec["id"] = int(lines[i].strip().split(": ", 1)[1]); i += 1
            # name
            rec["name"] = lines[i].strip().split(": ", 1)[1]; i += 1
            # address:
            i += 1  # skip "address:" line
            city = lines[i].strip().split(": ", 1)[1]; i += 1
            zip_code = int(lines[i].strip().split(": ", 1)[1]); i += 1
            rec["address"] = {"city": city, "zip": zip_code}
            # optional tags
            if i < len(lines) and lines[i].strip().startswith("tags:"):
                tag_str = lines[i].strip().split(": ", 1)[1].strip("[]")
                rec["tags"] = tag_str.split(",") if tag_str else []
                i += 1
            out.append(rec)
        else:
            i += 1
    return out
