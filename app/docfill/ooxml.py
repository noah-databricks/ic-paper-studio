"""Low-level OOXML helpers for Word content controls (w:sdt).

python-docx has no content-control API, so these build the elements directly.
"""

from __future__ import annotations

import itertools

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

_ids = itertools.count(41_000)

NAVY = "113D70"
BLUE = "035595"
MID_BLUE = "4892CC"
LIGHT_BLUE = "E3EEFA"
INK = "19284D"
GUIDE_GREY = "8A94A6"


def _el(tag: str, **attrs) -> OxmlElement:
    e = OxmlElement(tag)
    for k, v in attrs.items():
        e.set(qn(k), str(v))
    return e


def sdt_pr(tag: str, alias: str, kind: str, placeholder: bool = True) -> OxmlElement:
    """kind: text (rich text block), plain (inline plain text), table (rich text block), image (picture)."""
    pr = _el("w:sdtPr")
    pr.append(_el("w:alias", **{"w:val": alias}))
    pr.append(_el("w:tag", **{"w:val": tag}))
    pr.append(_el("w:id", **{"w:val": next(_ids)}))
    if placeholder:
        pr.append(_el("w:showingPlcHdr"))
    if kind == "plain":
        pr.append(_el("w:text"))
    elif kind == "image":
        pr.append(_el("w:picture"))
    # rich text controls (text, table) carry no type element
    return pr


def guide_run(text: str) -> OxmlElement:
    r = _el("w:r")
    rpr = _el("w:rPr")
    rpr.append(_el("w:i"))
    rpr.append(_el("w:color", **{"w:val": GUIDE_GREY}))
    r.append(rpr)
    t = _el("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    return r


def paragraph(style_id: str | None = None, runs: list[OxmlElement] | None = None) -> OxmlElement:
    p = _el("w:p")
    if style_id:
        ppr = _el("w:pPr")
        ppr.append(_el("w:pStyle", **{"w:val": style_id}))
        p.append(ppr)
    for r in runs or []:
        p.append(r)
    return p


def block_sdt(tag: str, alias: str, kind: str, children: list[OxmlElement]) -> OxmlElement:
    sdt = _el("w:sdt")
    sdt.append(sdt_pr(tag, alias, kind))
    content = _el("w:sdtContent")
    for c in children:
        content.append(c)
    sdt.append(content)
    return sdt


def inline_sdt(tag: str, alias: str, guidance: str) -> OxmlElement:
    sdt = _el("w:sdt")
    sdt.append(sdt_pr(tag, alias, "plain"))
    content = _el("w:sdtContent")
    content.append(guide_run(guidance))
    sdt.append(content)
    return sdt


def _insert_ordered(parent, el, successors: tuple[str, ...]) -> None:
    """Insert el before the first existing child named in successors, keeping OOXML schema order."""
    for child in parent:
        if child.tag in {qn(t) for t in successors}:
            child.addprevious(el)
            return
    parent.append(el)


_TCPR_AFTER_SHD = ("w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark")
_TBLPR_AFTER_BORDERS = ("w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription")
_TBLPR_AFTER_CELLMAR = ("w:tblLook", "w:tblCaption", "w:tblDescription")


def shade_cell(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    for old in tcpr.findall(qn("w:shd")):
        tcpr.remove(old)
    _insert_ordered(tcpr, _el("w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": fill}), _TCPR_AFTER_SHD)


def cell_margins(table, top=60, bottom=60, left=90, right=90) -> None:
    tblpr = table._tbl.tblPr
    mar = _el("w:tblCellMar")
    for side, v in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        mar.append(_el(f"w:{side}", **{"w:w": v, "w:type": "dxa"}))
    for old in tblpr.findall(qn("w:tblCellMar")):
        tblpr.remove(old)
    _insert_ordered(tblpr, mar, _TBLPR_AFTER_CELLMAR)


def table_borders(table, color: str = "C9D4E3", size: int = 4, inside_v: bool = False) -> None:
    tblpr = table._tbl.tblPr
    for old in tblpr.findall(qn("w:tblBorders")):
        tblpr.remove(old)
    b = _el("w:tblBorders")
    for side in ("top", "bottom", "insideH") + (("insideV", "left", "right") if inside_v else ()):
        b.append(_el(f"w:{side}", **{"w:val": "single", "w:sz": size, "w:space": 0, "w:color": color}))
    for side in (() if inside_v else ("left", "right", "insideV")):
        b.append(_el(f"w:{side}", **{"w:val": "nil"}))
    # CT_TblBorders child order: top, left, bottom, right, insideH, insideV
    order = ["top", "left", "bottom", "right", "insideH", "insideV"]
    for child in sorted(list(b), key=lambda c: order.index(c.tag.split("}")[1])):
        b.append(child)
    _insert_ordered(tblpr, b, _TBLPR_AFTER_BORDERS)


def repeat_header(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    trpr.append(_el("w:tblHeader"))


def page_field_run(instr: str = "PAGE") -> OxmlElement:
    fld = _el("w:fldSimple", **{"w:instr": instr})
    r = _el("w:r")
    rpr = _el("w:rPr")
    rpr.append(_el("w:color", **{"w:val": "5B667A"}))
    rpr.append(_el("w:sz", **{"w:val": 15}))
    r.append(rpr)
    t = _el("w:t")
    t.text = "1"
    r.append(t)
    fld.append(r)
    return fld
