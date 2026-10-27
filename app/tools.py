"""The agent's tools, served at /mcp: retrieve from the firm's document library, query the metric views, choose a
template and fill the paper.

The Studio's own Omnigent host calls these over loopback with a key that stands for the analyst who owns the chat (the
AgentIdentity middleware in app.py swaps it for their forwarded identity and the chat's deal), so every read runs as
them and every fill lands in that deal's paper. Nothing is pre-assigned to a deal: the agent finds what it needs in the
library, and whatever it reads or cites becomes part of the deal's sources for every chat.
"""
import asyncio
import uuid
from types import SimpleNamespace

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import paper as P
import sources as src

INSTRUCTIONS = """\
IC Paper Studio drafts Quillwood Credit Partners credit papers into the firm's Word templates from the firm's document
library (data rooms, credit policy, research, past IC papers, valuations, internal email; parsed and tagged) and Unity
Catalog metric views over the deals and the loan book. Every filled point carries its source.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
HOW_TO_FILL = {
    "text": "fill_text with paragraphs of segments. Quote source passages word for word (element_id + quote); you may "
            "shorten a quote or join two, and add only short linking words of your own. Numbers come from a metric view "
            "(metric_ref) where the slot's preferred source says metric.",
    "table": "fill_table with element_id of a source table (optionally pick columns/rows), metric_ref, or rows + "
             "row_sources when you assemble one.",
    "image": "fill_image with element_id of a source figure, or chart from a one-dimension metric query.",
    "metric": "fill_metric with metric_ref + measure (or element_id + quote for a fact like a name or date)."}


ACRONYMS = {"id": "ID", "tdc": "TDC", "lvr": "LVR", "ltc": "LTC", "icr": "ICR", "abn": "ABN", "ic": "IC", "qs": "QS", "ebitda": "EBITDA", "grv": "GRV"}


def _label(k: str) -> str:
    """deal_id -> Deal ID, land_value_as_is -> Land value as is: the deal record's columns as an analyst reads them."""
    words = [ACRONYMS.get(w, w) for w in k.split("_")]
    return " ".join([words[0][:1].upper() + words[0][1:]] + words[1:])


def _value(k: str, v) -> str:
    """The deal record's values as an analyst reads them: A$65.0M, 54.9%, Yes (the table stores 6.5E7, 0.549, true)."""
    t = str(v)
    if t.lower() in ("true", "false"):
        return "Yes" if t.lower() == "true" else "No"
    try:
        x = float(t)
    except ValueError:
        return t
    if "date" in k or k.endswith("_year"):
        return t
    if abs(x) >= 100_000:
        return f"A${x / 1e6:,.1f}M"
    if 0 < abs(x) < 1.5 and any(w in k for w in ("lvr", "ltc", "pct", "ratio", "share", "occupancy", "margin", "rate", "presales")):
        return f"{x * 100:.1f}%"
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}"

