import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { casesApi } from '../api/cases';
import { localId } from '../api/localId';
import MeasurementView from './MeasurementView';
import { useLanguage } from '../LanguageContext';

export function FileUpload(props) {
  return props.transfers ? <UploadQueue {...props} /> : <p role="status">正在读取工程归档身份…</p>;
}

function UploadQueue({caseId, onBusyChange, transfers, disabled = false, composer = false}) {
  const input = useRef(null), folder = useRef(null), replaceInput = useRef(null), replacing = useRef(null);
  const [notice,setNotice] = useState('');
  const snapshot = useSyncExternalStore(transfers.subscribe,transfers.getSnapshot);
  const working = snapshot.active.some(entry=>Boolean(entry.composer)===composer);
  const missing = snapshot.missing.filter(entry=>!composer || entry.composer);
  const locked = working || disabled;
  useEffect(()=>{onBusyChange?.(working);},[onBusyChange,working]);
  async function upload(entries) {
    const result = await transfers.upload(entries,entry=>casesApi.uploadAttachment(caseId,entry.file,entry.key,entry.label,transfers.scope),
      (index,total,label)=>setNotice(`正在归档 ${index}/${total} · ${label}`));
    setNotice(`已确认归档 ${result.saved}/${result.total} 份${result.saved<result.total?'，可单独重试未确认文件。':'。'}`);
  }
  function choose(event) {
    const entries = Array.from(event.target.files,file=>({file,key:localId(),composer}));
    event.target.value='';
    if (entries.length) upload(entries);
  }
  function reselect(event) {
    const item = replacing.current, file = event.target.files[0];
    event.target.value='';
    if (item && file) upload([{key:item.key,file,reselected:true}]);
  }
  return <div className="case-file-upload">
    <div className="action-row">
      <input ref={input} type="file" multiple hidden onChange={choose} />
      <button type="button" className="btn btn-secondary" disabled={locked} onClick={() => input.current.click()}>
        {composer ? '＋ 资料 / 数据 / 图片' : '选择文件'}
      </button>
      {!composer && <><input ref={folder} type="file" webkitdirectory="" multiple hidden onChange={choose} />
        <button type="button" className="btn btn-secondary" disabled={locked} onClick={() => folder.current.click()}>选择文件夹</button></>}
    </div>
    {notice && <div role="status" className="field-note">{notice}</div>}
    {snapshot.warning && <div role="alert">{snapshot.warning}</div>}
    {!!missing.length && <div role="alert">
      <input ref={replaceInput} type="file" aria-label="重新选择待归档的原件" hidden onChange={reselect}/>
      <ul>{missing.map(entry=><li key={entry.key}><strong>{entry.label}</strong> · {entry.message || '尚未收到归档回执。'}
        {!entry.file && <p>{entry.hash ? '重新选择文件后将校验原始内容，并保留原目录。'
          : '上次在读完内容指纹前中断。请放弃此项，再单独选择并归档文件。'}（{entry.size} 字节）</p>}
        <div className="action-row">{!entry.file && entry.hash && <button type="button" className="btn btn-secondary" disabled={locked} onClick={()=>{
          replacing.current=entry; replaceInput.current.click();
        }}>重新选择文件</button>}
        <button type="button" className="bench-text-btn" disabled={locked} onClick={()=>{transfers.discard(entry.key); setNotice('已放弃这项未确认上传。');}}>放弃这项上传</button></div>
      </li>)}</ul>
      {missing.some(entry=>entry.file) && <button type="button" className="btn btn-secondary" disabled={locked}
        onClick={()=>upload(missing.filter(entry=>entry.file))}>重试未确认文件</button>}
    </div>}
  </div>;
}

export const fileModeLabels = { text: '文本预览', workbook: '工作表预览', image: '图像预览', archive: '归档原件' };

export function ArchiveList({files, children, initialId, label}) {
  const { t } = useLanguage();
  const [query,setQuery] = useState('');
  const [page,setPage] = useState(null);
  const size = 24;
  const ordered = useMemo(()=>[...files].sort((a,b)=>b.id-a.id),[files]);
  const matches = useMemo(()=>{
    const words = query.trim().replaceAll('\\','/').toLowerCase().split(/\s+/).filter(Boolean);
    return words.length ? ordered.filter(file=>{
      const path=(file.relative_path || file.filename).toLowerCase();
      return words.every(word=>path.includes(word));
    }) : ordered;
  },[ordered,query]);
  const pages = Math.max(1,Math.ceil(matches.length/size));
  const initialPage = initialId ? Math.floor(Math.max(0,matches.findIndex(file=>file.id===initialId))/size) : 0;
  const current = Math.min(page ?? initialPage,pages-1);
  return <section className="archive-list" aria-label={label}>
    {(files.length>1 || query) && <div className="archive-search"><label>{t('caseDetail.findFile')}
      <input type="search" value={query} placeholder={t('caseDetail.findFileHint')} onChange={event=>{
        setQuery(event.target.value);setPage(0);
      }} /></label><small aria-live="polite">{matches.length} / {files.length}</small></div>}
    {query && matches.length===0 && <p>{t('caseDetail.noMatchingFile')}</p>}
    {matches.slice(current*size,(current+1)*size).map(children)}
    {pages>1 && <div className="action-row archive-pages">
      <button type="button" className="btn btn-secondary" disabled={!current} onClick={()=>setPage(current-1)}>{t('caseDetail.prevFiles')}</button>
      <small aria-live="polite">{current+1} / {pages}</small>
      <button type="button" className="btn btn-secondary" disabled={current+1===pages} onClick={()=>setPage(current+1)}>{t('caseDetail.nextFiles')}</button>
    </div>}
  </section>;
}

