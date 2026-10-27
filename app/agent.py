"""The Studio's agent: Omnigent chats inside a deal, scoped to the analyst who started them.

Omnigent's server and host run beside the Studio (omni_runtime). A deal is a project: it has one paper, the sources its chats used, and any
number of chats. The Studio relays only the chats an analyst owns, and a chat's tools reach this app over loopback with
a key that stands for that analyst and that deal, so every read runs under their Unity Catalog grants and every fill
lands in the deal's paper. What the agent may change without asking is the chat's approval mode (Omnigent policies).
"""
import hashlib
import io
import json
import logging
import os
import secrets
import tarfile

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import omni_runtime as rt
import paper as P
import sources as src

log = logging.getLogger("agent")
router = APIRouter(prefix="/api/agent")
APP_PORT = int(os.getenv("DATABRICKS_APP_PORT", "8000"))
_models = lambda var, default: [m for m in os.getenv(var, default).split(",") if m]  # noqa: E731
# the harnesses Omnigent drives here, and the workspace models each can use (Pi's Anthropic path rejects Claude 5's
# thinking setting, so Pi runs GPT only; Claude runs on the Claude Agent SDK)
HARNESSES = {
    "claude-sdk": {"label": "Claude Agent SDK", "models": _models("STUDIO_MODELS", "databricks-claude-sonnet-5-5,databricks-claude-opus-5-5")},
    "codex": {"label": "Codex", "models": _models("STUDIO_GPT_MODELS", "databricks-gpt-5-5,databricks-gpt-5-4")},
    "pi": {"label": "Pi", "models": _models("STUDIO_GPT_MODELS", "databricks-gpt-5-5,databricks-gpt-5-4")},
}
MODELS = HARNESSES["claude-sdk"]["models"]

PROMPT = """\
You are the credit paper agent in Quillwood Credit Partners' IC Paper Studio, working with {user}, a credit analyst, on
one deal: {deal}. You work from the firm's document library (every borrower's data room, the credit policy, research,
past IC papers and committee minutes, valuations and internal email, parsed and tagged), Unity Catalog metric views over
the deals and the loan book, and one paper that every chat on this deal shares. Nothing is pre-sorted into a deal pack:
you find what the deal needs. The analyst watches the paper beside you and reviews it.

You have tools; use your judgement about which and when. Typical work:
- Find out what the deal is and what is being asked (get_deal, then the deal's own documents:
  find_documents(counterparty="deal"), read_document).
- Retrieve like a careful analyst: find_documents to see what exists (by type, counterparty, date), search_library for
  concepts and questions across the library (policy limits, market evidence, a sponsor's track record, precedents),
  grep to confirm an exact figure, clause or name, and read_document around anything before you quote it. Look beyond
  the deal's data room where a credit analyst would: the current credit policy for limits, past IC papers and minutes
  for an existing facility or the same sponsor, research and comparable valuations for the market.
- Judge each source: prefer the latest version (superseded_by marks an older one; never quote a superseded document as
  current), weigh independent reports (valuer, QS, auditor, research) above the sponsor's or borrower's own material
  and say so when they disagree, check the date, and make sure a document is about this borrower or asset: different
  companies can share a word of their name (check the ABN, the asset and the sponsor).
- Choose the paper template that fits where the deal is in its lifecycle and what is being asked (list_templates,
  set_template with a reason grounded in the deal's facts). The analyst may disagree; if they name another template,
  use that. If the deal already has a template, keep it unless asked.
- Draft the paper (get_brief, then fill_text / fill_table / fill_image / fill_metric), answer questions, or redo
  parts the analyst asks about. Changes may need the analyst's approval depending on the chat's mode; if a call is
  declined, ask what they want instead.

The rules the Investment Committee relies on:
- Carry the sources' own words across. Quote passages word for word (element_id + quote). You may shorten a quote,
  or join two quotes with a few linking words of your own, but do not paraphrase: the tools reject quotes that are not
  on the page, and the analyst sees a diff for anything you shorten.
- Every number comes from a source: a metric view (query_metric, then metric_ref) for governed deal and book metrics
  and names on the deal record (query Deal or Sponsor as a dimension, then cite it by that dimension's name), or a
  quoted passage or table for anything else.
- Every paragraph reads as finished prose for the IC: when a quote starts mid-sentence, open with a few words of your
  own; no bare labels such as "Strengths:". Keep each slot to what its guidance asks for.
- Never invent facts, terms, ratings or opinions. If the library does not cover a slot, leave it and say so. The
  recommendation is the analyst's to make: draft it only from the sources and say it needs their decision.
- Accepted slots belong to the analyst: ask before changing them.
- The paper is read by the Investment Committee: never put internal system names in it (metric views, tables,
  catalogs), and never add a "Source" column to a table. Sources are shown by the citations; if a table needs to say
  where its rows came from, use row_sources.

Talking to the analyst: be brief and plain, and name parts of the paper the way the paper does ("the covenant table", not a slot tag). When you state a fact from a source, cite it inline as
[[cite:<element_id>]] for a passage, or [[metric:<view>.<measure>]] for a governed number; the Studio turns these into
numbered source links. Use show_slot when you talk about a specific part of the paper. Messages starting with [Studio]
come from the app (for example a redo request for one slot: redo only that slot). Files the analyst attaches arrive as
an "[Attached files]" list of /Volumes paths: add_file_to_deal adds one to the deal's sources. Tables in replies render.
"""

