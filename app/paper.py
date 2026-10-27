"""The credit paper for a deal: which template it uses, what each slot holds, where every part of it came from, and
the Word document it exports to. One paper per deal, shared by every chat in that deal.

The agent fills slots through the tools in tools.py; the analyst accepts, edits or asks for a redo in the Studio. A
text slot is a list of segments, each one either a passage quoted from a parsed source element (kept verbatim, or
lightly edited with the original kept for the diff), a value from a metric view, or the agent's own linking words.
Quotes are checked against the parsed text before they are accepted, so a citation always points at words that are
really on that page.
"""
import copy
import difflib
import re
import io
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import sources as src

log = logging.getLogger("paper")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TDIR = HERE / "templates"


def _registry() -> dict[str, dict]:
    """The firm's paper templates (templates.json): what each is for and when to use it, with its slot spec."""
    f = TDIR / "templates.json"
    reg = json.loads(f.read_text()) if f.exists() else [
        {"template_id": "full_ic", "name": "Investment Committee credit paper", "docx": "credit_paper_template.docx",
         "spec": "template_spec.json", "purpose": "Full credit approval at Investment Committee.",
         "use_when": "A new facility with due diligence, valuation and an indicative term sheet in hand."}]
    return {t["template_id"]: entry(t, json.loads((TDIR / t["spec"]).read_text())["slots"]) for t in reg}


def entry(t: dict, slots: list[dict]) -> dict:
    """A registry entry: the template's description plus its slots in order and by tag."""
    return t | {"slots": slots, "order": [x["tag"] for x in slots], "by_tag": {x["tag"]: x for x in slots}}


def docx_path(t: dict) -> Path:
    """The template's Word file: shipped with the app, or (made in the Studio) cached from the volume."""
    return Path(t["docx_path"]) if t.get("docx_path") else TDIR / t["docx"]


TEMPLATES = _registry()


def template_of(p: dict) -> dict | None:
    return TEMPLATES.get(p.get("template_id") or "")


def SLOTS_OF(p: dict) -> dict:
    t = template_of(p)
    return t["by_tag"] if t else {}


def ORDER_OF(p: dict) -> list[str]:
    t = template_of(p)
    return t["order"] if t else []
DATA = Path(os.getenv("STUDIO_DATA", "/tmp/ic-studio-data"))
PAPERS = DATA / "papers"
_lock = threading.RLock()
_papers: dict[str, dict] = {}
_dirty: set[str] = set()


class FillError(ValueError):
    pass


def _now() -> float:
    return round(time.time(), 3)


def ensure(deal_id: str, owner: str) -> dict:
    """The deal's paper, created empty (no template yet) the first time anyone opens a chat on the deal."""
    with _lock:
        p = get(deal_id)
        if p is None:
            p = {"sid": deal_id, "deal_id": deal_id, "owner": owner, "created": _now(), "updated": _now(), "version": 0,
                 "template_id": None, "template_reason": None, "slots": {}, "activity": []}
            _papers[deal_id] = p
            _save(deal_id)
        return p


def set_template(pid: str, template_id: str, reason: str, who: str) -> dict:
    """Choose (or change) the paper's template. Slots both templates share keep their content; the rest of the old
    draft is kept in the paper's history, so switching back loses nothing."""
    if template_id not in TEMPLATES:
        raise FillError(f"No template '{template_id}'. Templates: {', '.join(TEMPLATES)}")
    p = get(pid)
    with _lock:
        old = p.get("template_id")
        if old and old != template_id:
            p.setdefault("previous_drafts", {})[old] = copy.deepcopy(p["slots"])
        t = TEMPLATES[template_id]
        keep = p["slots"]
        restored = p.get("previous_drafts", {}).get(template_id, {})
        p["slots"] = {tag: (keep.get(tag) if tag in keep and keep[tag].get("status") != "empty" else restored.get(tag))
                      or {"tag": tag, "status": "empty", "kind": t["by_tag"][tag]["kind"]} for tag in t["order"]}
        p["template_id"], p["template_reason"] = template_id, reason
        _touch(p, None, f"template {template_id}", who)
        _save(pid)
    return {"template_id": template_id, "name": t["name"], "slots": len(t["order"]),
            "kept": sum(1 for x in p["slots"].values() if x["status"] != "empty")}


