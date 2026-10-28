// Recorded proof dependencies as a local, presentation-only graph.
// The graph never checks Lean or changes a notebook record.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, claimLabel, esc, projectClaims, recorded, statusOf, title} = workbench;

  const shortName = name => String(name).split('.').pop();
  const importedTone = status => ({checked: 'green', refuted: 'red', conditional: 'amber', stale: 'amber', failed: 'red', open: 'blue'})[status] || 'muted';
  const importedLabel = status => ({checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', failed: 'Failed', open: 'Open goal'})[status] || 'Unknown';
  const localId = name => `local:${name}`;
  const importId = (module, name) => `import:${module}:${name}`;
  const MAX_GRAPH_NODES = 240;
  const MAX_GRAPH_EDGES = 2400;

  function legacyImport(data, use) {
    for (const link of data.links || []) {
      const found = Array.isArray(link.claims) && link.claims.find(entry => entry.use === use);
      if (found) return {module: link.module, name: found.name || use.slice(use.indexOf(':') + 1), path: link.file, status: found.status};
    }
    const split = use.indexOf(':');
    return {module: use.slice(0, split), name: use.slice(split + 1), path: null, status: null};
  }

  function claimGraphModel(data) {
    const project = data.preview?.mode === 'project';
    const rendered = project ? projectClaims(data) : (Array.isArray(data.claims) ? data.claims : []);
    const claimsByName = new Map(rendered.map(entry => [entry.name, entry]));
    const sourceNames = project
      ? (data.cells || []).filter(cell => cell.kind === 'source' && typeof cell.claim === 'string' && cell.claim).map(cell => cell.claim)
      : rendered.map(entry => entry.name);
    const savedByName = new Map((data.project_claims || []).filter(saved => typeof saved?.name === 'string').map(saved => [saved.name, saved]));
    const allNames = [...new Set([...sourceNames, ...rendered.map(entry => entry.name), ...savedByName.keys()])];
    const orderedNames = allNames.slice(0, MAX_GRAPH_NODES);
    const omittedNodeIds = new Set(allNames.slice(MAX_GRAPH_NODES).map(localId));
    let omittedEdges = 0, dependencyCount = 0;
    const nodes = [];
    const byId = new Map();
    const add = node => {
      if (byId.has(node.id)) return byId.get(node.id);
      nodes.push(node); byId.set(node.id, node); return node;
    };
    for (const name of orderedNames) {
      const entry = claimsByName.get(name);
      const saved = savedByName.get(name);
      // The graph and its export describe the saved snapshot. A live checking
      // badge must never replace the recorded status here.
      const hasLegacyRecord = project || entry?.status === 'stale' || Boolean(entry?.record || entry?.refutation);
      const status = entry && hasLegacyRecord ? statusOf(entry, true) : {key: 'unknown', label: 'Unknown', tone: 'muted'};
      const stale = saved?.current === false || (!project && entry?.status === 'stale');
      add({id: localId(name), name, label: entry ? claimLabel(entry) : shortName(name), kind: 'local', path: null,
        fullLabel: name, tone: stale ? 'amber' : status.key === 'unknown' ? 'muted' : status.tone, status: status.label, stale,
        relationsKnown: project ? Array.isArray(saved?.uses) : hasLegacyRecord && Array.isArray(entry?.uses),
        truncated: project ? saved?.uses_truncated === true : entry?.uses_truncated === true, malformed: false});
    }

    const dependencies = new Map(nodes.map(node => [node.id, []]));
    const unavailable = new Map();
    const addDependency = (user, dependency) => {
      const list = dependencies.get(user.id);
      if (!list || list.includes(dependency.id)) return;
      if (dependencyCount >= MAX_GRAPH_EDGES) omittedEdges += 1;
      else { list.push(dependency.id); dependencyCount += 1; }
    };
    for (const user of nodes.filter(node => node.kind === 'local')) {
      const entry = claimsByName.get(user.name);
      const saved = savedByName.get(user.name);
      const raw = project ? saved?.uses : entry?.uses;
      if (!Array.isArray(raw)) {
        if (raw !== undefined && raw !== null) user.malformed = true;
        continue;
      }
      for (const use of raw) {
        if (project) {
          if (!use || typeof use !== 'object' || typeof use.name !== 'string' || typeof use.module !== 'string' || typeof use.in_notebook !== 'boolean') {
            user.malformed = true; continue;
          }
          if (use.in_notebook) {
            const target = byId.get(localId(use.name));
            if (target) addDependency(user, target); else unavailable.set(user.id, (unavailable.get(user.id) || 0) + 1);
            continue;
          }
          const id = importId(use.module, use.name);
          let target = byId.get(id);
          if (!target && nodes.length < MAX_GRAPH_NODES) target = add({id, name: use.name, label: `${use.module}:${shortName(use.name)}`, fullLabel: `${use.module}:${use.name}`, kind: 'imported', path: typeof use.path === 'string' && use.path ? use.path : null,
            module: use.module, tone: 'muted', status: 'Unknown', stale: false, relationsKnown: false, truncated: false, malformed: false});
          if (!target) { omittedNodeIds.add(id); omittedEdges += 1; continue; }
          if (!dependencies.has(id)) dependencies.set(id, []);
          addDependency(user, target);
          continue;
        }
        if (typeof use !== 'string' || !use) { user.malformed = true; continue; }
        if (!use.includes(':')) {
          const target = byId.get(localId(use));
          if (target) addDependency(user, target); else unavailable.set(user.id, (unavailable.get(user.id) || 0) + 1);
          continue;
        }
        const found = legacyImport(data, use);
        const id = importId(found.module, found.name);
        let target = byId.get(id);
        if (!target && nodes.length < MAX_GRAPH_NODES) target = add({id, name: found.name, label: `${found.module}:${shortName(found.name)}`, fullLabel: `${found.module}:${found.name}`, kind: 'imported', path: found.path,
          module: found.module, tone: importedTone(found.status), status: importedLabel(found.status), stale: found.status === 'stale', relationsKnown: false, truncated: false, malformed: false});
        if (!target) { omittedNodeIds.add(id); omittedEdges += 1; continue; }
        if (!dependencies.has(id)) dependencies.set(id, []);
        addDependency(user, target);
      }
    }

    const reachCache = new Map();
    const reachable = start => {
      if (reachCache.has(start)) return reachCache.get(start);
      const seen = new Set(), todo = [...(dependencies.get(start) || [])];
      while (todo.length) {
        const current = todo.pop();
        if (seen.has(current)) continue;
        seen.add(current);
        todo.push(...(dependencies.get(current) || []));
      }
      reachCache.set(start, seen);
      return seen;
    };
    // Transitive reduction is unambiguous for the expected acyclic proof
    // relation. Preserve every saved edge if hostile input contains a cycle;
    // reducing through a strongly connected component can erase all choices.
    const cyclic = nodes.some(node => reachable(node.id).has(node.id));
    const direct = new Map();
    for (const [user, own] of dependencies) {
      direct.set(user, cyclic ? [...own] : own.filter(dependency => !own.some(other => other !== dependency && reachable(other).has(dependency))));
    }
    const edges = [];
    for (const [user, own] of direct) for (const dependency of own) edges.push({from: dependency, to: user});
    const users = new Map(nodes.map(node => [node.id, []]));
    for (const edge of edges) users.get(edge.from)?.push(edge.to);
    for (const node of nodes) {
      node.uses = direct.get(node.id) || [];
      node.usedBy = users.get(node.id) || [];
      node.unavailable = unavailable.get(node.id) || 0;
    }
    return {nodes, byId, edges, dependencies: direct, omittedNodes: omittedNodeIds.size, omittedEdges, cyclic};
  }

  function graphLayout(model) {
    const depth = new Map(model.nodes.map(node => [node.id, 0]));
    const indegree = new Map(model.nodes.map(node => [node.id, (model.dependencies.get(node.id) || []).length]));
    const users = new Map(model.nodes.map(node => [node.id, []]));
    for (const [user, own] of model.dependencies) for (const dependency of own) users.get(dependency)?.push(user);
    const ready = model.nodes.filter(node => indegree.get(node.id) === 0).map(node => node.id);
    while (ready.length) {
      const current = ready.shift();
      for (const user of users.get(current) || []) {
        depth.set(user, Math.max(depth.get(user), depth.get(current) + 1));
        indegree.set(user, indegree.get(user) - 1);
        if (indegree.get(user) === 0) ready.push(user);
      }
    }
    const rows = new Map();
    for (const node of model.nodes) {
      const level = depth.get(node.id) || 0;
      if (!rows.has(level)) rows.set(level, []);
      rows.get(level).push(node.id);
    }
    for (let pass = 0; pass < 6; pass++) {
      for (const level of [...rows.keys()].sort((a, b) => a - b)) {
        const below = new Map((rows.get(level - 1) || []).map((id, index) => [id, index]));
        const above = new Map((rows.get(level + 1) || []).map((id, index) => [id, index]));
        const score = id => {
          const refs = (model.dependencies.get(id) || []).filter(other => below.has(other)).map(other => below.get(other));
          refs.push(...(model.byId.get(id).usedBy || []).filter(other => above.has(other)).map(other => above.get(other)));
          return refs.length ? refs.reduce((sum, value) => sum + value, 0) / refs.length : 0;
        };
        rows.get(level).sort((a, b) => score(a) - score(b));
      }
    }
    const widest = Math.max(1, ...[...rows.values()].map(row => row.length));
    const width = Math.max(760, widest * 220), rowHeight = 112, top = 54;
    const topLevel = Math.max(0, ...rows.keys()), positions = new Map();
    for (const [level, row] of rows) {
      const gap = width / (row.length + 1);
      row.forEach((id, index) => positions.set(id, {x: gap * (index + 1), y: top + (topLevel - level) * rowHeight}));
    }
    return {positions, width, height: top + (topLevel + 1) * rowHeight};
  }

  const nodeList = (ids, model) => ids.length ? ids.map(id => esc(model.byId.get(id)?.fullLabel || model.byId.get(id)?.name || id)).join(', ') : 'None';
  function graphDetailHTML(node, model, staticPage) {
    if (!node) return '<h3>Pick a statement</h3><p class="muted">Use Statement details to read its saved relationships.</p>';
    const action = !staticPage && node.kind === 'local'
      ? `<button type="button" class="primary" data-act="graph-node" data-graph-id="${esc(node.id)}">Show in Document</button>`
      : !staticPage && node.path ? `<button type="button" class="primary" data-act="graph-node" data-graph-id="${esc(node.id)}">Open notebook</button>` : '';
    const note = node.relationsKnown ? '' : '<p class="small muted">Dependency observations are unknown for this statement.</p>';
    const truncated = node.truncated ? '<p class="small warn-text">The saved check recorded only the first uses; more dependencies may exist.</p>' : '';
    const malformed = node.malformed ? '<p class="small warn-text">Malformed dependency observations were ignored.</p>' : '';
    const unavailable = node.unavailable ? `<p class="small muted">${node.unavailable} saved use could not be matched to a statement in this notebook.</p>` : '';
    return `<h3 tabindex="-1">${esc(node.fullLabel || node.name)}</h3><p><span class="pill" data-tone="${esc(node.tone)}">${esc(node.status)}</span>${node.stale && node.status !== 'Out of date' ? ' <span class="pill" data-tone="amber">Out of date</span>' : ''}</p>
      <dl><dt>Uses</dt><dd>${nodeList(node.uses, model)}</dd><dt>Used by</dt><dd>${nodeList(node.usedBy, model)}</dd></dl>${note}${truncated}${malformed}${unavailable}${action}`;
  }

  function proofGraphHTML(data, {staticPage = false, selected = null, zoom = 1} = {}) {
    const model = claimGraphModel(data), layout = graphLayout(model), boxWidth = 188, boxHeight = 50;
    const marker = staticPage ? 'proof-arrow-static' : 'proof-arrow';
    const paths = model.edges.map(edge => {
      const from = layout.positions.get(edge.from), to = layout.positions.get(edge.to);
      const start = from.y - boxHeight / 2, end = to.y + boxHeight / 2, mid = (start + end) / 2;
      return `<path class="proof-edge" data-from="${esc(edge.from)}" data-to="${esc(edge.to)}" d="M${from.x},${start} C${from.x},${mid} ${to.x},${mid} ${to.x},${end + 6}" marker-end="url(#${marker})"/>`;
    }).join('');
    const nodes = model.nodes.map(node => {
      const point = layout.positions.get(node.id), chosen = node.id === selected ? ' chosen' : '';
      const fullLabel = node.fullLabel || node.name;
      const nodeAction = node.kind === 'local' ? 'Open in Document' : node.path ? 'Open notebook' : 'Show details';
      const attrs = staticPage ? '' : ` data-act="graph-node" data-graph-id="${esc(node.id)}" tabindex="0" role="button" aria-label="${esc(fullLabel)}, ${esc(node.status)}. ${nodeAction}"`;
      return `<g class="proof-node${chosen}" data-graph-name="${esc(node.kind === 'imported' ? node.label : node.name)}" data-kind="${node.kind}" data-tone="${esc(node.tone)}" transform="translate(${point.x},${point.y})"${attrs}>
        <rect x="${-boxWidth / 2}" y="${-boxHeight / 2}" width="${boxWidth}" height="${boxHeight}" rx="8"/><circle class="proof-node-status" cx="${-boxWidth / 2 + 15}" cy="0" r="5"/>
        <title>${esc(fullLabel)} — ${esc(node.status)}</title><text class="proof-node-label" text-anchor="middle" y="-3">${esc(node.label.length > 27 ? node.label.slice(0, 26) + '…' : node.label)}</text><text class="proof-node-meta" text-anchor="middle" y="15">${esc(node.kind === 'imported' ? 'Imported · ' + node.status : node.status + (node.stale && node.status !== 'Out of date' ? ' · Out of date' : ''))}</text></g>`;
    }).join('');
    const unknown = model.nodes.filter(node => !node.relationsKnown).map(node => node.label);
    const malformed = model.nodes.filter(node => node.malformed).map(node => node.label);
    const truncated = model.nodes.filter(node => node.truncated).map(node => node.fullLabel || node.name);
    const explanation = unknown.length ? `<p class="small muted graph-observation-note">Dependency observations are unknown for ${esc(unknown.join(', '))}.</p>` : '';
    const malformedNote = malformed.length ? `<p class="small warn-text">Malformed dependency observations were ignored for ${esc(malformed.join(', '))}.</p>` : '';
    const truncatedNote = truncated.length ? `<p class="small warn-text">The saved check recorded only the first uses for ${esc(truncated.join(', '))}; more dependencies may exist.</p>` : '';
    const cyclicNote = model.cyclic ? '<p class="small warn-text">A dependency cycle was recorded. All saved relationships are shown without transitive reduction.</p>' : '';
    const bounded = model.omittedNodes || model.omittedEdges ? `<p class="small warn-text">Graph limit reached: ${model.omittedNodes ? `${model.omittedNodes} additional node${model.omittedNodes === 1 ? '' : 's'}` : ''}${model.omittedNodes && model.omittedEdges ? ' and ' : ''}${model.omittedEdges ? `${model.omittedEdges} additional relationship${model.omittedEdges === 1 ? '' : 's'}` : ''} omitted.</p>` : '';
    const toolbar = staticPage ? '' : `<div class="proof-graph-toolbar" aria-label="Graph size"><button type="button" class="secondary" data-act="graph-zoom-out" aria-label="Zoom out">−</button><button type="button" class="secondary" data-act="graph-fit">Fit width</button><button type="button" class="secondary" data-act="graph-zoom-in" aria-label="Zoom in">+</button></div>`;
    const detailOptions = model.nodes.map(node => `<option value="${esc(node.id)}"${node.id === selected ? ' selected' : ''}>${esc(node.fullLabel || node.name)} — ${esc(node.status)}</option>`).join('');
    const picker = '<se' + `lect data-act="graph-detail-picker">${detailOptions}</se` + 'lect>';
    const detail = staticPage ? '' : `<aside class="proof-graph-detail" id="graph-detail" aria-live="polite"><label class="proof-graph-detail-picker">Statement details${picker}</label>${graphDetailHTML(model.byId.get(selected), model, false)}</aside>`;
    const svgRole = staticPage ? ' role="img"' : ' role="group"';
    const svgWidth = staticPage ? layout.width : `${Math.round(zoom * 100)}%`;
    return `<section class="proof-graph-view${staticPage ? ' proof-graph-export' : ''}" aria-label="Recorded proof graph"><header><div><h2>Recorded proof graph</h2><p>Arrows run from a statement to each statement that directly uses it. Foundations are at the bottom; results rise to the top. This view does not check or update results.</p></div>${toolbar}</header>
      <div class="proof-graph-layout"><div class="proof-graph-canvas"><svg class="proof-map" width="${svgWidth}" viewBox="0 0 ${layout.width} ${layout.height}"${svgRole} aria-label="Recorded statement dependencies"><defs><marker id="${marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>${paths}${nodes}</svg></div>${detail}</div>${explanation}${truncatedNote}${malformedNote}${cyclicNote}${bounded}${staticPage ? '<p class="small muted">Static copy of the saved graph; it does not check or update results.</p>' : ''}</section>`;
  }

  function renderProofGraph() {
    const root = document.getElementById('graph-view');
    if (!root || !UI.data) return;
    const model = claimGraphModel(UI.data);
    if (!model.byId.has(UI.graphSelected)) UI.graphSelected = model.byId.has(localId(UI.selected)) ? localId(UI.selected) : model.nodes[0]?.id || null;
    root.innerHTML = proofGraphHTML(UI.data, {selected: UI.graphSelected, zoom: UI.graphZoom || 1});
  }
  const graphNode = id => claimGraphModel(UI.data).byId.get(id);

  Object.assign(workbench, {claimGraphModel, graphDetailHTML, graphNode, proofGraphHTML, renderProofGraph});
})();
