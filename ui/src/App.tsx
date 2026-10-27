import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight } from "lucide-react";
import { api, citationNumbers, parse, toolName, type Citation, type DealDetail, type DealRow, type FileJob, type Me, type Options, type Paper, type PdfCitation, type Snapshot, type TemplateRow, type ThreadRow } from "./api";
import { Chat, type ChatRef } from "./components/Chat";
import { DocxPreview, ExportButton, PaperView, type Sel } from "./components/Paper";
import { Sidebar } from "./components/Sidebar";
import { docAt, DocViewer, SourceHoverProvider } from "./components/Source";
import { SourcesPanel } from "./components/SourcesPanel";
import { TemplatesView } from "./components/TemplatesView";

const ago = (t: number) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString("en-AU");
};

/** A drag handle between two panes; widths persist per browser. */
function useWidth(key: string, initial: number, min: number, max: number, dir = 1) {
  const [w, setW] = useState(() => { const v = Number(localStorage.getItem(key)); return v >= min && v <= max ? v : initial; });
  useEffect(() => { localStorage.setItem(key, String(w)); }, [key, w]);
  const start = (e: React.PointerEvent) => {
    const x0 = e.clientX, w0 = w;
    const move = (ev: PointerEvent) => setW(Math.max(min, Math.min(max, w0 + dir * (ev.clientX - x0))));
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); document.body.classList.remove("resizing"); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); document.body.classList.add("resizing");
  };
  return [w, start] as const;
}