def get(sid: str) -> dict | None:
    with _lock:
        if sid not in _papers:
            f = PAPERS / f"{sid}.json"
            if f.exists():
                _papers[sid] = json.loads(f.read_text())
            else:
                try:  # a fresh container: the paper was snapshotted to the volume
                    _papers[sid] = json.loads(src.sp().files.download(f"{src.VOL}/papers/{sid}.json").contents.read())
                except Exception:
                    return None
        return _papers[sid]


def _save(sid: str):
    PAPERS.mkdir(parents=True, exist_ok=True)
    tmp = PAPERS / f"{sid}.json.tmp"
    tmp.write_text(json.dumps(_papers[sid]))
    tmp.replace(PAPERS / f"{sid}.json")
    _dirty.add(sid)


def flush():
    """Copy changed papers to the volume so they survive a redeploy (called by the app's background loop)."""
    while _dirty:
        sid = _dirty.pop()
        try:
            with _lock:
                data = json.dumps(_papers[sid]).encode()
            src.sp().files.upload(f"{src.VOL}/papers/{sid}.json", io.BytesIO(data), overwrite=True)
        except Exception:
            log.exception("could not snapshot paper %s", sid)


def _touch(p: dict, tag: str | None, what: str, who: str):
    p["version"] += 1
    p["updated"] = _now()
    p["activity"] = (p["activity"] + [{"at": _now(), "tag": tag, "what": what, "who": who}])[-200:]


def use_source(deal_id: str, doc_id: str, how: str, thread: str | None = None):
    """A document the agent read or the analyst attached joins the deal's sources for every chat (citing one does too,
    through the citation itself). Nobody has to pin anything."""
    p = get(deal_id)
    if p is None:
        return
    with _lock:
        u = p.setdefault("sources_used", {}).setdefault(doc_id, {"how": [], "first": _now(), "thread": thread})
        if how in u["how"]:
            return
        u["how"].append(how)
        p["version"] += 1
        p["updated"] = _now()
        _save(deal_id)


def use_table(deal_id: str, key: str, about: dict, query: dict, thread: str | None = None):
    """A table the agent looked at (the deal record, or a metric view it queried) joins the deal's sources, with its last
    few queries and what they returned, so the analyst sees exactly what the agent saw."""
    p = get(deal_id)
    if p is None:
        return
    with _lock:
        t = p.setdefault("tables_used", {}).setdefault(key, {"first": _now(), "thread": thread, "queries": []})
        t.update(about)
        t["queries"] = ([query | {"at": _now()}] + [q for q in t["queries"] if q["sql"] != query["sql"]])[:6]  # the same query again: its latest result
        p["version"] += 1
        p["updated"] = _now()
        _save(deal_id)


def sources_used(p: dict) -> dict[str, dict]:
    """Every document the deal's chats have used: read, attached or cited."""
    out = {k: {"how": list(v["how"]), "first": v["first"]} for k, v in (p.get("sources_used") or {}).items()}
    for s in p.get("slots", {}).values():
        for c in s.get("citations") or []:
            if c.get("kind") == "pdf":
                u = out.setdefault(c["doc_id"], {"how": [], "first": c.get("at") or p.get("updated")})
                if "cited" not in u["how"]:
                    u["how"].append("cited")
    return out


def _slot(p: dict, tag: str, agent: bool = True) -> dict:
    if not p.get("template_id"):
        raise FillError("This deal's paper has no template yet: choose one with set_template first.")
    if tag not in SLOTS_OF(p):
        raise FillError(f"Unknown slot '{tag}' in the {p['template_id']} template. Slots: {', '.join(ORDER_OF(p))}")
    s = p["slots"][tag]
    if agent and s.get("status") == "accepted":
        raise FillError(f"The analyst accepted '{tag}'. Ask them before changing it; they can reopen it.")
    return s


