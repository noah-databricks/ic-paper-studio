"""The document library as the agent and the Studio read it: parsed and tagged documents, their AI Search index, and
the metric views, in Unity Catalog.

Reads run as the signed-in analyst (their forwarded token), so what the agent can quote is what they may see. The
parsed elements come from ai_parse_document v2 (setup/02_parse.py to 06_index.py); their bounding boxes are in the
pixel space of the page images the parser wrote, which is what the Studio draws highlights on.
"""
import html.parser
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

log = logging.getLogger("sources")
CATALOG = os.getenv("UC_CATALOG", "main")
SCHEMA = os.getenv("UC_SCHEMA", "ic_paper_studio")
FQ = f"{CATALOG}.{SCHEMA}"
VOL = f"/Volumes/{CATALOG}/{SCHEMA}"
WAREHOUSE = os.getenv("WAREHOUSE_ID", "")
DEAL_ID = os.getenv("DEAL_ID", "QCP-RE-2026-0417")
HERE = Path(__file__).resolve().parent
CACHE = Path(os.getenv("STUDIO_CACHE", "/tmp/ic-studio-cache"))

_sp: WorkspaceClient | None = None
_lock = threading.Lock()
_state: dict = {"docs": None, "elements": None, "loaded": 0.0}
RELOAD_S = 900


def sp() -> WorkspaceClient:
    """The app's own identity (its service principal in Apps, the CLI profile locally)."""
    global _sp
    if _sp is None:
        _sp = WorkspaceClient()
    return _sp


def client(token: str | None) -> WorkspaceClient:
    """A client acting as the analyst when the Apps proxy forwarded their token, else as the app."""
    if not token:
        return sp()
    return WorkspaceClient(host=sp().config.host, token=token, auth_type="pat")


def sql(statement: str, token: str | None = None, params: list | None = None, timeout_s: int = 120) -> tuple[list[str], list[list]]:
    w = client(token)
    r = w.statement_execution.execute_statement(warehouse_id=WAREHOUSE, statement=statement, wait_timeout="30s",
                                                parameters=[StatementParameterListItem(name=p["name"], value=p["value"]) for p in params] if params else None)
    end = time.time() + timeout_s
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING) and time.time() < end:
        time.sleep(1)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        msg = r.status.error.message if r.status.error else str(r.status.state)
        raise RuntimeError(msg)
    cols = [c.name for c in r.manifest.schema.columns] if r.manifest and r.manifest.schema and r.manifest.schema.columns else []
    rows = list(r.result.data_array or []) if r.result else []
    chunk = r.result.next_chunk_index if r.result else None
    while chunk is not None:
        c = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
        rows += c.data_array or []
        chunk = c.next_chunk_index
    return cols, rows


def _records(cols, rows) -> list[dict]:
    return [dict(zip(cols, r)) for r in rows]


# ---- source documents and their parsed elements -------------------------------------------------------------------

def load(token: str | None = None, force: bool = False):
    """The library's documents (with their ai_extract metadata) and parsed elements, cached and re-read every 15 minutes
    so a new ingest run shows up without a redeploy.
    The Studio renders citations and checks quotes from this; the agent's own reads go through retrieval() as the user."""
    with _lock:
        if _state["elements"] is not None and not force and time.time() - _state["loaded"] < RELOAD_S:
            return _state["docs"], _state["elements"]
        import json
        docs = _records(*sql(f"""SELECT f.doc_id, f.collection, coalesce(m.title, f.title) AS title, coalesce(m.author, f.issuer) AS issuer, f.subject,
                                        f.pages, f.path, f.page_images, m.doc_type, m.as_of, m.borrower, m.sponsor, m.asset, m.independent,
                                        m.summary, m.superseded_by, to_json(m.counterparty_ids) AS counterparty_ids
                                 FROM {FQ}.library_documents f LEFT JOIN {FQ}.documents m USING (doc_id) ORDER BY f.doc_id""", token))
        for d in docs:
            d["pages"] = int(d["pages"])
            d["page_images"] = json.loads(d["page_images"])
            d["counterparty_ids"] = json.loads(d["counterparty_ids"] or "[]")
        els = _records(*sql(f"""SELECT element_id, doc_id, seq, page, type, content, description, x0, y0, x1, y1,
                                       page_width, page_height, page_image, figure_image, section
                                FROM {FQ}.library_elements ORDER BY doc_id, seq""", token))
        for e in els:
            for k in ("seq", "page"):
                e[k] = int(e[k])
            for k in ("x0", "y0", "x1", "y1", "page_width", "page_height"):
                e[k] = float(e[k])
        _state.update(docs={d["doc_id"]: d for d in docs}, elements={e["element_id"]: e for e in els}, loaded=time.time())
        _state["counterparties"] = _records(*sql(f"SELECT counterparty_id, legal_name, abn, to_json(aliases) AS aliases FROM {FQ}.counterparties", token))
        _state["deal_cps"] = _records(*sql(f"SELECT deal_id, role, counterparty_id FROM {FQ}.deal_counterparties", token))
        _load_attachments()
        return _state["docs"], _state["elements"]


