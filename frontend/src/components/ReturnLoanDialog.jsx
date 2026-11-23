import React, { useEffect, useRef, useState } from 'react';

export default function ReturnLoanDialog({ loan, onReturn, onClose }) {
  const dialog = useRef(null);
  const [condition, setCondition] = useState('unchanged'), [note, setNote] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => { dialog.current.showModal(); }, []);
  const submit = async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError('');
    try { await onReturn(loan, { note: note.trim() || undefined, condition_on_return: condition === 'unchanged' ? undefined : condition }); }
    catch (cause) { setError(cause.message); setBusy(false); }
  };
  return <dialog ref={dialog} className="inventory-return-dialog" aria-labelledby="return-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <form onSubmit={submit}>
      <h2 id="return-title">确认归还</h2><p>{loan.borrower_name} · 借用记录 #{loan.id} · 数量 {loan.quantity}</p>
      <label>归还检查<select value={condition} disabled={busy} onChange={event => setCondition(event.target.value)}>
        <option value="unchanged">保持现有器件状态</option><option value="damaged">发现损坏，标记损坏</option><option value="uncertain">状态待检查</option>
      </select></label><p className="muted">归还记录会保留。解除已有损坏标记需要 reviewer 或 admin 核验。</p>
      <label>归还备注<textarea value={note} maxLength={2000} disabled={busy} onChange={event => setNote(event.target.value)} /></label>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="inventory-return-actions"><button type="button" className="btn btn-secondary" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="btn btn-primary" disabled={busy}>{busy ? '正在归还…' : '确认归还'}</button></div>
    </form>
  </dialog>;
}