# ---- citations ------------------------------------------------------------------------------------------------------

def _pdf_citation(element_id: str, quote: str | None, token: str | None) -> dict:
    docs, els = src.load(token)
    e = els.get(element_id)
    if not e:
        raise FillError(f"No source element '{element_id}'. Use search_library or read_document for element ids.")
    if quote:
        if not src.find_quote(e, quote):
            raise FillError(f"The quote is not in {element_id} word for word. Copy the exact words from read_document "
                            f"(you may shorten it, but not reword it). Element text starts: {src.as_text(e)[:300]!r}")
    d = docs[e["doc_id"]]
    return {"id": "c_" + uuid.uuid4().hex[:10], "kind": "pdf", "element_id": element_id, "doc_id": e["doc_id"],
            "doc_title": d["title"], "issuer": d["issuer"], "page": e["page"], "type": e["type"],
            "doc_type": d.get("doc_type"), "as_of": d.get("as_of"), "superseded_by": d.get("superseded_by"),
            "quote": src.norm(quote) if quote else None, "source_text": src.as_text(e)[:4000],
            "rects": src.quote_rects(e, quote, token) if quote else [[e["x0"], e["y0"], e["x1"], e["y1"]]],
            "box": [e["x0"], e["y0"], e["x1"], e["y1"]], "page_width": e["page_width"], "page_height": e["page_height"]}


def metric_citation(q: dict, measure: str, row: dict | None = None) -> dict:
    """A governed value: a measure, or a dimension's value (a name, a status) on the queried row."""
    if measure not in q["measures"] and measure not in q["dimensions"]:
        raise FillError(f"'{measure}' is not in that query. It returned measures {q['measures']} and dimensions {q['dimensions']}; "
                        "to cite a dimension's value (e.g. Deal or Sponsor), query it as a dimension first.")
    row = row if row is not None else (q["rows"][0] if q["rows"] else {})
    is_dim = measure in q["dimensions"] and measure not in q["measures"]
    return {"id": "c_" + uuid.uuid4().hex[:10], "kind": "metric", "view": q["view"], "label": src.VIEW_LABELS.get(q["view"], q["view"]),
            "fq": q["fq"], "measure": measure,
            "definition": q["definitions"].get(measure) or ("dimension" if is_dim else None), "value": row.get(measure),
            "formatted": str(row.get(measure)) if is_dim else (row.get("_formatted") or {}).get(measure),
            "dimensions": {d: row.get(d) for d in q["dimensions"]},
            "filters": q["filters"], "sql": q["sql"], "as_of": q["as_of"],
            "catalog_path": "/explore/data/" + q["fq"].replace(".", "/"),
            # what the query returned, as the analyst would read it (the preview shows this before the SQL)
            "table": {"columns": q["dimensions"] + q["measures"],
                      "rows": [[str(r.get(d) or "") for d in q["dimensions"]] + [(r.get("_formatted") or {}).get(m) or "" for m in q["measures"]]
                               for r in q["rows"][:12]]}}


def join(segments: list[dict]) -> str:
    """Segment texts as one sentence: a space between them, except before closing punctuation."""
    out = ""
    for s in segments:
        t = s["text"]
        out += t if not out or t[:1] in ".,;:)%" else " " + t
    return out


def no_internal_names(text: str):
    """The paper is read by the IC: system names (metric views, tables, catalogs) never appear in it; citations carry them."""
    m = src.INTERNAL.search(text or "")
    if m:
        raise FillError(f"'{m.group(0)}' is an internal system name; the IC won't know it. Leave sources to the citations "
                        "(they are numbered and shown beside the paper) and describe the data in plain words if needed.")


def similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, src.norm(a).lower(), src.norm(b).lower()).ratio(), 3)


