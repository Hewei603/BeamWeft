import React, { useId, useState } from 'react';
import { equalValues } from './projectDraft';
import {modelFields as captions} from './modelFields';

const display = v => v === undefined ? '未记录' : v === null ? '空值（null）' : typeof v === 'object' ? JSON.stringify(v) : String(v);
function changes(a, b, path = []) {
  if (equalValues(a, b)) return [];
  if (a != null && b != null && typeof a === 'object' && typeof b === 'object' && Array.isArray(a) === Array.isArray(b)) {
    return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap(k =>
      changes(a[k], b[k], [...path, Array.isArray(b) ? String(Number(k) + 1) : captions[k] || k]));
  }
  return [(path.length ? path.join(' / ') + '　' : '') + display(a) + ' → ' + display(b)];
}

function Changes({ before, after }) {
  const lines = changes(before, after);
  return <div className="draft-differences">{lines.slice(0, 5).map((line, i) => <p key={i}>{line}</p>)}
    {lines.length > 5 && <details><summary>其余 {lines.length - 5} 项</summary>{lines.slice(5).map((line, i) => <p key={i}>{line}</p>)}</details>}</div>;
}

export default function DraftReview({ plan, latestId, disabled, onApply, onDownload,
  localLabel = '我的草稿', explanation = '你的草稿仍在。下面列出双方相对草稿起点的改动。核对后可以回到光路图预览，再决定是否保存。',
  actionLabel = '用核对结果继续编辑' }) {
  const [choices, setChoices] = useState({});
  const formId = useId();
  const clashes = plan.groups.filter(g => g.kind === 'conflict');
  const currentChoices = Object.fromEntries(clashes.filter(g => choices[g.key]?.group === g)
    .map(g => [g.key, choices[g.key].side]));
  const ready = clashes.every(g => currentChoices[g.key]);
  return <section className="draft-review" aria-label="核对两边的改动">
    <h3>工程已有新版本 #{latestId}</h3>
    <p>{explanation}</p>
    {plan.groups.map(g => <article key={g.key}><strong>{g.label}</strong>
      {g.kind === 'conflict' ? <><p>两边都改了这一组参数，请选择要保留的一边。</p>
        <label><input type="radio" name={formId + g.key} checked={currentChoices[g.key] === 'local'}
          onChange={() => setChoices({...choices, [g.key]:{group:g, side:'local'}})} />采用{localLabel}</label><Changes before={g.before} after={g.local} />
        <label><input type="radio" name={formId + g.key} checked={currentChoices[g.key] === 'remote'}
          onChange={() => setChoices({...choices, [g.key]:{group:g, side:'remote'}})} />采用版本 #{latestId}</label><Changes before={g.before} after={g.incoming} /></>
        : <><small>{g.kind === 'same' ? '两边改动一致' : g.kind === 'local' ? '来自' + localLabel : '来自已存工程'}</small>
          <Changes before={g.before} after={g.kind === 'remote' ? g.incoming : g.local} /></>}
    </article>)}
    <div className="bench-actions"><button className="btn btn-primary" disabled={!ready || disabled} onClick={() => onApply(plan.build(currentChoices))}>{actionLabel}</button>
      {onDownload && <button className="btn btn-secondary" onClick={onDownload}>导出我的草稿</button>}</div>
  </section>;
}
