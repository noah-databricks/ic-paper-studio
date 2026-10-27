"""Flatten library_parsed into library_documents + library_elements, and crop every figure for the Word export.

Bounding boxes are in the pixel space of the page images ai_parse_document wrote, so the app draws highlights straight
onto those images. Figures are cropped from their page image into the parsed volume (figures/<doc>_<id>.png).
"""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
import io
import json
import os
from collections import Counter
from pathlib import Path

import pymupdf
from PIL import Image

from common import FQ, VOL, sql, w

from common import ROOT
def trim_top_band(img):
    """Drop a sliver of text the parser's figure box caught from the line above (ink, then a blank gap, near the top)."""
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    inked = [any(px[x, y] < 200 for x in range(0, w, 2)) for y in range(min(h, 60))]
    if inked and inked[0]:
        for y in range(1, len(inked)):
            if not inked[y] and all(not v for v in inked[y:y + 4]):
                return img.crop((0, y, w, h))
    return img


def figure_top(e: dict, page_elements: list[dict]) -> float:
    """Where a figure's crop should start: below any 'Figure ...' title line the parser's box overlaps (the paper's
    caption names the figure, so the source's title line would only be duplicated, and is often cut in half)."""
    (x0, y0, x1, y1) = e["bbox"][0]["coord"]
    top = y0
    for o in page_elements:
        if o is e or o["type"] not in ("section_header", "caption", "text", "title") or not o.get("bbox"):
            continue
        ox0, oy0, ox1, oy1 = o["bbox"][0]["coord"]
        text = (o.get("content") or "").strip().lower()
        if text.startswith(("figure", "chart", "exhibit")) and oy1 > y0 - 4 and oy0 < y0 + 80 and ox1 > x0 and ox0 < x1:
            top = max(top, oy1 + 3)
    return top


SKIP = {"page_header", "page_footer", "page_number"}


def stream_pdf(path: str):
    """A library file that is not in the local content folder (added in the workspace)."""
    return pymupdf.open(stream=w.files.download(path).contents.read(), filetype="pdf")

LIBRARY = Path(os.getenv("LIBRARY_DIR", ROOT / "content" / "library"))
_, rows = sql(f"SELECT path, collection, stem, to_json(parsed) FROM {FQ}.library_parsed ORDER BY path")
dupes = [s for s, n in Counter(r[2] for r in rows).items() if n > 1]
assert not dupes, f"file names must be unique across the library: {dupes}"
docs, elements = [], []
for path, collection, stem, raw in rows:
    doc_id = stem
    parsed = json.loads(raw)["document"]
    rel = path.split("/library/", 1)[1]
    local = LIBRARY / rel
    meta = (pymupdf.open(local) if local.exists() else stream_pdf(path)).metadata
    pages = {}
    for p in parsed["pages"]:
        img = Image.open(io.BytesIO(w.files.download(p["image_uri"]).contents.read()))
        pages[p["id"]] = {"uri": p["image_uri"], "w": img.width, "h": img.height, "img": img}
    docs.append({"doc_id": doc_id, "collection": collection, "title": meta.get("title") or stem.replace("_", " ").capitalize(),
                 "issuer": meta.get("author") or "", "subject": meta.get("subject") or "", "pages": len(pages), "path": f"{VOL}/library/{rel}",
                 "page_images": json.dumps([{"page": k + 1, "uri": v["uri"], "width": v["w"], "height": v["h"]} for k, v in sorted(pages.items())])})
    section = ""
    order = 0
    for e in parsed["elements"]:
        if e["type"] in SKIP or not e.get("bbox"):
            continue
        b = e["bbox"][0]
        pid, (x0, y0, x1, y1) = b["page_id"], b["coord"]
        page = pages[pid]
        fig_uri = None
        if e["type"] == "figure":
            pad = 4
            same_page = [o for o in parsed["elements"] if o.get("bbox") and o["bbox"][0]["page_id"] == pid]
            top = figure_top(e, same_page)
            crop = page["img"].crop((max(0, x0 - pad), max(0, top - (pad if top == y0 else 0)), min(page["w"], x1 + pad), min(page["h"], y1 + pad)))
            crop = trim_top_band(crop)
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, "PNG", optimize=True)
            fig_uri = f"{VOL}/parsed/figures/{doc_id}_{e['id']}.png"
            w.files.upload(fig_uri, io.BytesIO(buf.getvalue()), overwrite=True)
        if e["type"] in ("section_header", "title"):
            section = " ".join((e.get("content") or "").split())[:160]
        order += 1
        elements.append({"element_id": f"{doc_id}#{e['id']}", "doc_id": doc_id, "seq": order, "page": pid + 1, "type": e["type"],
                         "content": e.get("content") or "", "description": e.get("description") or "",
                         "x0": x0, "y0": y0, "x1": x1, "y1": y1, "page_width": page["w"], "page_height": page["h"],
                         "page_image": page["uri"], "figure_image": fig_uri, "confidence": e.get("confidence"), "section": section})
    print(doc_id, order, "elements")

for name, data in (("library_documents", docs), ("library_elements", elements)):
    path = f"{VOL}/raw/{name}.json"
    w.files.upload(path, io.BytesIO("\n".join(json.dumps(r) for r in data).encode()), overwrite=True)
    sql(f"CREATE OR REPLACE TABLE {FQ}.{name} AS SELECT * FROM read_files('{path}', format => 'json')")
    print(name, sql(f"SELECT count(*) FROM {FQ}.{name}")[1][0][0])
sql(f"COMMENT ON TABLE {FQ}.library_elements IS 'Parsed source elements (text, tables, figures) with page and bounding box, from ai_parse_document v2. Synthetic.'")
sql(f"COMMENT ON TABLE {FQ}.library_documents IS 'Documents in the library (data rooms, credit policy, research, past papers) with page images. Synthetic.'")
