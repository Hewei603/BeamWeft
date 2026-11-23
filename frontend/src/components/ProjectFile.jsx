import React, {useEffect, useRef, useState} from 'react';
import { Link } from 'react-router-dom';
import {casesApi} from '../api/cases';
import {useLanguage} from '../LanguageContext';
import {useMenuAction} from './menuCommands';

export const nativeFileId = globalThis.document?.querySelector('meta[name="laserclaw-native-file"]')?.content;

export function OpenProjectFile() {
  const {t} = useLanguage();
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    casesApi.documentPicker().then(value => {if (current) setAvailable(value);}).catch(() => {});
    return () => {current = false;};
  }, []);
  if (!available) return null;
  async function open() {
    setBusy(true); setError('');
    try {
      const url = await casesApi.openDocument();
      if (url) window.location.assign(url);
    } catch (cause) {setError(cause.message);}
    finally {setBusy(false);}
  }
  return <><button type="button" className="btn btn-secondary" onClick={open} disabled={busy}>{t('caseDetail.fileOpen')}</button>
    {error && <span role="alert">{error}</span>}</>;
}

export function ProjectFileActions({id, title, createdAt, editor, transfers, onDelete, onRepairInput}) {
  const {t} = useLanguage();
  const [status, setStatus] = useState(null);
  const writing = useRef(false);
  const isNative = String(id) === nativeFileId;
  async function save(format = 'project', omit = []) {
    if (writing.current) return;
    writing.current = true;
    setStatus({busy:true, text:t('common.saving')});
    const activity = new AbortController();
    const from = window.location.href;
    let stayHere = false;
    try {
      if (!transfers) throw Error('尚未读到当前工程的归档身份。请重试读取工程。');
      const legacy = format === 'zip';
      const nativeSave = isNative && !legacy;
      if (!legacy) {
        if (!editor.current) throw Error('工作台正在读取，请稍后保存。');
        editor.current.checkFileInputs();
      }
      const handle = !nativeSave && !legacy && window.showSaveFilePicker ? await window.showSaveFilePicker({
        suggestedName: (title.replace(/[<>:"/\\|?*]/g, '_').slice(0, 90) || 'BeamWeft') + '.lclaw',
        types: [{description:'BeamWeft', accept:{'application/octet-stream':['.lclaw']}}],
      }) : null;
      const archivesReady = async () => {
        if (transfers.pending.size) setStatus({busy:true, text:t('caseDetail.waitingForFiles')});
        const missing = (await transfers.wait()).filter(item => !omit.includes(item.key));
        if (!missing.length) return true;
        setStatus({error:true, missing, format, text:t('caseDetail.filesUnconfirmed')+' '+missing.map(item=>item.label).join('、')});
        return false;
      };
      if (!await archivesReady()) return;
      setStatus({busy:true, text:t('common.saving')});
      if (!legacy && !editor.current) throw Error('工作台正在读取，请稍后保存。');
      let selection;
      if (nativeSave && format === 'as') {
        selection = await casesApi.chooseSaveLocation();
        if (!selection) {setStatus(null); return;}
        // Keep later source edits visible if preparing or opening the snapshot takes time.
        for (const event of ['input', 'pointerdown', 'keydown', 'popstate']) {
          window.addEventListener(event, () => {stayHere = true;}, {capture:true, signal:activity.signal});
        }
      }
      if (window.location.href !== from) throw Error(t('caseDetail.saveViewChanged'));
      const revision = legacy ? null : await editor.current.prepareFileSave();
      if (!legacy && !await archivesReady()) return;
      const url = legacy ? casesApi.bundleUrl(id) : casesApi.projectFileUrl(id, revision);
      if (nativeSave) {
        const receipt = await casesApi.saveOpenedFile(revision, selection);
        if (receipt.open_error) throw Error(receipt.open_error);
        if (receipt.url) {
          if (stayHere || window.location.href !== from) {
            setStatus({text:t('caseDetail.savedSeparateFile')+' · '+receipt.filename, open:receipt.url});
          } else {
            window.location.assign(receipt.url);
          }
          return;
        }
        setStatus({text:t('caseDetail.fileSaved')+' · '+receipt.filename});
      } else if (handle) {
        const response = await fetch(url);
        if (!response.ok) {
          const problem = await response.json().catch(() => ({}));
          throw Error(problem.detail || response.statusText);
        }
        const target = await handle.createWritable();
        await response.body.pipeTo(target);
        setStatus({text:t('caseDetail.fileSaved')+' · '+handle.name});
      } else {
        const link = document.createElement('a');
        link.href = url; link.target = '_blank'; link.rel = 'noopener'; link.click();
        setStatus({text:t('caseDetail.downloadStarted')});
      }
    } catch (cause) {
      setStatus(cause.name === 'AbortError' ? null : {error:true, text:cause.message, repair:cause.code==='unapplied_input'});
    } finally {activity.abort(); writing.current = false;}
  }
  useMenuAction('save', !status?.busy, () => save());
  useMenuAction('saveAs', isNative && !status?.busy, () => save('as'));
  useEffect(() => {
    const key = event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault(); save(event.shiftKey ? 'as' : 'project');
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  });
  return <><button className="btn btn-secondary" onClick={() => save()} disabled={status?.busy}>{t('caseDetail.fileSave')}</button>
    {isNative && <button className="btn btn-secondary" onClick={() => save('as')} disabled={status?.busy}>{t('caseDetail.fileCopy')}</button>}
    {status && <small role={status.error ? 'alert' : 'status'}>{status.text}</small>}
    {status?.open && <a className="btn btn-secondary" href={status.open} target="_blank" rel="noopener">{t('caseDetail.openSavedFile')}</a>}
    {status?.repair && <button type="button" className="btn btn-secondary" onClick={()=>{setStatus(null);onRepairInput();}}>回到工程编辑</button>}
    {status?.missing && <button type="button" className="btn btn-secondary"
      onClick={()=>save(status.format, status.missing.map(item=>item.key))}>{t('caseDetail.saveArchivedOnly')}</button>}
    <details className="case-manage" onKeyDown={e=>{
      if(e.key==='Escape'){e.currentTarget.open=false;e.currentTarget.querySelector('summary').focus();}
    }} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget))e.currentTarget.open=false;}}>
      <summary>{t('caseDetail.operations')}</summary><div className="case-manage-menu">
        <small>{t('caseDetail.created')}{new Date(createdAt).toLocaleString()}</small>
        <Link to={`/cases/${id}/edit`} className="btn btn-secondary">{t('common.edit')}</Link>
        <button type="button" onClick={()=>save('zip')} disabled={status?.busy} className="btn btn-secondary">{t('caseDetail.exportBundle')}</button>
        {onDelete && <button onClick={onDelete} className="btn btn-danger">{t('common.delete')}</button>}
      </div></details></>;
}
