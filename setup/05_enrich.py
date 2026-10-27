"""Tag every library document and build what retrieval runs on.

1. ai_classify (document type) and ai_extract (author, date, version, borrower, ABN, sponsor, asset, independence, summary)
   straight on the ai_parse_document output, once per document (incremental, like the parse).
2. Customer linking: the names a document prints are matched to counterparties by ABN, then by exact legal name,
   then by a name that is the start of exactly one other (an ambiguous short name stays unlinked rather than guessed).
3. Superseded documents: a later document of the same type, about the same counterparty (or from the same author when
   it is about none), that says it replaces an earlier version marks that one superseded.
4. passages: one row per parsed element worth searching, each with a short header of its document's context, which is
   what AI Search embeds. Merged (not replaced) so the index syncs only what changed.
"""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
import html.parser
import io
import json
import re
import sys
from collections import defaultdict

from common import FQ, VOL, sql, w

SCHEMA = {
    "title": {"type": "string", "description": "Document title as printed on the cover"},
    "author": {"type": "string", "description": "Organisation that prepared the document (valuer, QS firm, research house, sponsor, adviser or Quillwood Credit Partners)"},
    "prepared_for": {"type": "string", "description": "Who the document is addressed to or prepared for, if stated"},
    "as_of": {"type": "string", "description": "Report, valuation or document date in YYYY-MM-DD format"},
    "version": {"type": "string", "description": "Version or draft label if printed (for example v1, Draft, Final, v4.0)"},
    "supersedes": {"type": "string", "description": "The earlier document or version this one says it replaces or supersedes, if it says so"},
    "borrower": {"type": "string", "description": "Legal name of the borrower or company the document is about, exactly as printed"},
    "borrower_abn": {"type": "string", "description": "ABN or ACN of that borrower, digits only, if printed"},
    "sponsor": {"type": "string", "description": "Sponsor, developer group or owner behind the borrower, if any"},
    "other_parties": {"type": "array", "items": {"type": "string"}, "description": "Other named companies involved (guarantors, acquisition targets, builders, major tenants); not the author"},
    "asset": {"type": "string", "description": "Property or asset address or name, if the document is about a property"},
    "state": {"type": "enum", "labels": ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT", "National", "None"]},
    "sector": {"type": "string", "description": "Industry or property sector, in a few words"},
    "independent": {"type": "boolean", "description": "True if prepared by a party independent of the borrower and sponsor (valuer, QS, auditor, research house, the lender); false if prepared by the borrower, the sponsor or their adviser"},
    "summary": {"type": "string", "description": "Two sentences on what the document is and its key conclusion or figures"},
}
TYPES = {
    "valuation": "A valuation report or desktop valuation itself (not an email or note about one)",
    "qs_report": "Quantity surveyor or cost report on a construction project",
    "builder_assessment": "Assessment of a builder's capacity, financials or track record",
    "information_memorandum": "Information memorandum, teaser or management presentation prepared to raise finance",
    "deal_intake": "Origination or deal intake note, early approach or request to credit",
    "term_sheet": "Term sheet or indicative terms for a facility",
    "financials": "Financial statements, management accounts or financial due diligence",
    "compliance_certificate": "Covenant compliance certificate from a borrower",
    "facility_summary": "Summary of an existing facility's terms and status",
    "variation_request": "Request to vary, extend or upsize an existing facility",
    "ic_paper": "Credit or investment committee paper recommending a facility",
    "committee_minutes": "Minutes or record of a credit or investment committee decision",
    "credit_policy": "Lending or credit policy, limits or portfolio concentration rules",
    "research": "Sector, market or economic research note",
    "leasing_report": "Leasing, occupancy or tenancy report",
    "correspondence": "Email, letter, memo or meeting note, whatever it is about (including one about a valuation or a deal)",
    "other": "Anything else",
}
TYPE_LABEL = {"valuation": "Valuation", "qs_report": "QS report", "builder_assessment": "Builder assessment", "information_memorandum": "Information memorandum",
              "deal_intake": "Deal intake note", "term_sheet": "Term sheet", "financials": "Financials", "compliance_certificate": "Compliance certificate",
              "facility_summary": "Facility summary", "variation_request": "Variation request", "ic_paper": "IC paper", "committee_minutes": "Committee minutes",
              "credit_policy": "Credit policy", "research": "Research", "leasing_report": "Leasing report", "correspondence": "Correspondence", "other": "Document"}


def q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ---- 1. classify and extract (new documents only) -------------------------------------------------------------------
sql(f"""CREATE TABLE IF NOT EXISTS {FQ}.document_enrichment (doc_id STRING, extracted VARIANT, doc_type VARIANT, enriched_at TIMESTAMP)
COMMENT 'ai_extract and ai_classify output per library document'""")
sql(f"""INSERT INTO {FQ}.document_enrichment
SELECT stem, ai_extract(parsed, '{q(json.dumps(SCHEMA))}', map('version', '2.1')),
       ai_classify(parsed, '{q(json.dumps(TYPES))}', map('version', '2.1')), current_timestamp()
FROM {FQ}.library_parsed WHERE stem NOT IN (SELECT doc_id FROM {FQ}.document_enrichment)""", timeout_s=3600)
sql(f"DELETE FROM {FQ}.document_enrichment WHERE doc_id NOT IN (SELECT stem FROM {FQ}.library_parsed)")
if "--reclassify" in sys.argv:  # the document types changed: classify everything again (extraction is kept)
    sql(f"""MERGE INTO {FQ}.document_enrichment e USING {FQ}.library_parsed p ON e.doc_id = p.stem
            WHEN MATCHED THEN UPDATE SET doc_type = ai_classify(p.parsed, '{q(json.dumps(TYPES))}', map('version', '2.1'))""", timeout_s=3600)

_, rows = sql(f"""SELECT e.doc_id, to_json(e.extracted), to_json(e.doc_type), d.title, d.issuer, d.collection,
                         regexp_extract(p.path, '/data_room/([^/]+)/', 1)
                  FROM {FQ}.document_enrichment e JOIN {FQ}.library_documents d ON d.doc_id = e.doc_id
                  JOIN {FQ}.library_parsed p ON p.stem = e.doc_id""")


def iso_date(s: str | None) -> str | None:
    """ai_extract is asked for YYYY-MM-DD; take '24 September 2026' or 'September 2026' too."""
    from datetime import datetime
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    for f in ("%d %B %Y", "%B %Y", "%d %b %Y", "%b %Y", "%Y-%m"):
        try:
            return datetime.strptime(s.strip(), f).date().isoformat()
        except ValueError:
            pass
    return None


def val(x):
    """ai_extract v2.1 wraps each value as {"value": ...}; arrays hold such objects."""
    if isinstance(x, dict) and "value" in x:
        return x["value"]
    if isinstance(x, list):
        return [val(i) for i in x if val(i)]
    return x


docs = {}
room_of = {}  # the borrower whose data room a document sits in (its folder), e.g. harbour_point_developments
for doc_id, ext, cls, pdf_title, pdf_author, collection, room in rows:
    room_of[doc_id] = room or None
    r = (json.loads(ext) or {}).get("response") or {}
    m = {k: val(r.get(k)) for k in SCHEMA}
    c = (json.loads(cls) or {}).get("response")
    t = val(c[0] if isinstance(c, list) and c else c) or "other"
    as_of = iso_date(m["as_of"])
    t = t if t in TYPES else "other"
    if collection == "correspondence" and t not in ("correspondence", "committee_minutes"):
        t = "correspondence"  # it came from the mailbox, whatever it is about
    docs[doc_id] = {"doc_id": doc_id, "collection": collection, "title": pdf_title or m["title"] or doc_id, "doc_type": t,
                    "author": m["author"] or pdf_author or None, "prepared_for": m["prepared_for"], "as_of": as_of, "version": m["version"],
                    "supersedes_text": m["supersedes"], "borrower": m["borrower"], "borrower_abn": re.sub(r"\D", "", m["borrower_abn"] or "") or None,
                    "sponsor": m["sponsor"], "other_parties": m["other_parties"] or [], "asset": m["asset"],
                    "state": None if m["state"] in (None, "None") else m["state"], "sector": m["sector"],
                    "independent": m["independent"], "summary": m["summary"]}
print(len(docs), "documents tagged")

# ---- 2. customer linking ----------------------------------------------------------------------------------------------
SUFFIX = {"pty", "ltd", "limited", "proprietary", "plc", "inc", "the", "co", "company"}


def norm(name: str | None) -> tuple:
    return tuple(t for t in re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and ")).split() if t not in SUFFIX)


_, deal_rows = sql(f"SELECT deal_id, deal_name, borrower, sponsor FROM {FQ}.deals")
def name_of(text: str | None) -> str | None:
    """The company name in what a document printed: 'X Group (trading as Y)' is Y; a bracketed note or 'and related
    entities' is dropped; a lowercase phrase ('existing facility') is not a company."""
    if not text:
        return None
    m = re.search(r"trading as ([^)]+)\)", text)
    name = (m.group(1) if m else re.sub(r"\s*\(.*?\)|\s+and related entities.*$", "", text)).strip(" .,")
    return name if name and name[0].isupper() else None


mentions = []  # (doc_id or None, role, name, abn)
for d in docs.values():
    if name_of(d["borrower"]):
        mentions.append((d["doc_id"], "borrower", name_of(d["borrower"]), d["borrower_abn"]))
    if name_of(d["sponsor"]):
        mentions.append((d["doc_id"], "sponsor", name_of(d["sponsor"]), None))
    for o in d["other_parties"]:
        o = name_of(o)
        if o and norm(o) and norm(o) != norm(d["author"]):
            mentions.append((d["doc_id"], "party", o, None))
for deal_id, _, borrower, sponsor in deal_rows:
    mentions += [(None, "borrower", borrower, None), (None, "sponsor", sponsor, None)]

# counterparties: canonical names are the ones with an ABN, a legal suffix (Pty Ltd, Limited) or on a deal record; a
# shorter name (no suffix) is the same counterparty when it is the start of exactly one canonical name, else its own
LEGAL = re.compile(r"\b(pty|ltd|limited|plc|inc)\b", re.I)
deal_names = {norm(x) for _, _, b_, s_ in deal_rows for x in (b_, s_)}
by_abn, by_name, names = {}, {}, {}
for _, _, name, _ in mentions:
    if norm(name) and len(name) > len(names.get(norm(name), "")):
        names[norm(name)] = name
for _, _, name, abn in mentions:
    n = norm(name)
    if n and (abn or LEGAL.search(name) or n in deal_names):
        cid = by_name.get(n) or (by_abn.get(abn) if abn else None) or "_".join(n)
        by_name[n] = cid
        if abn:
            by_abn.setdefault(abn, cid)
        if len(name) > len(names.get(n, "")):
            names[n] = name
strong = {norm(nm) for _, _, nm, ab in mentions if norm(nm) and (ab or LEGAL.search(nm))}
partial = set()
for n in [k for k in by_name if k not in strong]:  # a deal record's short name folds into the one legal name it starts
    longer = {cid for k, cid in by_name.items() if k in strong and len(k) > len(n) and k[:len(n)] == n}
    if len(longer) == 1:
        by_name[n] = longer.pop()
        partial.add(n)
canonical = dict(by_name)
for _, _, name, abn in mentions:
    n = norm(name)
    if not n or n in by_name:
        continue
    longer = {cid for k, cid in canonical.items() if len(k) > len(n) and k[:len(n)] == n}
    shorter = {cid for k, cid in canonical.items() if len(k) >= 2 and len(k) < len(n) and n[:len(k)] == k and set(n[len(k):]) <= {"group", "holdings"}}
    if len(longer) == 1 or (not longer and len(shorter) == 1):
        by_name[n] = (longer or shorter).pop()
        partial.add(n)
    elif not longer and len(n) >= 2:
        by_name[n] = "_".join(n)  # a group or trading name seen only like this
        names.setdefault(n, name)


def resolve(name: str, abn: str | None) -> tuple[str | None, str]:
    n = norm(name)
    if abn and abn in by_abn:
        return by_abn[abn], "abn"
    if n in by_name:
        return by_name[n], "partial" if n in partial else "name"
    longer = {cid for k, cid in by_name.items() if len(k) > len(n) and k[:len(n)] == n}
    if n and len(longer) == 1:
        return longer.pop(), "partial"
    return None, "unresolved"


entities, seen = [], set()
for doc_id, role, name, abn in mentions:
    if doc_id is None:
        continue
    cid, how = resolve(name, abn)
    key = (doc_id, role, cid or name)
    if key in seen:
        continue
    seen.add(key)
    entities.append({"doc_id": doc_id, "role": role, "name_as_written": name, "abn": abn, "counterparty_id": cid, "match": how})
# a borrower's data room: what sits in data_room/<borrower>/ is that borrower's, whatever names the document prints
# (an intake note may name only the sponsor, or a short name two counterparties share)
for doc_id, room in room_of.items():
    cid = resolve(room.replace("_", " "), None)[0] if room else None  # the folder is the borrower's name, e.g. greenhaul_waste
    if cid and not any(e["doc_id"] == doc_id and e["counterparty_id"] == cid and e["role"] == "borrower" for e in entities):
        entities.append({"doc_id": doc_id, "role": "borrower", "name_as_written": None, "abn": None, "counterparty_id": cid, "match": "data room"})
cps = {}
for n, cid in sorted(by_name.items(), key=lambda kv: kv[0] not in strong):
    c = cps.setdefault(cid, {"counterparty_id": cid, "legal_name": names.get(n, " ".join(n)), "abn": None, "aliases": set()})
    c["aliases"].add(names.get(n, " ".join(n)))
    if len(names.get(n, "")) > len(c["legal_name"]):
        c["legal_name"] = names[n]
for abn, cid in by_abn.items():
    if cid in cps:
        cps[cid]["abn"] = abn
deal_cps = []
for deal_id, deal_name, borrower, sponsor in deal_rows:
    for role, name in (("borrower", borrower), ("sponsor", sponsor)):
        cid, how = resolve(name, None)
        if cid:
            deal_cps.append({"deal_id": deal_id, "role": role, "counterparty_id": cid})
for d in docs.values():
    d["counterparty_ids"] = sorted({e["counterparty_id"] for e in entities if e["doc_id"] == d["doc_id"] and e["counterparty_id"] and e["role"] in ("borrower", "sponsor")})
print(len(cps), "counterparties;", sum(e["match"] == "unresolved" for e in entities), "names left unlinked")

# ---- 3. superseded documents ------------------------------------------------------------------------------------------
for d in docs.values():
    d["superseded_by"] = None
STOP = {"the", "and", "of", "for", "a", "an", "in", "on", "to", "report", "review", "update", "note", "outlook", "market", "australian", "australia"}


def topic(d: dict) -> set:
    return {t for t in re.findall(r"[a-z]+", (d["title"] or "").lower()) if t not in STOP and len(t) > 2}


PLACE = {"nsw", "qld", "vic", "wa", "sa", "tas", "act", "street", "road", "drive", "avenue", "lot", "lots", "stage", "stages"}


def words(text: str | None, stop: set) -> set:
    return {t for t in re.findall(r"[a-z]+", (text or "").lower()) if t not in stop and len(t) > 2}


def same_thing(o: dict, d: dict) -> bool:
    """About the same borrower or the same asset; for documents about no one (policy, research), the same subject."""
    ob, db = resolve(o["borrower"], o["borrower_abn"])[0] if o["borrower"] else None, resolve(d["borrower"], d["borrower_abn"])[0] if d["borrower"] else None
    if ob and ob == db:
        return True
    if o["asset"] and d["asset"] and len(words(o["asset"], STOP | PLACE) & words(d["asset"], STOP | PLACE)) >= 2:
        return True
    return not o["counterparty_ids"] and not d["counterparty_ids"] and len(topic(o) & topic(d)) >= 2


def same_author(o: dict, d: dict) -> bool:
    a, b = norm((o["author"] or "").split(",")[0]), norm((d["author"] or "").split(",")[0])
    return bool(a) and a == b


DRAFT = re.compile(r"\b(draft|preliminary)\b", re.I)
for d in sorted(docs.values(), key=lambda x: x["as_of"] or ""):
    if not d["as_of"]:
        continue
    older = [o for o in docs.values() if o is not d and o["doc_type"] == d["doc_type"] and o["as_of"] and o["as_of"] < d["as_of"]
             and same_author(o, d) and same_thing(o, d)]
    says = bool(d["supersedes_text"])
    for o in older:
        if o["superseded_by"] is None and (says or DRAFT.search(o["version"] or "") or DRAFT.search(o["title"] or "")):
            o["superseded_by"] = d["doc_id"]
print(sum(1 for d in docs.values() if d["superseded_by"]), "documents marked superseded")


# ---- 4. passages ------------------------------------------------------------------------------------------------------
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


def as_text(t: str, content: str, description: str) -> str:
    if t == "table":
        p = _Table()
        p.feed(content or "")
        return "\n".join(" | ".join(r) for r in p.rows)
    if t == "figure":
        return f"[Figure] {description}".strip()
    return content or ""


cp_name = {c["counterparty_id"]: c["legal_name"] for c in cps.values()}
_, els = sql(f"SELECT element_id, doc_id, page, type, content, description, section FROM {FQ}.library_elements ORDER BY doc_id, seq")
passages = []
for eid, doc_id, page, t, content, description, section in els:
    d = docs.get(doc_id)
    text = as_text(t, content, description).strip()
    if not d or t in ("section_header", "title") or (len(text) < 25 and t != "table"):
        continue
    about = ", ".join(cp_name[c] for c in d["counterparty_ids"]) or d["asset"] or d["sector"] or ""
    head = " · ".join(x for x in [d["title"], TYPE_LABEL[d["doc_type"]], d["author"], d["as_of"], about, section] if x)
    passages.append({"element_id": eid, "doc_id": doc_id, "page": int(page), "type": t, "section": section or "", "text": text,
                     "search_text": f"{head}\n{text}", "doc_type": d["doc_type"], "collection": d["collection"],
                     "as_of_int": int(d["as_of"].replace("-", "")) if d["as_of"] else 0, "is_current": d["superseded_by"] is None,
                     "independent": bool(d["independent"])})
print(len(passages), "passages")


def stage(name: str, data: list[dict]) -> str:
    path = f"{VOL}/raw/{name}.json"
    w.files.upload(path, io.BytesIO("\n".join(json.dumps(r, default=list) for r in data).encode()), overwrite=True)
    return path


for name, data, comment in (
    ("documents", list(docs.values()), "One row per library document: type, author, date, version, borrower, sponsor, asset, independence and summary (ai_classify + ai_extract), linked counterparties and whether a later version supersedes it."),
    ("counterparties", [{**c, "aliases": sorted(c["aliases"])} for c in cps.values()], "Borrowers, sponsors and other parties named in the library, one per ABN or legal name."),
    ("document_entities", entities, "Each name a document prints, its role and the counterparty it was linked to (by ABN, exact name, or unambiguous short name), or unresolved."),
    ("deal_counterparties", deal_cps, "The borrower and sponsor counterparties of each deal in the origination system."),
):
    sql(f"CREATE OR REPLACE TABLE {FQ}.{name} COMMENT '{q(comment)}' AS SELECT * FROM read_files('{stage(name, data)}', format => 'json')")
    print(name, sql(f"SELECT count(*) FROM {FQ}.{name}")[1][0][0])

sql(f"""CREATE TABLE IF NOT EXISTS {FQ}.passages (element_id STRING NOT NULL, doc_id STRING, page INT, type STRING, section STRING, text STRING,
  search_text STRING, doc_type STRING, collection STRING, as_of_int INT, is_current BOOLEAN, independent BOOLEAN, CONSTRAINT passages_pk PRIMARY KEY (element_id))
COMMENT 'Searchable passages of the library (one per parsed element), each headed by its document context. Source of the AI Search index.'
TBLPROPERTIES (delta.enableChangeDataFeed = true)""")
sql(f"""MERGE INTO {FQ}.passages t USING (SELECT element_id, doc_id, cast(page AS INT) page, type, section, text, search_text, doc_type, collection,
          cast(as_of_int AS INT) as_of_int, is_current, independent FROM read_files('{stage("passages", passages)}', format => 'json')) s
ON t.element_id = s.element_id
WHEN MATCHED AND (t.search_text <> s.search_text OR t.is_current <> s.is_current OR t.doc_type <> s.doc_type OR t.independent <> s.independent) THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
WHEN NOT MATCHED BY SOURCE THEN DELETE""")
print("passages table", sql(f"SELECT count(*) FROM {FQ}.passages")[1][0][0])
