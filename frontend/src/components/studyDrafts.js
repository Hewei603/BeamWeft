import {bufferDraft, clearDraft, draftStorageKey, flushDraft, previousDraftKey, readDraft} from './projectDraft.js';
import {isBuiltInProvider, previousBuiltInProvider, productName} from '../brand.js';
import {sourceKey} from './studyConnections.js';

const changes=new EventTarget();

// A source handoff can be resumed without reusing inputs that belonged to another
// file, project version or output. Labels and computed payloads are not identities.
export function studyDraftOrigin(request) {
  if (request?.file) return ['file',request.file.id,request.file.content_hash];
  if (request?.reference) return ['output',sourceKey(request.reference)];
  if (request?.revision_id) return ['project',request.revision_id,request.port,request.quantity,
    Object.entries(request.match || {}).sort(([a],[b])=>a.localeCompare(b))];
  return ['manual'];
}

export function studyFormSlot(method, origin) {
  return {method:{name:method.name,provider:method.provider,version:method.version},origin};
}

export function storedStudyForm(record) {
  const object=value=>value!==null && typeof value==='object' && !Array.isArray(value);
  if (!object(record) || !object(record.values) || !object(record.connections) ||
      Object.values(record.values).some(value=>typeof value!=='string') ||
      Object.values(record.connections).some(value=>!object(value)) ||
      !(record.raw===null || typeof record.raw==='string') || typeof record.choice!=='string' ||
      !(record.revision===null || Number.isSafeInteger(record.revision)) || typeof record.token!=='string') return null;
  return record;
}

// Each form is saved separately. Editing a large ray table must not serialize every
// other calculation in this project. Buffer writes until the user pauses or leaves.
export class StudyDraftShelf {
  constructor(server,caseId,scope,onWarning) {
    this.prefix=draftStorageKey(server,caseId,scope,'study-inputs');
    this.pending=new Map();
    this.onWarning=onWarning;
  }

  key(slot, builtInName=productName) {
    if (!isBuiltInProvider(slot?.method?.provider)) return JSON.stringify(slot);
    return JSON.stringify({...slot, method:{...slot.method, provider:builtInName}});
  }
  storageKey(key) { return this.prefix+':'+key; }
  previousKey(slot) { return previousDraftKey(this.storageKey(this.key(slot, previousBuiltInProvider))); }

  read(slot) {
    const key=this.key(slot);
    if (!this.prefix) return this.pending.get(key)?.value ?? null;
    try { return readDraft(this.storageKey(key), this.previousKey(slot)); }
    catch { return null; }
  }

  write(slot,value) {
    const key=this.key(slot);
    clearTimeout(this.pending.get(key)?.timer);
    if (this.prefix) bufferDraft(this.storageKey(key),value);
    const timer=setTimeout(()=>this.flush(key),200);
    this.pending.set(key,{value,timer});
  }

  flush(key) {
    const entry=this.pending.get(key);
    if (!entry) return;
    clearTimeout(entry.timer);
    if (this.prefix) {
      try { flushDraft(this.storageKey(key)); this.pending.delete(key); return; }
      catch { /* Keep the in-memory edit if browser storage is unavailable. */ }
    }
    this.onWarning();
  }

  flushAll() {
    for (const key of this.pending.keys()) this.flush(key);
  }

  list() {
    const entries=this.read(['index']);
    return Array.isArray(entries) ? entries.filter(entry=>typeof entry?.slot?.method?.name==='string' &&
      Array.isArray(entry.slot.origin) && typeof entry.title==='string') : [];
  }

  subscribe(listener) {
    const channel=this.prefix || 'unscoped';
    changes.addEventListener(channel,listener);
    return ()=>changes.removeEventListener(channel,listener);
  }

  updateEntries(entries) {
    this.write(['index'],entries);
    changes.dispatchEvent(new Event(this.prefix || 'unscoped'));
  }

  addEntry(slot,title,source) {
    const entries=this.list(), key=this.key(slot);
    if (entries.some(entry=>this.key(entry.slot)===key)) return false;
    this.updateEntries([...entries,{slot,title,source}]);
    return true;
  }

  remove(slot,token) {
    // A late response for an old submit may arrive after the user edits that
    // method again in a remounted form.
    if (!token || this.read(slot)?.token!==token) return false;
    const key=this.key(slot);
    clearTimeout(this.pending.get(key)?.timer);
    this.pending.delete(key);
    if (this.prefix) {
      try { clearDraft(this.storageKey(key), this.previousKey(slot)); }
      catch { this.onWarning(); }
    }
    this.updateEntries(this.list().filter(entry=>this.key(entry.slot)!==key));
    this.flush(this.key(['index']));
    return true;
  }
}
