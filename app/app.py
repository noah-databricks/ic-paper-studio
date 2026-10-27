"""IC Paper Studio: Quillwood Credit Partners' credit-paper drafting desk, as a Databricks App.

The analyst talks to an agent (Omnigent, embedded: omni_runtime, agent) that drafts the Investment Committee paper
into the firm's Word template from the document library in Unity Catalog (sources), through the tools in tools.py. Every
filled point keeps its source (paper). The Studio shows the paper filling, each point's proof, and exports the .docx.
"""
import asyncio
import io
import logging
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import omni_runtime
import paper as P
import sources as src
import template_editor as TE
import tools

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("studio")
app = FastAPI(title="IC Paper Studio")
app.include_router(agent.router)
tools.mount(app)


def _paper(deal_id: str, request: Request) -> dict:
    """A deal's paper: anyone signed in to the Studio works on the deal's one paper (deal access is UC access)."""
    email, _ = agent.user(request)
    if "~" in deal_id and not any(x["id"] == deal_id for x in src.projects()):
        raise HTTPException(404, "No such project.")
    return P.ensure(deal_id, email)


def _token(request: Request) -> str | None:
    return agent.user(request)[1]


async def _off(fn, *a):
    try:
        return await asyncio.to_thread(fn, *a)
    except P.FillError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _public_paper(p: dict) -> dict:
    t = P.template_of(p)
    return {k: v for k, v in p.items() if k not in ("metric_refs", "previous_drafts")} | {
        "status": P.status(p), "template": {"template_id": t["template_id"], "name": t["name"], "slots": t["slots"]} if t else None}


@app.get("/api/me")
def me(request: Request):
    email, token = agent.user(request)
    return {"email": email, "obo": bool(token), "workspace": src.sp().config.host, "catalog_schema": src.FQ}


@app.get("/api/templates")
def templates():
    return [{k: t.get(k) for k in ("template_id", "name", "purpose", "use_when", "custom", "based_on")} | {"slots": len(t["order"])}
            for t in P.TEMPLATES.values()]


@app.get("/api/templates/{template_id}")
def template_detail(template_id: str):
    t = P.TEMPLATES.get(template_id)
    if not t:
        raise HTTPException(404, "No such template.")
    return {k: t.get(k) for k in ("template_id", "name", "purpose", "use_when", "docx", "custom", "based_on")} | {"slots": t["slots"]}


class TemplateEdit(BaseModel):
    base_id: str
    name: str
    purpose: str = ""
    use_when: str = ""
    edits: dict = {}


@app.post("/api/templates")
async def template_create(body: TemplateEdit, request: Request):
    """Save an edited copy of a template as a new one."""
    email, _ = agent.user(request)
    try:
        t = await asyncio.to_thread(TE.save, None, body.base_id, body.name, body.purpose, body.use_when, body.edits, email)
    except TE.TemplateError as e:
        raise HTTPException(400, str(e))
    return {"template_id": t["template_id"]}


@app.post("/api/templates/upload")
async def template_upload(request: Request, file: UploadFile = File(...)):
    """A Word document uploaded as a new template: its headings become sections and slots."""
    email, _ = agent.user(request)
    form = await request.form()
    data = await file.read()
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "Upload a Word document (.docx).")
    try:
        t = await asyncio.to_thread(TE.upload, data, file.filename, str(form.get("name") or ""), str(form.get("purpose") or ""),
                                    str(form.get("use_when") or ""), email)
    except TE.TemplateError as e:
        raise HTTPException(400, str(e))
    return {"template_id": t["template_id"], "slots": len(t["slots_spec"]),
            "sections": len({s["section"] for s in t["slots_spec"]})}


@app.put("/api/templates/{template_id}")
async def template_update(template_id: str, body: TemplateEdit, request: Request):
    """Change a template made in the Studio."""
    email, _ = agent.user(request)
    try:
        t = await asyncio.to_thread(TE.save, template_id, template_id, body.name, body.purpose, body.use_when, body.edits, email)
    except TE.TemplateError as e:
        raise HTTPException(400, str(e))
    return {"template_id": t["template_id"]}