MODES = {"ask": "Ask before decisions", "auto": "Auto-approve", "read": "Read only"}
DECISION_TOOLS = ["set_template", "add_file_to_deal"]
WRITE_TOOLS = DECISION_TOOLS + ["fill_text", "fill_table", "fill_image", "fill_metric"]
POLICY_PREFIX = "studio-mode"


def _cel(tools: list[str], result: str, reason: str) -> str:
    names = " || ".join(f'event.data.name.endsWith("{t}")' for t in tools)
    return f'event.type == "tool_call" && ({names}) ? {{"result": "{result}", "reason": "{reason}"}} : {{"result": "ALLOW"}}'


def mode_policies(mode: str) -> list[dict]:
    """Ask: choosing the template and adding files to the deal wait for the analyst; drafting runs. Read only: no
    changes at all. Auto: everything runs, and the analyst reviews the paper afterwards."""
    cel = "omnigent.policies.builtins.cel.cel_policy"
    if mode == "ask":
        return [{"name": f"{POLICY_PREFIX}-ask", "type": "python", "handler": cel,
                 "factory_params": {"expression": _cel(DECISION_TOOLS, "ASK", "A decision on the deal: approve or decline")}}]
    if mode == "read":
        return [{"name": f"{POLICY_PREFIX}-read", "type": "python", "handler": cel,
                 "factory_params": {"expression": _cel(WRITE_TOOLS, "DENY", "Read-only chat: switch mode to let the agent change the paper")}}]
    return []


def set_mode(sid: str, mode: str):
    if mode not in MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(MODES)}")
    have = omni("GET", f"/v1/sessions/{sid}/policies").json()
    for pol in have.get("data", have if isinstance(have, list) else []):
        if str(pol.get("name", "")).startswith(POLICY_PREFIX):
            omni("DELETE", f"/v1/sessions/{sid}/policies/{pol['id']}")
    for pol in mode_policies(mode):
        omni("POST", f"/v1/sessions/{sid}/policies", json=pol)


def get_mode(sid: str) -> str:
    have = omni("GET", f"/v1/sessions/{sid}/policies").json()
    names = {str(pol.get("name")) for pol in have.get("data", have if isinstance(have, list) else [])}
    return "ask" if f"{POLICY_PREFIX}-ask" in names else "read" if f"{POLICY_PREFIX}-read" in names else "auto"


_KEYS = rt.DATA / "keys.json"
try:
    _keys: dict[str, dict] = json.loads(_KEYS.read_text())
except (OSError, ValueError):
    _keys = {}


def _save_keys():
    try:
        rt.DATA.mkdir(parents=True, exist_ok=True)
        tmp = _KEYS.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        tmp.write_text(json.dumps(_keys))
        tmp.replace(_KEYS)
    except OSError:
        log.warning("could not save agent keys")


def owner_key(email: str) -> str:
    return hashlib.sha1(email.lower().encode()).hexdigest()[:16]