def _score(slot: dict):
    """How much of a text slot is the source's own wording: verbatim segments count fully, edited ones by their
    similarity to the quote, metric values fully, the agent's own linking words not at all."""
    segs = [s for para in slot.get("paragraphs", []) for s in para["segments"]]
    total = sum(len(s["text"]) for s in segs) or 1
    kept = 0.0
    for s in segs:
        if s["mode"] in ("verbatim", "metric"):
            kept += len(s["text"])
        elif s["mode"] in ("edited", "analyst_edited"):
            kept += len(s["text"]) * s.get("similarity", 0)
    slot["source_wording"] = round(kept / total, 3)
    slot["agent_words"] = sum(len(s["text"]) for s in segs if s["mode"] == "agent")


# ---- fills (the agent's tools call these) ---------------------------------------------------------------------------

def fill_text(sid: str, tag: str, paragraphs: list[dict], who: str, token: str | None, metrics: dict) -> dict:
    """paragraphs: [{"segments": [{"element_id", "quote", "text"?} | {"metric_ref", "measure", "text"?} | {"text"}]}]
    or [{"bullet": true, "segments": [...]}]."""
    p = get(sid)
    slot = _slot(p, tag)
    if SLOTS_OF(p)[tag]["kind"] != "text":
        raise FillError(f"'{tag}' is a {SLOTS_OF(p)[tag]['kind']} slot; use fill_{SLOTS_OF(p)[tag]['kind']}.")
    out_paras, cites = [], []
    for para in paragraphs:
        segs = []
        for s in para.get("segments") or []:
            if s.get("element_id"):
                quote = s.get("quote") or ""
                if not quote:
                    raise FillError("A quoted segment needs the exact quote from the source.")
                c = _pdf_citation(s["element_id"], quote, token)
                text = s.get("text") or c["quote"]
                sim = similarity(text, c["quote"])
                mode = "verbatim" if sim >= 0.985 else "edited"
                if mode == "edited" and sim < 0.45:
                    raise FillError(f"The text for {s['element_id']} is too far from its quote ({sim:.0%} similar). Keep the "
                                    "source's wording: quote it, or shorten it, rather than rewriting it.")
                segs.append({"text": text, "mode": mode, "similarity": sim, "citation": c["id"]})
                cites.append(c)
            elif s.get("metric_ref"):
                q = metrics.get(s["metric_ref"])
                if not q:
                    raise FillError(f"Unknown metric_ref {s['metric_ref']}; use the ref query_metric returned.")
                measure = s.get("measure") or q["measures"][0]
                c = metric_citation(q, measure)
                segs.append({"text": s.get("text") or c["formatted"], "mode": "metric", "citation": c["id"]})
                cites.append(c)
            elif (s.get("text") or "").strip():
                no_internal_names(s["text"])
                segs.append({"text": s["text"], "mode": "agent", "citation": None})
        if segs:
            out_paras.append({"bullet": bool(para.get("bullet")), "segments": segs})
    if not out_paras:
        raise FillError("Nothing to fill.")
    with _lock:
        _archive(slot)
        slot.update(status="filled", paragraphs=out_paras, citations=cites, filled_at=_now(), filled_by=who)
        _score(slot)
        _touch(p, tag, "filled", who)
        _save(sid)
    return _summary(slot)


