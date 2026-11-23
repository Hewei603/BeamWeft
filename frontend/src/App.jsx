import React, { lazy, Suspense, useEffect, useState } from 'react';
import { BrowserRouter as Router, Link, Route, Routes, useLocation } from 'react-router-dom';
import apiClient, { nativeFileSource } from './api/client';
import AgentWorkspace from './pages/AgentWorkspace';
import CaseForm from './pages/CaseForm';
import CasesList from './pages/CasesList';
import Home from './pages/Home';
import {AgentSettingsLauncher} from './components/AgentSettings';
import { LanguageProvider, useLanguage } from './LanguageContext';
import { EXPECTED_BACKEND_VERSION } from './expectedBackend';
import './App.css';
import AppMenu from './components/AppMenu';
import './print.css';
import { productName } from './brand';

const CaseDetail = lazy(() => import('./pages/CaseDetail'));
const InventoryPage = lazy(() => import('./pages/InventoryPage'));
const LiteraturePage = lazy(() => import('./pages/LiteraturePage'));
const LabDocuments = lazy(() => import('./pages/LabDocuments'));

class PageLoadBoundary extends React.Component {
  state = {failed:false};
  static getDerivedStateFromError() { return {failed:true}; }
  render() {
    return this.state.failed ? <div className="error" role="alert">{this.props.message} <button
      className="btn btn-secondary" onClick={() => window.location.reload()}>{this.props.retry}</button></div> : this.props.children;
  }
}

function PageContent({children}) {
  const {t}=useLanguage();
  const {pathname}=useLocation();
  const homeTitle = productName + ' · ' + t('home.title');
  const file = nativeFileSource && /^\/cases\/(\d+)(?:\/edit)?\/?$/.exec(pathname)?.[1] === nativeFileSource.caseId
    ? nativeFileSource : null;
  useEffect(() => {
    const title = file ? file.label + ' · ' + productName + ' · ' + file.path : homeTitle;
    document.title = title;
    if (!file) return;
    // Browser print headers can include the window title.
    const beforePrint = () => {document.title = homeTitle;};
    const afterPrint = () => {document.title = title;};
    window.addEventListener('beforeprint', beforePrint);
    window.addEventListener('afterprint', afterPrint);
    return () => {
      window.removeEventListener('beforeprint', beforePrint);
      window.removeEventListener('afterprint', afterPrint);
    };
  }, [file, homeTitle]);
  return <PageLoadBoundary key={pathname} message={t('library.pageError')} retry={t('library.retry')}>
    <Suspense fallback={<p role="status">{t('common.loading')}</p>}>{children}</Suspense>
  </PageLoadBoundary>;
}

function FooterText() {
  const { t } = useLanguage();
  return <p>{t('common.footer')}</p>;
}

function ProviderBanner() {
  const { t } = useLanguage();
  const { pathname } = useLocation();
  const [provider, setProvider] = useState(null);
  const [backendVersion, setBackendVersion] = useState(undefined); // undefined = not yet answered

  useEffect(() => {
    let cancelled = false;
    let settingsChanged = false;
    const onSettingsUpdated = (event) => {
      settingsChanged = true;
      setProvider(event.detail);
    };
    window.addEventListener('agent-settings-updated', onSettingsUpdated);
    apiClient.get('/health')
      .then((res) => {
        if (cancelled) return;
        if (!settingsChanged) setProvider(res.data?.provider || null);
        // Pre-2.1 backends report no version at all — that is exactly the
        // stale-process case this banner exists for, so map it to null.
        setBackendVersion(res.data?.version || null);
      })
      .catch(() => {});      // unreachable backend already yields page-level errors
    return () => {
      cancelled = true;
      window.removeEventListener('agent-settings-updated', onSettingsUpdated);
    };
  }, []);

  // The launcher-started backend keeps serving old code until its window is
  // closed; the user cannot tell from the page alone, so tell them loudly.
  const versionBanner = (backendVersion !== undefined && backendVersion !== EXPECTED_BACKEND_VERSION)
    ? (
      <div className="error" style={{ margin: '0.5rem 1rem' }}>
        ⚠ {t('banner.backendOutdated')}
        {backendVersion ? `（${t('banner.backendVersion')} ${backendVersion} ≠ ${EXPECTED_BACKEND_VERSION}）` : ''}
      </div>
    )
    : null;

  if (!provider) return versionBanner;
  // Mock output is indistinguishable from a real model's by tone — the user
  // must always be able to see which one produced what they are reading.
  if (provider.error) {
    return (
      <>
        {versionBanner}
        <div className="error" style={{ margin: '0.5rem 1rem' }}>{t('banner.providerError')}{provider.error}</div>
      </>
    );
  }
  if (provider.is_mock) {
    return (
      <>
        {versionBanner}
        <div className="disclaimer provider-notice">
          <span>{t('banner.demoWorkbench')}</span> {pathname === '/' ? <Link to="/#agent-settings">{t('banner.configure')}</Link>
            : <AgentSettingsLauncher className="provider-settings-link">{t('banner.configure')}</AgentSettingsLauncher>}
        </div>
      </>
    );
  }
  return versionBanner;
}

function App() {
  return (
    <LanguageProvider>
      <Router>
        <div className="app">
          <AppMenu />
          <ProviderBanner />
          <main className="main-content">
            <PageContent><Routes>
              <Route path="/" element={<Home />} />
              <Route path="/agent" element={<AgentWorkspace />} />
              <Route path="/cases" element={<CasesList />} />
              <Route path="/cases/new" element={<CaseForm />} />
              <Route path="/cases/:id" element={<CaseDetail />} />
              <Route path="/cases/:id/edit" element={<CaseForm />} />
              <Route path="/lab-documents" element={<LabDocuments />} />
              <Route path="/inventory" element={<InventoryPage />} />
              <Route path="/literature" element={<LiteraturePage />} />
            </Routes></PageContent>
          </main>
          <footer className="footer">
            <FooterText />
          </footer>
        </div>
      </Router>
    </LanguageProvider>
  );
}


export default App;
