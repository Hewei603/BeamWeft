// GFM accepts excess cells, but its HTML conversion silently discards them.
// Inspect parsed table rows so normal text, code fences and valid tables pass through unchanged.
export function preserveTableRows() {
  const processor = this;
  return (tree, file) => {
    const source = String(file);
    function visit(node) {
      if (node.type !== 'table') {
        node.children?.forEach(visit);
        return;
      }
      const header = node.children[0];
      const columns = header.children.length;
      const padded = offset => /[ \t]/.test(source[offset - 1] || '') && /[ \t]/.test(source[offset + 1] || '');
      const paddedHeader = header.children.slice(1).every(cell => padded(cell.position.start.offset));
      for (const row of node.children.slice(1)) {
        if (row.children.length <= columns) continue;
        const start = row.position.start.offset, original = source.slice(start, row.position.end.offset);
        const separators = row.children.slice(1).map(cell => cell.position.start.offset);
        // In ordinary Markdown, a pipe inside an inline code span or a link belongs to its content.
        // GFM's table parser splits first, so recover those spans from the original line.
        const spans = [];
        function findSpans(part) {
          if (part.type === 'inlineCode' || part.type === 'link') spans.push(part.position);
          else part.children?.forEach(findSpans);
        }
        findSpans(processor.parse(original));
        let span = 0;
        const outside = separators.filter(offset => {
          const relative = offset - start;
          while (span < spans.length && spans[span].end.offset <= relative) span++;
          return span === spans.length || relative < spans[span].start.offset;
        });
        // A row that follows the header's spaced delimiters can also contain literal compact pipes.
        // Only accept this interpretation if the number of columns matches exactly.
        const boundaries = outside.length === columns - 1 ? outside : paddedHeader ? outside.filter(padded) : [];
        if (columns > 1 && boundaries.length === columns - 1) {
          const structural = new Set(boundaries);
          let repaired = '', cursor = 0;
          for (const offset of separators) {
            if (structural.has(offset)) continue;
            repaired += original.slice(cursor, offset - start) + '\\|';
            cursor = offset - start + 1;
          }
          repaired += original.slice(cursor);
          const heading = '|' + ' x |'.repeat(columns) + '\n|' + ' --- |'.repeat(columns) + '\n';
          const parsed = processor.parse(heading + repaired).children[0]?.children?.[1];
          if (parsed?.type === 'tableRow' && parsed.children.length === columns) {
            row.children = parsed.children;
            continue;
          }
        }
        // The intended boundaries are ambiguous. Keep the entire source row under the table.
        row.data = {...row.data, hChildren:[{type:'element', tagName:'td', properties:{colSpan:columns},
          children:[{type:'element', tagName:'small', properties:{}, children:[{type:'text',value:'原文（列边界待核对） '}]},
                    {type:'text',value:original}]}]};
      }
    }
    visit(tree);
  };
}
