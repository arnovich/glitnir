// Recorded proof dependencies as a local, presentation-only graph.
// The graph never checks Lean or changes a notebook record.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, claimLabel, esc, inlineHTML, projectClaims, recorded, statusOf, title, walkthroughCompatibility} = workbench;

  const shortName = name => String(name).split('.').pop();
  const importedTone = status => ({checked: 'green', refuted: 'red', conditional: 'amber', stale: 'amber', failed: 'red', open: 'blue'})[status] || 'muted';
  const importedLabel = status => ({checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', failed: 'Failed', open: 'Open goal'})[status] || 'Unknown';
  const localId = name => `local:${name}`;
  const importId = (module, name) => `import:${module}:${name}`;
  const STATEMENT_KINDS = new Set(['definition', 'lemma', 'proposition', 'theorem', 'corollary', 'algorithm', 'appendix']);
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

  function finalizeGraph(nodes, byId, dependencies, unavailable = new Map(), extra = {}) {
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
    // relation. Preserve every edge if hostile input contains a cycle.
    const cyclic = nodes.some(node => reachable(node.id).has(node.id));
    const direct = new Map();
    for (const [user, own] of dependencies) {
      direct.set(user, cyclic ? [...own] : own.filter(dependency => !own.some(other => other !== dependency && reachable(other).has(dependency))));
    }
    const edges = [];
    for (const [user, own] of direct) for (const dependency of own) {
      edges.push({from: dependency, to: user, appendix: byId.get(user)?.statementKind === 'appendix'});
    }
    const users = new Map(nodes.map(node => [node.id, []]));
    for (const edge of edges) users.get(edge.from)?.push(edge.to);
    for (const node of nodes) {
      node.uses = direct.get(node.id) || [];
      node.usedBy = users.get(node.id) || [];
      node.unavailable = unavailable.get(node.id) || 0;
    }
    return {nodes, byId, edges, dependencies: direct, cyclic, ...extra};
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

    return finalizeGraph(nodes, byId, dependencies, unavailable, {omittedNodes: omittedNodeIds.size, omittedEdges, walkthrough: false});
  }

  const walkthroughId = id => `walkthrough:${id}`;
  function walkthroughGraphModel(data) {
    const outline = data.walkthrough || {};
    const compatibility = walkthroughCompatibility(data);
    const allNodes = Array.isArray(outline.nodes) ? outline.nodes : [];
    const rawNodes = allNodes.slice(0, MAX_GRAPH_NODES);
    const rendered = data.preview?.mode === 'project' ? projectClaims(data) : (Array.isArray(data.claims) ? data.claims : []);
    const sourceClaims = (data.cells || []).filter(cell => typeof cell?.claim === 'string' && cell.claim)
      .map(cell => ({name: cell.claim, label: shortName(cell.claim), sourceOnly: true, start: cell.start}));
    const walkthroughClaims = [...rendered, ...sourceClaims.filter(source => !rendered.some(entry => entry.name === source.name))]
      .filter(entry => typeof entry?.name === 'string');
    const savedByName = new Map((data.project_claims || []).filter(saved => typeof saved?.name === 'string').map(saved => [saved.name, saved]));
    const matchingClaim = requested => {
      if (!requested) return null;
      const exact = walkthroughClaims.find(entry => entry.name === requested);
      if (exact) return exact;
      const suffix = walkthroughClaims.filter(entry => entry.name.endsWith(`.${requested}`));
      return suffix.length === 1 ? suffix[0] : null;
    };
    const nodes = [], byId = new Map(), claimProblems = [];
    for (const raw of rawNodes) {
      if (!raw || typeof raw.id !== 'string' || !raw.id || byId.has(walkthroughId(raw.id))) continue;
      const matchedClaim = matchingClaim(typeof raw.claim === 'string' ? raw.claim : '');
      const saved = matchedClaim ? savedByName.get(matchedClaim.name) : null;
      const hasRecord = data.preview?.mode === 'project' ? Boolean(saved) : Boolean(matchedClaim?.record || matchedClaim?.refutation || matchedClaim?.status === 'stale');
      const status = matchedClaim && hasRecord ? statusOf(matchedClaim, true, data) : {key: 'unknown', label: 'Unknown', tone: 'muted'};
      const stale = compatibility.supported && (saved?.current === false || matchedClaim?.status === 'stale');
      if (raw.claim && !matchedClaim) claimProblems.push(`No saved walkthrough result matches ${raw.claim}.`);
      const statementKind = STATEMENT_KINDS.has(raw.kind) ? raw.kind : 'unknown';
      const node = {id: walkthroughId(raw.id), outlineId: raw.id, name: matchedClaim?.name || null,
        label: typeof raw.label === 'string' && raw.label ? raw.label : raw.id, fullLabel: typeof raw.label === 'string' && raw.label ? raw.label : raw.id,
        kind: 'walkthrough', statementKind, section: typeof raw.section === 'string' ? raw.section : '',
        pages: typeof raw.pages === 'string' ? raw.pages : '', statement: typeof raw.statement === 'string' ? raw.statement : '', idea: typeof raw.idea === 'string' ? raw.idea : '',
        paper: typeof outline.paper === 'string' ? outline.paper : '', paperLocal: outline.paper_local === true, lean: Array.isArray(raw.lean) ? raw.lean.slice(0, 32) : [], rawUses: Array.isArray(raw.uses) ? raw.uses.slice(0, MAX_GRAPH_NODES) : [],
        claimRequested: typeof raw.claim === 'string' ? raw.claim : '', tone: stale ? 'amber' : status.key === 'unknown' ? 'muted' : status.tone,
        status: status.label, stale, hasRecord, sourceLine: Number.isSafeInteger(matchedClaim?.start) ? matchedClaim.start : null,
        relationsKnown: true, truncated: false, malformed: !Array.isArray(raw.uses), path: null};
      nodes.push(node); byId.set(node.id, node);
    }
    const dependencies = new Map(nodes.map(node => [node.id, []])), missingReferences = [];
    let dependencyCount = 0, omittedEdges = 0;
    for (const node of nodes) for (const reference of node.rawUses) {
      if (typeof reference !== 'string') { node.malformed = true; continue; }
      const target = byId.get(walkthroughId(reference));
      if (target) {
        const own = dependencies.get(node.id);
        if (!own.includes(target.id)) {
          if (dependencyCount < MAX_GRAPH_EDGES) { own.push(target.id); dependencyCount += 1; }
          else omittedEdges += 1;
        }
      } else if (missingReferences.length < MAX_GRAPH_EDGES) missingReferences.push({node: node.outlineId, reference});
      else omittedEdges += 1;
    }
    const backendProblems = Array.isArray(outline.problems) ? outline.problems.filter(item => typeof item === 'string').slice(0, 80) : [];
    const describedReferences = new Set();
    for (const problem of backendProblems) {
      const found = /^(.+): unknown statement (.+) in uses$/.exec(problem);
      if (found) describedReferences.add(`${found[1]}\0${found[2]}`);
    }
    const problems = [...backendProblems,
      ...missingReferences.filter(item => !describedReferences.has(`${item.node}\0${item.reference}`)).map(item => `${item.node}: missing statement reference ${item.reference}.`), ...claimProblems];
    return finalizeGraph(nodes, byId, dependencies, new Map(), {omittedNodes: Math.max(0, allNodes.length - MAX_GRAPH_NODES), omittedEdges,
      walkthrough: true, compatibility, title: typeof outline.title === 'string' ? outline.title : 'Walkthrough statement map', overview: typeof outline.overview === 'string' ? outline.overview : '',
      problems, missingReferences});
  }

  const graphModel = data => data.walkthrough ? walkthroughGraphModel(data) : claimGraphModel(data);

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
  function safeHTTPS(value) {
    if (typeof value !== 'string' || /\s/.test(value)) return '';
    try { const url = new URL(value); return url.protocol === 'https:' && url.hostname ? value : ''; } catch { return ''; }
  }
  function paperHref(node) {
    const target = safeHTTPS(node.paper);
    if (!target) return '';
    const page = /\d+/.exec(node.pages || '')?.[0];
    if (!page) return target;
    const url = new URL(target);
    if (url.pathname.toLowerCase().endsWith('.pdf')) url.hash = `page=${page}`;
    return url.href;
  }
  function walkthroughLinks(node) {
    const paper = paperHref(node);
    const paperLink = paper ? `<a class="external-link" href="${esc(paper)}" target="_blank" rel="noopener noreferrer">Paper page ${esc(/\d+/.exec(node.pages || '')?.[0] || node.pages || '')}</a>` : '';
    const localPaper = !paper && node.paperLocal ? `<p class="small muted">Paper page ${esc(/\d+/.exec(node.pages || '')?.[0] || node.pages || '')} is available only in the local walkthrough folder; this preview links only to public HTTPS sources.</p>` : '';
    const lean = node.lean.map(entry => {
      if (!entry || typeof entry.name !== 'string') return '';
      const href = safeHTTPS(entry.href);
      const label = `<code>${esc(entry.name)}</code>${entry.note ? ` — ${inlineHTML(entry.note)}` : ''}`;
      return `<li>${href ? `<a class="external-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label}</li>`;
    }).filter(Boolean).join('');
    return `${paperLink ? `<p>${paperLink}</p>` : localPaper}${lean ? `<h4>Lean declarations</h4><ul class="walkthrough-lean">${lean}</ul>` : '<p class="small muted">No Lean declaration is recorded for this statement.</p>'}`;
  }
  function graphDetailHTML(node, model, staticPage) {
    if (!node) return '<h3>Pick a statement</h3><p class="muted">Use Statement details to read its saved relationships.</p>';
    if (node.kind === 'walkthrough') {
      const action = node.name
        ? staticPage ? node.hasRecord ? `<a class="primary walkthrough-claim-link" href="#cell-${esc(node.name)}">Show in walkthrough</a>` : '' : `<button type="button" class="primary" data-act="graph-node" data-graph-id="${esc(node.id)}">Show in Document</button>`
        : node.claimRequested ? `<p class="small muted">No saved walkthrough result matches ${esc(node.claimRequested)}.</p>` : '<p class="small muted">This paper statement has no mapped walkthrough result.</p>';
      const where = [node.statementKind, node.section, node.pages ? `pp. ${node.pages}` : ''].filter(Boolean).join(' · ');
      return `<h3${staticPage ? '' : ' tabindex="-1"'}>${esc(node.fullLabel)}</h3><p><span class="pill" data-tone="${esc(node.tone)}">${esc(node.status)}</span>${node.stale && node.status !== 'Out of date' ? ' <span class="pill" data-tone="amber">Out of date</span>' : ''}</p>
        ${where ? `<p class="small muted">${esc(where)}</p>` : ''}<div class="walkthrough-statement">${inlineHTML(node.statement)}</div><p>${inlineHTML(node.idea)}</p>
        <dl><dt>Uses</dt><dd>${nodeList(node.uses, model)}</dd><dt>Used by</dt><dd>${nodeList(node.usedBy, model)}</dd></dl>${walkthroughLinks(node)}${action}`;
    }
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
    const model = graphModel(data), layout = graphLayout(model), boxWidth = 188, boxHeight = 50;
    const marker = staticPage ? 'proof-arrow-static' : 'proof-arrow';
    const paths = model.edges.map(edge => {
      const from = layout.positions.get(edge.from), to = layout.positions.get(edge.to);
      const start = from.y - boxHeight / 2, end = to.y + boxHeight / 2, mid = (start + end) / 2;
      return `<path class="proof-edge${edge.appendix ? ' appendix-edge' : ''}" data-from="${esc(edge.from)}" data-to="${esc(edge.to)}" d="M${from.x},${start} C${from.x},${mid} ${to.x},${mid} ${to.x},${end + 6}" marker-end="url(#${marker})"/>`;
    }).join('');
    const nodes = model.nodes.map(node => {
      const point = layout.positions.get(node.id), chosen = node.id === selected ? ' chosen' : '';
      const fullLabel = node.fullLabel || node.name;
      const nodeAction = node.kind === 'local' || node.kind === 'walkthrough' && node.name ? 'Open in Document' : node.path ? 'Open notebook' : 'Show details';
      const accessibleKind = node.kind === 'walkthrough' ? `${node.statementKind}, ` : '';
      const nodeLabel = `${accessibleKind}${fullLabel}, ${node.status}. ${nodeAction}`;
      const attrs = staticPage ? '' : ` data-act="graph-node" data-graph-id="${esc(node.id)}" tabindex="0" role="button" aria-label="${esc(nodeLabel)}"`;
      const graphName = node.kind === 'walkthrough' || node.kind === 'imported' ? node.label : node.name;
      const meta = node.kind === 'imported' ? 'Imported · ' + node.status : node.kind === 'walkthrough' ? `${node.statementKind} · ${node.status}` : node.status + (node.stale && node.status !== 'Out of date' ? ' · Out of date' : '');
      const kindClass = node.kind === 'walkthrough' ? ` walkthrough-node statement-${node.statementKind}` : '';
      const group = `<g class="proof-node${kindClass}${chosen}" data-graph-name="${esc(graphName)}" data-kind="${node.kind}"${node.statementKind ? ` data-statement-kind="${esc(node.statementKind)}"` : ''} data-tone="${esc(node.tone)}" transform="translate(${point.x},${point.y})"${attrs}>
        <rect x="${-boxWidth / 2}" y="${-boxHeight / 2}" width="${boxWidth}" height="${boxHeight}" rx="8"/><circle class="proof-node-status" cx="${-boxWidth / 2 + 15}" cy="0" r="5"/>
        <title>${esc(fullLabel)} — ${esc(node.status)}</title><text class="proof-node-label" text-anchor="middle" y="-3">${esc(node.label.length > 27 ? node.label.slice(0, 26) + '…' : node.label)}</text><text class="proof-node-meta" text-anchor="middle" y="15">${esc(meta)}</text></g>`;
      return staticPage && node.kind === 'walkthrough' && node.name && node.hasRecord ? `<a href="#cell-${esc(node.name)}" aria-label="${esc(nodeLabel)}">${group}</a>` : group;
    }).join('');
    const unknown = model.nodes.filter(node => !node.relationsKnown).map(node => node.label);
    const malformed = model.nodes.filter(node => node.malformed).map(node => node.label);
    const truncated = model.nodes.filter(node => node.truncated).map(node => node.fullLabel || node.name);
    const explanation = unknown.length ? `<p class="small muted graph-observation-note">Dependency observations are unknown for ${esc(unknown.join(', '))}.</p>` : '';
    const malformedNote = malformed.length ? `<p class="small warn-text">Malformed dependency observations were ignored for ${esc(malformed.join(', '))}.</p>` : '';
    const truncatedNote = truncated.length ? `<p class="small warn-text">The saved check recorded only the first uses for ${esc(truncated.join(', '))}; more dependencies may exist.</p>` : '';
    const cyclicNote = model.cyclic ? '<p class="small warn-text">A dependency cycle was recorded. All saved relationships are shown without transitive reduction.</p>' : '';
    const bounded = model.omittedNodes || model.omittedEdges ? `<p class="small warn-text">Graph limit reached: ${model.omittedNodes ? `${model.omittedNodes} additional node${model.omittedNodes === 1 ? '' : 's'}` : ''}${model.omittedNodes && model.omittedEdges ? ' and ' : ''}${model.omittedEdges ? `${model.omittedEdges} additional relationship${model.omittedEdges === 1 ? '' : 's'}` : ''} omitted.</p>` : '';
    const outlineProblems = model.walkthrough && model.problems.length ? `<p class="small warn-text">${esc(model.problems.join(' '))}</p>` : '';
    const compatibility = model.walkthrough && !model.compatibility.supported ? `<p class="banner quiet walkthrough-compatibility" role="status">${esc(model.compatibility.message)}</p>` : '';
    const statementKinds = model.walkthrough ? [...new Set(model.nodes.map(node => node.statementKind))] : [];
    const legend = statementKinds.length ? `<div class="walkthrough-legend" aria-label="Statement kinds">${statementKinds.map(kind => `<span><i class="statement-swatch statement-${kind}" aria-hidden="true"></i>${esc(kind)}</span>`).join('')}</div>` : '';
    const toolbar = staticPage ? '' : `<div class="proof-graph-toolbar" aria-label="Graph size"><button type="button" class="secondary" data-act="graph-zoom-out" aria-label="Zoom out">−</button><button type="button" class="secondary" data-act="graph-fit">Fit width</button><button type="button" class="secondary" data-act="graph-zoom-in" aria-label="Zoom in">+</button></div>`;
    const detailOptions = model.nodes.map(node => `<option value="${esc(node.id)}"${node.id === selected ? ' selected' : ''}>${node.kind === 'walkthrough' ? `${esc(node.statementKind)} — ` : ''}${esc(node.fullLabel || node.name)} — ${esc(node.status)}</option>`).join('');
    const picker = '<se' + `lect data-act="graph-detail-picker">${detailOptions}</se` + 'lect>';
    const detail = staticPage ? '' : `<aside class="proof-graph-detail" id="graph-detail" aria-live="polite"><label class="proof-graph-detail-picker">Statement details${picker}</label>${graphDetailHTML(model.byId.get(selected), model, false)}</aside>`;
    const staticDetails = staticPage && model.walkthrough ? `<div class="walkthrough-static-details">${model.nodes.map(node => `<section id="walkthrough-detail-${esc(node.outlineId)}">${graphDetailHTML(node, model, true)}</section>`).join('')}</div>` : '';
    const svgRole = staticPage && !model.walkthrough ? ' role="img"' : ' role="group"';
    const svgWidth = staticPage ? layout.width : `${Math.round(zoom * 100)}%`;
    const heading = model.walkthrough ? model.title : 'Recorded proof graph';
    const description = model.walkthrough ? `${model.overview ? inlineHTML(model.overview) + ' ' : ''}Arrows show direct paper-level dependencies. Statement colour shows its kind; the dot shows the displayed walkthrough result status.` : 'Arrows run from a statement to each statement that directly uses it. Foundations are at the bottom; results rise to the top. This view does not check or update results.';
    const label = model.walkthrough ? `${model.title} statement map` : 'Recorded proof graph';
    return `<section class="proof-graph-view${staticPage ? ' proof-graph-export' : ''}${model.walkthrough ? ' walkthrough-map' : ''}" aria-label="${esc(label)}"><header><div><h2>${esc(heading)}</h2><p>${description}</p>${legend}</div>${toolbar}</header>
      ${compatibility}<div class="proof-graph-layout"><div class="proof-graph-canvas"><svg class="proof-map" width="${svgWidth}" viewBox="0 0 ${layout.width} ${layout.height}"${svgRole} aria-label="Statement dependencies"><defs><marker id="${marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>${paths}${nodes}</svg></div>${detail}</div>${explanation}${truncatedNote}${malformedNote}${cyclicNote}${bounded}${outlineProblems}${staticDetails}${staticPage ? '<p class="small muted">Static copy of the saved graph; it does not check or update results.</p>' : ''}</section>`;
  }

  function renderProofGraph() {
    const root = document.getElementById('graph-view');
    if (!root || !UI.data) return;
    const model = graphModel(UI.data);
    if (!model.byId.has(UI.graphSelected)) {
      const selected = model.walkthrough ? model.nodes.find(node => node.name === UI.selected)?.id : localId(UI.selected);
      UI.graphSelected = model.byId.has(selected) ? selected : model.nodes[0]?.id || null;
    }
    root.innerHTML = proofGraphHTML(UI.data, {selected: UI.graphSelected, zoom: UI.graphZoom || 1});
  }
  const graphNode = id => graphModel(UI.data).byId.get(id);

  Object.assign(workbench, {claimGraphModel, graphDetailHTML, graphNode, proofGraphHTML, renderProofGraph, walkthroughGraphModel});
})();