def fill_table(sid: str, tag: str, who: str, token: str | None, metrics: dict, element_id: str | None = None,
               columns: list[int] | None = None, rows: list[list[str]] | None = None, metric_ref: str | None = None,
               caption: str | None = None, row_sources: list[dict] | None = None) -> dict:
    p = get(sid)
    slot = _slot(p, tag)
    if SLOTS_OF(p)[tag]["kind"] != "table":
        raise FillError(f"'{tag}' is a {SLOTS_OF(p)[tag]['kind']} slot.")
    cites, row_cites = [], {}
    if element_id:
        _, els = src.load(token)
        e = els.get(element_id)
        if not e or e["type"] != "table":
            raise FillError(f"{element_id} is not a parsed table.")
        table = src.table_rows(e["content"])
        if columns:
            table = [[r[i] for i in columns if i < len(r)] for r in table]
        if rows:  # the agent may pick rows, but every cell must come from the source table
            have = {tuple(r) for r in table}
            missing = [r for r in rows if tuple(r) not in have]
            if missing:
                raise FillError(f"Rows not in the source table as written: {missing[:2]}")
            table = [table[0]] + [r for r in rows if tuple(r) != tuple(table[0])]
        cites.append(_pdf_citation(element_id, None, token))
    elif metric_ref:
        q = metrics.get(metric_ref)
        if not q:
            raise FillError(f"Unknown metric_ref {metric_ref}.")
        table = [q["dimensions"] + q["measures"]] + [[str(r.get(d) or "") for d in q["dimensions"]] + [r["_formatted"][m] for m in q["measures"]] for r in q["rows"]]
        cites.append(metric_citation(q, q["measures"][0]))
    elif rows:  # assembled from several cited sources, one per row
        if any(re.fullmatch(r"\s*sources?\s*", h or "", re.I) for h in rows[0]):
            raise FillError("Don't add a Source column: each row's source is its citation (row_sources), numbered beside the paper.")
        for r in rows:
            for cell in r:
                no_internal_names(cell)
        if not row_sources or len(row_sources) != len(rows) - 1:
            raise FillError("An assembled table needs row_sources: one {element_id, quote} or {metric_ref, measure} per body row.")
        table = rows
        for i, rs in enumerate(row_sources, start=1):
            if rs.get("metric_ref"):
                q = metrics.get(rs["metric_ref"])
                if not q:
                    raise FillError(f"Unknown metric_ref {rs['metric_ref']}.")
                c = metric_citation(q, rs.get("measure") or q["measures"][0])
            else:
                c = _pdf_citation(rs["element_id"], rs.get("quote"), token)
            cites.append(c)
            row_cites[str(i)] = c["id"]
    else:
        raise FillError("Give element_id (a source table), metric_ref (a metric view query) or rows with row_sources.")
    with _lock:
        _archive(slot)
        slot.update(status="filled", rows=table, caption=caption, citations=cites, row_citations=row_cites,
                    filled_at=_now(), filled_by=who, source_wording=1.0)
        _touch(p, tag, "filled", who)
        _save(sid)
    return _summary(slot)


def fill_image(sid: str, tag: str, who: str, token: str | None, metrics: dict, element_id: str | None = None,
               chart: dict | None = None, caption: str | None = None) -> dict:
    p = get(sid)
    slot = _slot(p, tag)
    if SLOTS_OF(p)[tag]["kind"] != "image":
        raise FillError(f"'{tag}' is a {SLOTS_OF(p)[tag]['kind']} slot.")
    if element_id:
        _, els = src.load(token)
        e = els.get(element_id)
        if not e or e["type"] != "figure" or not e.get("figure_image"):
            raise FillError(f"{element_id} is not a parsed figure. search_library with a figure's subject, or read_document, lists them.")
        image, cites = {"uri": e["figure_image"], "source": "figure"}, [_pdf_citation(element_id, None, token)]
    elif chart:
        q = metrics.get(chart.get("metric_ref"))
        if not q:
            raise FillError("chart.metric_ref must be a query_metric ref with one dimension.")
        path = render_chart(q, chart.get("type", "bar"), chart.get("title") or caption or "")
        image, cites = {"uri": str(path), "source": "chart"}, [metric_citation(q, q["measures"][0])]
    else:
        raise FillError("Give element_id (a source figure) or chart {metric_ref, type, title}.")
    with _lock:
        _archive(slot)
        slot.update(status="filled", image=image, caption=caption, citations=cites, filled_at=_now(), filled_by=who,
                    source_wording=1.0)
        _touch(p, tag, "filled", who)
        _save(sid)
    return _summary(slot)


