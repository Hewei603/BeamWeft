import { useEffect, useMemo, useRef, useState } from 'react';
import { workbenchApi } from '../api/workbench';
import { createPreviewQueue } from './previewQueue';

export function useBenchPreview(caseId, system, needed, compute = workbenchApi.previewProject, revision = null, history) {
  const queue = useRef(null);
  const [receipt, setReceipt] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const model = history?.model;
  const contextVersion = history?.contextVersion;
  const context = useMemo(() => ({caseId, revision, compute, contextVersion}), [caseId, revision, compute, contextVersion]);
  const scope = useMemo(() => ({context, needed}), [context, needed]);
  const known = needed && model && receipt?.recent?.find(entry =>
    entry.context === context && entry.result.model === model && entry.result.attempt === attempt);

  useEffect(() => {
    if (!scope.needed) return;
    const previews = createPreviewQueue(
      (input, signal) => context.compute(context.caseId, input, signal, context.revision),
      result => setReceipt(last => {
        let recent = last?.recent;
        if (result.model && result.data != null) {
          // Keep only successful results from this version, tied to the exact immutable model.
          const others = (recent || []).filter(entry => entry.context === context && entry.result.model !== result.model);
          recent = [{context, result}, ...others.slice(0, 3)];
        }
        return {scope, result, recent, success: result.data != null ? result : last?.scope === scope ? last.success : null};
      }),
    );
    queue.current = previews;
    return () => {
      previews.cancel();
      queue.current = null;
    };
  }, [scope]);
  useEffect(() => {
    if (known) queue.current?.cancel();
    else queue.current?.submit({system, attempt, model});
  }, [scope, system, attempt, model, known]);

  const latest = receipt?.scope === scope ? receipt : null;
  const current = latest && latest.result.system === system && latest.result.attempt === attempt ? latest.result : null;
  const previous = !current ? latest?.success : null;
  return { data: known?.result?.data ?? current?.data, error: known ? null : current?.error,
    previous: known ? null : previous, retry: () => setAttempt(n => n + 1) };
}
