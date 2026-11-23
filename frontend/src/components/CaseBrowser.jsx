import React, { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { casesApi } from '../api/cases';
import { useLanguage } from '../LanguageContext';
import './CaseBrowser.css';

export function CaseBrowser({ renderItem, keepSearch = false, revision = 0 }) {
  const { t } = useLanguage();
  const [params, setParams] = useSearchParams();
  const [localQuery, setLocalQuery] = useState('');
  const query = keepSearch ? params.get('q') || '' : localQuery;
  const search = value => {
    const term = value.trim();
    if (keepSearch) {
      const next = new URLSearchParams(params);
      if (term) next.set('q', term); else next.delete('q');
      setParams(next);
    } else setLocalQuery(term);
  };
  return <section className="case-browser" aria-label={t('library.directory')}>
    <form role="search" onSubmit={event => { event.preventDefault(); search(event.currentTarget.elements.q.value); }}>
      <input type="search" name="q" key={query} aria-label={t('library.search')} placeholder={t('library.search')} maxLength={255} defaultValue={query} />
      <button type="submit" className="btn btn-secondary">{t('common.search')}</button>
      {query && <button type="button" className="btn btn-secondary" onClick={() => search('')}>{t('library.reset')}</button>}
    </form>
    <CaseMatches key={JSON.stringify([query,revision])} query={query} renderItem={renderItem}/>
  </section>;
}

function CaseMatches({query,renderItem}) {
  const {t} = useLanguage();
  const [request, setRequest] = useState({before:null});
  const [page, setPage] = useState({items:[], next:null});
  useEffect(() => {
    const controller = new AbortController();
    casesApi.library({q:query || undefined, before:request.before || undefined}, controller.signal)
      .then(data => {
        if (!controller.signal.aborted) setPage(previous => ({
          request, items:[...previous.items,...data.items], next:data.next_before,
        }));
      }).catch(cause => {
        if (!controller.signal.aborted) setPage(previous => ({...previous,request,error:cause.message}));
      });
    return () => controller.abort();
  }, [query,request]);
  const loading = page.request !== request;
  const error = !loading && page.error;
  return <>
    <ul>{page.items.map(item => <li key={item.id}>{renderItem(item)}</li>)}</ul>
    {loading && <p role="status">{t('common.loading')}</p>}
    {error && <div className="error" role="alert">{error} <button type="button" className="btn btn-secondary" onClick={() => setRequest({...request})}>{t('library.retry')}</button></div>}
    {!error && !loading && !page.items.length && <p role="status">{query ? t('library.noMatch') : t('casesList.noCases')}</p>}
    {!error && page.next && <button disabled={loading} type="button" className="btn btn-secondary"
      onClick={() => setRequest({before:page.next})}>{t('library.more')}</button>}
  </>;
}

export function CaseSelect({ id, value, onChange, disabled, label, emptyLabel }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [known, setKnown] = useState(null);
  const toggle = useRef(null);
  const selected = value && known?.id === Number(value) ? known.title : (value ? '#'+value : emptyLabel);
  const choose = item => {
    setKnown(item); onChange(item ? String(item.id) : ''); setOpen(false); toggle.current?.focus();
  };
  return <div className="case-select">
    <button id={id} className="btn btn-secondary" type="button" aria-label={label + ' · ' + selected}
      aria-expanded={open} disabled={disabled} ref={toggle} onClick={() => setOpen(v => !v)}>{selected} ▾</button>
    {open && !disabled && <div className="case-select-panel">
      <CaseBrowser renderItem={item => <button type="button" className="case-pick" onClick={() => choose(item)}>{item.title} <small>#{item.id}</small></button>}/>
      {value && <button type="button" className="btn btn-secondary" onClick={() => choose(null)}>{t('library.clearSelection')}</button>}
    </div>}
  </div>;
}

export function CaseCard({ item, destination, actions }) {
  const { t, lang } = useLanguage();
  const modelRevision = item.opening_revision_id ?? item.revision_id;
  const created = item.created_at && (/[zZ]|[+-]\d\d:\d\d$/.test(item.created_at) ? item.created_at : item.created_at + 'Z');
  return <article className="case-card"><div>
    <h2><Link to={destination}>{item.title}</Link></h2>
    {item.description && <p>{item.description}</p>}
    <div className="case-card-meta">
      <span>#{item.id}</span><span>{t('caseStatus.' + item.status)}</span>
      {created && <time dateTime={created}>{t('casesList.created')} {new Date(created).toLocaleDateString(lang)}</time>}
      {modelRevision && <span>{t('library.model')} #{modelRevision}</span>}
      {item.file_count > 0 && <span>{t('library.files')} {item.file_count}</span>}
      {item.study_count > 0 && <span>{t('library.studies')} {item.study_count}</span>}
      {!modelRevision && item.cavity_type && item.cavity_type !== 'custom' && <span>{t('library.registered')} · {t('cavityTypes.' + item.cavity_type)}</span>}
      {item.project_id && <span>{t('casesList.project')} #{item.project_id}</span>}
      {(item.tags || []).map((tag,i) => <span key={i}>{tag}</span>)}
    </div></div>{actions && <details className="case-card-actions" onKeyDown={event => {
      if (event.key === 'Escape') {
        event.currentTarget.open = false;
        event.currentTarget.querySelector('summary').focus();
      }
    }}>
      <summary aria-label={t('library.manage')+' · '+item.title}>{t('library.manage')}</summary>
      <div>{actions}</div>
    </details>}
  </article>;
}
