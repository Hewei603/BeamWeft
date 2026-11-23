import { useCallback, useEffect, useMemo, useState } from 'react';

// Each view refreshes its own records. A late response from a closed view
// must never replace the data or the error shown in the next view.
export function useCaseRead(caseId, read, enabled = true, kind = null) {
  const [record, setRecord] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const refresh = useCallback(() => setAttempt(value => value + 1), []);
  const request = useMemo(() => ({ caseId, read, enabled, kind, attempt }), [caseId, read, enabled, kind, attempt]);

  useEffect(() => {
    if (!request.enabled) return;
    let current = true;
    request.read(request.caseId, request.kind).then(data => {
      if (current) setRecord({ request, data, error: null });
    }).catch(cause => {
      if (current) setRecord(previous => ({
        request, error: cause.message,
        data: previous?.request.caseId === request.caseId && previous.request.kind === request.kind ? previous.data : null,
      }));
    });
    return () => { current = false; };
  }, [request]);

  const matches = record?.request.caseId === caseId && record.request.kind === kind;
  return {
    data: matches ? record.data : null,
    pending: enabled && record?.request !== request,
    error: record?.request === request ? record.error : null,
    refresh,
  };
}