def fill_metric(sid: str, tag: str, who: str, metrics: dict, metric_ref: str | None = None, measure: str | None = None,
                text: str | None = None, element_id: str | None = None, quote: str | None = None, token: str | None = None) -> dict:
    p = get(sid)
    slot = _slot(p, tag)
    if SLOTS_OF(p)[tag]["kind"] != "metric":
        raise FillError(f"'{tag}' is a {SLOTS_OF(p)[tag]['kind']} slot.")
    if metric_ref:
        q = metrics.get(metric_ref)
        if not q:
            raise FillError(f"Unknown metric_ref {metric_ref}.")
        c = metric_citation(q, measure or q["measures"][0])
        value = text or c["formatted"]
        if not value:
            raise FillError(f"{measure} has no value for that query; check the filters.")
    elif element_id:
        c = _pdf_citation(element_id, quote, token)
        value = text or quote
    elif text:
        c, value = None, text
    else:
        raise FillError("Give metric_ref, element_id + quote, or text.")
    with _lock:
        _archive(slot)
        slot.update(status="filled", text=value, citations=[c] if c else [], filled_at=_now(), filled_by=who,
                    source_wording=1.0 if c else 0.0)
        _touch(p, tag, "filled", who)
        _save(sid)
    return _summary(slot)


def _archive(slot: dict):
    if slot.get("status") not in (None, "empty"):
        prev = {k: v for k, v in slot.items() if k != "history"}
        slot["history"] = (slot.get("history") or []) + [copy.deepcopy(prev)]


def _summary(slot: dict) -> dict:
    out = {"tag": slot["tag"], "status": slot["status"], "citations": len(slot.get("citations") or [])}
    if "source_wording" in slot:
        out["source_wording"] = slot["source_wording"]
    if (slot.get("image") or {}).get("uri"):  # the figure itself is in the paper, not just its description
        out["image"] = "figure image placed" if slot["image"].get("source") == "figure" else "chart image placed"
    return out


# ---- the analyst's review -------------------------------------------------------------------------------------------

def review(sid: str, tag: str, action: str, who: str, text: str | None = None) -> dict:
    p = get(sid)
    slot = _slot(p, tag, agent=False)
    with _lock:
        if action == "accept":
            slot["status"] = "accepted"
        elif action == "reopen":
            slot["status"] = "filled"
        elif action == "clear":
            _archive(slot)
            for k in [k for k in slot if k not in ("tag", "kind", "history")]:
                slot.pop(k)
            slot["status"] = "empty"
        elif action == "revert":
            if not slot.get("history"):
                raise FillError("Nothing to revert to.")
            prev = slot["history"].pop()
            hist = slot["history"]
            slot.clear()
            slot.update(prev, history=hist)
        elif action == "edit_metric" and slot["kind"] == "metric" and text is not None:
            _archive(slot)
            slot.update(text=text, status="accepted", edited_by=who)
        else:
            raise FillError(f"Unknown review action {action}")
        _touch(p, tag, action, who)
        _save(sid)
    return slot


