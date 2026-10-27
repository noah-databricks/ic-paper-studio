import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Check, Download, PenLine, ArrowUp, RotateCcw, Undo2 } from "lucide-react";
import { api, type Citation, type Paper as PaperT, type Slot, type SlotSpec, type TemplateRow } from "../api";
import { useSourceHover } from "./Source";
import { SlotEditor } from "./Editor";

export type Sel = { id: string; text?: string } | null;
export type Ctx = { nums: Map<string, number>; sel: Sel; hover: string | null; pick: (c: Citation, text?: string) => void; setHover: (id: string | null) => void };

const COVER = ["deal_name", "borrower", "sponsor", "facility_summary", "ic_date", "recommendation_short"];
const label = (s: SlotSpec) => s.alias || s.tag.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

function Sup({ c, ctx, text, edited }: { c: Citation; ctx: Ctx; text?: string; edited?: { text: string; original?: string } }) {
  const card = useSourceHover();
  const n = ctx.nums.get(c.id);
  if (!n) return null;
  return (
    <sup className={`cite ${edited ? "edited" : ""} ${ctx.sel?.id === c.id ? "on" : ""} ${ctx.hover === c.id ? "hot" : ""}`} role="button" tabIndex={0}
      aria-label={edited ? `Source ${n}, edited by you` : `Source ${n}`} onClick={(e) => { e.stopPropagation(); ctx.pick(c, text); }}
      onKeyDown={(e) => { if (e.key === "Enter") ctx.pick(c, text); }}
      onMouseEnter={(e) => { ctx.setHover(c.id); card.show({ c, edited }, e.currentTarget); }} onMouseLeave={() => { ctx.setHover(null); card.hide(); }}>
      {n}{edited && <PenLine size={8} strokeWidth={2.6} aria-hidden />}</sup>
  );
}

function Segments({ slot, ctx }: { slot: Slot; ctx: Ctx }) {
  const byId = useMemo(() => Object.fromEntries((slot.citations ?? []).map((c) => [c.id, c])), [slot.citations]);
  const paras = slot.paragraphs ?? [];
  const render = (segs: NonNullable<Slot["paragraphs"]>[number]["segments"]) => segs.map((s, i) => {
    const c = s.citation ? byId[s.citation] : null;
    const last = !c || segs[i + 1]?.citation !== s.citation;  // one number after a passage, even when formatting splits it
    const on = c && (ctx.sel?.id === c.id || ctx.hover === c.id);
    return (
      <Fragment key={i}>
        {i > 0 && !/^[.,;:)%]/.test(s.text) && " "}
        <span className={`seg ${s.mode} ${on ? "on" : ""}`} style={{ fontWeight: s.bold ? 600 : undefined, fontStyle: s.italic ? "italic" : undefined }}
          title={s.mode === "agent" ? "The agent's own linking words" : s.mode === "edited" ? "Shortened from the source"
            : s.mode === "analyst_edited" ? "Edited by you: hover the number for the original" : s.mode === "analyst" ? "Your words" : undefined}>{s.text}</span>
        {c && last && <Sup c={c} ctx={ctx} text={s.text} edited={s.mode === "analyst_edited" ? { text: s.text, original: s.original } : undefined} />}
      </Fragment>
    );
  });
  if (paras.every((p) => p.bullet)) return <ul>{paras.map((p, i) => <li key={i}>{render(p.segments)}</li>)}</ul>;
  return <>{paras.map((p, i) => <p key={i}>{render(p.segments)}</p>)}</>;
}

function Caption({ slot, ctx }: { slot: Slot; ctx: Ctx }) {
  const rowIds = new Set(Object.values(slot.row_citations ?? {}));
  const cites = (slot.citations ?? []).filter((c) => !rowIds.has(c.id));
  return <div className="caption">{slot.caption}{cites.map((c) => <Sup key={c.id} c={c} ctx={ctx} />)}</div>;
}

