"""Analyst-made templates: a copy of one of the firm's templates with sections or slots removed, sections renamed,
and slot guidance, labels and preferred sources rewritten.

The Word file is rebuilt from the template it is based on: removed slots lose their content control (a value inside a
table row loses the row, one inside a sentence loses the sentence), a section with nothing left loses its heading and
everything under it, and numbered headings are renumbered. New templates are kept on the volume so they survive
redeploys, and the agent can pick them like any other.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

import paper as P
import sources as src

STORE = os.getenv("STUDIO_TEMPLATE_STORE", f"{src.VOL}/agent_state/templates")
LOCAL = src.CACHE / "templates"
FIXED = ("Cover",)  # the cover block isn't a Word heading; its values can go but the section stays
EDITABLE = ("alias", "guidance", "preferred_source")
_lock = threading.Lock()
_custom: list[dict] = []


class TemplateError(ValueError):
    pass


def _heading(el) -> str | None:
    """The text of a Heading 1 paragraph, else None."""
    if el.tag != qn("w:p"):
        return None
    st = el.find(qn("w:pPr") + "/" + qn("w:pStyle"))
    if st is None or st.get(qn("w:val")) != "Heading1":
        return None
    return "".join(t.text or "" for t in el.iter(qn("w:t")))


def _set_text(p, text: str):
    ts = list(p.iter(qn("w:t")))
    ts[0].text = text
    for t in ts[1:]:
        t.text = ""


def _tag(sdt) -> str | None:
    t = sdt.find(qn("w:sdtPr") + "/" + qn("w:tag"))
    return t.get(qn("w:val")) if t is not None else None


def _number(name: str) -> tuple[str, str]:
    m = re.match(r"^(\d+)\.\s+(.*)$", name)
    return (m.group(1), m.group(2)) if m else ("", name)


def build(base: dict, edits: dict, out: Path) -> list[dict]:
    """Write the edited Word template to `out` and return its slot spec. `edits` = {sections: {old: new name},
    remove: [tags], slots: {tag: {alias, guidance, preferred_source}}}."""
    renames = {k: v.strip() for k, v in (edits.get("sections") or {}).items() if v and v.strip()}
    remove = set(edits.get("remove") or [])
    unknown = remove - set(base["order"])
    if unknown:
        raise TemplateError(f"Not in this template: {', '.join(sorted(unknown))}")
    doc = Document(str(P.docx_path(base)))
    body = doc.element.body
    sections = [s["section"] for s in base["slots"]]
    gone_sections = {sec for sec in set(sections) if sec not in FIXED and all(s["tag"] in remove for s in base["slots"] if s["section"] == sec)}

    # whole sections: the heading and everything up to the next heading
    kids, drop, cur = list(body), set(), None
    for el in kids:
        h = _heading(el)
        if h is not None:
            cur = h.strip()
        if cur in gone_sections and el.tag != qn("w:sectPr"):
            drop.add(el)
    for el in drop:
        body.remove(el)

    # single slots: the control, or the table row / sentence it sits in
    removed = set()
    for sdt in list(body.iter(qn("w:sdt"))):
        tag = _tag(sdt)
        if tag not in remove or sdt.getparent() is None:
            continue
        row = para = None
        el = sdt.getparent()
        while el is not None and el is not body:
            if el.tag == qn("w:tr") and row is None:
                row = el
            if el.tag == qn("w:p") and para is None:
                para = el
            el = el.getparent()
        if el is None:  # already went with a row or sentence removed above
            continue
        holder = row if row is not None else para if para is not None else sdt
        holder.getparent().remove(holder)
        removed.add(tag)
    # values that went with a removed row or sentence
    left = {_tag(s) for s in body.iter(qn("w:sdt"))}
    removed |= {s["tag"] for s in base["slots"] if s["tag"] not in left}

    # rename, then renumber the numbered headings in reading order
    names, n = {}, 0
    for el in body:
        h = _heading(el)
        if h is None:
            continue
        old = h.strip()
        num, title = _number(old)
        if old in renames:
            title = _number(renames[old])[1]
        if num:
            n += 1
            new = f"{n}. {title}"
        else:
            new = title
        names[old] = new
        _set_text(el, new)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))

    per_slot = edits.get("slots") or {}
    slots = []
    for s in base["slots"]:
        if s["tag"] in removed:
            continue
        s = dict(s) | {k: str(v).strip() for k, v in (per_slot.get(s["tag"]) or {}).items() if k in EDITABLE and v is not None}
        s["section"] = names.get(s["section"], s["section"])
        slots.append(s)
    if not slots:
        raise TemplateError("A template needs at least one slot.")
    return slots


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "template"
    tid, i = base, 2
    while tid in P.TEMPLATES:
        tid, i = f"{base}_{i}", i + 1
    return tid


def _persist():
    src.sp().files.upload(f"{STORE}/registry.json", io.BytesIO(json.dumps({"templates": _custom}, indent=1).encode()), overwrite=True)


def _register(t: dict):
    P.TEMPLATES[t["template_id"]] = P.entry(t, t["slots_spec"])


def save(template_id: str | None, base_id: str, name: str, purpose: str, use_when: str, edits: dict, who: str) -> dict:
    """Save edits to `base_id` as a new template (template_id None) or over an analyst-made one."""
    base = P.TEMPLATES.get(base_id)
    if not base:
        raise TemplateError(f"No template '{base_id}'.")
    name = (name or "").strip()
    if not name:
        raise TemplateError("Give the template a name.")
    with _lock:
        if template_id is not None:
            mine = next((t for t in _custom if t["template_id"] == template_id), None)
            if not mine:
                raise TemplateError("Only templates made in the Studio can be changed; save this one as a new template.")
        elif any(t["name"].lower() == name.lower() for t in P.TEMPLATES.values()):
            raise TemplateError(f"There is already a template called '{name}'.")
        tid = template_id or _slug(name)
        local = LOCAL / f"{tid}.docx"
        slots = build(base, edits, local)
        (LOCAL / f"{tid}.png").unlink(missing_ok=True)  # an uploaded template's plain thumbnail shows its sections
        src.sp().files.upload(f"{STORE}/{tid}.docx", io.BytesIO(local.read_bytes()), overwrite=True)
        t = {"template_id": tid, "name": name, "purpose": (purpose or "").strip(), "use_when": (use_when or "").strip(),
             "docx": f"{tid}.docx", "docx_path": str(local), "based_on": base.get("based_on") or base["template_id"],
             "custom": True, "made_by": who, "slots_spec": slots}
        _custom[:] = [x for x in _custom if x["template_id"] != tid] + [t]
        _register(t)
        _persist()
    return t


def load():
    """Analyst-made templates from the volume, into the registry (startup)."""
    try:
        reg = json.loads(src.sp().files.download(f"{STORE}/registry.json").contents.read())["templates"]
    except Exception:  # none made yet
        return
    LOCAL.mkdir(parents=True, exist_ok=True)
    with _lock:
        for t in reg:
            local = LOCAL / f"{t['template_id']}.docx"
            try:
                local.write_bytes(src.sp().files.download(f"{STORE}/{t['template_id']}.docx").contents.read())
            except Exception:
                continue
            t["docx_path"] = str(local)
            _custom.append(t)
            _register(t)


def _text(el) -> str:
    return " ".join("".join(t.text or "" for t in el.iter(qn("w:t"))).split())


def _style_name(doc, el) -> str:
    st = el.find(qn("w:pPr") + "/" + qn("w:pStyle")) if el.tag == qn("w:p") else None
    if st is None:
        return ""
    sid = st.get(qn("w:val"))
    try:
        return doc.styles.get_by_id(sid, 1).name or sid  # 1 = paragraph style
    except Exception:
        return sid


def _level(doc, el) -> int | None:
    m = re.match(r"^heading\s*(\d)$", _style_name(doc, el).strip().lower())
    return int(m.group(1)) if m else None


def _tagged(doc, body) -> list[dict]:
    """Slots of a Word file that already has tagged content controls (one of ours, or made the same way)."""
    slots, section = [], "Cover"
    for el in body.iter():
        if el.tag == qn("w:p") and _level(doc, el) == 1:
            section = _text(el) or section
        if el.tag != qn("w:sdt"):
            continue
        tag = _tag(el)
        if not tag:
            continue
        pr = el.find(qn("w:sdtPr"))
        alias = pr.find(qn("w:alias"))
        alias = alias.get(qn("w:val")) if alias is not None else tag.replace("_", " ")
        inline = el.getparent().tag == qn("w:p") or any(a.tag == qn("w:tc") for a in el.iterancestors())
        content = el.find(qn("w:sdtContent"))
        kind = ("image" if pr.find(qn("w:picture")) is not None else "metric" if inline
                else "table" if content is not None and content.find(".//" + qn("w:tbl")) is not None else "text")
        guide = re.sub(r"^\[[^\]]*\]\s*", "", _text(content) if content is not None else "")
        slots.append({"tag": tag, "section": section, "kind": kind, "alias": alias, "guidance": guide or alias, "preferred_source": ""})
    return slots


def convert(data: bytes, out: Path) -> list[dict]:
    """Turn an uploaded Word document into a template and return its slot spec. A file with tagged content controls
    keeps them. Otherwise every heading starts a slot: the text under it becomes one text slot (its wording becomes
    the guidance), each table a table slot and each picture a chart-or-figure slot. Sections are the Heading 1s."""
    from docfill import ooxml

    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise TemplateError("That isn't a Word document (.docx) we can open.")
    body = doc.element.body
    if any(_tag(s) for s in body.iter(qn("w:sdt"))):
        slots = _tagged(doc, body)
        doc.save(str(out))
        return slots
    kids = [el for el in body if el.tag != qn("w:sectPr")]
    heads = [i for i, el in enumerate(kids) if _level(doc, el) is not None]
    if not heads:
        raise TemplateError("No headings found. Mark each section heading with Word's Heading 1 style, then upload again.")
    top = min(_level(doc, kids[i]) for i in heads)
    used, slots, section = set(), [], None

    def tag_for(alias: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", alias.lower()).strip("_")[:40] or "slot"
        t, i = base, 2
        while t in used:
            t, i = f"{base}_{i}", i + 1
        used.add(t)
        return t

    for n, i in enumerate(heads):
        h = kids[i]
        title = _text(h)
        if _level(doc, h) == top:
            section = title
        title_plain = re.sub(r"^\d+(\.\d+)*\.?\s+", "", title) or title
        rng = kids[i + 1: heads[n + 1] if n + 1 < len(heads) else len(kids)]
        text_paras, first, made = [], None, 0
        for el in rng:
            if el.tag == qn("w:tbl"):
                cols = [_text(c) for c in el.findall(qn("w:tr"))[0].findall(qn("w:tc"))] if el.findall(qn("w:tr")) else []
                alias = f"{title_plain} table"
                guide = f"Table with columns: {', '.join(c for c in cols if c)}." if any(cols) else f"Table for {title_plain}."
                sdt = ooxml.block_sdt(tag_for(alias), alias, "table", [ooxml.paragraph(None, [ooxml.guide_run(f"[{alias}] {guide}")])])
                el.addprevious(sdt)
                body.remove(el)
                made += 1
                slots.append({"tag": _tag(sdt), "section": section, "kind": "table", "alias": alias, "guidance": guide, "preferred_source": ""})
            elif el.tag == qn("w:p") and el.find(".//" + qn("w:drawing")) is not None:
                alias = f"{title_plain} chart"
                guide = "One chart or figure from the sources."
                sdt = ooxml.block_sdt(tag_for(alias), alias, "text", [ooxml.paragraph(None, [ooxml.guide_run(f"[{alias}] {guide}")])])
                el.addprevious(sdt)
                body.remove(el)
                made += 1
                slots.append({"tag": _tag(sdt), "section": section, "kind": "image", "alias": alias, "guidance": guide, "preferred_source": ""})
            elif el.tag == qn("w:p"):
                if first is None:
                    first = el
                if _text(el):
                    text_paras.append(_text(el))
        has_sub = n + 1 < len(heads) and _level(doc, kids[heads[n + 1]]) > _level(doc, h)
        if not text_paras and (made or (not rng and has_sub)):
            for el in rng:
                if el.tag == qn("w:p") and el.getparent() is not None:
                    body.remove(el)
            continue  # only a table or figure here, or everything sits in its sub-headings
        guide = re.sub(r"[\[\]]", "", " ".join(text_paras))[:600] or f"Write the {title_plain} section."
        sdt = ooxml.block_sdt(tag_for(title_plain), title_plain, "text", [ooxml.paragraph(None, [ooxml.guide_run(f"[{title_plain}] {guide}")])])
        # the text slot goes where the section's text was (straight after the heading if it had none)
        if first is not None:
            first.addprevious(sdt)
        else:
            h.addnext(sdt)
        for el in rng:
            if el.tag == qn("w:p") and el.getparent() is not None:
                body.remove(el)
        slots.append({"tag": _tag(sdt), "section": section or title, "kind": "text", "alias": title_plain, "guidance": guide, "preferred_source": ""})
    # spec order = reading order in the file
    order = [_tag(s) for s in body.iter(qn("w:sdt"))]
    slots.sort(key=lambda s: order.index(s["tag"]))
    doc.save(str(out))
    return slots


def upload(data: bytes, filename: str, name: str, purpose: str, use_when: str, who: str) -> dict:
    """A Word document uploaded as a new template."""
    name = (name or "").strip() or re.sub(r"[_-]+", " ", Path(filename or "Template").stem).strip()
    with _lock:
        if any(t["name"].lower() == name.lower() for t in P.TEMPLATES.values()):
            raise TemplateError(f"There is already a template called '{name}'.")
        tid = _slug(name)
        local = LOCAL / f"{tid}.docx"
        local.parent.mkdir(parents=True, exist_ok=True)
        slots = convert(data, local)
        if not slots:
            raise TemplateError("Nothing to fill was found in that document.")
        src.sp().files.upload(f"{STORE}/{tid}.docx", io.BytesIO(local.read_bytes()), overwrite=True)
        t = {"template_id": tid, "name": name, "purpose": (purpose or "").strip(), "use_when": (use_when or "").strip(),
             "docx": f"{tid}.docx", "docx_path": str(local), "based_on": None, "uploaded": filename,
             "custom": True, "made_by": who, "slots_spec": slots}
        _custom.append(t)
        _register(t)
        _persist()
    return t


def thumb(t: dict) -> Path:
    """A plain first-page picture for an uploaded template (the app can't render Word pages): its name and sections."""
    from PIL import Image, ImageDraw, ImageFont

    f = LOCAL / f"{t['template_id']}.png"
    if f.exists():
        return f
    img = Image.new("RGB", (620, 877), "white")
    d = ImageDraw.Draw(img)
    try:
        big, small = ImageFont.truetype("DejaVuSans-Bold.ttf", 30), ImageFont.truetype("DejaVuSans.ttf", 19)
    except Exception:
        big, small = ImageFont.load_default(size=30), ImageFont.load_default(size=19)
    d.rectangle([40, 60, 580, 180], fill="#113d70")
    d.text((62, 95), t["name"][:30], fill="white", font=big)
    y, seen = 220, []
    for s in t["slots"]:
        if s["section"] and s["section"] not in seen:
            seen.append(s["section"])
            d.text((62, y), s["section"][:40], fill="#113d70", font=small)
            d.rectangle([62, y + 30, 540, y + 36], fill="#e3eefa")
            y += 62
            if y > 820:
                break
    f.parent.mkdir(parents=True, exist_ok=True)
    img.save(f)
    return f
