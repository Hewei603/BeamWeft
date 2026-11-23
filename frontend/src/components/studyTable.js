// A wildcard in a column addresses records inside one parent row. Only columns
// from the same child collection share a row index.
export function tableRows(rows, columns, limit) {
  const fields = Object.keys(columns);
  const paths = fields.map(field => field.split('.'));
  const groups = new Set(paths.filter(path => path.includes('*')).map(path => path.slice(0, path.indexOf('*')).join('.')));
  const group = groups.size === 1 ? [...groups][0] : null;
  const output = [];
  for (const parent of rows) {
    const children = group === null ? null : group === '' ? parent : propertyValue(parent, group);
    const count = Array.isArray(children) ? Math.max(1, children.length) : 1;
    for (let i = 0; i < count; i++) {
      if (output.length === limit) return {rows: output, more: true};
      output.push(paths.map(path => {
        const star = path.indexOf('*');
        if (group !== null && star >= 0) return descend(children?.[i], path, star + 1);
        return descend(parent, path, 0);
      }));
    }
  }
  return {rows: output, more: false};
}

function descend(value, path, index) {
  if (index === path.length || value == null) return value;
  if (path[index] === '*') return Array.isArray(value) ? value.map(item => descend(item, path, index + 1)) : undefined;
  return descend(value[path[index]], path, index + 1);
}

export function propertyValue(value, path) {
  return descend(value, path.split('.'), 0);
}

// A table may explicitly identify which input sample a result came from.
// Receipts retain original row numbers after selection and paired-row filtering.
export function sourceRowGroups(definition, evidence = []) {
  const groups = [];
  for (const receipt of evidence || []) {
    if (!receipt?.source?.path || !Array.isArray(receipt.row_numbers) || !receipt.row_numbers.length) continue;
    const byIndex = new Map();
    for (const field of receipt.fields || []) {
      const indexPath = definition.table_input_rows?.[field];
      if (!indexPath) continue;
      const labels = byIndex.get(indexPath) || [];
      labels.push(definition.inputs?.properties?.[field]?.title || definition.input_ports?.[field]?.label || field);
      byIndex.set(indexPath, labels);
    }
    const source = [receipt.source.path, receipt.selection?.sheet].filter(Boolean).join(' · ');
    for (const [indexPath, labels] of byIndex) groups.push({indexPath, labels, source, rowNumbers:receipt.row_numbers});
  }
  return groups;
}

export function sourceRowLabel(group, inputIndex, qualify) {
  const row = Number.isSafeInteger(inputIndex) && inputIndex > 0 ? group.rowNumbers[inputIndex - 1] : null;
  const source = qualify ? group.labels.join('、') + ' · ' + group.source : group.source;
  return source + ' · ' + (Number.isSafeInteger(row) && row > 0 ? '第 ' + row + ' 行' : '无对应行号');
}

export function curveTraces(series, output) {
  return series.map(([name,xPath,yPath,mode])=>({
    name,type:'scatter',mode,x:propertyValue(output,xPath),y:propertyValue(output,yPath)
  })).filter(t=>Array.isArray(t.x) && Array.isArray(t.y) && t.x.length && t.x.length===t.y.length);
}