def analyst_edit(sid: str, tag: str, paragraphs: list[dict], who: str) -> dict:
    """The analyst's own edit of a text slot. Each segment keeps the citation it came from; a cited passage whose words
    changed is marked as edited by the analyst (the original stays on it for the card), and new words are theirs.
    An edited slot counts as accepted: it is the analyst's now, so the agent has to ask before changing it."""
    p = get(sid)
    slot = _slot(p, tag, agent=False)
    if slot["kind"] != "text":
        raise FillError("Only text sections can be edited here.")
    by_id = {c["id"]: c for c in slot.get("citations") or []}
    before = {}
    for para in slot.get("paragraphs") or []:
        for x in para["segments"]:
            if x.get("citation") and x["citation"] not in before:
                before[x["citation"]] = x
    # a passage is judged as a whole: bolding part of it splits it into pieces but does not change its words
    whole: dict[str, str] = {}
    for para in paragraphs:
        for s in para.get("segments") or []:
            if s.get("citation") in by_id and (s.get("text") or "").strip():
                whole[s["citation"]] = (whole.get(s["citation"], "") + " " + s["text"]).strip()
    out = []
    for para in paragraphs:
        segs = []
        for s in para.get("segments") or []:
            text = s.get("text") or ""
            if not text.strip():
                continue
            cid = s.get("citation")
            marks = {k: True for k in ("bold", "italic") if s.get(k)}
            if cid and cid in by_id:
                orig = before.get(cid) or {}
                if src.norm(whole[cid]) == src.norm(orig.get("text", "")):
                    segs.append({**{k: v for k, v in orig.items() if k not in ("bold", "italic", "text")}, "text": text, **marks})
                else:
                    base = by_id[cid].get("quote") or orig.get("text") or ""
                    segs.append({"text": text, "mode": "analyst_edited", "citation": cid, "original": orig.get("text"),
                                 "similarity": similarity(whole[cid], base) if base else 0, **marks})
            else:
                segs.append({"text": text, "mode": "analyst", "citation": None, **marks})
        if segs:
            out.append({"bullet": bool(para.get("bullet")), "segments": segs})
    with _lock:
        _archive(slot)
        used = {x["citation"] for para in out for x in para["segments"] if x.get("citation")}
        keep = [c for c in slot.get("citations") or [] if c["id"] in used or c["kind"] == "metric" and c["id"] not in before]
        slot.update(paragraphs=out, citations=keep, status="accepted", edited_by=who, edited_at=_now())
        _score(slot)
        _touch(p, tag, "edited", who)
        _save(sid)
    return slot


def status(p: dict) -> dict:
    if not p.get("template_id"):
        return {"slots": 0, "filled": 0, "accepted": 0, "citations": 0, "source_wording": None, "template_id": None}
    slots = p["slots"].values()
    filled = [s for s in slots if s["status"] in ("filled", "accepted")]
    texts = [s for s in filled if s["kind"] == "text"]
    chars = sum(sum(len(x["text"]) for para in s["paragraphs"] for x in para["segments"]) for s in texts) or 1
    wording = sum(s.get("source_wording", 0) * sum(len(x["text"]) for para in s["paragraphs"] for x in para["segments"]) for s in texts) / chars
    return {"template_id": p["template_id"], "slots": len(ORDER_OF(p)), "filled": len(filled), "accepted": sum(1 for s in slots if s["status"] == "accepted"),
            "citations": sum(len(s.get("citations") or []) for s in filled), "source_wording": round(wording, 3) if texts else None}


# ---- charts drawn from metric views --------------------------------------------------------------------------------

def render_chart(q: dict, kind: str, title: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if len(q["dimensions"]) != 1:
        raise FillError("A chart needs a query with exactly one dimension.")
    d, m = q["dimensions"][0], q["measures"][0]
    labels = [str(r.get(d)) for r in q["rows"]][:14]
    vals = [float(r.get(m) or 0) for r in q["rows"]][:14]
    fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=200)
    navy, light = "#113d70", "#9fbfdf"
    colors = [navy if i == 0 else light for i in range(len(vals))]
    if kind == "barh":
        ax.barh(labels[::-1], vals[::-1], color=colors[::-1])
    else:
        ax.bar(labels, vals, color=colors)
        plt.xticks(rotation=30, ha="right")
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold", color="#0d1528")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(labelsize=8, colors="#19284d")
    fig.text(0.01, 0.01, f"Source: {q['fq']} ({m})", fontsize=6.5, color="#5b6676")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out = DATA / "charts" / f"{uuid.uuid4().hex[:12]}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


# ---- Word export ----------------------------------------------------------------------------------------------------

def _runs(segments: list[dict]) -> list[dict]:
    """A paragraph's segments as Word runs, with the analyst's bold and italic, spaced the way join() spaces them."""
    out = []
    for i, s in enumerate(segments):
        t = s["text"] if i == 0 or s["text"][:1] in ".,;:)%" else " " + s["text"]
        out.append({"text": t, "bold": s.get("bold"), "italic": s.get("italic")})
    return out


