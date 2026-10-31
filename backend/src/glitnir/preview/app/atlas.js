// Curated argument exploration. This code never assigns proof status.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, esc, inlineHTML, walkthroughCompatibility} = workbench;
  const PAGE_SIZE = 12;
  let state = null, history = [], future = [], owner = null;
  const root = $('graph-view');
  const snapshot = () => ({...state, trail: [...state.trail], scroll: window.scrollY});
  function model(data) {
    const atlas = data.walkthrough?.atlas;
    if (!atlas?.nodes?.length || !atlas.routes?.length) return null;
    return {...atlas, byId: new Map(atlas.nodes.map(n => [n.id, n])), statements: new Map(data.walkthrough.nodes.map(n => [n.id, n]))};
  }
  function initialize(data, m) {
    const key = `${data.file}:${JSON.stringify(data.walkthrough.atlas)}`;
    if (owner !== key) {
      owner = key; history = []; future = [];
      state = {node: m.routes[0].root, route: m.routes[0].id, layout: 'reading', pane: 'argument', trail: [], page: 0, scroll: 0};
    }
  }
  const button = (label, action, value = '', extra = '') => `<button type="button" class="secondary" data-atlas="${action}" data-value="${esc(value)}" ${extra}>${esc(label)}</button>`;
  function sourcesHTML(n, m) {
    const statement = m.statements.get(n.statement);
    const refs = (statement?.lean || []).filter(ref => !n.declarations.length || n.declarations.includes(ref.name));
    return refs.map(ref => `<section class="atlas-reference"><h4>${esc(ref.name)}</h4><p class="small muted">${esc(ref.file)}:${esc(ref.line)}</p>${ref.note ? `<p>${inlineHTML(ref.note)}</p>` : ''}${ref.href ? `<a href="${esc(ref.href)}" target="_blank" rel="noopener noreferrer">Open pinned source ↗</a>` : '<p class="small muted">Public source link unavailable.</p>'}</section>`).join('') || '<p>No mapped declaration references.</p>';
  }
  function explanation(n) {
    return `<div class="atlas-explanation"><h3>What this establishes</h3><p>${inlineHTML(n.goal)}</p><h3>Why it matters</h3><p>${inlineHTML(n.role)}</p><h3>Scope and assumptions</h3><p>${inlineHTML(n.scope)}</p><h3>Proof idea</h3><p>${inlineHTML(n.idea)}</p>${n.difference ? `<h3>Paper ↔ Lean</h3><p>${inlineHTML(n.difference)}</p>` : ''}</div>`;
  }
  function supportsHTML(n, m, route) {
    const ids = (Object.hasOwn(route.supports, n.id) ? route.supports[n.id] : []), shown = ids.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
    const remaining = ids.length - shown.length;
    return `<section class="atlas-supports"><h3>Selected supporting arguments</h3><p class="small muted">Curated reading links; other proof steps may be omitted.</p>${shown.map(id => button(m.byId.get(id).label, 'node', id)).join('')}${!ids.length ? '<p class="small muted">Exploration boundary: no deeper explanation authored here. This does not mean no dependencies.</p>' : ''}${remaining > 0 ? `<p class="small muted">${remaining} supporting arguments outside this page.</p>` : ''}${state.page > 0 ? button('Previous arguments', 'page', String(state.page - 1)) : ''}${(state.page + 1) * PAGE_SIZE < ids.length ? button('More arguments', 'page', String(state.page + 1)) : ''}</section>`;
  }
  function renderAtlas(data) {
    const m = model(data); if (!m) return false;
    initialize(data, m);
    if (state.layout === 'graph') return false;
    const n = m.byId.get(state.node), route = m.routes.find(r => r.id === state.route);
    const compatibility = walkthroughCompatibility(data);
    const known = m.statements.get(n.statement);
    const mappedClaim = known?.claim;
    const claimModel = mappedClaim ? workbench.walkthroughGraphModel(data).nodes.find(node => node.outlineId === n.statement) : null;
    const historyTrail = [...state.trail, state.node].map((id, i) => button(m.byId.get(id).label, 'trail', String(i))).join('<span aria-hidden="true"> / </span>');
    const argument = state.pane === 'sources' ? `<section><h3>Declaration references</h3>${sourcesHTML(n, m)}${button('Return to argument', 'pane', 'argument')}</section>` : explanation(n);
    const supports = supportsHTML(n, m, route);
    root.innerHTML = `<section class="proof-graph-view proof-atlas" aria-label="Proof atlas"><header><div><h2>${esc(data.walkthrough.title)}</h2><p>Proof atlas · curated argument exploration</p></div></header>${!compatibility.supported ? `<p class="banner quiet">${esc(compatibility.message)}</p>` : ''}<div class="atlas-controls">${button('← Back', 'back', '', history.length ? '' : 'disabled')}${button('Forward →', 'forward', '', future.length ? '' : 'disabled')}<label>Route (starts at result) <${'select'} data-atlas-route>${m.routes.map(r => `<option value="${esc(r.id)}"${r.id === state.route ? ' selected' : ''}>${esc(r.label)}</option>`).join('')}</${'select'}></label>${button('Reading first', 'layout', 'reading', `aria-pressed="${state.layout === 'reading'}"`)}${button('Map first', 'layout', 'map', `aria-pressed="${state.layout === 'map'}"`)}${button('Statement graph', 'layout', 'graph')}</div><nav class="atlas-trail" aria-label="Exploration trail">${historyTrail}</nav><h2 id="atlas-selected" tabindex="-1">${esc(n.label)}</h2><p class="small muted">${esc(route.label)} · curated, not extracted dependencies · source revision ${esc(m.commit)}</p><p class="small" role="status">${claimModel ? `Mapped notebook statement ${esc(claimModel.name)}: ${esc(claimModel.status)}` : 'No mapped saved observation'} · This argument is exposition; navigation grants no proof status.</p><div class="atlas-controls">${button('Argument', 'pane', 'argument', `aria-pressed="${state.pane === 'argument'}"`)}${button('Source references', 'pane', 'sources', `aria-pressed="${state.pane === 'sources'}"`)}${claimModel?.name ? `<button type="button" class="secondary" data-act="graph-node" data-graph-id="${esc(claimModel.id)}">Show in notebook</button>` : ''}</div><div class="atlas-body ${state.layout === 'map' ? 'atlas-map-first' : ''}">${state.layout === 'map' ? `<div class="atlas-neighborhood"><p class="small muted">Selected argument</p><div class="atlas-focus">${esc(n.label)}</div><p class="atlas-arrow" aria-hidden="true">↓</p>${supports}</div>${argument}` : `${argument}${supports}`}</div></section>`;
    workbench.typesetMath?.(root);
    return true;
  }
  function go(action, value) {
    const m = model(UI.data); if (!m) return;
    initialize(UI.data, m);
    const previous = snapshot();
    state = {...state, scroll: previous.scroll};
    if (action === 'back' || action === 'forward') {
      const from = action === 'back' ? history : future, to = action === 'back' ? future : history;
      if (!from.length) return;
      to.push(previous); state = from.pop();
    } else {
      history.push(previous); future = [];
      if (action === 'node' && m.byId.has(value)) state = {...state, node: value, trail: [...state.trail, state.node].slice(-24), pane: 'argument', page: 0, scroll: 0};
      if (action === 'trail') {const path = [...state.trail, state.node], i = Number(value); state = {...state, node: path[i], trail: path.slice(0, i), page: 0, scroll: 0};}
      if (action === 'pane') state = {...state, pane: value};
      if (action === 'page') state = {...state, page: Number(value)};
      if (action === 'layout') state = {...state, layout: value};
      if (action === 'route') {const r = m.routes.find(r => r.id === value); state = {...state, route: value, node: r.root, trail: [], pane: 'argument', page: 0, scroll: 0};}
    }
    workbench.renderProofGraph();
    (root.querySelector('#atlas-selected') || root.querySelector('[data-atlas="layout"]'))?.focus({preventScroll: true});
    window.scrollTo?.(0, state.scroll);
  }
  root?.addEventListener('click', event => {const b = event.target.closest('[data-atlas]'); if (b) go(b.dataset.atlas, b.dataset.value);});
  root?.addEventListener('change', event => {if (event.target.matches('[data-atlas-route]')) go('route', event.target.value);});
  function atlasStaticHTML(data) {
    const m = model(data); if (!m) return '';
    return `<section class="proof-atlas"><h2>Proof atlas</h2><p>Curated reading routes; these are not extracted Lean dependencies. Source revision ${esc(m.commit)}. Navigation grants no proof status.</p>${m.routes.map(r => `<nav aria-label="${esc(r.label)}"><h3>${esc(r.label)}</h3><a href="#atlas-${esc(r.root)}">Start reading</a></nav>`).join('')}${m.nodes.map(n => `<details class="atlas-argument" id="atlas-${esc(n.id)}"><summary>${esc(n.label)}</summary>${explanation(n)}${sourcesHTML(n, m)}${m.routes.map(r => `<h4>${esc(r.label)} — selected supporting arguments</h4>${((Object.hasOwn(r.supports, n.id) ? r.supports[n.id] : [])).map(id => `<p><a href="#atlas-${esc(id)}">${esc(m.byId.get(id).label)}</a></p>`).join('') || '<p>Exploration boundary; further dependencies may exist.</p>'}`).join('')}</details>`).join('')}</section>`;
  }
  Object.assign(workbench, {renderAtlas, atlasStaticHTML});
})();
