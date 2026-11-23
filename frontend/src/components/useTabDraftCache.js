import { useEffect, useRef, useState } from 'react';
import { API_BASE_URL } from '../api/client';
import { bufferDraft, clearDraft, draftStorageKey, flushDraft, readDraft } from './projectDraft';
import { recoverPositionEntries } from './positionIntent';

export function useTabDraftCache(caseId, kind) {
  const key = useRef(null), timer = useRef(null), token = useRef(null);
  const [warning, setWarning] = useState('');
  const problem = '浏览器未能持久保存这份草稿。请在离开标签页前保留副本。';
  const flush = () => {
    clearTimeout(timer.current);
    try { flushDraft(key.current); } catch { setWarning(problem); }
  };
  useEffect(() => {
    const onVisibility = () => { if (document.visibilityState === 'hidden') flush(); };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('pagehide', flush);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('pagehide', flush);
      flush();
    };
  }, []);
  const read = scope => {
    key.current = draftStorageKey(API_BASE_URL, caseId, scope, kind);
    if (!key.current) return null;
    try {
      const record = readDraft(key.current);
      if (record && (kind === 'optical' ?
        record.selectionOnly ? !Number.isInteger(record.source) :
          !record.base?.system?.elements || !record.project?.system?.elements || (record.raw != null && typeof record.raw !== 'string')
        : typeof record.message !== 'string' || !Array.isArray(record.selected) || !record.refs)) throw new Error('草稿格式不符');
      token.current = record?.token || null;
      if (kind === 'optical' && record && 'positionEntries' in record) {
        const positionEntries = recoverPositionEntries(record.positionEntries);
        if (positionEntries !== record.positionEntries) {
          setWarning('部分待应用的位置输入无法恢复。已保留其余位置输入和工程草稿，请核对元件位置。');
          return {...record, positionEntries};
        }
      }
      return record;
    } catch { setWarning(problem); return null; }
  };
  const store = record => {
    if (!key.current) { setWarning(problem); return; }
    const marker = String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    token.current = marker;
    bufferDraft(key.current, {...record, token: marker});
    clearTimeout(timer.current);
    timer.current = setTimeout(flush, 200);
  };
  const clear = (expected = token.current) => {
    clearTimeout(timer.current);
    if (!key.current) return;
    try {
      // A slow save can finish after the author returns to the case and edits again.
      const cached = readDraft(key.current);
      if (cached && cached.token !== expected) return;
      clearDraft(key.current);
      token.current = null;
    } catch {
      if (expected === token.current) {
        try { clearDraft(key.current); } catch { /* The in-memory deletion still applies. */ }
      }
      setWarning(problem);
    }
  };
  return { read, store, clear, token, warning };
}