@app.get("/api/templates/{template_id}/file.docx")
def template_file(template_id: str):
    t = P.TEMPLATES.get(template_id)
    if not t:
        raise HTTPException(404, "No such template.")
    return FileResponse(P.docx_path(t), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        filename=t["docx"])


@app.get("/api/templates/{template_id}/thumb.png")
async def template_thumb(template_id: str):
    t = P.TEMPLATES.get(template_id) or {}
    f = P.TDIR / "thumbs" / f"{t.get('based_on') or template_id}.png"  # a Studio-made template shows the first page it was made from
    if t.get("custom") and not f.exists():
        f = await asyncio.to_thread(TE.thumb, t)  # uploaded: no rendered first page, so a plain one
    if not t or not f.exists():
        raise HTTPException(404, "No thumbnail.")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/deals/{deal_id}/warm")
async def warm(deal_id: str, request: Request):
    """Start fetching the page images of the documents this deal uses, so previews open instantly."""
    agent.user(request)
    p = P.get(deal_id)
    asyncio.get_event_loop().run_in_executor(None, src.warm, list(P.sources_used(p)) if p else [])
    return {"warming": deal_id}


@app.get("/api/deals")
async def deals(request: Request):
    tok = _token(request)
    rows = await _off(src.deals, tok)
    docs, _ = await _off(src.load, tok)
    about: dict[str, int] = {}
    for x in docs.values():
        for c in x.get("counterparty_ids") or []:
            about[c] = about.get(c, 0) + 1
    out = []
    for d in rows:
        n = sum(about.get(c["counterparty_id"], 0) for c in src.counterparties_of(d["deal_id"]))
        if not n:
            continue  # peer deals in the book with nothing in the library: context for the metric views, not projects
        row = {k: d.get(k) for k in ("deal_name", "borrower", "sponsor", "sector", "sub_sector", "state", "status",
                                     "lifecycle_stage", "request_summary", "facility_limit")} | {"documents": n}
        shown = [x for x in src.projects_of(d["deal_id"]) if not x.get("hidden")]
        for i, x in enumerate(shown):  # the latest project is the deal's; earlier ones keep their chats and paper
            p = P.get(x["id"])
            if i < len(shown) - 1 and not (p and (p.get("template_id") or p.get("sources_used") or p.get("tables_used"))):
                continue  # an earlier project nobody worked in (opening a deal makes an empty paper) has nothing to look back at
            out.append(row | {"deal_id": x["id"], "earlier": i < len(shown) - 1, "paper": P.status(p) if p else None,
                              "updated": p.get("updated") if p else None})
    return out


@app.get("/api/deals/{deal_id}")
async def deal(deal_id: str, request: Request):
    tok = _token(request)
    d = await _off(src.deal, deal_id, tok)
    if not d:
        raise HTTPException(404, "No such deal.")
    docs, _ = await _off(src.load, tok)
    p = P.get(deal_id)
    used = P.sources_used(p) if p else {}
    return {"deal": d, "counterparties": src.counterparties_of(deal_id),
            "documents": [{k: docs[i].get(k) for k in ("doc_id", "title", "issuer", "pages", "doc_type", "as_of", "superseded_by")} | {"how": u["how"]}
                          for i, u in sorted(used.items(), key=lambda kv: kv[1]["first"] or 0) if i in docs],
            "tables": [{"key": k} | v for k, v in sorted(((p or {}).get("tables_used") or {}).items(), key=lambda kv: kv[1]["first"])],
            "metric_views": [{"name": v["name"], "fq": v["fq"], "comment": v["comment"], "measures": [m["name"] for m in v["measures"]],
                              "dimensions": [x["name"] for x in v["dimensions"]]} for v in src.VIEWS.values()],
            "workspace": src.sp().config.host}


