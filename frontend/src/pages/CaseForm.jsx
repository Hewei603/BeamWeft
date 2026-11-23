import React, { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { casesApi } from '../api/cases';
import { useLanguage } from '../LanguageContext';

export default function CaseForm() {
  const { id } = useParams();
  return <ProjectForm key={id || 'new'} id={id}/>;
}

function ProjectForm({id}) {
  const {t} = useLanguage();
  const navigate = useNavigate();
  const { hash } = useLocation();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [goal,setGoal]=useState('');
  const [original,setOriginal] = useState(null);
  const [conflict, setConflict] = useState(null);
  const [loading, setLoading] = useState(Boolean(id));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!id) return;
    let alive = true;
    casesApi.get(id).then(data => {
      if (alive) { setOriginal(data); setTitle(data.title); setDescription(data.description || ''); setGoal(data.goal || ''); }
    }).catch(err => { if (alive) setError(err.message); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [id]);
  const submit = async event => {
    event.preventDefault();
    setSaving(true); setError('');
    try {
      let projectId = id;
      if (id) {
        const values = {title:title.trim(), goal, description};
        const changed = Object.keys(values).filter(key => values[key] !== (original[key] ?? ''));
        if (changed.length) await casesApi.updateDetails(id, {
          changes:Object.fromEntries(changed.map(key => [key,values[key]])),
          expected:Object.fromEntries(changed.map(key => [key,original[key]])),
        });
      } else {
        projectId = (await casesApi.create({title:title.trim()})).id;
      }
      navigate(`/cases/${projectId}${hash === '#agent' ? hash : ''}`);
    } catch (err) {
      setError(err.message);
      if (id && err.status === 409) {
        try {
          const latest = await casesApi.get(id);
          const fields = {title:title.trim(), goal, description};
          const setters = {title:setTitle, goal:setGoal, description:setDescription};
          const collisions = {};
          for (const key of Object.keys(fields)) {
            const before = original[key] ?? '', now = latest[key] ?? '';
            if (fields[key] === before) setters[key](now);
            else if (before !== now && now !== fields[key]) collisions[key] = now;
          }
          setOriginal(latest); setConflict(collisions); setError('');
        } catch (readError) {setError(readError.message);}
      }
    } finally { setSaving(false); }
  };
  return <div className="page-container" style={{ maxWidth: 780, margin: '0 auto' }}>
    <Link to={id ? `/cases/${id}` : '/cases'}>← {t('nav.cases')}</Link>
    <h1>{t(id ? 'caseForm.editTitle' : 'caseForm.newTitle')}</h1>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {loading ? <p>{t('common.loading')}</p> : <form className="card" onSubmit={submit}>
      <div className="form-group"><label htmlFor="case-title">{t('caseForm.name')}</label>
        <input className="form-input" id="case-title" autoFocus required maxLength={255} value={title} onChange={e => setTitle(e.target.value)} /></div>
      {id && <><div className="form-group"><label htmlFor="case-goal">{t('caseForm.optionalGoal')}</label>
        <input id="case-goal" className="form-input" maxLength={20000} value={goal} onChange={e=>setGoal(e.target.value)}/></div>
        <div className="form-group"><label htmlFor="case-description">{t('caseForm.notes')}</label>
        <textarea className="form-textarea" id="case-description" rows={5} maxLength={20000} value={description} onChange={e => setDescription(e.target.value)} placeholder={t('caseForm.notesHint')} /></div></>}
      {Object.entries(conflict || {}).map(([key, value]) => {
        const label = {title:'name',goal:'optionalGoal',description:'notes'}[key];
        const decide = useCurrent => {
          if (useCurrent) ({title:setTitle,goal:setGoal,description:setDescription})[key](value);
          setConflict(previous => Object.fromEntries(Object.entries(previous).filter(([field]) => field !== key)));
        };
        return <div key={key} role="group" aria-label={t('caseForm.conflict')}><p>{t('caseForm.conflict')} · {t('caseForm.'+label)}</p>
          <blockquote>{value || t('caseForm.empty')}</blockquote>
          <button type="button" className="btn btn-secondary" onClick={() => decide(true)}>{t('caseForm.useCurrent')}</button>
          <button type="button" className="btn btn-secondary" onClick={() => decide(false)}>{t('caseForm.keepMine')}</button></div>;
      })}
      <button className="btn btn-primary" disabled={saving || !title.trim() || (id && !original) || !!Object.keys(conflict || {}).length}>{t(saving ? 'common.saving' : id ? 'common.save' : 'caseForm.enter')}</button>
    </form>}
  </div>;
}
