import { Fragment } from "react";

const CLAUSES = /\s+(FROM|WHERE|GROUP BY ALL|GROUP BY|ORDER BY|LIMIT|AND)\s+/g;
const KW = /\b(SELECT|FROM|WHERE|GROUP BY ALL|GROUP BY|ORDER BY|LIMIT|AND|AS|DESC|ASC|MEASURE)\b/g;

/** One clause per line, one selected column per line under SELECT. */
export function formatSql(sql: string): string {
  const s = sql.replace(/\s+/g, " ").trim();
  const m = s.match(/^SELECT\s+(.*?)\s+FROM\s+(.*)$/i);
  if (!m) return s.replace(CLAUSES, "\n$1 ");
  const cols: string[] = [];
  let depth = 0, cur = "";
  for (const ch of m[1]) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) { cols.push(cur.trim()); cur = ""; } else cur += ch;
  }
  if (cur.trim()) cols.push(cur.trim());
  const rest = ("FROM " + m[2]).replace(CLAUSES, "\n$1 ");
  return `SELECT\n${cols.map((c) => "  " + c).join(",\n")}\n${rest}`;
}

export function Sql({ text }: { text: string }) {
  const parts = formatSql(text).split(KW);
  return <pre className="sql">{parts.map((p, i) => (i % 2 ? <span key={i} className="kw">{p}</span> : <Fragment key={i}>{p}</Fragment>))}</pre>;
}