export default function App() {
  const q = new URLSearchParams(location.search);
  const [me, setMe] = useState<Me | null>(null), [deals, setDeals] = useState<DealRow[]>([]), [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [options, setOptions] = useState<Options | null>(null), [threads, setThreads] = useState<ThreadRow[]>([]);
  const [dealId, setDealId] = useState<string | null>(q.get("deal")), [sid, setSid] = useState<string | null>(q.get("thread"));
  const [detail, setDetail] = useState<DealDetail | null>(null), [paper, setPaper] = useState<Paper | null>(null), [snap, setSnap] = useState<Snapshot | null>(null);
  const [live, setLive] = useState(""), [jobs, setJobs] = useState<FileJob[]>([]);
  const [inflight, setInflight] = useState<{ call_id: string; name: string; arguments?: string } | null>(null);  // the tool call running now
  const [view, setView] = useState<"review" | "word">("review"), [sel, setSel] = useState<Sel>(null), [hover, setHover] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<string | null>(null), [toast, setToast] = useState<string | null>(null), [bootErr, setBootErr] = useState<string | null>(null);
  const [templatesOpen, setTemplatesOpen] = useState(q.get("view") === "templates"), [doc, setDoc] = useState<PdfCitation | null>(null);
  const [focus, setFocus] = useState<{ key: string; at: number } | null>(null);
  const [sideW, dragSide] = useWidth("ic.side", 268, 210, 380);
  const [chatW, dragChat] = useWidth("ic.chat", 440, 340, 720);
  const [srcW, dragSrc] = useWidth("ic.src", 320, 260, 460, -1);  // its handle is on its left edge
  const desk = useRef<HTMLDivElement>(null), [, setDeskW] = useState(900);
  const hurry = useRef(0);

  useEffect(() => {
    Promise.all([api.me(), api.deals(), api.templates()]).then(([m, d, t]) => { setMe(m); setDeals(d); setTemplates(t); const first = d.find((x) => !x.earlier); if (!dealId && first) setDealId(first.deal_id); })
      .catch((e) => setBootErr(e.message));
    let t = 0;
    const go = () => api.options().then((o) => { setOptions(o); if (!o.runtime.up) t = window.setTimeout(go, 4000); }).catch(() => { t = window.setTimeout(go, 5000); });
    go();
    return () => window.clearTimeout(t);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const refreshThreads = useCallback(() => api.threads().then(setThreads).catch(() => {}), []);
  useEffect(() => { refreshThreads(); const t = window.setInterval(() => { if (!document.hidden) refreshThreads(); }, 8000); return () => window.clearInterval(t); }, [refreshThreads]);
  useEffect(() => {
    const u = new URL(location.href);
    if (dealId) u.searchParams.set("deal", dealId); else u.searchParams.delete("deal");
    if (sid) u.searchParams.set("thread", sid); else u.searchParams.delete("thread");
    history.replaceState(null, "", u);
  }, [dealId, sid]);
  useEffect(() => { const el = desk.current; if (!el) return; const ro = new ResizeObserver(() => setDeskW(el.clientWidth)); ro.observe(el); return () => ro.disconnect(); }, []);

  // the deal: its pack and its paper (one per deal, shared by every chat)
  const loadDeal = useCallback(() => { if (dealId) api.deal(dealId).then(setDetail).catch(() => setDetail(null)); }, [dealId]);
  useEffect(() => { setDetail(null); setPaper(null); setSel(null); setReviewing(null); setDoc(null); loadDeal(); if (dealId) api.warm(dealId).catch(() => {}); }, [dealId, loadDeal]);
  const loadPaper = useCallback(() => { if (dealId) api.paper(dealId).then((p) => setPaper((old) => (old && old.version === p.version && old.deal_id === p.deal_id ? old : p))).catch(() => {}); }, [dealId]);
  // what the chats read, queried and cited changes the paper's version: refresh the deal's sources with it (at most every 5 s)
  const lastDeal = useRef(0), dealTimer = useRef(0);
  useEffect(() => {
    if (!paper?.version) return;
    window.clearTimeout(dealTimer.current);
    dealTimer.current = window.setTimeout(() => { lastDeal.current = Date.now(); loadDeal(); }, Math.max(0, 5000 - (Date.now() - lastDeal.current)));
    return () => window.clearTimeout(dealTimer.current);
  }, [paper?.version, loadDeal]);
  const loadJobs = useCallback(() => { if (dealId) api.files(dealId).then((j) => { setJobs(j); if (j.some((x) => x.state === "ready")) loadDeal(); }).catch(() => {}); }, [dealId, loadDeal]);
  const parsing = jobs.some((j) => j.state === "parsing"), running = snap?.status === "running";
  useEffect(() => {
    if (!dealId) return;
    let on = true, t = 0;
    const go = () => {
      loadPaper();
      if (parsing) loadJobs();
      if (on) t = window.setTimeout(go, running || Date.now() < hurry.current ? 1200 : 5000);
    };
    go();
    return () => { on = false; window.clearTimeout(t); };
  }, [dealId, loadPaper, loadJobs, running, parsing]);

  // the open chat: snapshot on demand, live text and tool events over SSE
  const clearLive = useRef(false);  // the streamed text stays until the transcript holds the finished message
  const loadSnap = useCallback(() => {
    if (sid) api.thread(sid).then((s) => {
      setSnap(s); if (clearLive.current) { clearLive.current = false; setLive(""); }
      if (s.deal_id && s.deal_id !== dealId) setDealId(s.deal_id);
    }).catch(() => {});
  }, [sid, dealId]);
  useEffect(() => {
    setSnap(null); setLive(""); setInflight(null);
    if (!sid) return;
    loadSnap();
    const es = new EventSource(api.streamUrl(sid));
    let timer = 0;
    const soon = () => { window.clearTimeout(timer); timer = window.setTimeout(() => { loadSnap(); loadPaper(); }, 150); };
    es.addEventListener("response.output_text.delta", (e) => { const d = parse((e as MessageEvent).data); if (typeof d?.delta === "string") setLive((x) => x + d.delta); });
    es.addEventListener("response.output_item.done", (e) => {
      const it = (parse((e as MessageEvent).data) as { item?: { type?: string; status?: string; name?: string; arguments?: string; call_id?: string } } | null)?.item;
      if (it?.type === "message") clearLive.current = true;
      // a call is announced as it starts (in_progress) and again when its result is back
      if (it?.type === "function_call" && it.status === "in_progress" && it.name && !/ToolSearch/.test(it.name)) setInflight({ call_id: it.call_id ?? "", name: it.name, arguments: it.arguments });
      if (it?.type === "function_call_output" || it?.type === "message") setInflight(null);
      soon();
    });
    for (const ev of ["response.output_item.added", "response.elicitation_request", "session.status", "response.completed", "session.input.consumed"])
      es.addEventListener(ev, soon);
    const poll = window.setInterval(loadSnap, 6000); // the stream has no replay: a slow snapshot catches anything missed
    return () => { es.close(); window.clearInterval(poll); window.clearTimeout(timer); };
  }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (snap?.status === "idle") { setLive(""); setInflight(null); } }, [snap?.status]);

  const drafting = useMemo(() => {
    const open = new Set<string>(), done = new Set<string>();
    for (const it of snap?.items ?? []) if (it.type === "function_call_output") done.add(String(it.data.call_id));
    for (const it of snap?.items ?? []) if (it.type === "function_call" && /^fill_/.test(toolName(it.data.name)) && !done.has(String(it.data.call_id))) {
      const tag = parse(it.data.arguments)?.tag; if (tag) open.add(String(tag));
    }
    return running ? open : new Set<string>();
  }, [snap, running]);
  const nums = useMemo(() => citationNumbers(paper), [paper]);
  // fetch the cited pages once, so previews open without waiting
  useEffect(() => {
    const seen = new Set<string>();
    for (const s of Object.values(paper?.slots ?? {})) for (const c of s.citations ?? []) if (c.kind === "pdf") {
      const k = `${c.doc_id}/${c.page}`; if (seen.has(k)) continue; seen.add(k); new Image().src = api.pageUrl(c.doc_id, c.page);
    }
  }, [paper?.version, paper?.deal_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const allCites = useMemo(() => Object.values(paper?.slots ?? {}).flatMap((s) => (s.citations ?? []).map((c) => ({ c, tag: s.tag }))), [paper]);
  const pick = (c: Citation, text?: string) => setSel((s) => (s?.id === c.id ? null : { id: c.id, text }));
  const ctx = { nums, sel, hover, pick, setHover };
  const goTo = (tag: string) => { setView("review"); setReviewing(tag); window.setTimeout(() => document.getElementById(`slot-${tag}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 30); };
  const refs: ChatRef = {
    onSlot: goTo,
    onCite: (eid) => {
      const hit = allCites.find((x) => x.c.kind === "pdf" && x.c.element_id === eid);
      if (hit) { goTo(hit.tag); setSel({ id: hit.c.id }); } else setToast("That passage isn't cited in the paper yet.");
    },
    metric: (view, measure) => allCites.find((x) => x.c.kind === "metric" && x.c.view === view && x.c.measure === measure)?.c,
    openDoc: (id, page, eid) => { const d = detail?.documents.find((x) => x.doc_id === id); setDoc(docAt(id, page, eid, d?.title, d?.issuer)); },
    openSource: (key) => { setDoc(null); setFocus({ key, at: Date.now() }); },
  };
  const byNum = (n: number) => allCites.find((x) => nums.get(x.c.id) === n)?.c;
  const queue = useMemo(() => (paper?.template?.slots ?? []).map((x) => x.tag).filter((t) => paper?.slots[t]?.status === "filled"), [paper]);
  const next = (after?: string | null) => {
    const order = (paper?.template?.slots ?? []).map((x) => x.tag), from = after ? order.indexOf(after) : -1;
    const t = queue.find((x) => order.indexOf(x) > from) ?? queue.find((x) => x !== after);
    if (t) goTo(t); else { setReviewing(null); setToast("Everything drafted is accepted."); }
  };
  const acceptNext = async () => { if (!dealId || !reviewing) return next(); await api.review(dealId, reviewing, "accept"); loadPaper(); next(reviewing); };
  const start = async (msg: string, harness: string, model: string, mode: string, att: string[]) => {
    if (!dealId) return;
    const r = await api.start(dealId, msg, harness, model, mode, att);
    setSid(r.thread_id); hurry.current = Date.now() + 20000; refreshThreads();
  };
  const ask = async (text: string) => { if (sid) { await api.send(sid, text, []); hurry.current = Date.now() + 15000; loadSnap(); } };
  useEffect(() => { if (!toast) return; const t = window.setTimeout(() => setToast(null), 3500); return () => window.clearTimeout(t); }, [toast]);

  const st = paper?.status, dealName = detail?.deal.deal_name ?? deals.find((d) => d.deal_id === dealId)?.deal_name ?? "";
  return (
    <SourceHoverProvider workspace={me?.workspace ?? ""} onOpenDoc={setDoc}>
    <div className="app" style={{ gridTemplateColumns: `${sideW}px 0px ${chatW}px 0px minmax(0, 1fr)` }}>
      <Sidebar deals={deals} threads={threads} dealId={dealId} sid={sid} templatesOpen={templatesOpen} email={me?.email}
        onFresh={async (d) => {
          if (!window.confirm(`Start ${d.deal_name} fresh? The current project, with its chats and paper, moves to Earlier work. Nothing is deleted.`)) return;
          try { const r = await api.fresh(d.deal_id); setDeals(await api.deals()); setTemplatesOpen(false); setDealId(r.deal_id); setSid(null); setToast(`${d.deal_name} is ready to start fresh.`); }
          catch (e) { setToast((e as Error).message); }
        }}
        onDeal={(id) => { setTemplatesOpen(false); if (id !== dealId) { setDealId(id); setSid(null); } }} onThread={(d, id) => { setTemplatesOpen(false); setDealId(d); setSid(id); }}
        onNew={() => { setTemplatesOpen(false); setSid(null); }} onTemplates={() => setTemplatesOpen((v) => !v)} />
      <div className="handle" onPointerDown={dragSide} role="separator" aria-orientation="vertical" aria-label="Resize sidebar" />
      <Chat deal={detail} dealId={dealId} sid={sid} snap={snap} live={live} inflight={inflight} options={options} paper={paper} templates={templates} refs={refs}
        onStart={start} onSent={() => { hurry.current = Date.now() + 15000; loadSnap(); }} onRefresh={loadSnap} />
      <div className="handle" onPointerDown={dragChat} role="separator" aria-orientation="vertical" aria-label="Resize chat" />
      <main className="desk" ref={desk}>
        {templatesOpen ? (
          <>
            <div className="desk-head"><h2>Templates</h2><span className="meta"><span>The papers the agent drafts into</span></span></div>
            <TemplatesView templates={templates} onChanged={async () => setTemplates(await api.templates())} />
          </>
        ) : (
          <>
            <div className="desk-head">
              <h2>{paper?.template?.name ?? "Paper"}</h2>
              {st && st.slots > 0 && <span className="meta"><span><b className="num">{st.filled}</b>/{st.slots} drafted</span><span><b className="num">{st.accepted}</b> accepted</span>
                {st.source_wording != null && <span><b className="num">{Math.round(st.source_wording * 100)}%</b> source wording</span>}<span className="dim">{paper && ago(paper.updated)}</span></span>}
              <span className="grow" />
              {view === "review" && queue.length > 0 && (
                <span className="review-bar">
                  {reviewing && queue.includes(reviewing) && <button className="btn small" onClick={acceptNext}><Check size={13} /> Accept and next</button>}
                  <button className="btn small ghost" onClick={() => next(reviewing)}>{queue.length} to review <ChevronRight size={13} /></button>
                </span>
              )}
              {paper?.template && <div className="seg-switch" role="tablist">
                <button className={view === "review" ? "on" : ""} onClick={() => setView("review")} role="tab" aria-selected={view === "review"}>Review</button>
                <button className={view === "word" ? "on" : ""} onClick={() => setView("word")} role="tab" aria-selected={view === "word"}>Word</button>
              </div>}
            </div>
            {bootErr && <p className="err-line" style={{ padding: "10px 20px" }}>Could not load the deals: {bootErr}</p>}
            <div className="desk-body" style={{ gridTemplateColumns: `minmax(0, 1fr) 0px ${srcW}px` }}>
              {view === "word" && dealId && paper?.template ? <DocxPreview url={api.exportUrl(dealId)} version={paper.version} resolve={byNum} /> : (
                <div className="paper-scroll">
                  {dealId && <PaperView paper={paper} deal={dealId} dealName={dealName} thread={sid} ctx={ctx} drafting={drafting} focus={reviewing ?? paper?.focus?.tag ?? null}
                    onChanged={loadPaper} templates={templates} onAsk={ask} />}
                </div>
              )}
              <div className="handle" onPointerDown={dragSrc} role="separator" aria-orientation="vertical" aria-label="Resize sources" />
              {dealId && <SourcesPanel paper={paper} nums={nums} deal={detail} dealId={dealId} sel={sel} hover={hover} focus={focus} onPick={pick} onHover={setHover} onGoto={goTo}
                onOpenDoc={setDoc} jobs={jobs} onFilesChanged={() => { loadJobs(); window.setTimeout(loadJobs, 1500); }} workspace={me?.workspace ?? ""} width={srcW} />}
              {dealId && paper?.template && <div className="fab-wrap" style={{ right: srcW }}><ExportButton deal={dealId} /></div>}
            </div>
            {doc && <DocViewer c={doc} onClose={() => setDoc(null)} />}
          </>
        )}
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
    </SourceHoverProvider>
  );
}
