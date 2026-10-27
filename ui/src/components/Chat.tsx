import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AlertCircle, ArrowUp, BookOpen, Check, ChevronRight, FilePlus2, FileText, LayoutTemplate, Library, ListChecks, Paperclip, PenLine, Regex, Search, Square, Table2, X } from "lucide-react";
import { api, DOC_TYPE, parse, prettyModel, shortDate, toolName, VIEW_LABELS, type AgentItem, type Citation, type DealDetail, type Elicitation, type Options, type Paper, type Snapshot, type TemplateRow } from "../api";
import { useSourceHover } from "./Source";

type Att = { name: string; path?: string; state: "up" | "ok" | "err" };
export type ChatRef = { onSlot: (tag: string) => void; onCite: (elementId: string) => void; metric: (view: string, measure: string) => Citation | undefined;
  /** open a library document in the viewer (at a page, highlighting a passage), or show a table in the Sources panel */
  openDoc: (docId: string, page?: number, elementId?: string) => void; openSource: (key: string) => void };

function pair(items: AgentItem[]) {
  // calls paired with their outputs; Omnigent's MCP proxy can record a call twice, so collapse identical neighbours
  const used = new Set<number>(), rows: { it: AgentItem; out?: AgentItem }[] = [];
  items.forEach((it, i) => {
    if (it.type === "message") { rows.push({ it }); return; }
    if (it.type !== "function_call" || /ToolSearch/.test(String(it.data.name))) return;
    let out: AgentItem | undefined;
    for (let j = i + 1; j < items.length; j++) {
      const o = items[j];
      if (!used.has(j) && o.type === "function_call_output" && o.data.call_id === it.data.call_id) { used.add(j); out = o; break; }
    }
    const prev = rows[rows.length - 1];
    if (prev && prev.it.type === "function_call" && prev.it.data.name === it.data.name && prev.it.data.arguments === it.data.arguments) {
      rows[rows.length - 1] = { it, out: out ?? prev.out };
      return;
    }
    rows.push({ it, out });
  });
  return rows;
}

const ICON: Record<string, typeof FileText> = { get_deal: BookOpen, list_templates: LayoutTemplate, set_template: LayoutTemplate, get_brief: ListChecks,
  find_documents: Library, search_library: Search, grep: Regex, list_metric_views: Table2, read_document: FileText, query_metric: Table2, fill_text: PenLine, fill_table: PenLine,
  fill_image: PenLine, fill_metric: PenLine, get_paper: ListChecks, add_file_to_deal: FilePlus2, show_slot: ChevronRight };

function failed(out?: AgentItem) {
  const raw = out?.data.output ?? "";
  if (/denied|DENY/i.test(raw) && /policy|declin/i.test(raw)) return "Declined";
  return /Error executing tool|"isError":\s*true|ToolError/.test(raw) ? raw.replace(/^.*?Error executing tool \w+: /s, "").slice(0, 220) : null;
}

