import React, { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useLanguage } from '../LanguageContext';
import AgentSettings from '../components/AgentSettings';
import { casesApi, caseWorkspaceUrl } from '../api/cases';
import { OpenProjectFile } from '../components/ProjectFile';
import './Home.css';

function Home() {
  const { t } = useLanguage();
  const [recent, setRecent] = useState(null);
  const [recentError, setRecentError] = useState('');
  const [request, setRequest] = useState(0);
  const {hash} = useLocation();
  useEffect(() => {
    if (hash === '#agent-settings') document.getElementById('agent-settings')?.focus();
  }, [hash]);
  useEffect(() => {
    const controller = new AbortController();
    casesApi.library({limit:4}, controller.signal).then(data => {
      if (!controller.signal.aborted) setRecent(data.items);
    }).catch(error => { if (!controller.signal.aborted) setRecentError(error.message); });
    return () => controller.abort();
  }, [request]);
  return (
    <div className="home-page">
      <section className="home-cover" aria-labelledby="home-title">
        <picture className="home-optics" aria-hidden="true">
          <source media="(max-width: 650px)" srcSet="/optical-relay-small.webp"/>
          <img src="/optical-relay.webp" alt="" fetchPriority="high"/>
        </picture>
        <div className="home-intro">
          <h1 id="home-title" aria-label={t('home.title')}>{t('home.titleLead')}<br/><span>{t('home.titleEnd')}</span></h1>
          <p>{t('home.subtitle')}</p>
          <div className="home-actions">
            <Link to="/cases/new" className="home-create"><span aria-hidden="true">＋</span>{t('home.newCase')}</Link>
            <Link to="/cases" className="home-projects">{t('home.viewCases')} <span aria-hidden="true">↗</span></Link>
            <OpenProjectFile/>
          </div>
        </div>
      </section>
      <div className="home-library">
        <section aria-labelledby="home-project-heading">
          <h2 id="home-project-heading">{t('home.recentProjects')}</h2>
          {recentError ? <p role="alert">{recentError} <button type="button" onClick={()=>{
            setRecentError(''); setRequest(n=>n+1);
          }}>{t('library.retry')}</button></p> : recent?.length ? <nav className="home-recent" aria-labelledby="home-project-heading">
            {recent.map(item => {
              const revision = item.opening_revision_id ?? item.revision_id;
              return <Link key={item.id} to={caseWorkspaceUrl(item)} aria-label={item.title}>
                <i className="home-file-icon" aria-hidden="true"/><span>{item.title}</span>
                <small>{revision && t('library.model')+' #'+revision}</small>
                <b aria-hidden="true">↗</b></Link>;
            })}
          </nav> : <p className="home-empty" role="status">{recent ? t('home.empty') : t('common.loading')}</p>}
        </section>
        <section aria-labelledby="home-lab-heading">
          <h2 id="home-lab-heading">{t('home.lab')}</h2>
          <nav className="home-lab-links" aria-labelledby="home-lab-heading">
            {[['/inventory','home.inventoryTitle'],['/lab-documents','home.labDocsTitle'],['/agent','home.agentTitle']].map(([url,key])=>
              <Link key={url} to={url}>{t(key)} <span aria-hidden="true">↗</span></Link>)}
          </nav>
        </section>
      </div>
      <div className="home-config"><AgentSettings /></div>
    </div>
  );
}

export default Home;