def _load_attachments():
    """Files analysts added to a deal, parsed in earlier sessions (kept as JSON in the papers volume)."""
    import json
    try:
        for f in sp().files.list_directory_contents(f"{VOL}/papers/attachments"):
            if f.path.endswith(".json"):
                rec = json.loads(sp().files.download(f.path).contents.read())
                _state["docs"][rec["doc"]["doc_id"]] = rec["doc"]
                for e in rec["elements"]:
                    _state["elements"][e["element_id"]] = e
    except Exception as e:  # no attachments yet
        log.info("no deal attachments loaded: %s", str(e)[:120])


def counterparties_of(deal_id: str) -> list[dict]:
    """The deal's borrower and sponsor as counterparties in the library."""
    load()
    deal_id = deal_of(deal_id)
    names = {c["counterparty_id"]: c["legal_name"] for c in _state["counterparties"]}
    return [{"role": r["role"], "counterparty_id": r["counterparty_id"], "name": names.get(r["counterparty_id"], r["counterparty_id"])}
            for r in _state["deal_cps"] if r["deal_id"] == deal_id]


def resolve_counterparty(name: str) -> list[dict]:
    """Counterparties a name could mean: exact legal name or alias first, else every one whose name contains it."""
    import json
    load()
    n = " ".join(re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split())
    def names(c):
        return [c["legal_name"]] + json.loads(c["aliases"] or "[]")
    exact = [c for c in _state["counterparties"] if any(" ".join(re.sub(r"[^a-z0-9 ]+", " ", x.lower()).split()) == n for x in names(c))]
    hits = exact or [c for c in _state["counterparties"] if any(n in x.lower() for x in names(c))]
    return [{"counterparty_id": c["counterparty_id"], "legal_name": c["legal_name"], "abn": c["abn"]} for c in hits[:8]]


def deals(token: str | None = None) -> list[dict]:
    cols, rows = sql(f"SELECT * FROM {FQ}.deals ORDER BY deal_name", token)
    return _records(cols, rows)


PROJECTS = json.loads((HERE / "projects.json").read_text())["projects"]
OPENED = f"{VOL}/agent_state/projects.json"  # projects opened from the Studio (Start fresh), kept across redeploys


def projects() -> list[dict]:
    """Every project opened on a deal: the ones shipped with the app, then the ones opened from the Studio."""
    with _lock:
        if "opened" not in _state:
            try:
                _state["opened"] = json.loads(sp().files.download(OPENED).contents.read())["projects"]
            except Exception:  # none opened yet
                _state["opened"] = []
        return PROJECTS + _state["opened"]


def open_project(deal_id: str) -> str:
    """A fresh project on a deal (its own paper, chats and sources), after the deal's latest one: <deal_id>~<n>."""
    rounds = [int(p["id"].split("~")[1]) for p in projects() if deal_of(p["id"]) == deal_id and p["id"].split("~")[1].isdigit()]
    pid = f"{deal_id}~{max(rounds, default=1) + 1}"
    with _lock:
        _state["opened"].append({"id": pid, "opened": time.strftime("%Y-%m-%d %H:%M")})
        sp().files.upload(OPENED, io.BytesIO(json.dumps({"projects": _state["opened"]}, indent=1).encode()), overwrite=True)
    return pid


