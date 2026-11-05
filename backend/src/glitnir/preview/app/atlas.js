// Curated argument exploration. This code never assigns proof status.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, esc, inlineHTML, walkthroughCompatibility} = workbench;
  const PAGE_SIZE = 12, OVERVIEW_SIZE = 24;
  let query = '', resultsPage = 0, detailsExpanded = false;
  const routeIndexes = new WeakMap();
  let state = null, history = [], future = [], owner = null, lastHash = null, linkMessage = '';
  const permalink = (route, node) => '#atlas?' + new URLSearchParams({route, node});
  const root = $('graph-view');
  const snapshot = () => ({...state, scroll: window.scrollY});
  function model(data) {
    const atlas = data.walkthrough?.atlas;
    if (!atlas?.nodes?.length || !atlas.routes?.length) return null;
    return {...atlas, byId: new Map(atlas.nodes.map(n => [n.id, n])), statements: new Map(data.walkthrough.nodes.map(n => [n.id, n]))};
  }
  function rememberPosition(data) {
    const key = data.walkthrough?.position_key;
    if (!key || workbench.linkedPath() || state.layout === 'graph') return;
    const record = JSON.stringify({key, revision: data.walkthrough.atlas_revision, route: state.route, node: state.node, layout: state.layout});
    try {
      if (window.glitnirHost) window.glitnirHost.saveAtlasPosition?.(record);
      else localStorage.setItem('glitnir.atlas.' + key, record);
    } catch { /* Storage is optional. */ }
  }
  function resumePosition(data, m) {
    const key = data.walkthrough.position_key;
    if (!key || workbench.linkedPath() || (location.hash && !location.hash.startsWith('#atlas?'))) return;
    try {
      const raw = window.glitnirHost ? window.glitnirHost.readAtlasPosition?.() : localStorage.getItem('glitnir.atlas.' + key);
      if (!raw) return;
      const record = JSON.parse(raw);
      if (record?.key !== key) return;
      const route = m.routes.find(r => r.id === record.route);
      if (location.hash) {
        if (record.revision === data.walkthrough.atlas_revision && record.layout === 'map') {
          state.layout = 'map';
        }
        return;
      }
      UI.mode = 'graph';
      if (record.revision !== data.walkthrough.atlas_revision || !route || !routeIndex(route).depth.has(record.node)) {
        linkMessage = 'The saved reading position belongs to an earlier atlas. Starting at the result.';
        return;
      }
      state.route = route.id; state.node = record.node;
      state.layout = record.layout === 'map' ? 'map' : 'reading';
      Object.assign(state, positionPages(route, state.node));
    } catch { /* Ignore unreadable storage. */ }
  }
  function initialize(data, m) {
    const key = JSON.stringify([data.walkthrough.position_key || data.file, data.walkthrough.atlas_revision || data.walkthrough.atlas]);
    if (owner !== key) {
      query = ''; resultsPage = 0; detailsExpanded = false; linkMessage = '';
      owner = key; history = []; future = []; lastHash = null;
      state = {node: m.routes[0].root, route: m.routes[0].id, layout: 'reading', overviewPage: 0, flowPage: 0, scroll: 0};
      resumePosition(data, m);
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
    const flow = [], flowSeen = new Set(), flowPending = [{id: route.root, depth: 0}];
    while (flowPending.length) {
      const entry = flowPending.pop();
      if (flowSeen.has(entry.id)) continue;
      flowSeen.add(entry.id); flow.push(entry);
      for (const id of [...childrenOf(route, entry.id)].reverse()) flowPending.push({id, depth: entry.depth + 1});
    }
    const index = {order, depth, parents, flow};
    routeIndexes.set(route, index);
    return index;
  }
  function positionPages(route, id) {
    const index = routeIndex(route);
    return {overviewPage: Math.floor(index.order.indexOf(id) / OVERVIEW_SIZE), flowPage: Math.floor(index.flow.findIndex(entry => entry.id === id) / OVERVIEW_SIZE)};
  }
  function syncAtlasLocation(data, preserveLayout = false) {
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
    state = {...state, node, route: route.id, layout: preserveLayout ? state.layout : state.layout === 'graph' ? 'reading' : state.layout, ...positionPages(route, node), scroll: 0};
    linkMessage = ''; UI.mode = 'graph'; rememberPosition(data);
  }
  window.addEventListener('hashchange', () => {if (UI.data) {syncAtlasLocation(UI.data); workbench.render();}});
  const button = (label, action, value = '', extra = '') => `<button type="button" class="secondary" data-atlas="${action}" data-value="${esc(value)}" ${extra}>${esc(label)}</button>`;
  function atlasViewControls(layout) {
    return `<div class="atlas-view-controls" role="group" aria-label="Exploration view">${button('Read', 'layout', 'reading', `aria-pressed="${layout === 'reading'}" aria-description="Read the explanation of the selected argument"`)}${button('Route map', 'layout', 'map', `aria-pressed="${layout === 'map'}" aria-description="See the curated support links in this reading route"`)}${button('Statement graph', 'layout', 'graph', `aria-pressed="${layout === 'graph'}" aria-description="See the notebook statements and their links"`)}</div>`;
  }
  function sourcesHTML(n, m) {
    const statement = m.statements.get(n.statement);
    const refs = (statement?.lean || []).filter(ref => !n.declarations.length || n.declarations.includes(ref.name));
    return refs.map(ref => `<section class="atlas-reference"><h4>${esc(ref.name)}</h4><p class="small muted">${esc(ref.file)}:${esc(ref.line)}</p>${ref.note ? `<p>${inlineHTML(ref.note)}</p>` : ''}${ref.href ? `<a data-atlas-source="${esc(JSON.stringify([n.id, ref.name, ref.href]))}" href="${esc(ref.href)}" target="_blank" rel="noopener noreferrer">Open pinned source ↗</a>` : '<p class="small muted">Public source link unavailable.</p>'}</section>`).join('') || '<p>No mapped declaration references.</p>';
  }
  function matchesHTML(m) {
    const term = query.trim().toLowerCase();
    if (!term) return '';
    const matches = m.nodes.filter(n => {
      const names = n.declarations.length ? n.declarations : (m.statements.get(n.statement)?.lean || []).map(ref => ref.name);
      return [n.label, n.goal, n.idea, n.scope, n.role, n.difference, ...names].some(field => field.toLowerCase().includes(term));
    });
    const start = resultsPage * PAGE_SIZE;
    const items = matches.slice(start, start + PAGE_SIZE).map(n => {
      const routes = m.routes.filter(r => routeIndex(r).depth.has(n.id));
      const links = routes.map(r => `<a href="${esc(permalink(r.id, n.id))}">${esc(n.label)} · ${esc(r.label)}</a>`).join(' · ');
      return `<li>${links || esc(n.label) + ' · outside all routes'}<p>${esc(n.goal.slice(0, 180))}</p></li>`;
    });
    return `<p role="status">${matches.length ? matches.length + ' matching arguments' : 'No matching arguments'}</p><ul>${items.join('')}</ul>${start ? button('Previous results', 'results-page', String(resultsPage - 1)) : ''}${start + PAGE_SIZE < matches.length ? button('More results', 'results-page', String(resultsPage + 1)) : ''}`;
  }
  function updateMatches() {
    const results = root.querySelector('.atlas-matches');
    if (results) results.innerHTML = matchesHTML(model(UI.data));
  }
  root?.addEventListener('input', event => {
    if (event.target.id !== 'atlas-query') return;
    query = event.target.value; resultsPage = 0; updateMatches();
  });
  function explanation(n) {
    return `<article class="atlas-explanation" aria-label="Argument explanation"><p class="atlas-goal">${inlineHTML(n.goal)}</p><p class="atlas-role">${inlineHTML(n.role)}</p><h3>Proof idea</h3><p>${inlineHTML(n.idea)}</p><aside class="atlas-context"><h3>Assumptions</h3><p>${inlineHTML(n.scope)}</p>${n.difference ? `<h3>Paper and Lean</h3><p>${inlineHTML(n.difference)}</p>` : ''}</aside></article>`;
  }
  function compareHTML(n, m, current) {
    const others = m.routes.filter(r => r.id !== current.id && routeIndex(r).depth.has(n.id));
    if (!others.length) return '';
    const links = others.map(r => `<a href="${esc(permalink(r.id, n.id))}">${esc(r.label)}</a>`);
    return `<p class="atlas-compare">View this argument in ${links.join(' · ')}</p>`;
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
    return `<section class="atlas-overview" aria-label="Route overview"><h3>Route overview</h3><p class="small muted">${index.order.length} arguments · lines follow selected support links, not extracted dependencies. Shared steps appear once. Page ${state.overviewPage + 1} of ${Math.ceil(index.order.length / OVERVIEW_SIZE)}; lines show links within this page.</p><div class="atlas-overview-scroll" data-route="${esc(route.id)}" data-page="${state.overviewPage}"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 780 ${shown.length * 52 + 24}" aria-label="${esc(route.label)} argument map"><defs><marker id="atlas-arrowhead" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs><g class="atlas-overview-edges" aria-hidden="true">${edges}</g>${nodes}</svg></div>${state.overviewPage ? button('Previous map page', 'overview-page', String(state.overviewPage - 1)) : ''}${(state.overviewPage + 1) * OVERVIEW_SIZE < index.order.length ? button('Next map page', 'overview-page', String(state.overviewPage + 1)) : ''}</section>`;
  }
  function atlasShell(data, content) {
    const m = model(data);
    if (!m) return content;
    initialize(data, m);
    const route = m.routes.find(r => r.id === state.route), index = routeIndex(route);
    const start = state.flowPage * OVERVIEW_SIZE;
    const choices = m.routes.map(r => button(r.id === 'lean' ? 'Lean proof' : r.id === 'paper' ? 'Paper proof' : r.label, 'route', r.id, `aria-pressed="${r.id === route.id}"`)).join('');
    const steps = index.flow.slice(start, start + OVERVIEW_SIZE).map(entry => {
      const id = entry.id;
      const shared = (index.parents.get(id) || []).length > 1;
      return `<li style="padding-inline-start:${Math.min(entry.depth, 6) * 10}px">${button(m.byId.get(id).label, 'node', id, `aria-current="${id === state.node ? 'step' : 'false'}"`)}${shared || entry.depth > 6 ? `<span class="small muted">${entry.depth > 6 ? 'Depth ' + entry.depth : ''}${shared ? (entry.depth > 6 ? ' · ' : '') + 'Shared support' : ''}</span>` : ''}</li>`;
    }).join('');
    const previous = history.at(-1), next = future.at(-1);
    const visitLabel = entry => `${m.byId.get(entry.node).label} · ${entry.layout === 'reading' ? 'Read' : entry.layout === 'map' ? 'Route map' : 'Statement graph'}${entry.route !== route.id ? ' · ' + m.routes.find(r => r.id === entry.route).label : ''}`;
    const backLabel = previous ? 'Back to ' + visitLabel(previous) : 'Back';
    const forwardLabel = next ? 'Forward to ' + visitLabel(next) : 'Forward';
    return `<div class="atlas-shell"><aside class="atlas-sidebar" aria-label="Proof navigation"><h3>Follow the proof</h3><div class="atlas-search"><label for="atlas-query">Search proof</label><input id="atlas-query" type="search" maxlength="256" placeholder="Arguments or Lean names" value="${esc(query)}"><div class="atlas-matches">${matchesHTML(m)}</div></div><div class="atlas-route-controls" role="group" aria-label="Reading route">${choices}</div><p class="small muted">Each route follows its own explanation of the result.</p><nav class="atlas-flow" aria-label="Reading route steps"><h3>${esc(route.label)}</h3><p class="small muted">${index.order.length} arguments · current step highlighted</p><ol>${steps}</ol>${start ? button('Earlier steps', 'flow-page', String(state.flowPage - 1)) : ''}${start + OVERVIEW_SIZE < index.order.length ? button('More steps', 'flow-page', String(state.flowPage + 1)) : ''}</nav><div class="atlas-visit-history"><h3>Navigation history</h3>${button(backLabel, 'back', '', history.length ? '' : 'disabled')}${button(forwardLabel, 'forward', '', future.length ? '' : 'disabled')}</div></aside><div class="atlas-content">${content}</div></div>`;
  }
  function renderAtlas(data, {openClaimGraph = false, openExplore = false} = {}) {
    const m = model(data); if (!m) return false;
    initialize(data, m);
    if (openClaimGraph) state.layout = 'graph';
    if (openExplore && state.layout === 'graph') state.layout = 'reading';
    if (state.layout === 'graph') return false;
    const n = m.byId.get(state.node), route = m.routes.find(r => r.id === state.route);
    const compatibility = walkthroughCompatibility(data);
    const known = m.statements.get(n.statement);
    const mappedClaim = known?.claim;
    const claimModel = mappedClaim ? workbench.walkthroughGraphModel(data, workbench.walkthroughId(n.statement)).nodes.find(node => node.outlineId === n.statement) : null;
    const problems = (data.walkthrough.problems || []).map(p => `<p class="banner quiet">${esc(p)}</p>`).join('');
    const sidebarScroll = root.querySelector('.atlas-sidebar')?.scrollTop || 0;
    const detailsOpen = root.querySelector('.atlas-details')?.open ?? detailsExpanded;
    detailsExpanded = detailsOpen;
    const oldOverview = root.querySelector('.atlas-overview-scroll');
    const overviewScroll = oldOverview?.dataset.route === route.id && oldOverview?.dataset.page === String(state.overviewPage) ? [oldOverview.scrollTop, oldOverview.scrollLeft] : [0, 0];
    root.innerHTML = atlasShell(data, `<section class="proof-graph-view proof-atlas ${state.layout === 'reading' ? 'atlas-reading' : ''}" aria-label="Proof atlas">${problems}<div class="atlas-controls atlas-toolbar">${atlasViewControls(state.layout)}</div><p class="atlas-view-help small muted">${state.layout === 'map' ? 'Route map shows the curated support links in this reading route. Select an argument to read it.' : 'Read explains the argument selected in the sidebar. Route map shows the reading route; Statement graph shows the notebook statements and their links.'}</p><h2 id="atlas-selected" tabindex="-1">${esc(n.label)}</h2>${compareHTML(n, m, route)}${state.layout === 'map' ? overviewHTML(m, route) : ''}<div class="atlas-body ${state.layout === 'map' ? 'atlas-map-first' : ''}">${explanation(n)}</div><details class="atlas-details"${detailsOpen ? ' open' : ''}><summary>Sources and details</summary><p class="small" role="status">${claimModel?.name ? `Mapped notebook statement ${esc(claimModel.name)}: ${esc(claimModel.status)}` : mappedClaim ? `Mapped notebook statement ${esc(mappedClaim)}: no matching notebook result` : 'No mapped saved observation'} · This argument is exposition; navigation grants no proof status.</p>${!compatibility.supported ? `<p class="small muted">${esc(compatibility.message)}</p>` : ''}${sourcesHTML(n, m)}${claimModel?.name ? `<button type="button" class="secondary" data-act="graph-node" data-graph-id="${esc(claimModel.id)}">Show in notebook</button>` : ''}<p class="small muted">Source revision ${esc(m.commit)}</p><p><a class="atlas-permalink" href="${esc(permalink(route.id, n.id))}">Link to this argument</a> ${button('Copy argument link', 'copy-link')}</p></details><p class="atlas-link-status small" role="status">${esc(linkMessage)}</p></section>`);
    root.querySelector('.atlas-sidebar').scrollTop = sidebarScroll;
    const overview = root.querySelector('.atlas-overview-scroll');
    if (overview) {overview.scrollTop = overviewScroll[0]; overview.scrollLeft = overviewScroll[1];}
    workbench.typesetMath?.(root);
    return true;
  }
  async function go(action, value) {
    const m = model(UI.data); if (!m) return;
    initialize(UI.data, m);
    if (action === 'results-page') {
      resultsPage = Number(value); updateMatches();
      root.querySelector('.atlas-matches a')?.focus();
      return;
    }
    if (action === 'copy-link') {
      const fragment = permalink(state.route, state.node);
      try {
        // A webview URL is private and temporary. Copy a notebook-relative link.
        if (window.glitnirHost?.copyText) await window.glitnirHost.copyText(fragment);
        else await navigator.clipboard.writeText(new URL(fragment, location.href).href);
        linkMessage = window.glitnirHost ? 'Argument link copied. Use it in this notebook or its HTML export.' : 'Argument link copied.';
      } catch {linkMessage = 'Could not copy. Copy the link in Sources and details manually.'; const details = root.querySelector('.atlas-details'); if (details) details.open = true;}
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
      if (action === 'node' && m.byId.has(value)) state = {...state, node: value, scroll: 0};
      if (action === 'flow-page') state = {...state, flowPage: Number(value)};
      if (action === 'overview-page') state = {...state, overviewPage: Number(value)};
      if (action === 'layout') { state = {...state, layout: value}; UI.graphFocused = false; }
      if (action === 'route') {const r = m.routes.find(r => r.id === value); state = {...state, route: value, node: r.root, overviewPage: 0, scroll: 0};}
    }
    linkMessage = '';
    if (['node', 'route', 'back', 'forward'].includes(action)) {
      const route = m.routes.find(r => r.id === state.route);
      Object.assign(state, positionPages(route, state.node));
      lastHash = permalink(state.route, state.node);
      window.history.replaceState(null, '', lastHash);
    }
    if (state.layout === 'graph' && ['node', 'route', 'back', 'forward', 'layout'].includes(action)) {
      UI.graphSelected = workbench.walkthroughId(m.byId.get(state.node).statement);
    }
    rememberPosition(UI.data);
    workbench.render();
    (root.querySelector('#atlas-selected') || root.querySelector('[data-atlas="layout"][aria-pressed="true"]'))?.focus({preventScroll: true});
    window.scrollTo?.(0, state.scroll);
  }
  root?.addEventListener('toggle', event => {
    if (event.target.isConnected && event.target.matches('.atlas-details')) detailsExpanded = event.target.open;
  }, true);
  root?.addEventListener('click', event => {const b = event.target.closest('[data-atlas]'); if (b) {event.preventDefault(); go(b.dataset.atlas, b.dataset.value);}});
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href^="#atlas?"]');
    if (!link || link.hasAttribute('data-atlas') || event.defaultPrevented) return;
    event.preventDefault();
    const searchLayout = link.closest('.atlas-matches') ? state.layout : null;
    if (searchLayout) {query = ''; resultsPage = 0;}
    window.history.replaceState(null, '', link.getAttribute('href'));
    lastHash = null; syncAtlasLocation(UI.data, Boolean(searchLayout));
    if (searchLayout === 'graph') UI.graphSelected = workbench.walkthroughId(model(UI.data).byId.get(state.node).statement);
    workbench.render();
    const heading = root.querySelector('#atlas-selected');
    heading?.focus({preventScroll: true});
    heading?.scrollIntoView?.({block: 'nearest'});
  });
  function atlasStaticHTML(data) {
    const m = model(data); if (!m) return '';
    const routeMenus = m.routes.map(r => {
      const items = routeIndex(r).order.map(id => `<li><a href="${esc(permalink(r.id, id))}">${esc(m.byId.get(id).label)}</a></li>`);
      return `<nav aria-label="${esc(r.label)}"><h3>${esc(r.label)}</h3><a href="${esc(permalink(r.id, r.root))}">Start reading</a><ol>${items.join('')}</ol></nav>`;
    });
    const argumentsHTML = m.nodes.map(n => {
      const contexts = m.routes.filter(r => routeIndex(r).depth.has(n.id)).map(r => {
        const links = ids => ids.map(id => `<a href="${esc(permalink(r.id, id))}">${esc(m.byId.get(id).label)}</a>`).join(', ');
        const parents = links(routeIndex(r).parents.get(n.id) || []);
        const children = links(childrenOf(r, n.id));
        return `<section id="${esc(permalink(r.id, n.id).slice(1))}"><h4>${esc(n.label)} · ${esc(r.label)}</h4>${compareHTML(n, m, r)}<p>Used by: ${parents || 'Route result'}</p><p>Selected supporting arguments: ${children || 'Exploration boundary; further dependencies may exist.'}</p></section>`;
      });
      return `<details class="atlas-argument" id="atlas-${esc(n.id)}"><summary>${esc(n.label)}</summary>${explanation(n)}${sourcesHTML(n, m)}${contexts.join('')}</details>`;
    });
    return `<section class="proof-atlas"><h2>Proof atlas</h2><p>Curated reading routes; these are not extracted Lean dependencies. Source revision ${esc(m.commit)}. Navigation grants no proof status.</p>${routeMenus.join('')}${argumentsHTML.join('')}</section>`;
  }
  Object.assign(workbench, {atlasShell, atlasViewControls, renderAtlas, atlasStaticHTML, syncAtlasLocation});
})();