def numbers(p: dict) -> dict[str, int]:
    """Citation id -> its number in the paper, in reading order (the Studio numbers them the same way)."""
    out: dict[str, int] = {}
    for tag in ORDER_OF(p):
        s = p["slots"][tag]
        if s.get("status") in (None, "empty"):
            continue
        ids = [x.get("citation") for para in s.get("paragraphs") or [] for x in para["segments"]] + [c["id"] for c in s.get("citations") or []]
        for i in ids:
            if i and i not in out:
                out[i] = len(out) + 1
    return out


def _doc_cite(c: dict, ref: int | None = None) -> dict:
    if c["kind"] == "metric":
        return {"type": "metric", "source": c["fq"], "label": None, "view_label": c.get("label") or src.VIEW_LABELS.get(c["view"], c["view"]),
                "measure": c["measure"], "query": c["sql"], "value": c.get("formatted"),
                "dimension": c.get("definition") == "dimension", "ref": ref}
    return {"source": f"{c['doc_id']}.pdf", "page": c["page"], "quote": c.get("quote") or "", "title": c.get("doc_title"),
            "issuer": c.get("issuer"), "ref": ref}


def export(sid: str, token: str | None = None, include_unaccepted: bool = True) -> bytes:
    from docfill.fill import fill
    p = get(sid)
    if not p.get("template_id"):
        raise FillError("Choose a template before exporting.")
    values, nums = {}, numbers(p)  # each Word comment carries the citation's number in the paper
    for tag in ORDER_OF(p):
        s = p["slots"][tag]
        if s["status"] not in ("filled", "accepted") or (not include_unaccepted and s["status"] != "accepted"):
            continue
        by_id = {c["id"]: c for c in s.get("citations") or []}
        if s["kind"] == "text":
            paras = [{"text": join(para["segments"]), "runs": _runs(para["segments"]),
                      "citations": [_doc_cite(by_id[x["citation"]], nums.get(by_id[x["citation"]]['id'])) for x in para["segments"] if x.get("citation")]}
                     for para in s["paragraphs"]]
            if s["status"] != "accepted" and paras and not any(para["citations"] for para in paras):
                paras[0]["citations"] = [{"type": "draft"}]
            if all(para["bullet"] for para in s["paragraphs"]):
                values[tag] = {"kind": "text", "bullets": paras}
            else:
                values[tag] = {"kind": "text", "paragraphs": paras}
        elif s["kind"] == "table":
            values[tag] = {"kind": "table", "rows": s["rows"], "caption": s.get("caption"),
                           "citations": [_doc_cite(c, nums.get(c['id'])) for c in s["citations"] if c["id"] not in (s.get("row_citations") or {}).values()],
                           "row_citations": {k: [_doc_cite(by_id[v], nums.get(by_id[v]['id']))] for k, v in (s.get("row_citations") or {}).items()}}
        elif s["kind"] == "image":
            uri = s["image"]["uri"]
            path = Path(uri) if not uri.startswith("/Volumes/") else _local(uri, token)
            values[tag] = {"kind": "image", "image_path": str(path), "caption": s.get("caption"), "width_cm": 15.5,
                           "citations": [_doc_cite(c, nums.get(c['id'])) for c in s["citations"]]}
        elif s["kind"] == "metric":
            cites = [_doc_cite(c, nums.get(c['id'])) for c in s["citations"]]
            if not cites and s["status"] != "accepted":  # the agent's own wording (the recommendation): flag it in Word
                cites = [{"type": "draft"}]
            values[tag] = {"kind": "metric", "text": s["text"], "citations": cites}
    out = DATA / "exports" / f"{sid}.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    fill(docx_path(template_of(p)), values, out)
    return out.read_bytes()


def _local(uri: str, token: str | None) -> Path:
    path = DATA / "figures" / Path(uri).name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(src.volume_file(uri, token))
    return path