def deal_of(project_id: str) -> str:
    """The deal a project works on: a fresh project on a deal is the deal id plus "~" and a round."""
    return project_id.split("~")[0]


def projects_of(deal_id: str) -> list[dict]:
    """A deal's projects, oldest first: the deal id itself, then each fresh one opened on it."""
    return [{"id": deal_id}] + [p for p in projects() if deal_of(p["id"]) == deal_id]


def deal(deal_id: str, token: str | None = None) -> dict | None:
    """The deal record behind a project (None for a project nobody opened)."""
    if "~" in deal_id and not any(p["id"] == deal_id for p in projects()):
        return None
    cols, rows = sql(f"SELECT * FROM {FQ}.deals WHERE deal_id = :d", token, [{"name": "d", "value": deal_of(deal_id)}])
    return _records(cols, rows)[0] if rows else None


def add_document(doc: dict, elements: list[dict]):
    """An attachment parsed during a session joins the library for this process."""
    docs, els = load()
    docs[doc["doc_id"]] = doc
    for e in elements:
        els[e["element_id"]] = e


class _Table(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in, self._cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in:
            self._cell.append(data)


def table_rows(content: str) -> list[list[str]]:
    p = _Table()
    p.feed(content or "")
    return p.rows


def as_text(e: dict) -> str:
    """What the agent reads for an element: tables as pipe rows, figures as their description and labels."""
    if e["type"] == "table":
        return "\n".join(" | ".join(r) for r in table_rows(e["content"]))
    if e["type"] == "figure":
        return f"[Figure] {e['description']}".strip()
    return e["content"]


def public(e: dict, full: bool = True) -> dict:
    out = {"element_id": e["element_id"], "doc_id": e["doc_id"], "page": e["page"], "type": e["type"]}
    text = as_text(e)
    out["text"] = text if full else (text[:280] + ("..." if len(text) > 280 else ""))
    if e["type"] == "figure":
        out["has_image"] = bool(e.get("figure_image"))
    return out


# ---- retrieval: what the agent reads, always as the signed-in analyst ----------------------------------------------
INDEX = f"{FQ}.passages_index"
DOC_COLS = "doc_id, title, doc_type, author, as_of, independent, borrower, sponsor, asset, sector, summary, superseded_by, version"


def _doc_ids_for(token: str | None, counterparty: str | None, deal_id: str | None) -> list[str] | None:
    """Documents about a counterparty: 'deal' (this deal's borrower and sponsor), a counterparty id, or a name."""
    if not counterparty:
        return None
    if counterparty == "deal":
        ids = [c["counterparty_id"] for c in counterparties_of(deal_id or "")]
    else:
        ids = [c["counterparty_id"] for c in resolve_counterparty(counterparty)] or [counterparty]
    if not ids:
        return []
    _, rows = sql(f"SELECT DISTINCT doc_id FROM {FQ}.document_entities WHERE counterparty_id IN ({', '.join(f':c{i}' for i in range(len(ids)))})",
                  token, [{"name": f"c{i}", "value": v} for i, v in enumerate(ids)])
    return [r[0] for r in rows]


def _where(doc_type, since, include_superseded, doc_ids, alias="") -> tuple[str, list]:
    a = f"{alias}." if alias else ""
    w, params = ["1 = 1"], []
    types = [doc_type] if isinstance(doc_type, str) else (doc_type or [])
    if types:
        w.append(f"{a}doc_type IN ({', '.join(f':t{i}' for i in range(len(types)))})")
        params += [{"name": f"t{i}", "value": t} for i, t in enumerate(types)]
    if since:
        w.append(f"{a}as_of >= :since")
        params.append({"name": "since", "value": since})
    if not include_superseded:
        w.append(f"{a}superseded_by IS NULL")
    if doc_ids is not None:
        if not doc_ids:
            w.append("1 = 0")
        else:
            w.append(f"{a}doc_id IN ({', '.join(f':d{i}' for i in range(len(doc_ids)))})")
            params += [{"name": f"d{i}", "value": d} for i, d in enumerate(doc_ids)]
    return " AND ".join(w), params


def find_documents(token: str | None, deal_id: str | None = None, query: str | None = None, doc_type=None, counterparty: str | None = None,
                   since: str | None = None, include_superseded: bool = False, k: int = 20) -> list[dict]:
    """Document-level lookup over the tagged library: by type, counterparty, date, and words in the title or summary."""
    where, params = _where(doc_type, since, include_superseded, _doc_ids_for(token, counterparty, deal_id))
    # every word somewhere in what the document is (title, summary, asset, parties, author, type): "Harbour Point intake"
    fields = "lower(concat_ws(' ', title, summary, asset, borrower, sponsor, author, replace(doc_type, '_', ' ')))"
    for i, word in enumerate([w for w in re.findall(r"[a-z0-9$.%]+", (query or "").lower()) if len(w) > 1][:8]):
        where += f" AND {fields} LIKE :q{i}"
        params.append({"name": f"q{i}", "value": f"%{word}%"})
    cols, rows = sql(f"SELECT {DOC_COLS} FROM {FQ}.documents WHERE {where} ORDER BY as_of DESC NULLS LAST LIMIT {int(k)}", token, params)
    return [_card(r) for r in _records(cols, rows)]


def _card(r: dict) -> dict:
    docs, _ = load()
    d = docs.get(r["doc_id"], {})
    out = {k: v for k, v in r.items() if v not in (None, "")}
    out["pages"] = d.get("pages")
    out["independent"] = r.get("independent") in (True, "true")
    if r.get("superseded_by"):
        out["superseded_by"] = r["superseded_by"]
    return out


def search_library(query: str, token: str | None, deal_id: str | None = None, doc_type=None, counterparty: str | None = None,
                   doc_ids: list[str] | None = None, since: str | None = None, include_superseded: bool = False, k: int = 8) -> list[dict]:
    """Hybrid (keyword and semantic) search over the library's passages in AI Search, reranked, then read as the user.

    The index is asked for passage ids only; the passages themselves come from the governed table under the analyst's
    own identity, so a passage they may not read is dropped rather than shown (hence the over-fetch)."""
    import json
    ids = _doc_ids_for(token, counterparty, deal_id)
    if doc_ids:
        ids = [d for d in doc_ids if ids is None or d in ids]
    flt: dict = {}
    if ids is not None:
        if not ids:
            return []
        flt["doc_id"] = ids
    types = [doc_type] if isinstance(doc_type, str) else (doc_type or [])
    if types:
        flt["doc_type"] = types
    if since:
        flt["as_of_int >="] = int(since.replace("-", ""))
    if not include_superseded:
        flt["is_current"] = True
    body = {"query_text": query, "query_type": "HYBRID", "columns": ["element_id"], "num_results": min(50, k * 3),
            "reranker": {"model": "databricks_reranker", "parameters": {"columns_to_rerank": ["search_text"]}}}
    if flt:
        body["filters_json"] = json.dumps(flt)
    r = sp().api_client.do("POST", f"/api/2.0/vector-search/indexes/{INDEX}/query", body=body)
    hits = [(row[0], row[-1]) for row in (r.get("result") or {}).get("data_array") or []]
    if not hits:
        return []
    cols, rows = sql(f"""SELECT p.element_id, p.doc_id, p.page, p.type, p.section, p.text, d.title, d.doc_type, d.author, d.as_of, d.superseded_by
                         FROM {FQ}.passages p JOIN {FQ}.documents d USING (doc_id)
                         WHERE p.element_id IN ({', '.join(f':e{i}' for i in range(len(hits)))})""", token,
                     [{"name": f"e{i}", "value": e} for i, (e, _) in enumerate(hits)])
    got = {r["element_id"]: r for r in _records(cols, rows)}
    out = []
    for eid, score in hits:
        if eid in got and len(out) < k:
            g = got[eid]
            out.append({"element_id": eid, "doc_id": g["doc_id"], "title": g["title"], "doc_type": g["doc_type"], "author": g["author"], "as_of": g["as_of"],
                        "page": int(g["page"]), "type": g["type"], "section": g["section"] or None, "text": _clip(g["text"], 700),
                        "score": round(float(score), 3), **({"superseded_by": g["superseded_by"]} if g["superseded_by"] else {})})
    return out


def _clip(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + " ..."


def grep(pattern: str, token: str | None, deal_id: str | None = None, doc_ids: list[str] | None = None, counterparty: str | None = None,
         doc_type=None, include_superseded: bool = True, k: int = 30) -> list[dict]:
    """Regex match over the library's passage text, as the user: exact figures, clause numbers, names."""
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Not a valid regular expression: {e}")
    ids = _doc_ids_for(token, counterparty, deal_id)
    if doc_ids:
        ids = [d for d in doc_ids if ids is None or d in ids]
    where, params = _where(doc_type, None, include_superseded, ids, "d")
    cols, rows = sql(f"""SELECT p.element_id, p.doc_id, p.page, p.type, p.text, d.title, d.as_of, d.superseded_by
                         FROM {FQ}.passages p JOIN {FQ}.documents d USING (doc_id)
                         WHERE {where} AND regexp_like(p.text, :pat)
                         ORDER BY d.as_of DESC NULLS LAST, p.doc_id, p.page LIMIT {int(k)}""", token,
                     params + [{"name": "pat", "value": pattern if pattern.startswith("(?") else "(?i)" + pattern}])
    rx = re.compile(pattern, re.I)
    out = []
    for r in _records(cols, rows):
        m = rx.search(r["text"])
        a = max(0, m.start() - 160) if m else 0
        out.append({"element_id": r["element_id"], "doc_id": r["doc_id"], "title": r["title"], "as_of": r["as_of"], "page": int(r["page"]),
                    "type": r["type"], "match": (m.group(0) if m else None), "text": ("..." if a else "") + _clip(r["text"][a:], 420),
                    **({"superseded_by": r["superseded_by"]} if r["superseded_by"] else {})})
    return out


def read_document(doc_id: str, token: str | None, pages: list[int] | None = None) -> dict:
    """A document's card and every parsed element in reading order (or some pages), read as the user."""
    docs, els = load()
    if doc_id.startswith("att_"):  # an analyst's attachment, parsed in the app
        if doc_id not in docs:
            raise ValueError(f"No document {doc_id}.")
        d = docs[doc_id]
        items = [public(e) for e in els.values() if e["doc_id"] == doc_id and (not pages or e["page"] in pages)]
        return {"doc_id": doc_id, "title": d["title"], "author": d["issuer"], "pages": d["pages"], "elements": items}
    cols, rows = sql(f"SELECT {DOC_COLS} FROM {FQ}.documents WHERE doc_id = :d", token, [{"name": "d", "value": doc_id}])
    if not rows:
        raise ValueError(f"No document {doc_id} in the library (or you cannot read it). find_documents lists them.")
    card = _card(_records(cols, rows)[0])
    pf = f" AND page IN ({', '.join(str(int(p)) for p in pages)})" if pages else ""
    cols, rows = sql(f"""SELECT element_id, doc_id, page, type, content, description, figure_image FROM {FQ}.library_elements
                         WHERE doc_id = :d{pf} ORDER BY seq""", token, [{"name": "d", "value": doc_id}])
    items = []
    for e in _records(cols, rows):
        e["page"] = int(e["page"])
        items.append(public(e))
    return {**card, "elements": items}


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return " ".join(s.split())


def find_quote(e: dict, quote: str) -> tuple[int, int] | None:
    """Where a quote sits in an element's text (whitespace and typographic quotes normalised), or None."""
    hay, needle = norm(as_text(e)), norm(quote)
    i = hay.find(needle)
    if i < 0:
        i = hay.lower().find(needle.lower())
    return (i, i + len(needle)) if i >= 0 else None


def _pdf(doc_id: str, token: str | None):
    import pymupdf
    path = CACHE / "pdfs" / f"{doc_id}.pdf"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        docs, _ = load(token)
        path.write_bytes(client(token).files.download(docs[doc_id]["path"]).contents.read())
    return pymupdf.open(str(path))


def quote_rects(e: dict, quote: str, token: str | None = None) -> list[list[float]]:
    """The lines a quote occupies on its page, in page-image pixels, found in the PDF text layer inside the element's
    box. Falls back to the element's own box (tables, figures, scanned pages)."""
    box = [e["x0"], e["y0"], e["x1"], e["y1"]]
    if e["type"] not in ("text", "section_header", "title", "caption", "footnote") or not quote:
        return [box]
    try:
        import pymupdf
        pdf = _pdf(e["doc_id"], token)
        page = pdf[e["page"] - 1]
        sx, sy = page.rect.width / e["page_width"], page.rect.height / e["page_height"]
        clip = pymupdf.Rect(e["x0"] * sx - 4, e["y0"] * sy - 4, e["x1"] * sx + 4, e["y1"] * sy + 4)
        quads = page.search_for(norm(quote), clip=clip, quads=False)
        if not quads:  # long quotes: anchor on the first and last words instead
            words = norm(quote).split()
            head, tail = " ".join(words[:6]), " ".join(words[-6:])
            a, b = page.search_for(head, clip=clip), page.search_for(tail, clip=clip)
            if a and b:
                lines = [w for w in page.get_text("words", clip=clip)]
                top, bottom = a[0].y0, b[-1].y1
                quads = []
                for y in sorted({round(w[1], 1) for w in lines if top - 1 <= w[1] <= bottom}):
                    row = [w for w in lines if abs(w[1] - y) < 1.5]
                    x0 = a[0].x0 if abs(y - a[0].y0) < 1.5 else min(w[0] for w in row)
                    x1 = b[-1].x1 if abs(row[0][3] - b[-1].y1) < 1.5 else max(w[2] for w in row)
                    quads.append(pymupdf.Rect(x0, min(w[1] for w in row), x1, max(w[3] for w in row)))
        if quads:
            return [[r.x0 / sx, r.y0 / sy, r.x1 / sx, r.y1 / sy] for r in quads]
    except Exception:
        log.exception("quote location failed for %s", e["element_id"])
    return [box]


THUMB_W = 1100  # wide enough to read a page in a preview, a fraction of the parser's full-resolution page image


def page_image(doc_id: str, page: int, token: str | None = None, size: str = "thumb") -> bytes:
    """A page image: the parser's original ("full"), or a lighter copy for previews ("thumb"), both cached on disk.
    Highlight boxes are in the original's pixels; the Studio scales them by the page's recorded width, so either fits."""
    full = CACHE / "pages" / f"{doc_id}-{page}.jpg"
    if not full.exists():
        docs, _ = load(token)
        uri = next(p["uri"] for p in docs[doc_id]["page_images"] if p["page"] == page)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(client(token).files.download(uri).contents.read())
    if size == "full":
        return full.read_bytes()
    thumb = CACHE / "thumbs" / f"{doc_id}-{page}.jpg"
    if not thumb.exists():
        from PIL import Image
        img = Image.open(full)
        if img.width > THUMB_W:
            img = img.resize((THUMB_W, round(img.height * THUMB_W / img.width)), Image.LANCZOS)
        thumb.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(thumb, "JPEG", quality=78, optimize=True, progressive=True)
    return thumb.read_bytes()


def warm(doc_ids: list[str] | None = None):
    """Fetch and shrink the pages of some documents (or the whole library) ahead of the first preview."""
    docs, _ = load()
    for d in docs.values():
        if doc_ids is not None and d["doc_id"] not in doc_ids:
            continue
        for p in d["page_images"]:
            try:
                page_image(d["doc_id"], p["page"])
            except Exception:
                log.warning("could not warm %s page %s", d["doc_id"], p["page"])


def volume_file(uri: str, token: str | None = None) -> bytes:
    if not uri.startswith(VOL + "/") or ".." in uri:
        raise ValueError("outside the library")
    path = CACHE / "files" / uri[len(VOL) + 1:].replace("/", "__")
    if path.exists() and time.time() - path.stat().st_mtime < 600:  # re-parsed figures show within 10 minutes
        return path.read_bytes()
    data = client(token).files.download(uri).contents.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


# ---- metric views ---------------------------------------------------------------------------------------------------

def _views() -> dict:
    """The metric view definitions (the same YAML the views were created from), for the agent and for citations."""
    text = (HERE / "metric_views.sql").read_text()
    out = {}
    for m in re.finditer(r"CREATE OR REPLACE VIEW \$\{catalog\}\.\$\{schema\}\.(\w+).*?AS \$\$(.*?)\$\$", text, re.S):
        spec = yaml.safe_load(m.group(2))
        out[m.group(1)] = {"name": m.group(1), "fq": f"{FQ}.{m.group(1)}", "comment": spec.get("comment", ""),
                           "source": spec["source"].replace("${catalog}.${schema}", FQ),
                           "dimensions": [{"name": d["name"], "comment": d.get("comment", "")} for d in spec.get("dimensions", [])],
                           "measures": [{"name": x["name"], "comment": x.get("comment", ""), "expr": x["expr"],
                                         "format": x.get("format", {})} for x in spec.get("measures", [])]}
    return out


VIEWS = _views()
# what a reader of the paper calls each governed source (the technical name stays in the query)
VIEW_LABELS = {"mv_deal_metrics": "Quillwood deal records", "mv_portfolio_exposure": "Quillwood loan book",
               "mv_corporate_credit": "Corporate credit metrics"}
INTERNAL = re.compile(r"\bmv_\w+|metric view|unity catalog|\b(source|library)_elements\b|\bpassages_index\b|\b" + re.escape(CATALOG) + r"\.|\b" + re.escape(SCHEMA) + r"\b", re.I)  # the catalog only as part of a full name


def fmt(value, f: dict) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    t = (f or {}).get("type")
    places = ((f or {}).get("decimal_places") or {}).get("places")
    if t == "percentage":
        return f"{v * 100:.{1 if places is None else places}f}%"
    if t == "currency":
        return f"A${v / 1e6:,.1f}M" if abs(v) >= 1e6 else f"A${v:,.0f}"
    if t == "number" and places is not None:
        return f"{v:.{places}f}"
    return f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}"


def query_metric(view: str, measures: list[str], dimensions: list[str] | None = None, filters: dict | None = None,
                 token: str | None = None) -> dict:
    """Query a metric view with MEASURE(); names are checked against its definition, filter values are bound."""
    mv = VIEWS.get(view)
    if not mv:
        raise ValueError(f"Unknown metric view {view}. Known: {', '.join(VIEWS)}")
    mnames = {m["name"]: m for m in mv["measures"]}
    dnames = {d["name"] for d in mv["dimensions"]}
    bad = [m for m in measures if m not in mnames] + [d for d in (dimensions or []) + list((filters or {}).keys()) if d not in dnames]
    if bad:
        raise ValueError(f"Not in {view}: {', '.join(bad)}. Measures: {', '.join(mnames)}. Dimensions: {', '.join(sorted(dnames))}")
    dims = dimensions or []
    select = [f"`{d}`" for d in dims] + [f"MEASURE(`{m}`) AS `{m}`" for m in measures]
    where, params = [], []
    for i, (d, v) in enumerate((filters or {}).items()):
        where.append(f"`{d}` = :p{i}")
        params.append({"name": f"p{i}", "value": str(v)})
    stmt = f"SELECT {', '.join(select)} FROM {mv['fq']}" + (f" WHERE {' AND '.join(where)}" if where else "") + \
        (" GROUP BY ALL" if dims else "") + (f" ORDER BY `{measures[0]}` DESC" if dims else "")
    shown = stmt
    for p in params:
        shown = shown.replace(f":{p['name']}", "'" + p["value"].replace("'", "''") + "'")
    cols, rows = sql(stmt, token, params)
    recs = []
    for r in rows:
        rec = dict(zip(cols, r))
        rec["_formatted"] = {m: fmt(rec.get(m), mnames[m]["format"]) for m in measures}
        recs.append(rec)
    return {"view": view, "fq": mv["fq"], "measures": measures, "dimensions": dims, "filters": filters or {},
            "sql": shown, "rows": recs, "as_of": time.strftime("%Y-%m-%d %H:%M %Z"),
            "definitions": {m: mnames[m]["expr"] for m in measures}}


# ---- attachments ----------------------------------------------------------------------------------------------------

SKIP = {"page_header", "page_footer", "page_number"}


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


def flatten(doc_id: str, parsed: dict, path: str, title: str, issuer: str, token: str | None) -> tuple[dict, list[dict]]:
    """ai_parse_document output to the Studio's document + element records (same shape as setup/03_elements.py)."""
    from PIL import Image
    w = client(token)
    pages = {}
    for p in parsed["pages"]:
        img = Image.open(io.BytesIO(w.files.download(p["image_uri"]).contents.read()))
        pages[p["id"]] = {"uri": p["image_uri"], "w": img.width, "h": img.height, "img": img}
    doc = {"doc_id": doc_id, "title": title, "issuer": issuer, "subject": "Attached by the analyst", "pages": len(pages),
           "path": path, "page_images": [{"page": k + 1, "uri": v["uri"], "width": v["w"], "height": v["h"]} for k, v in sorted(pages.items())]}
    els, seq = [], 0
    for e in parsed["elements"]:
        if e["type"] in SKIP or not e.get("bbox"):
            continue
        b = e["bbox"][0]
        page = pages[b["page_id"]]
        x0, y0, x1, y1 = b["coord"]
        fig = None
        if e["type"] == "figure":
            same_page = [o for o in parsed["elements"] if o.get("bbox") and o["bbox"][0]["page_id"] == b["page_id"]]
            top = figure_top(e, same_page)
            crop = page["img"].crop((max(0, x0 - 4), max(0, top - (4 if top == y0 else 0)), min(page["w"], x1 + 4), min(page["h"], y1 + 4)))
            crop = trim_top_band(crop)
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, "PNG", optimize=True)
            fig = f"{VOL}/parsed/figures/{doc_id}_{e['id']}.png"
            w.files.upload(fig, io.BytesIO(buf.getvalue()), overwrite=True)
        seq += 1
        els.append({"element_id": f"{doc_id}#{e['id']}", "doc_id": doc_id, "seq": seq, "page": b["page_id"] + 1, "type": e["type"],
                    "content": e.get("content") or "", "description": e.get("description") or "", "x0": float(x0), "y0": float(y0),
                    "x1": float(x1), "y1": float(y1), "page_width": float(page["w"]), "page_height": float(page["h"]),
                    "page_image": page["uri"], "figure_image": fig})
    return doc, els