@app.post("/api/deals/{deal_id}/fresh")
async def fresh(deal_id: str, request: Request):
    """Start the deal afresh for a demo: a new empty project; the current one (its chats and paper) moves to Earlier work.
    Nothing is deleted. Refused while a chat on the deal is still working."""
    tok, base = _token(request), src.deal_of(deal_id)
    if not await _off(src.deal, base, tok):
        raise HTTPException(404, "No such deal.")
    if any(src.deal_of(t.get("deal_id") or "") == base and t.get("status") == "running" for t in await _off(agent.threads, request)):
        raise HTTPException(409, "A chat on this deal is still working. Stop it or let it finish first.")
    return {"deal_id": await _off(src.open_project, base)}


@app.get("/api/deals/{deal_id}/paper")
def get_paper(deal_id: str, request: Request):
    return _public_paper(_paper(deal_id, request))


class Review(BaseModel):
    action: str
    text: str | None = None


@app.post("/api/deals/{deal_id}/slots/{tag}/review")
async def review(deal_id: str, tag: str, r: Review, request: Request):
    _paper(deal_id, request)
    email, _ = agent.user(request)
    return await _off(P.review, deal_id, tag, r.action, email, r.text)


class Edit(BaseModel):
    paragraphs: list[dict]


@app.post("/api/deals/{deal_id}/slots/{tag}/edit")
async def edit(deal_id: str, tag: str, e: Edit, request: Request):
    """The analyst's own edit of a section (from the Studio's editor): formatting and wording, citations kept."""
    _paper(deal_id, request)
    email, _ = agent.user(request)
    return await _off(P.analyst_edit, deal_id, tag, e.paragraphs, email)


class Redo(BaseModel):
    thread_id: str
    note: str | None = None


@app.post("/api/deals/{deal_id}/slots/{tag}/redo")
def redo(deal_id: str, tag: str, r: Redo, request: Request):
    p = _paper(deal_id, request)
    agent.own(r.thread_id, request)
    label = (P.SLOTS_OF(p).get(tag) or {}).get("alias") or tag
    agent.tell(r.thread_id, f"The analyst asked you to redo the slot '{tag}' ({label})." + (f" Their note: {r.note}" if r.note else "")
               + " Redo only that slot, then reply in one or two sentences.")
    return {"sent": True}


@app.get("/api/deals/{deal_id}/export.docx")
async def export(deal_id: str, request: Request, accepted_only: bool = False):
    p = _paper(deal_id, request)
    data = await _off(P.export, deal_id, _token(request), not accepted_only)
    name = re.sub(r"[^A-Za-z0-9]+", "-", f"{(P.template_of(p) or {}).get('name', 'Paper')} {src.deal_of(deal_id)} {time.strftime('%Y-%m-%d')}").strip("-") + ".docx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


_jobs: dict[str, dict] = {}


@app.post("/api/deals/{deal_id}/files")
async def add_file(deal_id: str, request: Request, file: UploadFile = File(...)):
    """Add a file to the deal's pack from the sidebar: stored in the uploads volume, then parsed with ai_parse_document
    in the background (as the analyst), after which every chat on the deal can quote it."""
    _, token = agent.user(request)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or "file")[:120]
    path = f"{src.VOL}/uploads/{deal_id}/{int(time.time())}_{name}"
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Files are limited to 50 MB.")
    await asyncio.to_thread(src.client(token).files.upload, path, io.BytesIO(data), overwrite=True)
    job = {"id": path, "name": file.filename, "deal_id": deal_id, "state": "parsing", "started": time.time()}
    _jobs[path] = job

    async def run():
        try:
            job["result"] = await asyncio.to_thread(src.parse_upload, path, token, deal_id)
            P.use_source(deal_id, job["result"]["doc_id"], "attached")
            job["state"] = "ready"
        except Exception as e:
            job.update(state="failed", error=str(e)[:300])
    asyncio.get_event_loop().create_task(run())
    return job


@app.get("/api/deals/{deal_id}/files")
def file_jobs(deal_id: str, request: Request):
    agent.user(request)
    return [j for j in _jobs.values() if j["deal_id"] == deal_id]


