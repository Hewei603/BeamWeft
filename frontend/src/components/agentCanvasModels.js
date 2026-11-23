// Only models actually written by a run can be handed off as its output.
export function editableModels(receipt) {
  if (Array.isArray(receipt?.editable_models)) return receipt.editable_models;
  return receipt?.editable_model ? [receipt.editable_model] : [];
}

// The workbench follows later saved edits. Conversation links retain the
// exact revision recorded when the Agent originally handed the result over.
export function canvasModels(workbench) {
  if (Array.isArray(workbench?.canvas_models)) return workbench.canvas_models;
  const run = workbench?.runs?.find(item => editableModels(item).length > 0) || workbench?.canvas_run;
  return Array.isArray(run?.canvas_models) ? run.canvas_models : editableModels(run);
}

export function singleCanvasCandidate(models, pending) {
  // Several candidates may be saved before the run finishes. Until then the
  // first saved model is available to open, but cannot be the default choice.
  return !pending && models.length === 1 ? models[0].revision_id : null;
}
