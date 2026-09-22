import type { ReactNode } from "react";

function safeText(value: string): string {
  // Markdown is intentionally rendered as text-only React nodes.  Removing
  // tags also keeps copied HTML from becoming a visual/semantic island in the
  // document reader; no raw HTML is ever passed to the DOM.
  return value.replace(/<\/?[a-z][^>]*>/gi, "");
}

function inline(value: string): ReactNode[] {
  const parts = safeText(value).split(/(`[^`]+`)/g);
  return parts.map((part, index) =>
    part.startsWith("`") && part.endsWith("`")
      ? <code key={index}>{part.slice(1, -1)}</code>
      : <span key={index}>{part}</span>,
  );
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isTableDivider(line: string): boolean {
  return tableCells(line).length > 0 && tableCells(line).every((cell) => /^:?-{3,}:?$/.test(cell));
}

export function MarkdownPreview({ content, className = "" }: { content: string; className?: string }) {
  const lines = content.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim().startsWith("```")) {
      const language = line.trim().slice(3).trim();
      index += 1;
      const code: string[] = [];
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        code.push(safeText(lines[index]));
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(<pre className="markdown-code" key={`code-${index}`}><code data-language={language || undefined}>{code.join("\n")}</code></pre>);
      continue;
    }
    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const header = tableCells(line);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      blocks.push(
        <div className="markdown-table-wrap" key={`table-${index}`}>
          <table><thead><tr>{header.map((cell, cellIndex) => <th key={cellIndex}>{inline(cell)}</th>)}</tr></thead>
            <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{header.map((_cell, cellIndex) => <td key={cellIndex}>{inline(row[cellIndex] ?? "")}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line.trim());
    if (heading) {
      const level = Math.min(4, heading[1].length + 1);
      const Heading = `h${level}` as "h2" | "h3" | "h4";
      blocks.push(<Heading key={`heading-${index}`}>{inline(heading[2])}</Heading>);
      index += 1;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(<blockquote key={`quote-${index}`}>{quote.map((value, quoteIndex) => <p key={quoteIndex}>{inline(value)}</p>)}</blockquote>);
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ""));
        index += 1;
      }
      blocks.push(<ul key={`ul-${index}`}>{items.map((value, itemIndex) => <li key={itemIndex}>{inline(value)}</li>)}</ul>);
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ""));
        index += 1;
      }
      blocks.push(<ol key={`ol-${index}`}>{items.map((value, itemIndex) => <li key={itemIndex}>{inline(value)}</li>)}</ol>);
      continue;
    }
    if (!line.trim()) {
      blocks.push(<div className="markdown-gap" key={`gap-${index}`} aria-hidden="true" />);
      index += 1;
      continue;
    }
    blocks.push(<p key={`p-${index}`}>{inline(line)}</p>);
    index += 1;
  }
  return <article className={`markdown-document ${className}`.trim()}>{blocks}</article>;
}
