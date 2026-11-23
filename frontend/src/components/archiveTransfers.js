import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';
import { draftStorageKey } from './projectDraft.js';

const sessions = new Map();
const storageWarning = '此标签页无法保存上传凭据。刷新前请完成归档或保留原文件。';

async function fingerprint(file) {
  const hash = sha256.create();
  const reader = file.stream().getReader();
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      hash.update(value);
    }
    return bytesToHex(hash.digest());
  } finally {
    reader.releaseLock();
  }
}

// Only filenames and receipts are written to session storage. The File objects
// are retained in this tab's memory while their uploads are unconfirmed.
export class ArchiveTransfers {
  pending = new Set();
  entries = new Map();
  listeners = new Set();
  receipts = new Set();
  snapshot = {missing:[], active:[], warning:''};
  warning = '';

  constructor(key, storage) {
    this.prefix = key + '/';
    try {
      this.storage = storage || window.sessionStorage;
      for (let n=0; n<this.storage.length; n++) {
        const name = this.storage.key(n);
        if (!name.startsWith(this.prefix)) continue;
        const data = JSON.parse(this.storage.getItem(name));
        this.entries.set(data.key,{...data, pending:false});
      }
    } catch { this.warning = storageWarning; }
    this.publish();
  }

  publish() {
    const entries = [...this.entries.values()];
    this.snapshot = {missing:entries.filter(entry=>!entry.pending),active:entries.filter(entry=>entry.pending),warning:this.warning};
    this.listeners.forEach(fn=>fn());
  }
  getSnapshot = () => this.snapshot;
  subscribe = listener => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  onArchived = listener => { this.receipts.add(listener); return () => this.receipts.delete(listener); };
  persist(key) {
    const entry = this.entries.get(key);
    try {
      if (entry) {
        const {key, label, name, size, hash, message, composer} = entry;
        this.storage.setItem(this.prefix+key, JSON.stringify({key,label,name,size,hash,message,composer}));
      } else this.storage.removeItem(this.prefix+key);
    } catch { this.warning = storageWarning; }
  }
  remove(key) {
    this.entries.delete(key);
    this.persist(key);
  }
  discard(key) {
    if (this.entries.get(key)?.pending) return;
    this.remove(key);
    this.publish();
  }
  reconcile(files) {
    if (!this.entries.size) return;
    // A request can commit after its tab loses the HTTP response. Only the
    // server's receipt for the very same upload may remove a missing item.
    let changed = false;
    for (const file of files) {
      const entry = this.entries.get(file.upload_id);
      if (!entry || entry.pending || entry.label !== (file.relative_path || file.filename) || entry.hash !== file.content_hash) continue;
      this.remove(entry.key);
      this.receipts.forEach(fn=>fn(file, entry.composer));
      changed = true;
    }
    if (changed) this.publish();
  }
  async upload(requests, send, progress) {
    const batch = [];
    for (const request of requests) {
      const {file,key} = request;
      const old = this.entries.get(key);
      if (!file || old?.pending || (old && !old.file && !old.hash)) continue;
      const entry = {...(old || {key,name:file.name, label:file.webkitRelativePath || file.name, size:file.size, composer:request.composer}),
        file, pending:true, reselected:request.reselected};
      this.entries.set(key,entry);
      this.persist(key);
      batch.push(entry);
    }
    let finish;
    const done = new Promise(resolve=>{finish=resolve;});
    this.pending.add(done);
    this.publish();
    let saved = 0;
    try {
      for (const [index,entry] of batch.entries()) {
        progress?.(index+1,batch.length,entry.label);
        let verified = false;
        try {
          if (entry.size !== entry.file.size || (!entry.hash && entry.name !== entry.file.name))
            throw Error('所选文件与上次选择的原件不符。请重新选择。');
          const hash = await fingerprint(entry.file);
          if (entry.hash && entry.hash !== hash) throw Error('所选文件内容与上次选择的原件不同。请重新选择，或放弃此项后单独归档新文件。');
          entry.hash = hash;
          verified = true;
          this.persist(entry.key);
          const file = await send(entry);
          if (file.upload_id !== entry.key || file.content_hash !== hash || (file.relative_path || file.filename) !== entry.label)
            throw Error('服务端回执与此次上传的原件不一致。请核对归档记录后重试。');
          this.remove(entry.key);
          this.receipts.forEach(fn=>fn(file,entry.composer));
          saved++;
        } catch (error) {
          entry.pending = false;
          // A reselected replacement is a temporary candidate until its bytes
          // match. A mismatch must not become the file attached to "retry".
          if (entry.reselected && !verified) entry.file = null;
          entry.message = error.message;
          this.persist(entry.key);
        }
        this.publish();
      }
      return {saved,total:batch.length};
    } finally {
      this.pending.delete(done);
      finish();
    }
  }
  async wait() {
    while (this.pending.size) await Promise.all(this.pending);
    return this.snapshot.missing.map(({key,label})=>({key,label}));
  }
}

export function transfersFor(server,caseId,scope) {
  const key = draftStorageKey(server,caseId,scope,'uploads');
  if (!key) return null;
  if (!sessions.has(key)) {
    const transfers = new ArchiveTransfers(key);
    transfers.scope = scope;
    sessions.set(key,transfers);
  }
  return sessions.get(key);
}
