import React, {useRef, useState} from 'react';
import {casesApi} from '../api/cases';
import {useLanguage} from '../LanguageContext';
import {downloadBlob} from './downloadFile';

export function ModuleFile({file}) {
  const {t} = useLanguage();
  const picker = useRef(null);
  const details = useRef(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState(null);

  async function readOrRestore(backup) {
    setBusy(true);
    setFeedback(null);
    try {
      if (backup) {
        await casesApi.restoreModuleFile(file.id, backup);
        setFeedback({text:t('caseDetail.modulesTab.fileRestored') + ' #' + file.id});
      } else {
        downloadBlob(file.filename, await casesApi.downloadModuleFile(file.id));
      }
    } catch (cause) {
      setFeedback({error:true, text:cause.message});
      if (details.current) details.current.open = true;
    } finally {
      setBusy(false);
    }
  }

  return <div className="source-row">
    <div>
      <span>{file.filename} · #{file.id}</span>
      <details ref={details}>
        <summary>{t('caseDetail.modulesTab.fileInfo')}</summary>
        {file.content_hash ? <>
          <p className="field-note mono" style={{overflowWrap:'anywhere'}}>SHA-256 · {file.content_hash}</p>
          <input type="file" hidden ref={picker} aria-label={t('caseDetail.modulesTab.restoreFile')}
            onChange={event => {
              const backup = event.target.files[0];
              event.target.value = '';
              if (backup) readOrRestore(backup);
            }}/>
          <button type="button" className="btn btn-secondary" disabled={busy}
            onClick={() => picker.current.click()}>{t('caseDetail.modulesTab.restoreFile')}</button>
        </> : <p>{t('caseDetail.modulesTab.noFileDigest')}</p>}
      </details>
      {feedback && <p role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
    </div>
    <div className="action-row">
      <span className="meta-pill">{file.file_role}</span>
      <button type="button" className="btn btn-secondary" disabled={busy}
        onClick={() => readOrRestore()}>{t('common.download')}</button>
    </div>
  </div>;
}
