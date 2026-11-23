import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { casesApi, caseWorkspaceUrl } from '../api/cases';
import { useLanguage } from '../LanguageContext';
import { CaseBrowser, CaseCard } from '../components/CaseBrowser';
import { OpenProjectFile } from '../components/ProjectFile';

export default function CasesList() {
  const { t } = useLanguage();
  const [revision,setRevision] = useState(0);
  const [error,setError] = useState('');
  const remove = async item => {
    if (!window.confirm(t('casesList.confirmDelete') + ' ' + item.title)) return;
    try { await casesApi.delete(item.id); setError(''); setRevision(n => n+1); }
    catch (cause) { setError(cause.message); }
  };
  return <div>
    <div className="detail-header"><h1>{t('casesList.title')}</h1><div className="action-row"><OpenProjectFile/><Link to="/cases/new" className="btn btn-primary">{t('casesList.newCase')}</Link></div></div>
    {error && <div className="error" role="alert">{error}</div>}
    <CaseBrowser keepSearch revision={revision} renderItem={item => <CaseCard item={item} destination={caseWorkspaceUrl(item)}
      actions={<><Link to={'/cases/'+item.id+'/edit'} className="btn btn-secondary">{t('common.edit')}</Link>
        {!item.is_open_document && <button onClick={() => remove(item)} className="btn btn-secondary">{t('common.delete')}</button>}</>}/>}/>
  </div>;
}