def user(request: Request) -> tuple[str, str | None]:
    """The analyst behind a request: the Apps proxy forwards their email and token (locally: the CLI profile's user)."""
    email = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-preferred-username")
    token = request.headers.get("x-forwarded-access-token")
    if not email:
        if os.getenv("DATABRICKS_APP_NAME"):
            raise HTTPException(401, "Sign in through the app.")
        email = os.getenv("STUDIO_DEV_USER") or src.sp().current_user.me().user_name
        return email, None
    stale = False
    for v in _keys.values():  # keep the analyst's running threads on their freshest token
        if v["email"] == email and token and v.get("token") != token:
            v["token"], stale = token, True
    if stale:
        _save_keys()
    return email, token


def identity_for(key: str | None) -> dict | None:
    return _keys.get(key) if key else None


def omni(method: str, path: str, **kw) -> httpx.Response:
    try:
        r = httpx.request(method, rt.URL + path, timeout=60, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"The agent runtime is not reachable yet ({e.__class__.__name__}). {rt.state['error'] or ''}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code if r.status_code < 500 else 502, r.text[:500])
    return r


def own(sid: str, request: Request) -> dict:
    email, _ = user(request)
    snap = omni("GET", f"/v1/sessions/{sid}").json()
    if (snap.get("labels") or {}).get("owner") != owner_key(email):
        raise HTTPException(404, "No such thread.")
    return snap


def _bundle(harness: str, model: str, key: str, who: str, deal_label: str) -> bytes:
    config = {"spec_version": 1, "name": "ic-paper-agent",
              "description": "Drafts Quillwood IC credit papers from the firm's document library, with a source for every point",
              "executor": {"type": "omnigent", "model": model, "profile": rt.PROFILE, "config": {"harness": harness},
                           "auth": {"type": "databricks", "profile": rt.PROFILE}},
              "instructions": "AGENTS.md"}
    mcp = {"name": "studio", "description": "IC Paper Studio: the document library, metric views and the credit paper",
           "transport": "http", "url": f"http://127.0.0.1:{APP_PORT}/mcp", "timeout": 300,
           "headers": {"X-Studio-Key": key}}
    files = {"config.yaml": yaml.safe_dump(config, sort_keys=False),
             "AGENTS.md": PROMPT.format(user=who, deal=deal_label),
             "tools/mcp/studio.yaml": yaml.safe_dump(mcp, sort_keys=False)}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _msg(text: str) -> dict:
    return {"type": "message", "data": {"role": "user", "content": [{"type": "input_text", "text": text}]}}


def _with_attachments(text: str, paths: list[str]) -> dict:
    paths = [p for p in paths if p.startswith(f"{src.VOL}/uploads/") and ".." not in p]
    if paths:
        text += "\n\n[Attached files]\n" + "\n".join(f"- {p}" for p in paths)
    return _msg(text)


class NewThread(BaseModel):
    deal_id: str
    message: str
    harness: str = "claude-sdk"
    model: str | None = None
    mode: str = "ask"
    attachments: list[str] = []


class Mode(BaseModel):
    mode: str


class Verdict(BaseModel):
    action: str  # accept | decline


class Text(BaseModel):
    text: str
    attachments: list[str] = []


def ready_harnesses() -> list[str]:
    """The harnesses whose CLI this container has (the host reports them once it is up)."""
    try:
        hid = rt.state["host_id"]
        host = next((h for h in omni("GET", "/v1/hosts").json().get("hosts", []) if h["host_id"] == hid), {}) if hid else {}
        ready = host.get("configured_harnesses") or {}
        ok = [h for h in HARNESSES if ready.get(h) in (True, "needs-auth")]
        return ok or ["claude-sdk"]
    except HTTPException:
        return ["claude-sdk"]


@router.get("/options")
def options(request: Request):
    user(request)
    hs = ready_harnesses()
    return {"harnesses": [{"id": h, "label": HARNESSES[h]["label"], "models": HARNESSES[h]["models"], "default_model": HARNESSES[h]["models"][0]} for h in hs],
            "default_harness": hs[0], "models": MODELS, "default_model": MODELS[0], "modes": [{"id": k, "label": v} for k, v in MODES.items()],
            "runtime": {"up": bool(rt.state["host_id"]), "error": rt.state["error"]}}


@router.post("/threads")
def create(req: NewThread, request: Request):
    email, token = user(request)
    d = src.deal(req.deal_id, token)
    if not d:
        raise HTTPException(404, "No such deal.")
    try:
        hid = rt.host_id()
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    harness = req.harness if req.harness in HARNESSES else "claude-sdk"
    models = HARNESSES[harness]["models"]
    model = req.model if req.model in models else models[0]
    key = secrets.token_urlsafe(32)
    _keys[key] = {"email": email, "token": token or "", "client": f"mcp:omnigent/{harness}", "deal": req.deal_id}
    P.ensure(req.deal_id, email)
    title = req.message.strip().split("\n")[0][:80] or "New chat"
    meta = {"title": title, "labels": {"app": "ic-studio", "owner": owner_key(email), "model": model, "harness": harness, "deal": req.deal_id},
            "host_type": "external", "host_id": hid, "workspace": rt.workspace_dir(owner_key(email))}
    label = f"{d.get('deal_name')} ({d.get('deal_id')}), {d.get('borrower')}"
    r = omni("POST", "/v1/sessions", data={"metadata": json.dumps(meta)},
             files={"bundle": ("ic-paper-agent.tar.gz", _bundle(harness, model, key, email, label), "application/gzip")})
    sid = r.json().get("session_id") or r.json().get("id")
    _keys[key]["sid"] = sid
    _save_keys()
    set_mode(sid, req.mode)
    omni("POST", f"/v1/sessions/{sid}/events", json=_with_attachments(req.message, req.attachments))
    return {"thread_id": sid}


@router.get("/threads")
def threads(request: Request):
    email, _ = user(request)
    d = omni("GET", "/v1/sessions", params={"limit": 50}).json()
    out = []
    for s in d.get("data", []):
        lab = s.get("labels") or {}
        if lab.get("owner") != owner_key(email) or lab.get("app") != "ic-studio":
            continue
        out.append({k: s.get(k) for k in ("id", "title", "status", "created_at", "updated_at")} |
                   {"model": lab.get("model"), "harness": lab.get("harness") or "claude-sdk", "deal_id": lab.get("deal")})
    return out


def all_items(sid: str) -> list[dict]:
    """The whole transcript (the session snapshot carries only the last 100 items)."""
    items, after = [], None
    while True:
        page = omni("GET", f"/v1/sessions/{sid}/items", params={"limit": 1000, **({"after": after} if after else {})}).json()
        for it in page.get("data", []):  # this endpoint returns items flat; the snapshot (and the Studio) nest them under data
            if "data" not in it:
                head = {k: it[k] for k in ("id", "type", "status", "created_at", "response_id") if k in it}
                it = head | {"data": {k: v for k, v in it.items() if k not in head}}
            items.append(it)
        if not page.get("has_more") or not page.get("last_id"):
            return items
        after = page["last_id"]


@router.get("/threads/{sid}")
def thread(sid: str, request: Request):
    own(sid, request)
    snap = omni("GET", f"/v1/sessions/{sid}", params={"include_liveness": "true", "include_usage": "true"}).json()
    snap["items"] = all_items(sid)
    snap["mode"] = get_mode(sid)
    snap["deal_id"] = (snap.get("labels") or {}).get("deal")
    return snap


@router.put("/threads/{sid}/mode")
def change_mode(sid: str, m: Mode, request: Request):
    own(sid, request)
    set_mode(sid, m.mode)
    return {"mode": m.mode}


@router.post("/threads/{sid}/elicitations/{eid}")
def resolve(sid: str, eid: str, v: Verdict, request: Request):
    email, _ = user(request)
    own(sid, request)
    rt.workspace_dir(owner_key(email))
    return omni("POST", f"/v1/sessions/{sid}/elicitations/{eid}/resolve", json={"action": v.action}).json()


def trace_idle_threads():
    """Turn finished agent turns into MLflow traces (called by the app's background loop)."""
    import traces
    if not traces.enabled() or not rt.state["host_id"]:
        return
    for v in list(_keys.values()):
        sid = v.get("sid")
        if not sid:
            continue
        try:
            snap = omni("GET", f"/v1/sessions/{sid}", params={"include_usage": "true"}).json()
            if snap.get("status") != "idle":
                continue
            p = P.get(v.get("deal") or "")
            if p is None:
                continue
            done = set(p.get("traced") or [])
            lab = snap.get("labels") or {}
            # Omnigent keeps a running total per thread, so a turn's tokens are the total now less the total at the last trace
            seen = p.setdefault("trace_usage", {})
            now, items = snap.get("usage_by_model") or {}, all_items(sid)
            traced_before = any(t[0]["id"] in done for t in traces.turns(items))  # turns traced before tokens were recorded
            used = traces.usage_since(seen.get(sid), now) if (sid in seen or not traced_before) else None
            new = traces.export(sid, snap.get("title") or "", lab.get("model"), v["email"], items, done,
                                lab.get("harness") or "claude-sdk", used)
            if new or sid not in seen:
                p["traced"] = sorted(done | set(new))
                seen[sid] = now
                P._save(p["sid"])
        except Exception:
            log.exception("could not trace thread %s", sid)


@router.get("/threads/{sid}/stream")
async def stream(sid: str, request: Request):
    own(sid, request)

    async def gen():
        # end the stream when the app is shutting down, so a redeploy is not held open by browsers (they reconnect)
        import asyncio
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20)) as c:
            async with c.stream("GET", f"{rt.URL}/v1/sessions/{sid}/stream", headers={"Accept": "text/event-stream"}) as r:
                # one read stays pending across checks: cancelling a read (wait_for) would break the upstream stream after
                # any quiet spell, and the browser would miss whatever the agent wrote while it reconnected
                it = r.aiter_raw().__aiter__()
                pending = None
                try:
                    while not rt.state["stopping"]:
                        if pending is None:
                            pending = asyncio.ensure_future(it.__anext__())
                        done, _ = await asyncio.wait({pending}, timeout=5)
                        if not done:
                            if await request.is_disconnected():
                                break
                            continue
                        try:
                            chunk = pending.result()
                        except StopAsyncIteration:
                            break
                        pending = None
                        yield chunk
                finally:
                    if pending is not None and not pending.done():
                        pending.cancel()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/threads/{sid}/messages")