@app.get("/api/sources/{doc_id}")
async def source(doc_id: str, request: Request):
    docs, els = await _off(src.load, _token(request))
    if doc_id not in docs:
        raise HTTPException(404, "No such document.")
    return docs[doc_id] | {"elements": [{k: e[k] for k in ("element_id", "page", "type", "x0", "y0", "x1", "y1", "page_width", "page_height")}
                                        | {"text": src.as_text(e)[:600]} for e in els.values() if e["doc_id"] == doc_id]}


@app.get("/api/sources/{doc_id}/pages/{page}.jpg")
async def page_image(doc_id: str, page: int, request: Request, size: str = "thumb"):
    data = await _off(src.page_image, doc_id, page, _token(request), "full" if size == "full" else "thumb")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/file")
async def volume_file(uri: str, request: Request):
    if uri.startswith(str(P.DATA / "charts") + "/") and ".." not in uri:  # charts the agent drew
        return FileResponse(uri, media_type="image/png")
    data = await _off(src.volume_file, uri, _token(request))
    kind = "image/png" if uri.endswith(".png") else "image/jpeg" if uri.endswith((".jpg", ".jpeg")) else "application/octet-stream"
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/uploads")
async def upload(request: Request, deal_id: str = "chat", file: UploadFile = File(...)):
    _, token = agent.user(request)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or "attachment")[:120]
    path = f"{src.VOL}/uploads/{re.sub(r'[^A-Za-z0-9-]', '', deal_id)}/{int(time.time())}_{name}"
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Attachments are limited to 50 MB.")
    await asyncio.to_thread(src.client(token).files.upload, path, io.BytesIO(data), overwrite=True)
    return {"path": path, "name": file.filename, "bytes": len(data)}


class AgentIdentity:
    """Paper tool calls from this app's own Omnigent host act as the analyst who owns the thread: the host reaches /mcp
    over loopback with the thread's key, swapped here for that analyst's forwarded identity."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            hs = scope["headers"]
            key = next((v.decode() for k, v in hs if k == b"x-studio-key"), None)
            if key is not None:
                who = agent.identity_for(key)
                client = (scope.get("client") or ("",))[0]
                if who is None or client not in ("127.0.0.1", "::1"):
                    return await JSONResponse({"detail": "Unknown agent thread."}, status_code=401)(scope, receive, send)
                drop = {b"x-studio-key", b"x-forwarded-access-token", b"x-forwarded-email", b"x-studio-session",
                        b"x-studio-deal", b"x-forwarded-preferred-username"}
                scope = dict(scope, headers=[(k, v) for k, v in hs if k not in drop] + [
                    (b"x-forwarded-access-token", (who.get("token") or "").encode()), (b"x-forwarded-email", who["email"].encode()),
                    (b"x-studio-session", str(who.get("sid") or "").encode()), (b"x-studio-deal", str(who.get("deal") or "").encode())])
        await self.inner(scope, receive, send)


app.add_middleware(AgentIdentity)


async def _flusher():
    while True:
        await asyncio.sleep(15)
        await asyncio.to_thread(P.flush)
        await asyncio.to_thread(agent.trace_idle_threads)


@app.on_event("startup")
async def _startup():
    omni_runtime.start()
    await asyncio.to_thread(TE.load)
    asyncio.get_event_loop().run_in_executor(None, src.warm, None)  # every pack's page previews, in the background
    asyncio.get_event_loop().create_task(_flusher())


@app.on_event("shutdown")
def _shutdown():
    P.flush()
    omni_runtime.stop()


DIST = Path(__file__).parent / "static" / "dist"
app.mount("/assets", StaticFiles(directory=DIST / "assets", check_dir=False), name="assets")


@app.get("/{path:path}")
def index(path: str):
    if path.startswith(("api/", "mcp", "otel/")):
        raise HTTPException(404)
    f = DIST / path
    if path and f.is_file():
        return FileResponse(f)
    return FileResponse(DIST / "index.html")
