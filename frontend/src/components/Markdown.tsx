"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

// Headings (# ## ###), bullet/ordered lists, **bold**, *italic*, `code`.
// Renders via React elements only — raw HTML is never interpreted, so the
// input can come from user-supplied document bodies without XSS concerns.

type Block =
  | { kind: "heading"; level: 1 | 2 | 3; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "p"; text: string };

export default function Markdown({ text }: { text: string | null }) {
  const t = useTranslations("uiCommon");
  if (!text || !text.trim()) {
    return <p className="meta">{t("noContent")}</p>;
  }
  const blocks = parseBlocks(text);
  return <div style={{ lineHeight: 1.55 }}>{blocks.map((b, i) => renderBlock(b, i))}</div>;
}

function parseBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length as 1 | 2 | 3;
      blocks.push({ kind: "heading", level, text: h[2] });
      i++;
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      blocks.push({ kind: "ul", items });
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ kind: "ol", items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3})\s+/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push({ kind: "p", text: para.join(" ") });
  }
  return blocks;
}

function renderBlock(b: Block, key: number): ReactNode {
  switch (b.kind) {
    case "heading": {
      const style = {
        fontSize: b.level === 1 ? "1.4rem" : b.level === 2 ? "1.2rem" : "1.05rem",
        fontWeight: 600,
        margin: "0.8rem 0 0.4rem",
      };
      return (
        <div key={key} style={style}>
          {renderInline(b.text)}
        </div>
      );
    }
    case "ul":
      return (
        <ul key={key} style={{ paddingLeft: "1.2rem", margin: "0.3rem 0" }}>
          {b.items.map((it, i) => (
            <li key={`${i}-${it.slice(0, 8)}`}>{renderInline(it)}</li>
          ))}
        </ul>
      );
    case "ol":
      return (
        <ol key={key} style={{ paddingLeft: "1.2rem", margin: "0.3rem 0" }}>
          {b.items.map((it, i) => (
            <li key={`${i}-${it.slice(0, 8)}`}>{renderInline(it)}</li>
          ))}
        </ol>
      );
    case "p":
      return (
        <p key={key} style={{ margin: "0.4rem 0" }}>
          {renderInline(b.text)}
        </p>
      );
  }
}

function renderInline(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*([^*]+)\*\*)|(\*([^*]+)\*)|(`([^`]+)`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  m = re.exec(src);
  while (m !== null) {
    if (m.index > last) out.push(src.slice(last, m.index));
    if (m[1]) out.push(<strong key={key++}>{m[2]}</strong>);
    else if (m[3]) out.push(<em key={key++}>{m[4]}</em>);
    else if (m[5])
      out.push(
        <code key={key++} style={{ fontSize: "0.9em", background: "#f3f4f6", padding: "0 3px" }}>
          {m[6]}
        </code>,
      );
    last = m.index + m[0].length;
    m = re.exec(src);
  }
  if (last < src.length) out.push(src.slice(last));
  return out;
}
