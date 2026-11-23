import { useEffect, useState } from 'react';
import { workbenchApi } from '../api/workbench';

export function useBeamReadings(caseId, revision, refreshToken) {
  const [receipt, setReceipt] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const key = caseId + ':' + revision;
  useEffect(() => {
    if (!revision) return;
    let active = true;
    workbenchApi.readBeamReadings(caseId, revision).then(items => {
      if (active) setReceipt({key, items});
    }).catch(cause => {
      if (active) setReceipt(previous => ({key, items: previous?.key === key ? previous.items : [], error: cause.message}));
    });
    return () => { active = false; };
  }, [caseId, revision, key, refreshToken, attempt]);
  const refresh = () => setAttempt(n => n + 1);
  const record = item => {
    setReceipt(previous => {
      const items = previous?.key === key ? previous.items : [];
      return {key, items: [...items.filter(r => r.id !== item.id), item]};
    });
    refresh();
  };
  return {items: receipt?.key === key ? receipt.items : [], error: receipt?.key === key ? receipt.error : '', refresh, record};
}
