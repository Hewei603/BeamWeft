import React, {useEffect, useRef, useState} from 'react';
import {Link, useLocation} from 'react-router-dom';
import {useLanguage} from '../LanguageContext';
import {productName} from '../brand';
import {nativeFileId, nativeRequiresSaveAs} from '../api/client';
import AgentSettings from './AgentSettings';
import {OpenProjectFile} from './ProjectFile';
import {useCurrentAction} from './menuCommands';
import './AppMenu.css';

const copy = {
  zh: {
    file:'文件', edit:'编辑', resources:'资源', settings:'设置', help:'帮助',
    undo:'撤销光路修改', redo:'重做光路修改', editProject:'编辑工程资料…', library:'工程库',
    start:'快速入门', keys:'键盘快捷键', close:'关闭',
    guide:['在「文件」中新建工程，只需输入名称。也可以打开已保存的 .beamweft 工程文件。',
      '在光路图选中元件，可以拖动或输入位置。选择「保存新版本并复算」，即可查看该版本的计算结果。',
      '把测量原件放进工程的「实验资料」。在工程对话里描述目标，Agent 可以读取原件并调用物理工具。'],
    keysText:['Ctrl / ⌘ + S　保存工程文件','Ctrl / ⌘ + Z　撤销当前光路修改','Ctrl / ⌘ + Shift + Z　重做光路修改','Esc　关闭当前菜单或帮助']
  },
  en: {
    file:'File', edit:'Edit', resources:'Resources', settings:'Settings', help:'Help',
    undo:'Undo optical edit', redo:'Redo optical edit', editProject:'Edit project details…', library:'Project library',
    start:'Getting started', keys:'Keyboard shortcuts', close:'Close',
    guide:['Choose File and create a project with a name, or open an existing .beamweft project.',
      'Select an element on the optical diagram. Drag it or enter a position, then save and recalculate to see the result for that revision.',
      'Add measurement files to the project and describe your goal in the project conversation. The Agent can read the files and run optical tools.'],
    keysText:['Ctrl / ⌘ + S　Save project file','Ctrl / ⌘ + Z　Undo optical edit','Ctrl / ⌘ + Shift + Z　Redo optical edit','Esc　Close menu or help']
  }
};
const sections = ['file','edit','resources','settings','help'];
const availableItems = 'a[href],button:not(:disabled)';