export function FilePreview({ file, caseId, onQuote, selections, onUseSelection }) {
  const backupInput = useRef(null);
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState('');
  const [restored, setRestored] = useState(false);
  const [pages, setPages] = useState([0]);
  const [response, setResponse] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [forceTable, setForceTable] = useState(()=>Boolean(selections?.get(file.id)));
  const offset = pages[pages.length - 1];
  const result = response?.offset === offset && response.attempt === attempt ? response.data : null;
  const restore = async event => {
    const backup = event.target.files[0];
    event.target.value = '';
    if (!backup || restoring) return;
    setRestoring(true); setRestoreError(''); setRestored(false);
    try {
      await casesApi.restoreOriginal(file.id, backup);
      setRestored(true); setAttempt(n => n + 1);
    } catch (error) { setRestoreError(error.message); }
    finally { setRestoring(false); }
  };
  useEffect(() => {
    let live = true;
    const receive = data => { if (live) setResponse(previous=>({offset,attempt,data,tableAvailable:data.table_candidate || previous?.tableAvailable})); };
    casesApi.previewAttachment(file.id, offset)
      .then(receive)
      .catch(cause => receive({ status: 'failed', message: cause.message }));
    return () => { live = false; };
  }, [file.id, offset, attempt]);
  return <section className="case-file-preview" aria-label="原件预览">
    {caseId && !result?.failure_code && (response?.tableAvailable || forceTable) && <MeasurementView key={file.id} caseId={caseId} file={file} onQuote={onQuote} selections={selections} onUseSelection={onUseSelection}/>}
    {result ? <>
      {result.message && <p role={result.status === 'failed' ? 'alert' : undefined}>{result.message}</p>}
      {(result.content_hash || result.expected_sha256) && <details className="original-digest">
        <summary>核对原件指纹 · SHA-256</summary>
        {result.content_hash ? <p>{result.integrity === 'verified' ? '与归档时一致' : '本次读取所得，旧记录缺少归档指纹'} <code>{result.content_hash}</code></p>
          : <><p>归档时 <code>{result.expected_sha256}</code></p>{result.observed_sha256 && <p>本次读取 <code>{result.observed_sha256}</code></p>}</>}
      </details>}
      {result.failure_code && result.expected_sha256 && <div>
        <input ref={backupInput} type="file" hidden aria-label="选择原件备份" onChange={restore} />
        <button type="button" className="btn btn-secondary" disabled={restoring} onClick={()=>backupInput.current.click()}>
          {restoring ? '正在核对备份…' : '从备份恢复原件'}
        </button>
        {restoreError && <p role="alert">{restoreError}</p>}
      </div>}
      {restored && result.integrity === 'verified' && <p role="status">已恢复原件 #{file.id}，历史引用仍指向这份文件。</p>}
      {result.image_url && <img src={result.image_url} alt={file.filename} />}
      {result.text !== undefined && <pre>{result.text}</pre>}
      <div className="action-row">
        {caseId && !result.failure_code && !response.tableAvailable && !forceTable && (result.read_mode==='archive' || (result.read_mode==='text' && !/\.(pdf|md|json)$/i.test(file.filename))) &&
          <button type="button" className="btn btn-secondary" onClick={()=>setForceTable(true)}>按分隔文本读取</button>}
        {pages.length > 1 && <button type="button" className="btn btn-secondary" onClick={() => setPages(p => p.slice(0,-1))}>上一页</button>}
        {result.next_offset != null && <button type="button" className="btn btn-secondary" onClick={() => setPages(p => [...p,result.next_offset])}>下一页</button>}
        {result.status === 'failed' && <button type="button" className="btn btn-secondary" onClick={() => setAttempt(n => n+1)}>重新读取</button>}
      </div>
    </> : <p role="status">正在读取预览…</p>}
  </section>;
}