def send(sid: str, t: Text, request: Request):
    email, _ = user(request)
    own(sid, request)
    rt.workspace_dir(owner_key(email))
    try:
        return omni("POST", f"/v1/sessions/{sid}/events", json=_with_attachments(t.text, t.attachments)).json()
    except HTTPException as e:
        if "runner_unavailable" in str(e.detail) or "No runner bound" in str(e.detail):
            raise HTTPException(409, "This thread was started before the app was redeployed, so it can be read but not "
                                     "continued. Start a new thread.")
        raise


def tell(sid: str, text: str):
    """A [Studio] message into a thread, e.g. when the analyst asks for a slot to be redone."""
    omni("POST", f"/v1/sessions/{sid}/events", json=_msg("[Studio] " + text))


@router.post("/threads/{sid}/interrupt")
def interrupt(sid: str, request: Request):
    own(sid, request)
    return omni("POST", f"/v1/sessions/{sid}/events", json={"type": "interrupt", "data": {}}).json()


@router.delete("/threads/{sid}")
def delete(sid: str, request: Request):
    own(sid, request)
    omni("DELETE", f"/v1/sessions/{sid}")
    for k in [k for k, v in _keys.items() if v.get("sid") == sid]:
        _keys.pop(k, None)
    _save_keys()
    return {"deleted": sid}


@router.get("/status")
def status(request: Request):
    user(request)
    st = rt.state
    out = {"up": bool(st["host_id"]), "error": st["error"], "host_id": st["host_id"], "up_since": st["started"]}
    if request.query_params.get("logs"):
        out |= {"server_log": rt.tail("server", 40), "host_log": rt.tail("host", 60)}
        runners = sorted((rt.DATA / "data" / "logs" / "runner").glob("*.log"), key=lambda p: p.stat().st_mtime)
        out["runner_log"] = runners[-1].read_text(errors="replace")[-6000:] if runners else None
    return out

