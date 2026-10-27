import { useEffect, useRef, useState } from "react";
import { Bold, Italic, List } from "lucide-react";
import type { Slot } from "../api";

const esc = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** The section as editable HTML: each cited passage is a span that remembers its citation, so edits keep their source. */
function toHtml(slot: Slot) {
  const para = (segs: NonNullable<Slot["paragraphs"]>[number]["segments"]) => segs.map((s, i) => {
    let t = esc(s.text);
    if (s.bold) t = `<b>${t}</b>`;
    if (s.italic) t = `<i>${t}</i>`;
    const space = i > 0 && !/^[.,;:)%]/.test(s.text) ? " " : "";
    return space + (s.citation ? `<span data-cite="${s.citation}">${t}</span>` : t);
  }).join("");
  const ps = slot.paragraphs ?? [];
  if (ps.length && ps.every((p) => p.bullet)) return `<ul>${ps.map((p) => `<li>${para(p.segments)}</li>`).join("")}</ul>`;
  return ps.map((p) => (p.bullet ? `<ul><li>${para(p.segments)}</li></ul>` : `<p>${para(p.segments)}</p>`)).join("");
}

type Seg = { text: string; citation: string | null; bold?: boolean; italic?: boolean };

/** Read the edited HTML back into paragraphs of segments: same citation and formatting → one segment. */
function fromHtml(root: HTMLElement) {
  const blocks: { bullet: boolean; el: Element }[] = [];
  const walkBlocks = (el: Element) => {
    for (const c of Array.from(el.children)) {
      const tag = c.tagName;
      if (tag === "UL" || tag === "OL") walkBlocks(c);
      else if (tag === "LI") blocks.push({ bullet: true, el: c });
      else if (tag === "P" || tag === "DIV") blocks.push({ bullet: false, el: c });
    }
  };
  walkBlocks(root);
  if (!blocks.length) blocks.push({ bullet: false, el: root });
  return blocks.map(({ bullet, el }) => {
    const segs: Seg[] = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const p = n.parentElement!, txt = n.textContent ?? "";
      if (!txt) continue;
      const cite = p.closest<HTMLElement>("[data-cite]");
      const s: Seg = { text: txt, citation: cite && el.contains(cite) ? cite.dataset.cite! : null,
        bold: !!p.closest("b,strong") && el.contains(p.closest("b,strong")), italic: !!p.closest("i,em") && el.contains(p.closest("i,em")) };
      const last = segs[segs.length - 1];
      if (last && last.citation === s.citation && !!last.bold === s.bold && !!last.italic === s.italic) last.text += s.text;
      else segs.push(s);
    }
    return { bullet, segments: segs.map((s) => ({ ...s, text: s.text.replace(/\s+/g, " ").trim() })).filter((s) => s.text) };
  }).filter((p) => p.segments.length);
}

export function SlotEditor({ slot, onSave, onCancel }: { slot: Slot; onSave: (paragraphs: ReturnType<typeof fromHtml>) => Promise<void>; onCancel: () => void }) {
  const ref = useRef<HTMLDivElement>(null), [busy, setBusy] = useState(false), [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (ref.current) { ref.current.innerHTML = toHtml(slot); ref.current.focus(); } }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const cmd = (c: string) => { ref.current?.focus(); document.execCommand(c); };
  const save = async () => {
    if (!ref.current) return;
    setBusy(true); setErr(null);
    try { await onSave(fromHtml(ref.current)); } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <div className="editor">
      <div className="ed-bar" role="toolbar" aria-label="Formatting">
        <button className="icon-btn" aria-label="Bold" title="Bold (⌘B)" onMouseDown={(e) => { e.preventDefault(); cmd("bold"); }}><Bold size={14} /></button>
        <button className="icon-btn" aria-label="Italic" title="Italic (⌘I)" onMouseDown={(e) => { e.preventDefault(); cmd("italic"); }}><Italic size={14} /></button>
        <button className="icon-btn" aria-label="Bulleted list" title="Bulleted list" onMouseDown={(e) => { e.preventDefault(); cmd("insertUnorderedList"); }}><List size={14} /></button>
        <span className="grow" />
        <span className="ed-hint">Quoted passages you change are marked as edited</span>
        <button className="btn small ghost" onClick={onCancel} disabled={busy}>Cancel</button>
        <button className="btn small primary" onClick={save} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
      </div>
      <div ref={ref} className="prose ed-body" contentEditable suppressContentEditableWarning aria-label="Edit section"
        onKeyDown={(e) => { if (e.key === "Escape") onCancel(); if ((e.metaKey || e.ctrlKey) && e.key === "Enter") save(); }} />
      {err && <p className="err-line">{err}</p>}
    </div>
  );
}