def mount(app) -> FastMCP:
    mcp = FastMCP("IC Paper Studio", instructions=INSTRUCTIONS, streamable_http_path="/mcp", stateless_http=True,
                  json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def caller(ctx: Context) -> SimpleNamespace:
        req = getattr(ctx.request_context, "request", None)
        h = {k.lower(): v for k, v in (req.headers.items() if req is not None else [])}
        deal = h.get("x-studio-deal")
        if not deal or P.get(deal) is None:
            raise ToolError("This tool only works inside an IC Paper Studio chat.")
        return SimpleNamespace(deal=deal, token=h.get("x-forwarded-access-token") or None, who=h.get("x-forwarded-email") or "agent",
                               thread=h.get("x-studio-session"))

    async def off(fn, *a, **kw):
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except (P.FillError, ValueError) as e:
            raise ToolError(str(e))

    def metrics_of(deal: str) -> dict:
        return P.get(deal).setdefault("metric_refs", {})

    @mcp.tool(annotations=READ)
    async def get_deal(ctx: Context) -> dict:
        """Start here. The deal record from the origination system (request, facility, borrower, sponsor), the deal's
        borrower and sponsor as library counterparties (filter retrieval with counterparty="deal"), the documents earlier
        chats on this deal already read or cited, and the paper: its template (if chosen yet) and how far it is drafted."""
        c = caller(ctx)
        d = await off(src.deal, c.deal, c.token)
        p = P.get(c.deal)
        docs, _ = await off(src.load)
        hidden = {"lifecycle_stage", "status"}  # the agent works out what kind of paper this is from the facts, not from a stage label
        used = P.sources_used(p)
        seen = {k: v for k, v in (d or {}).items() if v not in (None, "") and k not in hidden}
        if d:
            P.use_table(c.deal, "record:deals", {"kind": "record", "title": "Deal record", "sub": "Origination system", "fq": f"{src.FQ}.deals",
                                                 "catalog_path": "/explore/data/" + f"{src.FQ}.deals".replace(".", "/")},
                        {"sql": f"SELECT * FROM {src.FQ}.deals WHERE deal_id = '{d['deal_id']}'",
                         "table": {"columns": ["Field", "Value"], "rows": [[_label(k), _value(k, v)] for k, v in seen.items()]}}, c.thread)
        return {"deal": seen,
                "counterparties": src.counterparties_of(c.deal),
                "sources_used": [{"doc_id": i, "title": docs[i]["title"], "doc_type": docs[i].get("doc_type"), "as_of": docs[i].get("as_of"), "how": u["how"]}
                                 for i, u in used.items() if i in docs],
                "paper": {"template_id": p.get("template_id"), "template_reason": p.get("template_reason"), **P.status(p)}}

    @mcp.tool(annotations=READ)
    async def list_templates(ctx: Context) -> list[dict]:
        """Quillwood's paper templates: what each is for, when to use it, and its sections. Pick the one that fits where
        the deal is in its lifecycle and what is being asked, then set_template."""
        caller(ctx)
        out = []
        for t in P.TEMPLATES.values():
            sections = []
            for s in t["slots"]:
                if s["section"] not in sections:
                    sections.append(s["section"])
            out.append({"template_id": t["template_id"], "name": t["name"], "purpose": t.get("purpose"),
                        "use_when": t.get("use_when"), "sections": sections, "slots": len(t["order"])})
        return out

    @mcp.tool(annotations=WRITE)
    async def set_template(template_id: str, reason: str, ctx: Context) -> dict:
        """Choose the template for this deal's paper, with a one-sentence reason grounded in the deal's facts (the
        analyst sees it). Changing template later keeps the slots both share and stores the rest of the old draft."""
        c = caller(ctx)
        return await off(P.set_template, c.deal, template_id, reason, "agent")

    @mcp.tool(annotations=READ)
    async def get_brief(ctx: Context) -> dict:
        """The chosen template's slots in order (tag, section, kind, guidance, preferred source) and what each holds."""
        c = caller(ctx)
        p = P.get(c.deal)
        t = P.template_of(p)
        if not t:
            raise ToolError("No template chosen yet: list_templates, then set_template.")
        return {"template": {"template_id": t["template_id"], "name": t["name"]},
                "slots": [{k: s.get(k) for k in ("tag", "section", "kind", "guidance", "preferred_source")} | {"status": p["slots"][s["tag"]]["status"]}
                          for s in t["slots"]], "how_to_fill": HOW_TO_FILL}

    @mcp.tool(annotations=READ)
    async def find_documents(ctx: Context, query: str | None = None, doc_type: list[str] | None = None, counterparty: str | None = None,
                             since: str | None = None, include_superseded: bool = False, k: int = 20) -> dict:
        """Look up documents in the library by what they are: doc_type (valuation, qs_report, builder_assessment,
        information_memorandum, deal_intake, term_sheet, financials, compliance_certificate, facility_summary,
        variation_request, ic_paper, committee_minutes, credit_policy, research, leasing_report, correspondence),
        counterparty ("deal" for this deal's borrower and sponsor, or a company name), since (YYYY-MM-DD), and words in
        the title, summary, asset or borrower. Newest first. Each card has the author, date, whether it is independent of
        the borrower and sponsor, a summary, and superseded_by when a later version replaces it (excluded unless
        include_superseded)."""
        c = caller(ctx)
        return {"documents": await off(src.find_documents, c.token, c.deal, query, doc_type, counterparty, since, include_superseded, k)}

    @mcp.tool(annotations=READ)
    async def search_library(query: str, ctx: Context, doc_type: list[str] | None = None, counterparty: str | None = None,
                             doc_ids: list[str] | None = None, since: str | None = None, include_superseded: bool = False,
                             k: int = 8) -> dict:
        """Hybrid keyword and semantic search over every passage in the library (AI Search, reranked), for a concept or
        question: "pre-sale requirements for land loans", "sponsor track record". Narrow it with the same filters as
        find_documents, or doc_ids. Returns passages (element_id, page, section, text) with their document's title, type
        and date. Read around a hit with read_document before you quote it."""
        c = caller(ctx)
        return {"passages": await off(src.search_library, query, c.token, c.deal, doc_type, counterparty, doc_ids, since, include_superseded, k)}

    @mcp.tool(annotations=READ)
    async def grep(pattern: str, ctx: Context, doc_ids: list[str] | None = None, counterparty: str | None = None,
                   doc_type: list[str] | None = None, k: int = 30) -> dict:
        """Regular-expression match over the library's passage text (case-insensitive): exact figures ("65\\.0M"),
        clause numbers ("4\\.2"), names, covenant terms. Use it to confirm a number or find every mention, where
        search_library finds concepts. Includes superseded documents (marked)."""
        c = caller(ctx)
        return {"matches": await off(src.grep, pattern, c.token, c.deal, doc_ids, counterparty, doc_type, True, k)}

    @mcp.tool(annotations=READ)
    async def read_document(doc_id: str, ctx: Context, pages: list[int] | None = None) -> dict:
        """A document's card and every parsed element in reading order (or only some pages): element_id, page, type and
        the full text. Tables come as pipe-separated rows, figures as their description. Quote from this text. A document
        you read joins this deal's sources for every chat."""
        c = caller(ctx)
        out = await off(src.read_document, doc_id, c.token, pages)
        P.use_source(c.deal, doc_id, "read", c.thread)
        return out

    @mcp.tool(annotations=READ)
    async def list_metric_views(ctx: Context) -> dict:
        """The Unity Catalog metric views with their measures and dimensions, and the filter for this deal."""
        c = caller(ctx)
        return {"metric_views": [{"name": v["name"], "comment": v["comment"], "measures": [m["name"] for m in v["measures"]],
                                  "dimensions": [d["name"] for d in v["dimensions"]]} for v in src.VIEWS.values()],
                "deal_filter": {"Deal ID": src.deal_of(c.deal)}}  # the deal, not the project (a fresh project is <deal_id>~n)

    @mcp.tool(annotations=READ)
    async def query_metric(view: str, measures: list[str], ctx: Context, dimensions: list[str] | None = None,
                           filters: dict | None = None) -> dict:
        """Query a Unity Catalog metric view with MEASURE(). For this deal pass filters={"Deal ID": "<deal_id>"}.
        Returns a metric_ref to cite in fills, the formatted values and the SQL that ran."""
        c = caller(ctx)
        filters = {k: src.deal_of(v) if k == "Deal ID" and isinstance(v, str) else v for k, v in (filters or {}).items()} or None
        q = await off(src.query_metric, view, measures, dimensions, filters, c.token)
        ref = "m_" + uuid.uuid4().hex[:8]
        metrics_of(c.deal)[ref] = q
        P.use_table(c.deal, f"mv:{q['view']}", {"kind": "metric", "title": src.VIEW_LABELS.get(q["view"], q["view"]), "sub": "Governed figures",
                                               "fq": q["fq"], "catalog_path": "/explore/data/" + q["fq"].replace(".", "/")},
                    {"sql": q["sql"], "table": {"columns": q["dimensions"] + q["measures"],
                                                "rows": [[str(r.get(x) or "") for x in q["dimensions"]] + [(r.get("_formatted") or {}).get(m) or "" for m in q["measures"]]
                                                         for r in q["rows"][:12]]}}, c.thread)
        return {"metric_ref": ref, "rows": [{k: v for k, v in r.items() if k != "_formatted"} | {"formatted": r["_formatted"]} for r in q["rows"]],
                "sql": q["sql"]}

    @mcp.tool(annotations=WRITE)
    async def fill_text(tag: str, paragraphs: list[dict], ctx: Context) -> dict:
        """Fill a text slot. paragraphs: [{"bullet": false, "segments": [...]}]. A segment is one of
        {"element_id": "...", "quote": "exact words from the element"} (optionally "text": a lightly shortened
        version of the quote), {"metric_ref": "...", "measure": "..."} for a governed number (optionally "text" to
        phrase it, e.g. "58.2%"), or {"text": "..."} for your own short linking words. Segments are joined with
        spaces, so write each as it should read. The analyst sees the paper fill as you go."""
        c = caller(ctx)
        return await off(P.fill_text, c.deal, tag, paragraphs, "agent", c.token, metrics_of(c.deal))

    @mcp.tool(annotations=WRITE)
    async def fill_table(tag: str, ctx: Context, element_id: str | None = None, columns: list[int] | None = None,
                         rows: list[list[str]] | None = None, metric_ref: str | None = None, caption: str | None = None,
                         row_sources: list[dict] | None = None) -> dict:
        """Fill a table slot from a source table (element_id; columns picks columns by index; rows picks rows exactly
        as they appear), from a metric query (metric_ref), or assembled (rows with a header row first, and
        row_sources: one {"element_id", "quote"} or {"metric_ref", "measure"} per body row)."""
        c = caller(ctx)
        return await off(P.fill_table, c.deal, tag, "agent", c.token, metrics_of(c.deal), element_id, columns, rows,
                         metric_ref, caption, row_sources)

    @mcp.tool(annotations=WRITE)
    async def fill_image(tag: str, ctx: Context, element_id: str | None = None, chart: dict | None = None,
                         caption: str | None = None) -> dict:
        """Fill an image slot with a figure from a source document (element_id of a figure element), or a chart drawn
        from a one-dimension metric query: chart={"metric_ref": "...", "type": "bar"|"barh", "title": "..."}."""
        c = caller(ctx)
        return await off(P.fill_image, c.deal, tag, "agent", c.token, metrics_of(c.deal), element_id, chart, caption)

    @mcp.tool(annotations=WRITE)
    async def fill_metric(tag: str, ctx: Context, metric_ref: str | None = None, measure: str | None = None,
                          text: str | None = None, element_id: str | None = None, quote: str | None = None) -> dict:
        """Fill an inline field: a governed value (metric_ref + measure or dimension; text overrides the formatting), a
        fact quoted from a source (element_id + quote), or, for the recommendation only, your own short text."""
        c = caller(ctx)
        return await off(P.fill_metric, c.deal, tag, "agent", metrics_of(c.deal), metric_ref, measure, text, element_id, quote, c.token)

    @mcp.tool(annotations=READ)
    async def get_paper(ctx: Context) -> dict:
        """The deal's paper as it stands: template, each slot's status, how much is source wording, and the analyst's
        review (accepted slots are theirs: ask before changing them)."""
        c = caller(ctx)
        p = P.get(c.deal)
        slots = []
        for t in P.ORDER_OF(p):
            s = p["slots"][t]
            row = {"tag": t, "status": s["status"]}
            if s["status"] != "empty":
                row["citations"] = len(s.get("citations") or [])
                if s["kind"] == "text":
                    row["text"] = " ".join(P.join(para["segments"]) for para in s["paragraphs"])[:400]
                    row["source_wording"] = s.get("source_wording")
                elif s["kind"] == "metric":
                    row["text"] = s.get("text")
            slots.append(row)
        return {"template_id": p.get("template_id"), "summary": P.status(p), "slots": slots}

    @mcp.tool(annotations=WRITE)
    async def add_file_to_deal(path: str, ctx: Context) -> dict:
        """Parse a file the analyst attached (a /Volumes/... path from their message) with ai_parse_document and add it
        to this deal's sources for every chat: then read_document and quote it like the others. Takes ~30-60 s."""
        c = caller(ctx)
        out = await off(src.parse_upload, path, c.token, c.deal)
        P.use_source(c.deal, out["doc_id"], "attached", c.thread)
        return out

    @mcp.tool(annotations=READ)
    async def show_slot(tag: str, ctx: Context) -> dict:
        """Scroll the analyst's paper view to a slot (use it when you talk about a specific part of the paper)."""
        c = caller(ctx)
        p = P.get(c.deal)
        if tag not in P.SLOTS_OF(p):
            raise ToolError(f"Unknown slot {tag}")
        p["focus"] = {"tag": tag, "at": P._now()}
        return {"shown": tag}

    http = mcp.streamable_http_app()
    for route in http.routes:
        app.router.routes.insert(0, route)
    inner = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(a):
        async with mcp.session_manager.run():
            async with inner(a) as st:
                yield st
    app.router.lifespan_context = lifespan
    return mcp
