import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { caseWorkspaceUrl } from '../api/cases';
import { CaseBrowser, CaseCard } from '../components/CaseBrowser';
import { useLanguage } from '../LanguageContext';
import LegacyAgentWorkspace from './LegacyAgentWorkspace';

export default function AgentWorkspace() {
  const { t } = useLanguage();
  const [legacy, setLegacy] = useState(false);
  return <div>
    <div className="detail-header"><div><h1>{t('library.agentTitle')}</h1><p>{t('library.agentPrompt')}</p></div>
      <div className="action-row"><Link className="btn btn-primary" to="/cases/new#agent">{t('casesList.newCase')}</Link>
        <button className="btn btn-secondary" onClick={() => setLegacy(v=>!v)}>{t(legacy?'library.back':'library.history')}</button></div>
    </div>
    {legacy ? <LegacyAgentWorkspace/> : <CaseBrowser keepSearch renderItem={item =>
      <CaseCard item={item} destination={caseWorkspaceUrl(item)+'#agent'}/>}/>}
  </div>;
}