def parse_upload(path: str, token: str | None = None, deal_id: str | None = None) -> dict:
    """Parse a file an analyst added to a deal (in the uploads volume), as the analyst, and keep it with the deal's sources.
    A copy goes to the library inbox, so the next ingest run tags and indexes it for search like every other document."""
    import json
    if not path.startswith(f"{VOL}/uploads/") or ".." in path:
        raise ValueError(f"Attachments live under {VOL}/uploads/.")
    name = path.rsplit("/", 1)[-1]
    doc_id = "att_" + re.sub(r"[^a-z0-9]+", "_", name.rsplit(".", 1)[0].lower()).strip("_")[:40]
    _, rows = sql(f"""SELECT to_json(ai_parse_document(content, map('version', '2.0', 'imageOutputPath', '{VOL}/parsed/',
                      'descriptionElementTypes', '*'))) FROM read_files(:p, format => 'binaryFile')""", token,
                  [{"name": "p", "value": path}], timeout_s=300)
    parsed = json.loads(rows[0][0])
    if parsed.get("error_status"):
        raise ValueError(f"ai_parse_document could not parse {name}: {parsed['error_status']}")
    doc, els = flatten(doc_id, parsed["document"], path, name, "Added by the analyst", token)
    doc["deal_id"] = deal_id
    if name.lower().endswith(".pdf"):
        (CACHE / "pdfs").mkdir(parents=True, exist_ok=True)
        (CACHE / "pdfs" / f"{doc_id}.pdf").write_bytes(client(token).files.download(path).contents.read())
    add_document(doc, els)
    try:
        sp().files.upload(f"{VOL}/library/inbox/{name}", io.BytesIO(client(token).files.download(path).contents.read()), overwrite=True)
    except Exception:
        log.exception("could not copy %s to the library inbox", name)
    try:
        sp().files.upload(f"{VOL}/papers/attachments/{doc_id}.json", io.BytesIO(json.dumps({"doc": doc, "elements": els}).encode()), overwrite=True)
    except Exception:
        log.exception("could not persist attachment %s", doc_id)
    return {"doc_id": doc_id, "title": name, "pages": doc["pages"], "elements": len(els),
            "types": sorted({e["type"] for e in els})}