function describe(name: string, a: Record<string, unknown>, r: Record<string, unknown> | null, done: boolean, deal: DealDetail | null, paper: Paper | null, templates: TemplateRow[]) {
  const doc = (id: unknown) => (r?.title as string) ?? deal?.documents.find((d) => d.doc_id === id)?.title ?? String(id ?? "").replace(/_/g, " ");
  const hits = list(r), docsIn = new Set((hits ?? []).map((h) => h.doc_id)).size;
  const types = (a.doc_type as string[] | undefined)?.map((t) => (DOC_TYPE[t] ?? t).toLowerCase()).join(", ");
  const about = a.counterparty === "deal" ? "this deal" : a.counterparty ? String(a.counterparty) : "";
  const found = (n: number, one: string, many: string) => (done && hits ? <span className="dim"> · {n ? `${n} ${n === 1 ? one : many}` : "nothing found"}</span> : null);
  const slot = (t: unknown) => { const s = paper?.template?.slots.find((x) => x.tag === t); return s?.alias || String(t ?? "").replace(/_/g, " "); };
  const tpl = (id: unknown) => templates.find((t) => t.template_id === id)?.name ?? String(id ?? "");
  switch (name) {
    case "get_deal": return done ? "Read the deal record from the origination system" : "Reading the deal record from the origination system";
    case "list_templates": return done ? "Looked at the paper templates" : "Looking at the paper templates";
    case "set_template": return <>{done ? "Chose" : "Choosing"} the <b>{tpl(a.template_id)}</b></>;
    case "get_brief": return done ? "Read the template's slots" : "Reading the template's slots";
    case "find_documents":
      return <>{done ? "Looked up" : "Looking up"} {types || "documents"}{about ? <> for <b>{about}</b></> : ""}{a.query ? <> matching “{String(a.query)}”</> : ""}{found(hits?.length ?? 0, "document", "documents")}</>;
    case "search_library":
      return <>{done ? "Searched" : "Searching"} the library for “{String(a.query ?? "")}”{about ? <> ({about})</> : ""}{found(hits?.length ?? 0, "passage", "passages")}{done && hits?.length ? <span className="dim"> from {docsIn} document{docsIn === 1 ? "" : "s"}</span> : null}</>;
    case "grep": return <>{done ? "Matched" : "Matching"} <code>{String(a.pattern ?? "")}</code>{found(hits?.length ?? 0, "hit", "hits")}</>;
    case "list_metric_views": return done ? "Looked at the governed figures" : "Looking at the governed figures";
    case "read_document": return <>{done ? "Read" : "Reading"} <b>{doc(a.doc_id)}</b>{done && r?.superseded_by ? <span className="dim"> · superseded</span> : null}</>;
    case "query_metric": {
      const f = (r?.rows as { formatted?: Record<string, string> }[] | undefined)?.[0]?.formatted;
      return <>{done ? "Queried" : "Querying"} <b>{VIEW_LABELS[String(a.view)] ?? "the governed figures"}</b>{f && Object.keys(f).length <= 3 ? `: ${Object.entries(f).map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}</>;
    }
    case "fill_text": case "fill_table": case "fill_image": case "fill_metric":
      return <>{done ? "Drafted" : "Drafting"} <b>{slot(a.tag)}</b>{r?.citations ? <span className="dim"> · {String(r.citations)} source{r.citations === 1 ? "" : "s"}</span> : ""}</>;
    case "get_paper": return done ? "Checked the paper" : "Checking the paper";
    case "add_file_to_deal": return <>{done ? "Added" : "Adding"} <b>{String(a.path ?? "").split("/").pop()?.replace(/^\d+_/, "")}</b> to the deal</>;
    case "show_slot": return <>{done ? "Pointed to" : "Pointing to"} <b>{slot(a.tag)}</b></>;
    default: return name.replace(/_/g, " ");
  }
}

type Hit = { element_id?: string; doc_id: string; title?: string; page?: number; text?: string; doc_type?: string; as_of?: string; author?: string; superseded_by?: string };
/** What a retrieval tool returned: its documents, passages or matches. */
function list(r: unknown): Hit[] | null {
  const o = r as { documents?: Hit[]; passages?: Hit[]; matches?: Hit[] } | null;
  return o?.documents ?? o?.passages ?? o?.matches ?? null;
}

/** What a retrieval step found: passages (hover shows the page) or document cards. */
function Hits({ hits, onOpen }: { hits: Hit[]; onOpen: (h: Hit) => void }) {
  const card = useSourceHover();
  const open = (h: Hit) => (e: React.MouseEvent) => { e.stopPropagation(); card.hide(); onOpen(h); };
  return (
    <div className="hits">
      {hits.slice(0, 8).map((h, i) => h.element_id ? (
        <div key={i} className="hit open" onClick={open(h)} onMouseEnter={(e) => card.show({ element: h.element_id! }, e.currentTarget)} onMouseLeave={card.hide}>
          <span className="t"><b>{h.title ?? h.doc_id}</b><span className="dim"> · page {h.page}{h.superseded_by ? " · superseded" : ""}</span></span>
          {h.text && <span className="x">{h.text.slice(0, 180)}{h.text.length > 180 ? "…" : ""}</span>}
        </div>
      ) : (
        <div key={i} className="hit open" onClick={open(h)}>
          <span className="t"><b>{h.title ?? h.doc_id}</b><span className="dim"> · {[DOC_TYPE[h.doc_type ?? ""] ?? h.doc_type, h.author, shortDate(h.as_of)].filter(Boolean).join(" · ")}{h.superseded_by ? " · superseded" : ""}</span></span>
        </div>
      ))}
      {hits.length > 8 && <span className="dim more">and {hits.length - 8} more</span>}
    </div>
  );
}

function Work({ rows, live, ...p }: { rows: { it: AgentItem; out?: AgentItem }[]; live: boolean; deal: DealDetail | null; paper: Paper | null; templates: TemplateRow[]; refs: ChatRef }) {
  const [all, setAll] = useState(false), [open, setOpen] = useState<string | null>(null);
  const LIMIT = 5;
  const hidden = !all && rows.length > LIMIT + 1 ? rows.length - LIMIT : 0;
  const shown = hidden ? rows.slice(hidden) : rows;
  const fills = new Set(rows.filter((r) => /^fill_/.test(toolName(r.it.data.name)) && r.out && !failed(r.out)).map((r) => String(parse(r.it.data.arguments)?.tag ?? ""))).size;
  return (
    <div className={`work ${live ? "live" : ""}`}>
      {hidden > 0 && <button className="work-more" onClick={() => setAll(true)}>{hidden} earlier step{hidden > 1 ? "s" : ""}{fills ? ` · ${fills} section${fills > 1 ? "s" : ""} drafted so far` : ""}</button>}
      {shown.map(({ it, out }) => {
        const name = toolName(it.data.name), a = parse(it.data.arguments) ?? {}, r = parse(out?.data.output), err = failed(out), Ic = ICON[name] ?? ChevronRight;
        const hits = /^(search_library|grep|find_documents)$/.test(name) && !err ? list(r) : null;
        const pages = a.pages as number[] | undefined;
        const jump = err ? null
          : /^fill_|show_slot/.test(name) ? () => p.refs.onSlot(String(a.tag))
          : hits?.length ? () => setOpen((o) => (o === it.id ? null : it.id))
          : name === "read_document" && a.doc_id ? () => p.refs.openDoc(String(a.doc_id), pages?.[0] ?? 1)
          : name === "query_metric" && out ? () => p.refs.openSource(`mv:${String(a.view)}`)
          : name === "get_deal" && out ? () => p.refs.openSource("record:deals")
          : null;
        return (
          <div key={it.id} className={`step ${err ? "err" : ""} ${jump ? "jump" : ""} ${!out && live ? "active" : ""}`} onClick={jump ?? undefined}
            aria-expanded={hits?.length ? open === it.id : undefined}>
            <span className="ic">{!out && live ? <span className="spin" /> : err ? <AlertCircle size={13} /> : <Ic size={13} />}</span>
            <span><span className="label">{describe(name, a, r, !!out, p.deal, p.paper, p.templates)}</span>{err && <span className="why">{err}</span>}
              {hits && open === it.id && <Hits hits={hits} onOpen={(h) => p.refs.openDoc(h.doc_id, h.page ?? 1, h.element_id)} />}</span>
          </div>
        );
      })}
    </div>
  );
}

function Reply({ text, refs }: { text: string; refs: ChatRef }) {
  const card = useSourceHover();
  // [[cite:id]] and [[metric:view.measure]] become numbered sources, in order of first appearance in this reply
  const order: string[] = [];
  const md = text.replace(/\s+(?=\[\[(cite|metric):)/g, "").replace(/\[\[(cite|metric):([^\]]+)\]\]/g, (_, kind, id) => {
    const key = `${kind}:${id}`;
    if (!order.includes(key)) order.push(key);
    return `[${order.indexOf(key) + 1}](${key})`;
  });
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={(u) => u}
      components={{
        a: ({ href, children }) => {
          if (href?.startsWith("cite:")) {
            const id = href.slice(5);
            return <sup className="cite" role="button" tabIndex={0} onMouseEnter={(e) => card.show({ element: id }, e.currentTarget)} onMouseLeave={card.hide}
              onClick={() => refs.onCite(id)}>{children}</sup>;
          }
          if (href?.startsWith("metric:")) {
            const [view, ...m] = href.slice(7).split("."), c = refs.metric(view, m.join("."));
            return <sup className="cite metric" onMouseEnter={(e) => c && card.show({ c }, e.currentTarget)} onMouseLeave={card.hide}>{children}</sup>;
          }
          return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
        },
      }}>{md}</ReactMarkdown>
  );
}

function userText(it: AgentItem) {
  const t = (it.data.content ?? []).map((c) => c.text ?? "").join("");
  const [body, att] = t.split("\n\n[Attached files]\n");
  return { body: body.replace(/^\[Studio\] /, ""), studio: t.startsWith("[Studio]"), files: (att ?? "").split("\n").filter(Boolean).map((l) => l.replace(/^- .*\/\d+_/, "")) };
}

function Approval({ el, templates, onResolve, onInstead }: { el: Elicitation; templates: TemplateRow[]; onResolve: (a: "accept" | "decline") => void; onInstead: (text: string) => void }) {
  const p = el.params ?? {}, raw = p.arguments ?? p.content_preview;
  const args = ((typeof raw === "string" ? parse(raw) : raw) ?? {}) as Record<string, unknown>;
  // the approval carries the call's arguments; the tool is named only sometimes, so recognise it from them
  const tool = toolName(String(p.tool_name ?? "")) || ("template_id" in args ? "set_template" : "path" in args ? "add_file_to_deal" : "");
  const t = templates.find((x) => x.template_id === args.template_id);
  return (
    <div className="approval" role="group" aria-label="Approval needed">
      {tool === "set_template" ? (
        <>
          <div className="ap-title">Use the <b>{t?.name ?? String(args.template_id)}</b> for this deal?</div>
          {args.reason ? <p className="ap-why">{String(args.reason)}</p> : null}
        </>
      ) : tool === "add_file_to_deal" ? (
        <div className="ap-title">Add <b>{String(args.path ?? "").split("/").pop()?.replace(/^\d+_/, "")}</b> to the deal's sources?</div>
      ) : <div className="ap-title">{String(p.message ?? tool.replace(/_/g, " "))}</div>}
      <div className="ap-row">
        <button className="btn small primary" onClick={() => onResolve("accept")}><Check size={13} /> Approve</button>
        <button className="btn small" onClick={() => onResolve("decline")}>Decline</button>
        {tool === "set_template" && (
          <select aria-label="Use another template" value="" onChange={(e) => { if (!e.target.value) return; const name = templates.find((x) => x.template_id === e.target.value)?.name; onResolve("decline"); onInstead(`No, use the ${name} instead.`); }}>
            <option value="">Use another template…</option>
            {templates.filter((x) => x.template_id !== args.template_id).map((x) => <option key={x.template_id} value={x.template_id}>{x.name}</option>)}
          </select>
        )}
      </div>
    </div>
  );
}

export function Chat({ deal, dealId, sid, snap, live, inflight, options, paper, templates, refs, onStart, onSent, onRefresh }: {
  deal: DealDetail | null; dealId: string | null; sid: string | null; snap: Snapshot | null; live: string; options: Options | null; paper: Paper | null;
  inflight: { call_id: string; name: string; arguments?: string } | null;
  templates: TemplateRow[]; refs: ChatRef; onStart: (msg: string, harness: string, model: string, mode: string, att: string[]) => Promise<void>; onSent: () => void; onRefresh: () => void;
}) {
  const [text, setText] = useState(""), [att, setAtt] = useState<Att[]>([]), [model, setModel] = useState(""), [mode, setMode] = useState("ask");
  const [harness, setHarness] = useState(() => localStorage.getItem("ic.harness") ?? "");
  const [err, setErr] = useState<string | null>(null), [busy, setBusy] = useState(false), [outbox, setOutbox] = useState<string | null>(null);
  const [decided, setDecided] = useState<Set<string>>(new Set());
  const log = useRef<HTMLDivElement>(null), file = useRef<HTMLInputElement>(null), pinned = useRef(true);
  const hOpt = options?.harnesses.find((h) => h.id === harness) ?? options?.harnesses.find((h) => h.id === options.default_harness);
  useEffect(() => { if (options && !options.harnesses.some((h) => h.id === harness)) setHarness(options.default_harness); }, [options, harness]);
  useEffect(() => { if (hOpt && !hOpt.models.includes(model)) setModel(hOpt.default_model); }, [hOpt, model]);
  const pickHarness = (h: string) => { setHarness(h); localStorage.setItem("ic.harness", h); };
  useEffect(() => { if (snap?.mode) setMode(snap.mode); }, [snap?.mode]);
  useEffect(() => { setOutbox(null); setErr(null); }, [sid]);
  const rows = useMemo(() => pair(snap?.items ?? []), [snap]);
  const running = snap?.status === "running" || snap?.status === "waiting";
  const groups = useMemo(() => {
    const out: ({ kind: "msg"; it: AgentItem } | { kind: "work"; rows: typeof rows })[] = [];
    for (const r of rows) {
      if (r.it.type === "message") out.push({ kind: "msg", it: r.it });
      else if (out.length && out[out.length - 1].kind === "work") (out[out.length - 1] as { rows: typeof rows }).rows.push(r);
      else out.push({ kind: "work", rows: [r] });
    }
    return out;
  }, [rows]);
  const pending = (snap?.pending_elicitations ?? []).filter((e) => !decided.has(String(e.elicitation_id ?? e.id ?? "")));
  useEffect(() => {
    if (!outbox || !snap) return;
    if (snap.items.some((it) => it.type === "message" && it.data.role === "user" && userText(it).body.trim().startsWith(outbox.slice(0, 40)))) setOutbox(null);
  }, [snap, outbox]);
  useEffect(() => { const el = log.current; if (el && pinned.current) el.scrollTop = el.scrollHeight; }, [rows.length, outbox, live, pending.length]);

  const attach = async (files: FileList | null) => {
    if (!dealId) return;
    for (const f of Array.from(files ?? [])) {
      setAtt((a) => [...a, { name: f.name, state: "up" }]);
      try { const r = await api.upload(dealId, f); setAtt((a) => a.map((x) => (x.name === f.name ? { ...x, path: r.path, state: "ok" } : x))); }
      catch { setAtt((a) => a.map((x) => (x.name === f.name ? { ...x, state: "err" } : x))); }
    }
  };
  const submit = async (msg?: string) => {
    const m = (msg ?? text).trim(), paths = att.filter((a) => a.state === "ok").map((a) => a.path!);
    if (!m && !paths.length) return;
    setBusy(true); setErr(null); setOutbox(m || "Here are some files."); pinned.current = true;
    try {
      if (!sid) await onStart(m, hOpt?.id ?? "claude-sdk", model, mode, paths); else { await api.send(sid, m || "Here are some files.", paths); onSent(); }
      if (!msg) { setText(""); setAtt([]); }
    } catch (e) { setErr((e as Error).message); setOutbox(null); } finally { setBusy(false); }
  };
  const resolve = async (el: Elicitation, action: "accept" | "decline") => {
    if (!sid) return;
    const k = String(el.elicitation_id ?? el.id ?? "");
    setDecided((d) => new Set(d).add(k));
    try { await api.resolve(sid, k, action); onSent(); } catch (e) { setErr((e as Error).message); }
  };
  const changeMode = async (m: string) => { setMode(m); if (sid) try { await api.setMode(sid, m); onRefresh(); } catch (e) { setErr((e as Error).message); } };
  const dealName = deal?.deal.deal_name ?? "";

  return (
    <section className="chat" aria-label="Agent">
      <div className="chat-head">
        <h2>{snap?.title && sid ? snap.title : dealName ? `New chat · ${dealName}` : "Choose a deal"}</h2>
        <span className="state">{running ? <><span className="spin" /> Working</> : ""}</span>
      </div>
      <div className="log" ref={log} onScroll={(e) => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
        {!sid && !outbox && deal && (
          <div className="start">
            <p className="muted">{deal.deal.request_summary ?? `${deal.deal.borrower} · ${deal.deal.status}`}</p>
            {[["Prepare the credit paper for this deal", "Look at the deal, pick the right template, then draft it with sources."],
              ["What is being asked, and what template fits?", "A quick read of the deal before anything is drafted."],
              ["What are the key risks?", "With a source for each."]].map(([b, s]) => (
              <button key={b} className="suggest" disabled={busy} onClick={() => submit(b)}><b>{b}</b><span>{s}</span></button>
            ))}
          </div>
        )}
        {groups.map((g, i) => g.kind === "work"
          ? <Work key={i} rows={g.rows} live={running && i === groups.length - 1} deal={deal} paper={paper} templates={templates} refs={refs} />
          : g.it.data.role === "user" ? (() => {
            const u = userText(g.it);
            return u.studio ? <div key={i} className="studio-note">{u.body.replace(/ Redo only that slot.*$/, "")}</div> : (
              <div key={i} className="msg user"><div className="bubble">{u.body}{u.files.length > 0 && <div className="att">{u.files.map((f) => <span key={f} className="att-pill"><Paperclip size={12} />{f}</span>)}</div>}</div></div>
            );
          })() : (
            <div key={i} className="msg agent"><Reply text={(g.it.data.content ?? []).map((c) => c.text ?? "").join("")} refs={refs} /></div>
          ))}
        {live && <div className="msg agent streaming"><Reply text={live} refs={refs} /></div>}
        {running && inflight && !rows.some((r) => r.it.data.call_id === inflight.call_id && r.out) && (
          <div className="work live"><div className="step active" role="status">
            <span className="ic"><span className="spin" /></span>
            <span><span className="label">{describe(toolName(inflight.name), parse(inflight.arguments) ?? {}, null, false, deal, paper, templates)}</span></span>
          </div></div>
        )}
        {running && !live && !inflight && !pending.length && !(rows.length && rows[rows.length - 1].it.type === "function_call" && !rows[rows.length - 1].out) && (
          <div className="thinking" role="status"><span className="pulse" aria-hidden /><span className="label">{rows.length ? "Thinking" : "Reading the deal"}</span></div>
        )}
        {pending.map((el) => <Approval key={String(el.elicitation_id ?? el.id)} el={el} templates={templates} onResolve={(a) => resolve(el, a)} onInstead={(t) => submit(t)} />)}
        {outbox && <div className="msg user"><div className="bubble">{outbox}</div></div>}
        {!running && outbox && busy && rows.length === 0 && <div className="thinking" role="status"><span className="pulse" aria-hidden /><span className="label">Starting the chat</span></div>}
        {err && <p className="err-line">{err}</p>}
      </div>
      <div className="composer">
        <div className="box">
          {att.length > 0 && (
            <div className="atts">{att.map((a) => (
              <span key={a.name} className="att-pill">{a.state === "up" ? <span className="spin" /> : <Paperclip size={12} />}{a.name}{a.state === "err" && " (failed)"}
                <button aria-label={`Remove ${a.name}`} onClick={() => setAtt((x) => x.filter((y) => y.name !== a.name))}><X size={12} /></button></span>
            ))}</div>
          )}
          <textarea value={text} onChange={(e) => setText(e.target.value)} disabled={!dealId}
            placeholder={!dealId ? "Choose a deal on the left" : sid ? "Reply, ask about the paper, or ask for a change" : "Ask the agent about this deal"}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }} aria-label="Message the agent" />
          <div className="box-row">
            <input ref={file} type="file" hidden multiple accept=".pdf,.docx,.png,.jpg,.jpeg" onChange={(e) => { attach(e.target.files); e.target.value = ""; }} />
            <button className="icon-btn" aria-label="Attach a file" title="Attach a file" disabled={!dealId} onClick={() => file.current?.click()}><Paperclip size={16} /></button>
            {options && (
              <select value={mode} onChange={(e) => changeMode(e.target.value)} aria-label="Approval mode" title="What the agent may do without asking">
                {options.modes.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            )}
            {!sid && options && options.harnesses.length > 1 && (
              <select value={hOpt?.id ?? ""} onChange={(e) => pickHarness(e.target.value)} aria-label="Agent harness" title="Which agent harness runs this chat">
                {options.harnesses.map((h) => <option key={h.id} value={h.id}>{h.label}</option>)}
              </select>
            )}
            {!sid && hOpt && (
              <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
                {hOpt.models.map((m) => <option key={m} value={m}>{prettyModel(m)}</option>)}
              </select>
            )}
            {sid && snap?.labels?.harness && <span className="run-on">{options?.harnesses.find((h) => h.id === snap.labels!.harness)?.label ?? snap.labels.harness} · {prettyModel(snap.labels.model ?? "")}</span>}
            <span className="grow" />
            {running && sid ? <button className="btn small" onClick={() => api.stop(sid)}><Square size={11} /> Stop</button>
              : <button className="btn small primary send" disabled={busy || !dealId || (!text.trim() && !att.some((a) => a.state === "ok"))} onClick={() => submit()} aria-label="Send"><ArrowUp size={14} /></button>}
          </div>
        </div>
      </div>
    </section>
  );
}