function Body({ slot, spec, ctx }: { slot: Slot; spec: SlotSpec; ctx: Ctx }) {
  if (slot.status === "empty") return spec.kind === "metric" ? <span className="muted">Not drafted</span> : <div className="placeholder">{spec.guidance}</div>;
  if (spec.kind === "text") return <div className="prose"><Segments slot={slot} ctx={ctx} /></div>;
  if (spec.kind === "table") {
    const rows = slot.rows ?? [], byId = Object.fromEntries((slot.citations ?? []).map((c) => [c.id, c])), rc = slot.row_citations ?? {};
    return (
      <>
        <table className="tbl">
          <thead><tr>{(rows[0] ?? []).map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
          <tbody>{rows.slice(1).map((r, i) => {
            const c = rc[String(i + 1)] ? byId[rc[String(i + 1)]] : null;
            return <tr key={i} className={c && ctx.sel?.id === c.id ? "on" : ""}>{r.map((v, j) => <td key={j}>{v}{c && j === r.length - 1 && <Sup c={c} ctx={ctx} />}</td>)}</tr>;
          })}</tbody>
        </table>
        <Caption slot={slot} ctx={ctx} />
      </>
    );
  }
  if (spec.kind === "image") return <figure className="fig">{slot.image && <img src={api.fileUrl(slot.image.uri)} alt={slot.caption ?? label(spec)} />}<Caption slot={slot} ctx={ctx} /></figure>;
  const c = slot.citations?.[0];
  return <span className="inline-val">{slot.text}{c ? <Sup c={c} ctx={ctx} text={slot.text} /> : <span className="draft"> agent draft</span>}</span>;
}

function SlotBar({ spec, slot, deal, thread, drafting, onChanged, onEdit, editing }: { spec: SlotSpec; slot: Slot; deal: string; thread: string | null; drafting: boolean;
  onChanged: () => void; onEdit: () => void; editing: boolean }) {
  const [redo, setRedo] = useState(false), [note, setNote] = useState(""), [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); onChanged(); } finally { setBusy(false); } };
  const filled = slot.status !== "empty";
  return (
    <div className="slot-bar">
      <span className="name">{label(spec)}</span>
      {drafting ? <span className="state-tag drafting">Drafting</span> : slot.status === "accepted" ? <span className="state-tag accepted"><Check size={11} /> Accepted</span>
        : filled ? <span className="state-tag filled">For review</span> : null}
      {filled && spec.kind === "text" && slot.source_wording != null && <span className="wording">{Math.round(slot.source_wording * 100)}% source wording</span>}
      {filled && (
        <span className="acts">
          {slot.status !== "accepted" ? <button className="btn small" disabled={busy} onClick={() => act(() => api.review(deal, spec.tag, "accept"))}><Check size={13} /> Accept</button>
            : <button className="btn small ghost" disabled={busy} onClick={() => act(() => api.review(deal, spec.tag, "reopen"))}>Reopen</button>}
          {spec.kind === "text" && !drafting && <button className="btn small ghost" disabled={busy || editing} onClick={onEdit}><PenLine size={13} /> Edit</button>}
          {thread && <button className="btn small ghost" disabled={busy} onClick={() => setRedo((v) => !v)}><RotateCcw size={13} /> Redo</button>}
          {(slot.history?.length ?? 0) > 0 && <button className="icon-btn" aria-label="Back to the previous version" title="Back to the previous version" disabled={busy} onClick={() => act(() => api.review(deal, spec.tag, "revert"))}><Undo2 size={14} /></button>}
        </span>
      )}
      {redo && thread && (
        <div className="redo-pop">
          <textarea autoFocus placeholder="What should change? (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <button className="btn small ghost" onClick={() => setRedo(false)}>Cancel</button>
            <button className="btn small primary" onClick={() => act(async () => { await api.redo(deal, spec.tag, thread, note || undefined); setRedo(false); setNote(""); })}><ArrowUp size={13} /> Ask the agent</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function PaperView({ paper, deal, dealName, thread, ctx, drafting, focus, onChanged, templates, onAsk }: {
  paper: PaperT | null; deal: string; dealName: string; thread: string | null; ctx: Ctx; drafting: Set<string>; focus: string | null;
  onChanged: () => void; templates: TemplateRow[]; onAsk: (text: string) => void;
}) {
  const specs = paper?.template?.slots ?? [];
  const sections = useMemo(() => {
    const out: { name: string; slots: SlotSpec[] }[] = [];
    for (const s of specs) {
      if (s.section === "Cover" || COVER.includes(s.tag)) continue;
      if (!out.length || out[out.length - 1].name !== s.section) out.push({ name: s.section, slots: [] });
      out[out.length - 1].slots.push(s);
    }
    return out;
  }, [specs]);
  useEffect(() => { if (focus) document.getElementById(`slot-${focus}`)?.scrollIntoView({ behavior: "smooth", block: "center" }); }, [focus]);
  const slot = (t: string) => paper?.slots[t] ?? ({ tag: t, kind: "text", status: "empty" } as Slot);
  const [editing, setEditing] = useState<string | null>(null);
  const cover = specs.filter((s) => COVER.includes(s.tag) || s.section === "Cover");

  if (!paper?.template) {
    return (
      <div className="sheet">
        <div className="pg pg-top"><Band /><h1>{dealName}</h1>
          <p className="sub">No template yet. The agent suggests one once it has looked at the deal; you can pick a different one.</p>
          <div className="tpl-list">{templates.map((t) => (
            <button key={t.template_id} className="tpl" disabled={!thread} onClick={() => onAsk(`Use the ${t.name} for this deal.`)}>
              <b>{t.name}</b><span>{t.use_when ?? t.purpose}</span>
            </button>
          ))}</div>
          {!thread && <p className="muted">Start a chat on this deal to begin.</p>}
        </div>
      </div>
    );
  }
  return (
    <div className="sheet">
      <div className="pg pg-top">
        <Band />
        <h1>{paper.slots.deal_name?.text ?? dealName}</h1>
        <div className="tpl-line">
          <span className="tpl-name">{paper.template.name}</span>
        </div>
        {paper.template_reason && <p className="tpl-why">{paper.template_reason}</p>}
        <table className="cover"><tbody>{cover.map((s) => (
          <tr key={s.tag} id={`slot-${s.tag}`} className={`${drafting.has(s.tag) ? "drafting" : ""} ${focus === s.tag ? "focus" : ""}`}>
            <th>{label(s)}</th><td><Body slot={slot(s.tag)} spec={s} ctx={ctx} /></td>
          </tr>
        ))}</tbody></table>
      </div>
      {sections.map((sec) => (
        <Fragment key={sec.name}>
          <div className="pg"><h2>{sec.name}</h2></div>
          {sec.slots.map((s) => (
            <Fragment key={s.tag}>
              <div className={`pg slot ${drafting.has(s.tag) ? "drafting" : ""} ${focus === s.tag ? "focus" : ""} ${editing === s.tag ? "editing" : ""}`} id={`slot-${s.tag}`}>
                <SlotBar spec={s} slot={slot(s.tag)} deal={deal} thread={thread} drafting={drafting.has(s.tag)} onChanged={onChanged}
                  onEdit={() => setEditing(s.tag)} editing={editing === s.tag} />
                {editing === s.tag
                  ? <SlotEditor slot={slot(s.tag)} onCancel={() => setEditing(null)} onSave={async (paras) => { await api.edit(deal, s.tag, paras); setEditing(null); onChanged(); }} />
                  : <div onDoubleClick={() => s.kind === "text" && slot(s.tag).status !== "empty" && !drafting.has(s.tag) && setEditing(s.tag)}><Body slot={slot(s.tag)} spec={s} ctx={ctx} /></div>}
              </div>
            </Fragment>
          ))}
        </Fragment>
      ))}
      <div className="pg pg-end" />
    </div>
  );
}

function Band() {
  return <div className="page-band"><img src="/studio-logo.svg" alt="Quillwood Credit Partners" /><span>Strictly confidential</span></div>;
}

/** A .docx rendered in the browser. Comment markers open the Studio's own source card for the reference they carry. */
export function DocxPreview({ url, version, resolve }: { url: string; version: number; resolve?: (n: number) => Citation | undefined }) {
  const ref = useRef<HTMLDivElement>(null), box = useRef<HTMLDivElement>(null);
  const card = useSourceHover();
  const [err, setErr] = useState<string | null>(null);
  const fit = () => {
    const page = ref.current?.querySelector<HTMLElement>("section.docx"), wrap = ref.current?.querySelector<HTMLElement>(".docx-wrapper");
    if (!page || !wrap || !box.current) return;
    wrap.style.zoom = String(Math.min(1, (box.current.clientWidth - 56) / page.offsetWidth));
  };
  useEffect(() => { const el = box.current; if (!el) return; const ro = new ResizeObserver(fit); ro.observe(el); return () => ro.disconnect(); }, []);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(await r.text());
        const blob = await r.blob();
        const { renderAsync } = await import("docx-preview");
        if (live && ref.current) {
          ref.current.innerHTML = "";
          await renderAsync(blob, ref.current, undefined, { inWrapper: true, renderComments: !!resolve, ignoreLastRenderedPageBreak: true });
          fit();
        }
      } catch (e) { if (live) setErr((e as Error).message); }
    })();
    return () => { live = false; };
  }, [url, version]); // eslint-disable-line react-hooks/exhaustive-deps
  const over = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>(".docx-comment-ref");
    if (!el || !resolve) return;
    const m = (el.nextElementSibling?.textContent ?? "").match(/\[(\d+)\]/), c = m ? resolve(Number(m[1])) : undefined;
    if (c) card.show({ c }, el);
  };
  return (
    <div className="word-view" ref={box} onMouseOver={over} onMouseOut={(e) => { if ((e.target as HTMLElement).closest(".docx-comment-ref")) card.hide(); }}>
      {err && <p className="err-line">Could not render the Word document: {err}</p>}<div ref={ref} />
    </div>
  );
}

/** Floating over the paper: the one thing to do with it when it is ready. */
export function ExportButton({ deal }: { deal: string }) {
  return <a className="export-fab" href={api.exportUrl(deal)} download><Download size={15} /> Export .docx</a>;
}
