// Curated argument exploration. This code never assigns proof status.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, esc, inlineHTML, walkthroughCompatibility} = workbench;
  const PAGE_SIZE = 12, OVERVIEW_SIZE = 24;
  const routeIndexes = new WeakMap();
  let state = null, history = [], future = [], owner = null, lastHash = null, linkMessage = '';
  const permalink = (route, node) => '#atlas?' + new URLSearchParams({route, node});
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
      owner = key; history = []; future = []; lastHash = null;
      state = {node: m.routes[0].root, route: m.routes[0].id, layout: 'reading', pane: 'argument', trail: [], page: 0, parentPage: 0, overview: false, overviewPage: 0, scroll: 0};
    }
  }
  const childrenOf = (route, id) => Object.hasOwn(route.supports, id) ? route.supports[id] : [];
  function routeIndex(route) {
    if (routeIndexes.has(route)) return routeIndexes.get(route);
    const order = [], depth = new Map([[route.root, 0]]), parents = new Map();
    const pending = [route.root];
    for (let i = 0; i < pending.length; i++) {
      const id = pending[i]; order.push(id);
      for (const child of childrenOf(route, id)) {
        if (!parents.has(child)) parents.set(child, []);
        parents.get(child).push(id);
        if (!depth.has(child)) {depth.set(child, depth.get(id) + 1); pending.push(child);}
      }
    }
    const index = {order, depth, parents};
    routeIndexes.set(route, index);
    return index;
  }
  function syncAtlasLocation(data) {
    const m = model(data); if (!m) return;
    initialize(data, m);
    if (lastHash === location.hash) return;
    lastHash = location.hash;
    if (!lastHash.startsWith('#atlas?')) return;
    const params = new URLSearchParams(lastHash.slice(7));
    const route = m.routes.find(r => r.id === params.get('route')), node = params.get('node');
    if (!route || !routeIndex(route).depth.has(node)) {
      linkMessage = 'This argument link is not available in the current atlas route.';
      UI.mode = 'graph'; return;
    }
    if (state.node !== node || state.route !== route.id) {history.push(snapshot()); future = [];}
    state = {...state, node, route: route.id, pane: 'argument', layout: state.layout === 'graph' ? 'reading' : state.layout, trail: [], page: 0, parentPage: 0, overviewPage: Math.floor(routeIndex(route).order.indexOf(node) / OVERVIEW_SIZE), scroll: 0};
    linkMessage = ''; UI.mode = 'graph';
  }
  window.addEventListener('hashchange', () => {if (UI.data) {syncAtlasLocation(UI.data); workbench.render();}});
  const button = (label, action, value = '', extra = '') => `<button type="button" class="secondary" data-atlas="${action}" data-value="${esc(value)}" ${extra}>${esc(label)}</button>`;
  function sourcesHTML(n, m) {
    const statement = m.statements.get(n.statement);
    const refs = (statement?.lean || []).filter(ref => !n.declarations.length || n.declarations.includes(ref.name));
    return refs.map(ref => `<section class="atlas-reference"><h4>${esc(ref.name)}</h4><p class="small muted">${esc(ref.file)}:${esc(ref.line)}</p>${ref.note ? `<p>${inlineHTML(ref.note)}</p>` : ''}${ref.href ? `<a data-atlas-source="${esc(JSON.stringify([n.id, ref.name, ref.href]))}" href="${esc(ref.href)}" target="_blank" rel="noopener noreferrer">Open pinned source ↗</a>` : '<p class="small muted">Public source link unavailable.</p>'}</section>`).join('') || '<p>No mapped declaration references.</p>';
  }
  function explanation(n) {
    return `<div class="atlas-explanation"><h3>What this establishes</h3><p>${inlineHTML(n.goal)}</p><h3>Why it matters</h3><p>${inlineHTML(n.role)}</p><h3>Scope and assumptions</h3><p>${inlineHTML(n.scope)}</p><h3>Proof idea</h3><p>${inlineHTML(n.idea)}</p>${n.difference ? `<h3>Paper ↔ Lean</h3><p>${inlineHTML(n.difference)}</p>` : ''}</div>`;
  }
  function supportsHTML(n, m, route) {
    const ids = (childrenOf(route, n.id)), shown = ids.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
    const remaining = ids.length - shown.length;
    return `<section class="atlas-supports"><h3>Selected supporting arguments</h3><p class="small muted">Curated reading links; other proof steps may be omitted.</p>${shown.map(id => button(m.byId.get(id).label, 'node', id)).join('')}${!ids.length ? '<p class="small muted">Exploration boundary: no deeper explanation authored here. This does not mean no dependencies.</p>' : ''}${remaining > 0 ? `<p class="small muted">${remaining} supporting arguments outside this page.</p>` : ''}${state.page > 0 ? button('Previous arguments', 'page', String(state.page - 1)) : ''}${(state.page + 1) * PAGE_SIZE < ids.length ? button('More arguments', 'page', String(state.page + 1)) : ''}</section>`;
  }
  function backlinksHTML(n, m, route) {
    const parents = routeIndex(route).parents.get(n.id) || [];
    return `<section class="atlas-backlinks"><h3>Used by</h3><p class="small muted">In this curated route.</p>${parents.slice(state.parentPage * PAGE_SIZE, (state.parentPage + 1) * PAGE_SIZE).map(id => button(m.byId.get(id).label, 'node', id)).join('') || '<p class="small muted">This is the route’s result.</p>'}${state.parentPage ? button('Previous parents', 'parent-page', String(state.parentPage - 1)) : ''}${(state.parentPage + 1) * PAGE_SIZE < parents.length ? button('More parents', 'parent-page', String(state.parentPage + 1)) : ''}</section>`;
  }
  function overviewHTML(m, route) {
    const index = routeIndex(route), shown = index.order.slice(state.overviewPage * OVERVIEW_SIZE, (state.overviewPage + 1) * OVERVIEW_SIZE);
    const positions = new Map(shown.map((id, i) => [id, {x: 20 + Math.min(index.depth.get(id), 6) * 24, y: 20 + i * 52}]));
    const edges = shown.flatMap((id, lane) => (childrenOf(route, id)).filter(child => positions.has(child)).map(child => {
      const a = positions.get(id), b = positions.get(child);
      return `<path d="M 620 ${a.y + 17} H ${640 + lane * 5} V ${b.y + 17} H 620" marker-end="url(#atlas-arrowhead)"/>`;
    })).join('');
    const nodes = shown.map(id => {
      const n = m.byId.get(id), point = positions.get(id), shared = (index.parents.get(id)?.length || 0) > 1;
      const label = `${n.label}${shared ? ' · shared' : ''}`;
      return `<a href="${esc(permalink(route.id, id))}" data-atlas="node" data-value="${esc(id)}" aria-label="${esc(label)}"${id === state.node ? ' aria-current="true"' : ''}><${'title'}>${esc(label)} · depth ${index.depth.get(id)}</${'title'}><rect x="${point.x}" y="${point.y}" width="${620 - point.x}" height="34" rx="5"/><text x="${point.x + 10}" y="${point.y + 22}">${esc(label.length > Math.floor((600 - point.x) / 7) ? label.slice(0, Math.floor((600 - point.x) / 7) - 1) + '…' : label)}</text></a>`;
    }).join('');
    return `<section class="atlas-overview" aria-label="Route overview"><h3>Route overview</h3><p class="small muted">${index.order.length} arguments · lines follow selected support links, not extracted dependencies. Shared steps appear once. Page ${state.overviewPage + 1} of ${Math.ceil(index.order.length / OVERVIEW_SIZE)}; lines show links within this page. Use “Used by” for all parents.</p><div class="atlas-overview-scroll" data-route="${esc(route.id)}" data-page="${state.overviewPage}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 780 ${shown.length * 52 + 24}" aria-label="${esc(route.label)} argument map"><defs><marker id="atlas-arrowhead" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs><g class="atlas-overview-edges" aria-hidden="true">${edges}</g>${nodes}</svg></div>${state.overviewPage ? button('Previous map page', 'overview-page', String(state.overviewPage - 1)) : ''}${(state.overviewPage + 1) * OVERVIEW_SIZE < index.order.length ? button('Next map page', 'overview-page', String(state.overviewPage + 1)) : ''}</section>`;
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
    const supports = supportsHTML(n, m, route) + backlinksHTML(n, m, route);
    const problems = (data.walkthrough.problems || []).map(p => `<p class="banner quiet">${esc(p)}</p>`).join('');
    const oldOverview = root.querySelector('.atlas-overview-scroll');
    const overviewScroll = oldOverview?.dataset.route === route.id && oldOverview?.dataset.page === String(state.overviewPage) ? [oldOverview.scrollTop, oldOverview.scrollLeft] : [0, 0];
    root.innerHTML = `<section class="proof-graph-view proof-atlas" aria-label="Proof atlas"><header><div><h2>${esc(data.walkthrough.title)}</h2><p>Proof atlas · curated argument exploration</p></div></header>${problems}${!compatibility.supported ? `<p class="banner quiet">${esc(compatibility.message)}</p>` : ''}<div class="atlas-controls">${button('← Back', 'back', '', history.length ? '' : 'disabled')}${button('Forward →', 'forward', '', future.length ? '' : 'disabled')}<label>Route (starts at result) <${'select'} data-atlas-route>${m.routes.map(r => `<option value="${esc(r.id)}"${r.id === state.route ? ' selected' : ''}>${esc(r.label)}</option>`).join('')}</${'select'}></label>${button('Reading first', 'layout', 'reading', `aria-pressed="${state.layout === 'reading'}"`)}${button('Map first', 'layout', 'map', `aria-pressed="${state.layout === 'map'}"`)}${button('Statement graph', 'layout', 'graph')}${button(state.overview ? 'Hide overview' : 'Route overview', 'overview', '', `aria-expanded="${state.overview}"`)}</div>${state.overview ? overviewHTML(m, route) : ''}<p><a class="atlas-permalink" href="${esc(permalink(route.id, n.id))}">Link to this argument</a> ${button('Copy argument link', 'copy-link')}</p><p class="atlas-link-status small" role="status">${esc(linkMessage)}</p><nav class="atlas-trail" aria-label="Exploration trail">${historyTrail}</nav><h2 id="atlas-selected" tabindex="-1">${esc(n.label)}</h2><p class="small muted">${esc(route.label)} · curated, not extracted dependencies · source revision ${esc(m.commit)}</p><p class="small" role="status">${claimModel?.name ? `Mapped notebook statement ${esc(claimModel.name)}: ${esc(claimModel.status)}` : mappedClaim ? `Mapped notebook statement ${esc(mappedClaim)}: no matching notebook result` : 'No mapped saved observation'} · This argument is exposition; navigation grants no proof status.</p><div class="atlas-controls">${button('Argument', 'pane', 'argument', `aria-pressed="${state.pane === 'argument'}"`)}${button('Source references', 'pane', 'sources', `aria-pressed="${state.pane === 'sources'}"`)}${claimModel?.name ? `<button type="button" class="secondary" data-act="graph-node" data-graph-id="${esc(claimModel.id)}">Show in notebook</button>` : ''}</div><div class="atlas-body ${state.layout === 'map' ? 'atlas-map-first' : ''}">${state.layout === 'map' ? `<div class="atlas-neighborhood"><p class="small muted">Selected argument</p><div class="atlas-focus">${esc(n.label)}</div><p class="atlas-arrow" aria-hidden="true">↓</p>${supports}</div>${argument}` : `${argument}${supports}`}</div></section>`;
    const overview = root.querySelector('.atlas-overview-scroll');
    if (overview) {overview.scrollTop = overviewScroll[0]; overview.scrollLeft = overviewScroll[1];}
    workbench.typesetMath?.(root);
    return true;
  }
  async function go(action, value) {
    const m = model(UI.data); if (!m) return;
    initialize(UI.data, m);
    if (action === 'copy-link') {
      const fragment = permalink(state.route, state.node);
      try {
        // A webview URL is private and temporary. Copy a notebook-relative link.
        if (window.glitnirHost?.copyText) await window.glitnirHost.copyText(fragment);
        else await navigator.clipboard.writeText(new URL(fragment, location.href).href);
        linkMessage = window.glitnirHost ? 'Argument link copied. Use it in this notebook or its HTML export.' : 'Argument link copied.';
      } catch {linkMessage = 'Could not copy. Copy the argument link above manually.';}
      const status = root.querySelector('.atlas-link-status'); if (status) status.textContent = linkMessage;
      return;
    }
    const previous = snapshot();
    state = {...state, scroll: previous.scroll};
    if (action === 'back' || action === 'forward') {
      const from = action === 'back' ? history : future, to = action === 'back' ? future : history;
      if (!from.length) return;
      to.push(previous); state = from.pop();
    } else {
      history.push(previous); future = [];
      if (action === 'node' && m.byId.has(value)) state = {...state, node: value, trail: [...state.trail, state.node].slice(-24), pane: 'argument', page: 0, parentPage: 0, scroll: 0};
      if (action === 'trail') {const path = [...state.trail, state.node], i = Number(value); state = {...state, node: path[i], trail: path.slice(0, i), page: 0, parentPage: 0, scroll: 0};}
      if (action === 'overview') state = {...state, overview: !state.overview};
      if (action === 'parent-page') state = {...state, parentPage: Number(value)};
      if (action === 'overview-page') state = {...state, overviewPage: Number(value)};
      if (action === 'pane') state = {...state, pane: value};
      if (action === 'page') state = {...state, page: Number(value)};
      if (action === 'layout') state = {...state, layout: value, overview: value === 'map' || state.overview};
      if (action === 'route') {const r = m.routes.find(r => r.id === value); state = {...state, route: value, node: r.root, overviewPage: 0, trail: [], pane: 'argument', page: 0, parentPage: 0, scroll: 0};}
    }
    linkMessage = '';
    if (['node', 'trail', 'route', 'back', 'forward'].includes(action)) {
      const route = m.routes.find(r => r.id === state.route);
      state.overviewPage = Math.floor(routeIndex(route).order.indexOf(state.node) / OVERVIEW_SIZE);
      lastHash = permalink(state.route, state.node);
      window.history.replaceState(null, '', lastHash);
    }
    workbench.renderProofGraph();
    (root.querySelector('#atlas-selected') || root.querySelector('[data-atlas="layout"]'))?.focus({preventScroll: true});
    window.scrollTo?.(0, state.scroll);
  }
  root?.addEventListener('click', event => {const b = event.target.closest('[data-atlas]'); if (b) {event.preventDefault(); go(b.dataset.atlas, b.dataset.value);}});
  root?.addEventListener('change', event => {if (event.target.matches('[data-atlas-route]')) go('route', event.target.value);});
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#atlas?"]');
    if (!link || link.hasAttribute('data-atlas') || event.defaultPrevented) return;
    event.preventDefault();
    window.history.replaceState(null, '', link.getAttribute('href'));
    lastHash = null; syncAtlasLocation(UI.data); workbench.render();
    root.querySelector('#atlas-selected')?.focus({preventScroll: true});
  });
  function atlasStaticHTML(data) {
    const m = model(data); if (!m) return '';
    return `<section class="proof-atlas"><h2>Proof atlas</h2><p>Curated reading routes; these are not extracted Lean dependencies. Source revision ${esc(m.commit)}. Navigation grants no proof status.</p>${m.routes.map(r => `<nav aria-label="${esc(r.label)}"><h3>${esc(r.label)}</h3><a href="${esc(permalink(r.id, r.root))}">Start reading</a><ol>${routeIndex(r).order.map(id => `<li><a href="${esc(permalink(r.id, id))}">${esc(m.byId.get(id).label)}</a></li>`).join('')}</ol></nav>`).join('')}${m.nodes.map(n => `<details class="atlas-argument" id="atlas-${esc(n.id)}"><summary>${esc(n.label)}</summary>${m.routes.filter(r => routeIndex(r).depth.has(n.id)).map(r => `<span id="${esc(permalink(r.id, n.id).slice(1))}"></span>`).join('')}${explanation(n)}${sourcesHTML(n, m)}${m.routes.map(r => `<p>Used by (${esc(r.label)}): ${(routeIndex(r).parents.get(n.id) || []).map(id => `<a href="${esc(permalink(r.id, id))}">${esc(m.byId.get(id).label)}</a>`).join(', ') || 'No selected parent in this route.'}</p><h4>${esc(r.label)} — selected supporting arguments</h4>${((childrenOf(r, n.id))).map(id => `<p><a href="#atlas-${esc(id)}">${esc(m.byId.get(id).label)}</a></p>`).join('') || '<p>Exploration boundary; further dependencies may exist.</p>'}`).join('')}</details>`).join('')}</section>`;
  }
  Object.assign(workbench, {renderAtlas, atlasStaticHTML, syncAtlasLocation});
})();
