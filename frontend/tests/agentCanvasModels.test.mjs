import test from 'node:test';
import assert from 'node:assert/strict';
import {canvasModels, editableModels, singleCanvasCandidate} from '../src/components/agentCanvasModels.js';

const lens = {revision_id:12, kind:'draft', variant_name:'Lens train'};
const cavity = {revision_id:13, kind:'draft', variant_name:'Cavity'};

test('a calculation against an input model cannot present it as a model that the Agent built', () => {
  const receipt = {status:'completed', model_basis:lens, result_model:lens};
  assert.deepEqual(editableModels(receipt), []);
  assert.deepEqual(canvasModels({runs:[receipt]}), []);
});

test('a persisted optical result is usable after interruption, but running or competing designs need a choice', () => {
  const one = {status:'failed', editable_models:[lens], editable_model:lens};
  assert.equal(singleCanvasCandidate(canvasModels({runs:[one]}), false), 12);
  assert.equal(singleCanvasCandidate(canvasModels({runs:[one]}), true), null);
  const competing = {...one,editable_models:[lens,cavity]};
  assert.deepEqual(editableModels(competing), [lens,cavity]);
  assert.equal(singleCanvasCandidate(canvasModels({runs:[competing]}), false), null);
});

test('the workbench follows an unambiguous saved edit while archived replies preserve their experimental input', () => {
  const run = {status:'completed', editable_models:[lens], canvas_models:[cavity]};
  assert.equal(singleCanvasCandidate(canvasModels({runs:[run]}), false), cavity.revision_id);
  assert.deepEqual(editableModels(run), [lens]);
  const forks = {...run, canvas_models:[lens, cavity]};
  assert.deepEqual(canvasModels({runs:[forks]}), [lens, cavity]);
  assert.equal(singleCanvasCandidate(canvasModels({canvas_run:forks}), false), null);
});

test('the project can contain hand-built alternatives or alternatives from distinct conversations', () => {
  const last = {editable_models:[lens]};
  assert.deepEqual(canvasModels({runs:[last],canvas_models:[]}), []);
  assert.equal(singleCanvasCandidate(canvasModels({runs:[],canvas_models:[cavity]}),false), 13);
  assert.equal(singleCanvasCandidate(canvasModels({runs:[last],canvas_models:[lens,cavity]}),false), null);
});