export default function AppMenu() {
  const {lang, setLang, t} = useLanguage();
  const {pathname} = useLocation();
  const projectId = /^\/cases\/(\d+)\/?$/.exec(pathname)?.[1];
  const words = copy[lang];
  const [menu, setMenu] = useState(null);
  const [dialog, setDialog] = useState(null);
  const nav = useRef(null);
  const help = useRef(null);
  const triggers = useRef({});
  const panels = useRef({});
  const save = useCurrentAction('save'), saveAs = useCurrentAction('saveAs');
  const undo = useCurrentAction('undo'), redo = useCurrentAction('redo');

  useEffect(() => {
    if (!menu) return;
    function outside(event) { if (!nav.current?.contains(event.target)) setMenu(null); }
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [menu]);
  useEffect(() => {
    if (dialog === 'start' || dialog === 'keys') help.current.showModal();
  }, [dialog]);
  function dismissDialog() {
    setDialog(null);
    triggers.current[dialog === 'settings' ? 'settings' : 'help']?.focus();
  }
  function showDialog(which) { setMenu(null); setDialog(which); }
  function command(action) {
    setMenu(null);
    triggers.current[menu]?.focus();
    action();
  }
  function keys(event, section) {
    const key = event.key;
    if (key === 'Escape' && menu) {
      event.preventDefault(); setMenu(null); triggers.current[section].focus();
    } else if (key === 'ArrowLeft' || key === 'ArrowRight') {
      event.preventDefault();
      const next = sections[(sections.indexOf(section) + (key === 'ArrowLeft' ? -1 : 1) + sections.length) % sections.length];
      if (menu) setMenu(next);
      triggers.current[next].focus();
    } else if (key === 'ArrowDown' || key === 'ArrowUp') {
      event.preventDefault();
      setMenu(section);
      requestAnimationFrame(() => {
        const items = [...(panels.current[section]?.querySelectorAll(availableItems) || [])];
        if (!items.length) return;
        const index = items.indexOf(document.activeElement);
        const target = key === 'ArrowDown' ? index + 1 : index < 0 ? items.length - 1 : index - 1;
        items[(target + items.length) % items.length].focus();
      });
    }
  }
  const contents = {
    file:<><Link to="/cases/new">{t('nav.newCase')}</Link><OpenProjectFile/><Link to="/cases">{words.library}</Link>
      <hr/><button disabled={!save} onClick={()=>command(save)}>{t(nativeRequiresSaveAs && projectId === nativeFileId ? 'caseDetail.fileCopy' : 'caseDetail.fileSave')}</button>
      {saveAs && <button onClick={()=>command(saveAs)}>{t('caseDetail.fileCopy')}</button>}</>,
    edit:<><button disabled={!undo} onClick={()=>command(undo)}>{words.undo}</button>
      <button disabled={!redo} onClick={()=>command(redo)}>{words.redo}</button><hr/>
      {projectId ? <Link to={'/cases/'+projectId+'/edit'}>{words.editProject}</Link> : <button disabled>{words.editProject}</button>}</>,
    resources:<><Link to="/inventory">{t('nav.inventory')}</Link><Link to="/lab-documents">{t('nav.labDocs')}</Link>
      <Link to="/literature">{t('nav.literature')}</Link></>,
    settings:<><button onClick={()=>showDialog('settings')}>{t('agentSettings.open')}</button>
      <button onClick={()=>command(()=>setLang(lang === 'zh' ? 'en' : 'zh'))}>{lang === 'zh' ? 'English' : '简体中文'}</button></>,
    help:<><button onClick={()=>showDialog('start')}>{words.start}</button>
      <button onClick={()=>showDialog('keys')}>{words.keys}</button></>
  };
  return <>
    <nav ref={nav} className={'navbar app-menu'+(projectId?' case-navbar':'')} aria-label={t('nav.menu')}
      onBlur={e=>{if (e.relatedTarget && !e.currentTarget.contains(e.relatedTarget)) setMenu(null);}}>
      <Link to="/" className="product-wordmark" aria-label={productName+' · '+t('nav.home')} onClick={()=>setMenu(null)}>
        <i className="brand-optics" aria-hidden="true"/><span>{productName}</span></Link>
      <div className="app-menu-groups">{sections.map(section=><div key={section} className="app-menu-group" onKeyDown={e=>keys(e,section)}>
        <button type="button" className="app-menu-heading" ref={el=>{triggers.current[section]=el;}}
          aria-expanded={menu===section} aria-controls={'app-menu-'+section}
          onClick={()=>setMenu(menu===section?null:section)}>{words[section]}</button>
        <div id={'app-menu-'+section} ref={el=>{panels.current[section]=el;}} className="app-menu-panel"
          hidden={menu!==section} onClick={e=>{if(e.target.closest('a[href]'))setMenu(null);}}>{contents[section]}</div>
      </div>)}</div>
    </nav>
    {dialog==='settings' && <AgentSettings onDismiss={dismissDialog}/>}
    {(dialog==='start'||dialog==='keys') && <dialog className="app-menu-help" ref={help} aria-labelledby="menu-help-title" onClose={dismissDialog}>
      <h2 id="menu-help-title">{words[dialog]}</h2>
      <ul>{(dialog==='keys'?words.keysText:words.guide).map(line=><li key={line}>{line}</li>)}</ul>
      <button type="button" className="btn btn-secondary" onClick={()=>help.current.close()}>{words.close}</button>
    </dialog>}
  </>;
}
