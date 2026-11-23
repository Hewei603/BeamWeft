import { equalValues } from './projectDraft.js';

// Immutable model snapshots share their unchanged optical objects. A gesture
// publishes live positions, but contributes only its starting model to undo.
export class ProjectHistory {
  constructor(limit = 40) {
    this.limit = limit;
    this.listeners = new Set();
    this.reset(null);
  }

  subscribe = listener => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  getSnapshot = () => this.snapshot;

  publish() {
    const active = this.gesture && this.project !== this.gesture.base;
    this.snapshot = {
      project: this.project, dirty: this.project !== this.base,
      canUndo: Boolean(active || this.past.length), canRedo: !active && Boolean(this.future.length),
      undoLabel: (active ? this.gesture : this.past.at(-1))?.label,
      redoLabel: this.future.at(-1)?.label,
    };
    for (const listener of this.listeners) listener();
  }

  reset(project) {
    this.gesture = null;
    this.past = [];
    this.future = [];
    this.project = this.base = project;
    this.publish();
  }

  remember(project, label) {
    this.past.push({project, label});
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
  }

  saved(project) {
    this.finishGesture();
    this.project = this.base = project;
    this.publish();
  }

  atBaseline(project) {
    return equalValues(project, this.base) ? this.base : project;
  }

  edit(project, label = '') {
    this.finishGesture();
    if (equalValues(project, this.project)) return false;
    this.remember(this.project, label);
    this.project = this.atBaseline(project);
    this.publish();
    return true;
  }

  begin(base, label = '') {
    if (base !== this.project) return null;
    this.finishGesture();
    this.gesture = {base, label};
    return this.gesture;
  }

  move(gesture, project) {
    if (!gesture || this.gesture !== gesture) return false;
    if (!equalValues(project, this.project)) {
      this.project = equalValues(project, gesture.base) ? gesture.base : this.atBaseline(project);
      this.publish();
    }
    return true;
  }

  finishGesture(gesture = this.gesture) {
    if (!gesture || this.gesture !== gesture) return;
    this.gesture = null;
    if (this.project !== gesture.base) this.remember(gesture.base, gesture.label);
    this.publish();
  }

  cancelGesture(gesture) {
    if (!gesture || this.gesture !== gesture) return false;
    this.gesture = null;
    this.project = gesture.base;
    this.publish();
    return true;
  }

  step(direction) {
    this.finishGesture();
    const undo = direction === 'undo';
    const source = undo ? this.past : this.future;
    const target = undo ? this.future : this.past;
    const entry = source.pop();
    if (!entry) return false;
    target.push({project: this.project, label: entry.label});
    this.project = this.atBaseline(entry.project);
    this.publish();
    return true;
  }
}
