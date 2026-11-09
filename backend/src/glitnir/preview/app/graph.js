// Recorded proof dependencies as a local, presentation-only graph.
// The graph never checks Lean or changes a notebook record.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, claimLabel, esc, inlineHTML, projectClaims, recorded, sourceClaimRanges, statusOf, title, walkthroughCompatibility} = workbench;

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
    for (const [user, own] of dependencies) for (const dependency of own) users.get(dependency)?.push(user);
    for (const node of nodes) {
      node.uses = dependencies.get(node.id) || [];
      node.usedBy = users.get(node.id) || [];
      node.unavailable = unavailable.get(node.id) || 0;
    }
    return {nodes, byId, edges, dependencies: direct, cyclic, ...extra};
  }

  function claimGraphModel(data, selectedName = null, focusId = null, includeSource = true) {
    const project = data.preview?.mode === 'project';
    const rendered = project ? projectClaims(data) : (Array.isArray(data.claims) ? data.claims : []);
    const claimsByName = new Map(rendered.map(entry => [entry.name, entry]));
    const sourceRanges = project && includeSource ? sourceClaimRanges(data.cells || []) : new Map();
    const sourceNames = project
      ? [...sourceRanges.keys(), ...(data.cells || []).flatMap(cell => cell.claim ? [cell.claim] : [])]
      : rendered.map(entry => entry.name);
    const savedByName = new Map((data.project_claims || []).filter(saved => typeof saved?.name === 'string').map(saved => [saved.name, saved]));
    const sourceTargets = new Set((data.cells || []).flatMap(cell => [cell.id, cell.claim]));
    const allNames = [...new Set([...sourceNames, ...rendered.map(entry => entry.name), ...savedByName.keys()])];
    const recordedUses = name => project ? savedByName.get(name)?.uses : claimsByName.get(name)?.uses;
    const useId = use => {
      if (project) {
        if (!use || typeof use.name !== 'string' || typeof use.module !== 'string') return null;
        if (use.in_notebook === true) return localId(use.name);
        return use.in_notebook === false ? importId(use.module, use.name) : null;
      }
      if (typeof use !== 'string' || !use) return null;
      if (!use.includes(':')) return localId(use);
      const found = legacyImport(data, use);
      return importId(found.module, found.name);
    };
    let localLimit = MAX_GRAPH_NODES;
    if (focusId) {
      const related = new Set([focusId]);
      const own = recordedUses(selectedName);
      if (Array.isArray(own)) for (const use of own) related.add(useId(use));
      for (const name of allNames) {
        const uses = recordedUses(name);
        if (Array.isArray(uses) && uses.some(use => useId(use) === focusId)) related.add(localId(name));
      }
      const locals = allNames.filter(name => related.has(localId(name))).length;
      const imports = [...related].filter(id => id?.startsWith('import:')).length;
      const reserved = Math.min(imports, Math.max(focusId.startsWith('import:') ? 1 : 0, MAX_GRAPH_NODES - locals));
      localLimit -= reserved;
      allNames.sort((a, b) => Number(related.has(localId(b))) - Number(related.has(localId(a))));
    }
    if ((focusId || allNames.length > localLimit) && allNames.includes(selectedName)) {
      allNames.unshift(...allNames.splice(allNames.indexOf(selectedName), 1));
    }
    const orderedNames = allNames.slice(0, localLimit);
    const omittedNodeIds = new Set(allNames.slice(localLimit).map(localId));
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
      const sourceRange = sourceRanges.get(name);
      // The graph and its export describe the saved snapshot. A live checking
      // badge must never replace the recorded status here.
      const hasLegacyRecord = project || entry?.status === 'stale' || Boolean(entry?.record || entry?.refutation);
      const status = entry && hasLegacyRecord ? statusOf(entry, true) : {key: 'unknown', label: 'Unknown', tone: 'muted'};
      const stale = saved?.current === false || (!project && entry?.status === 'stale');
      const notCompared = saved?.current === null;
      add({id: localId(name), name, label: entry ? claimLabel(entry) : shortName(name), kind: 'local', path: null,
        fullLabel: name, hasSource: sourceTargets.has(name) || sourceRanges.has(name), sourceLine: sourceRange?.claim ? undefined : sourceRange?.start, hasRecord: Boolean(saved) || !project && hasLegacyRecord,
        freshnessUnknown: notCompared, statusTone: status.tone, tone: notCompared ? 'muted' : stale ? 'amber' : status.key === 'unknown' ? 'muted' : status.tone, status: status.label + (notCompared ? ' · Not compared' : ''), stale,
        relationsKnown: project ? Array.isArray(saved?.uses) : hasLegacyRecord && Array.isArray(entry?.uses),
        truncated: project ? saved?.uses_truncated === true : entry?.uses_truncated === true, malformed: false});
    }

    const dependencies = new Map(nodes.map(node => [node.id, []]));
    const unavailable = new Map();
    const omitUse = user => {
      omittedEdges += 1;
      user.omittedUses = (user.omittedUses || 0) + 1;
    };
    const addLocalUse = (user, name) => {
      const id = localId(name), target = byId.get(id);
      if (target) addDependency(user, target);
      else if (omittedNodeIds.has(id)) omitUse(user);
      else unavailable.set(user.id, (unavailable.get(user.id) || 0) + 1);
    };
    const addDependency = (user, dependency) => {
      const list = dependencies.get(user.id);
      if (!list || list.includes(dependency.id)) return;
      if (dependencyCount >= MAX_GRAPH_EDGES) omitUse(user);
      else { list.push(dependency.id); dependencyCount += 1; }
    };
    for (const user of nodes.filter(node => node.kind === 'local')) {
      const raw = recordedUses(user.name);
      if (!Array.isArray(raw)) {
        if (raw !== undefined && raw !== null) user.malformed = true;
        continue;
      }
      const selectedImport = focusId?.startsWith('import:') ? raw.findIndex(use => useId(use) === focusId) : -1;
      const uses = selectedImport > 0 ? [raw[selectedImport], ...raw.slice(0, selectedImport), ...raw.slice(selectedImport + 1)] : raw;
      for (const use of uses) {
        if (project) {
          if (!use || typeof use !== 'object' || typeof use.name !== 'string' || typeof use.module !== 'string' || typeof use.in_notebook !== 'boolean') {
            user.malformed = true; continue;
          }
          if (use.in_notebook) {
            addLocalUse(user, use.name);
            continue;
          }
          const id = importId(use.module, use.name);
          let target = byId.get(id);
          if (!target && nodes.length < MAX_GRAPH_NODES) target = add({id, name: use.name, label: `${use.module}:${shortName(use.name)}`, fullLabel: `${use.module}:${use.name}`, kind: 'imported', path: typeof use.path === 'string' && use.path ? use.path : null,
            module: use.module, tone: 'muted', status: 'Unknown', stale: false, relationsKnown: false, truncated: false, malformed: false});
          if (!target) { omittedNodeIds.add(id); omitUse(user); continue; }
          if (!dependencies.has(id)) dependencies.set(id, []);
          addDependency(user, target);
          continue;
        }
        if (typeof use !== 'string' || !use) { user.malformed = true; continue; }
        if (!use.includes(':')) {
          addLocalUse(user, use);
          continue;
        }
        const found = legacyImport(data, use);
        const id = importId(found.module, found.name);
        let target = byId.get(id);
        if (!target && nodes.length < MAX_GRAPH_NODES) target = add({id, name: found.name, label: `${found.module}:${shortName(found.name)}`, fullLabel: `${found.module}:${found.name}`, kind: 'imported', path: found.path,
          module: found.module, tone: importedTone(found.status), status: importedLabel(found.status), stale: found.status === 'stale', relationsKnown: false, truncated: false, malformed: false});
        if (!target) { omittedNodeIds.add(id); omitUse(user); continue; }
        if (!dependencies.has(id)) dependencies.set(id, []);
        addDependency(user, target);
      }
    }

    return finalizeGraph(nodes, byId, dependencies, unavailable, {omittedNodes: omittedNodeIds.size, omittedEdges, walkthrough: false});
  }

  const walkthroughId = id => `walkthrough:${id}`;
  function walkthroughGraphModel(data, selected = null, selectedClaim = null, focused = false) {
    const outline = data.walkthrough || {};
    const compatibility = walkthroughCompatibility(data);
    const allNodes = Array.isArray(outline.nodes) ? outline.nodes : [];
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
    const selectedIndex = selectedClaim
      ? allNodes.findIndex(node => node?.claim && matchingClaim(node.claim)?.name === selectedClaim)
      : allNodes.findIndex(node => node && walkthroughId(node.id) === selected);
    let rawNodes = allNodes.slice(0, MAX_GRAPH_NODES);
    if (focused && selectedIndex >= 0 && allNodes.length > MAX_GRAPH_NODES) {
      const centre = allNodes[selectedIndex];
      const uses = new Set(Array.isArray(centre.uses) ? centre.uses : []);
      const related = node => node !== centre && node && (uses.has(node.id) || Array.isArray(node.uses) && node.uses.includes(centre.id));
      rawNodes = [centre, ...allNodes.filter(related), ...allNodes.filter(node => node !== centre && !related(node))].slice(0, MAX_GRAPH_NODES);
    } else if (selectedIndex >= MAX_GRAPH_NODES) {
      rawNodes = [allNodes[selectedIndex], ...allNodes.slice(0, MAX_GRAPH_NODES - 1)];
    }
    const nodes = [], byId = new Map(), claimProblems = [];
    for (const raw of rawNodes) {
      if (!raw || typeof raw.id !== 'string' || !raw.id || byId.has(walkthroughId(raw.id))) continue;
      const matchedClaim = matchingClaim(typeof raw.claim === 'string' ? raw.claim : '');
      const saved = matchedClaim ? savedByName.get(matchedClaim.name) : null;
      const hasRecord = data.preview?.mode === 'project' ? Boolean(saved) : Boolean(matchedClaim?.record || matchedClaim?.refutation || matchedClaim?.status === 'stale');
      const status = matchedClaim && hasRecord ? statusOf(matchedClaim, true, data) : {key: 'unknown', label: 'Unknown', tone: 'muted'};
      const notCompared = saved?.current === null;
      const stale = compatibility.supported && (saved?.current === false || matchedClaim?.status === 'stale');
      if (raw.claim && !matchedClaim) claimProblems.push(`No saved walkthrough result matches ${raw.claim}.`);
      const statementKind = STATEMENT_KINDS.has(raw.kind) ? raw.kind : 'unknown';
      const node = {id: walkthroughId(raw.id), outlineId: raw.id, name: matchedClaim?.name || null,
        label: typeof raw.label === 'string' && raw.label ? raw.label : raw.id, fullLabel: typeof raw.label === 'string' && raw.label ? raw.label : raw.id,
        kind: 'walkthrough', statementKind, section: typeof raw.section === 'string' ? raw.section : '',
        pages: typeof raw.pages === 'string' ? raw.pages : '', statement: typeof raw.statement === 'string' ? raw.statement : '', idea: typeof raw.idea === 'string' ? raw.idea : '',
        paper: typeof outline.paper === 'string' ? outline.paper : '', paperLocal: outline.paper_local === true, lean: Array.isArray(raw.lean) ? raw.lean.slice(0, 32) : [], rawUses: Array.isArray(raw.uses) ? raw.uses.slice(0, MAX_GRAPH_NODES) : [],
        claimRequested: typeof raw.claim === 'string' ? raw.claim : '', tone: notCompared ? 'muted' : stale ? 'amber' : status.key === 'unknown' ? 'muted' : status.tone,
        status: status.label + (notCompared ? ' · Not compared' : ''), freshnessUnknown: notCompared, statusTone: status.tone, stale, hasRecord, sourceLine: Number.isSafeInteger(matchedClaim?.start) ? matchedClaim.start : null,
        relationsKnown: true, truncated: false, malformed: !Array.isArray(raw.uses), path: null};
      nodes.push(node); byId.set(node.id, node);
    }
    const knownIds = new Set(allNodes.filter(node => typeof node?.id === 'string' && node.id).map(node => node.id));
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
      } else if (knownIds.has(reference)) omittedEdges += 1;
      else if (missingReferences.length < MAX_GRAPH_EDGES) missingReferences.push({node: node.outlineId, reference});
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

  const graphModel = (data, selected = null, selectedClaim = null, focused = false, includeSource = true) => data.walkthrough
    ? walkthroughGraphModel(data, selected, selectedClaim, focused)
    : claimGraphModel(data, selectedClaim || (selected?.startsWith('local:') ? selected.slice(6) : null),
      focused || selected?.startsWith('import:') ? selected : null, includeSource);

  function focusedGraph(model, selected) {
    const centre = model.byId.get(selected);
    const visible = new Set([selected, ...centre.uses, ...centre.usedBy]);
    const nodes = model.nodes.filter(node => visible.has(node.id));
    const dependencies = new Map(nodes.map(node => [
      node.id, node.id === selected ? centre.uses : node.uses.filter(id => id === selected),
    ]));
    const edges = nodes.flatMap(node => dependencies.get(node.id).map(from => ({
      from, to: node.id, appendix: node.statementKind === 'appendix',
    })));
    return {...model, nodes, dependencies, edges};
  }

  function graphLayout(model, focused = false) {
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
    const width = Math.max(focused ? 220 : 760, widest * 220), rowHeight = 112, top = 54;
    const topLevel = Math.max(0, ...rows.keys()), positions = new Map();
    for (const [level, row] of rows) {
      const gap = width / (focused ? row.length : row.length + 1);
      row.forEach((id, index) => positions.set(id, {x: gap * (index + (focused ? .5 : 1)), y: top + (topLevel - level) * rowHeight}));
    }
    return {positions, width, height: top + (topLevel + 1) * rowHeight};
  }

  const nodeList = (ids, model, staticPage) => ids.length ? ids.map(id => {
    const label = esc(model.byId.get(id)?.fullLabel || model.byId.get(id)?.name || id);
    return staticPage ? label : `<button type="button" class="graph-relation" data-act="graph-select" data-graph-id="${esc(id)}">${label}</button>`;
  }).join(', ') : 'None';
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
  function graphResultLabel(node) {
    if (node.status === 'Unknown') {
      return node.kind === 'imported' ? 'Result unavailable' : node.hasRecord ? 'Result status unavailable' : 'No saved result';
    }
    return node.freshnessUnknown ? node.status.replace(/ · Not compared$/, '') : node.status;
  }

  function graphStatusHTML(node, staticPage) {
    if (staticPage) {
      return `<p><span class="pill" data-tone="${esc(node.tone)}">${esc(node.status)}</span>${node.stale && node.status !== 'Out of date' ? ' <span class="pill" data-tone="amber">Out of date</span>' : ''}</p>`;
    }
    const status = graphResultLabel(node);
    const label = status === 'Kernel checked' ? 'Previously kernel checked' : status;
    const badge = status === 'Out of date' ? '' : `<p><span class="pill" data-tone="${esc(node.statusTone || node.tone)}">${esc(label)}</span></p>`;
    if (node.stale || status === 'Out of date') return badge + '<p class="graph-freshness"><span class="pill" data-tone="amber">Saved result is out of date</span></p>';
    if (node.freshnessUnknown) return badge + '<p class="graph-freshness"><span class="pill" data-tone="muted">Freshness unknown</span></p><p class="small muted">The saved result hasn’t been compared with this project’s environment.</p>';
    return badge;
  }

  function graphDetailHTML(node, model, staticPage) {
    if (!node) return '<h3>Pick a statement</h3><p class="muted">Use Statement details to read its saved relationships.</p>';
    if (node.kind === 'walkthrough') {
      const action = node.name
        ? staticPage ? node.hasRecord ? `<a class="primary walkthrough-claim-link" href="#cell-${esc(node.name)}">Show in walkthrough</a>` : '' : `<button type="button" class="secondary graph-open" data-act="graph-node" data-graph-id="${esc(node.id)}">Show in Notebook</button>`
        : node.claimRequested ? `<p class="small muted">No saved walkthrough result matches ${esc(node.claimRequested)}.</p>` : '<p class="small muted">No saved Lean result is linked to this statement.</p>';
      const where = [node.statementKind, node.section, node.pages ? `pp. ${node.pages}` : ''].filter(Boolean).join(' · ');
      return `<h3${staticPage ? '' : ' tabindex="-1"'}>${esc(node.fullLabel)}</h3>${staticPage ? '' : action}${graphStatusHTML(node, staticPage)}
        ${where ? `<p class="small muted">${esc(where)}</p>` : ''}<div class="walkthrough-statement">${inlineHTML(node.statement)}</div><p>${inlineHTML(node.idea)}</p>
        <dl><dt>Uses</dt><dd>${nodeList(node.uses, model, staticPage)}</dd><dt>Used by</dt><dd>${nodeList(node.usedBy, model, staticPage)}</dd></dl>${walkthroughLinks(node)}${staticPage ? action : ''}`;
    }
    const action = !staticPage && node.kind === 'local'
      ? node.hasSource
        ? `<button type="button" class="secondary graph-open" data-act="graph-node" data-graph-id="${esc(node.id)}">Show in Notebook</button>`
        : '<p class="small muted">No matching declaration in this file.</p>'
      : !staticPage && node.path ? `<button type="button" class="secondary graph-open" data-act="graph-node" data-graph-id="${esc(node.id)}">Open notebook</button>` : '';
    let uses = nodeList(node.uses, model, staticPage);
    if (!staticPage && !node.uses.length) {
      if (!node.relationsKnown) uses = node.kind === 'imported' ? 'Dependencies aren’t available here' : 'Dependencies haven’t been recorded';
      else if (node.truncated || node.malformed || node.unavailable || node.omittedUses) uses = 'No dependencies shown';
      else uses = 'No recorded dependencies';
    }
    const usedBy = !staticPage && !node.usedBy.length
      ? model.omittedNodes || model.omittedEdges ? 'None shown' : 'None recorded'
      : nodeList(node.usedBy, model, staticPage);
    const truncated = node.truncated ? '<p class="small warn-text">The saved check recorded only the first uses; more dependencies may exist.</p>' : '';
    const malformed = node.malformed ? '<p class="small warn-text">Some dependency records could not be read.</p>' : '';
    const unavailable = node.unavailable ? `<p class="small muted">${node.unavailable} recorded reference could not be matched in this notebook.</p>` : '';
    return `<h3 tabindex="-1">${esc(staticPage ? node.fullLabel || node.name : node.label)}</h3>${action}${graphStatusHTML(node, staticPage)}
      <dl><dt>Uses</dt><dd>${uses}</dd><dt>Used by</dt><dd>${usedBy}</dd></dl>${truncated}${malformed}${unavailable}`;
  }

  function proofGraphHTML(data, {staticPage = false, selected = null, zoom = 1, fit = false, focused = false} = {}) {
    const model = graphModel(data, selected, null, focused && !staticPage, !staticPage);
    if (!staticPage && !model.nodes.length) {
      const message = model.walkthrough ? 'No statements to graph in this walkthrough.' : 'No theorems or lemmas identified in the saved source';
      return `<section class="proof-graph-view" aria-label="Graph"><h2 tabindex="-1">${esc(message)}</h2></section>`;
    }
    const showFocus = focused && !staticPage && model.byId.has(selected);
    const canvas = showFocus ? focusedGraph(model, selected) : model;
    const layout = graphLayout(canvas, showFocus), boxWidth = 188, boxHeight = 50;
    const marker = staticPage ? 'proof-arrow-static' : 'proof-arrow';
    const paths = canvas.edges.map(edge => {
      const from = layout.positions.get(edge.from), to = layout.positions.get(edge.to);
      const start = from.y - boxHeight / 2, end = to.y + boxHeight / 2, mid = (start + end) / 2;
      return `<path class="proof-edge${edge.from === selected || edge.to === selected ? ' related' : ''}${edge.appendix ? ' appendix-edge' : ''}" data-from="${esc(edge.from)}" data-to="${esc(edge.to)}" d="M${from.x},${start} C${from.x},${mid} ${to.x},${mid} ${to.x},${end + 6}" marker-end="url(#${marker})"/>`;
    }).join('');
    const nodes = canvas.nodes.map(node => {
      const point = layout.positions.get(node.id), chosen = node.id === selected ? ' chosen' : '';
      const fullLabel = node.fullLabel || node.name;
      const accessibleKind = node.kind === 'walkthrough' ? `${node.statementKind}, ` : '';
      const status = staticPage ? node.status : graphResultLabel(node);
      const freshness = node.stale && status !== 'Out of date' ? ' · Out of date' : !staticPage && node.freshnessUnknown ? ' · Freshness unknown' : '';
      const nodeLabel = `${accessibleKind}${fullLabel}, ${status}${freshness}. Show details`;
      const attrs = staticPage ? '' : ` data-act="graph-select" data-graph-id="${esc(node.id)}" tabindex="0" role="button" aria-pressed="${node.id === selected}" aria-label="${esc(nodeLabel)}"`;
      const graphName = node.kind === 'walkthrough' || node.kind === 'imported' ? node.label : node.name;
      const staticMeta = node.kind === 'walkthrough' ? `${node.statementKind} · ${status}` : status + freshness;
      let meta = status;
      if (staticPage) meta = node.kind === 'imported' ? 'Imported · ' + status : staticMeta;
      else if (node.stale) meta = 'Saved result out of date';
      else if (node.freshnessUnknown) meta = 'Freshness unknown';
      else if (node.kind === 'imported') meta = 'Imported';
      const kindClass = node.kind === 'walkthrough' ? ` walkthrough-node statement-${node.statementKind}` : '';
      const group = `<g class="proof-node${kindClass}${chosen}" data-graph-name="${esc(graphName)}" data-kind="${node.kind}"${node.statementKind ? ` data-statement-kind="${esc(node.statementKind)}"` : ''} data-tone="${esc(node.tone)}" transform="translate(${point.x},${point.y})"${attrs}>
        <rect x="${-boxWidth / 2}" y="${-boxHeight / 2}" width="${boxWidth}" height="${boxHeight}" rx="8"/><circle class="proof-node-status" cx="${-boxWidth / 2 + 15}" cy="0" r="5"/>
        <title>${esc(fullLabel)} — ${esc(status + freshness)}</title><text class="proof-node-label" text-anchor="middle" y="-3">${esc(node.label.length > 27 ? node.label.slice(0, 26) + '…' : node.label)}</text><text class="proof-node-meta" text-anchor="middle" y="15">${esc(meta)}</text></g>`;
      return staticPage && node.kind === 'walkthrough' && node.name && node.hasRecord ? `<a href="#cell-${esc(node.name)}" aria-label="${esc(nodeLabel)}">${group}</a>` : group;
    }).join('');
    const unknown = canvas.nodes.filter(node => !node.relationsKnown && (staticPage || node.kind !== 'imported')).map(node => node.label);
    const malformed = model.nodes.filter(node => node.malformed).map(node => node.label);
    const truncated = model.nodes.filter(node => node.truncated).map(node => node.fullLabel || node.name);
    const explanation = unknown.length ? `<p class="small muted graph-observation-note">Dependency records are unavailable for ${esc(unknown.join(', '))}.</p>` : '';
    const malformedNote = malformed.length ? `<p class="small warn-text">Malformed dependency observations were ignored for ${esc(malformed.join(', '))}.</p>` : '';
    const truncatedNote = truncated.length ? `<p class="small warn-text">The saved check recorded only the first uses for ${esc(truncated.join(', '))}; more dependencies may exist.</p>` : '';
    const cyclicNote = model.cyclic ? '<p class="small warn-text">A dependency cycle was recorded. The full graph shows the available links in the cycle.</p>' : '';
    const bounded = model.omittedNodes || model.omittedEdges ? `<p class="small warn-text">Graph limit reached: ${model.omittedNodes ? `${model.omittedNodes} additional node${model.omittedNodes === 1 ? '' : 's'}` : ''}${model.omittedNodes && model.omittedEdges ? ' and ' : ''}${model.omittedEdges ? `${model.omittedEdges} additional relationship${model.omittedEdges === 1 ? '' : 's'}` : ''} omitted.</p>` : '';
    const outlineProblems = model.walkthrough && model.problems.length ? `<p class="small warn-text">${esc(model.problems.join(' '))}</p>` : '';
    const compatibility = model.walkthrough && !model.compatibility.supported ? `<p class="banner quiet walkthrough-compatibility" role="status">${esc(model.compatibility.message)}</p>` : '';
    const statementKinds = model.walkthrough ? [...new Set(model.nodes.map(node => node.statementKind))] : [];
    const legend = statementKinds.length ? `<div class="walkthrough-legend" aria-label="Statement kinds">${statementKinds.map(kind => `<span><i class="statement-swatch statement-${kind}" aria-hidden="true"></i>${esc(kind)}</span>`).join('')}</div>` : '';
    const toolbar = staticPage ? '' : `<div class="proof-graph-toolbar" role="group" aria-label="Graph zoom"><button type="button" class="secondary" data-act="graph-zoom-out" aria-label="Zoom out">−</button><button type="button" class="secondary" data-act="graph-zoom-in" aria-label="Zoom in">+</button><button type="button" class="secondary" data-act="graph-fit" aria-pressed="${fit}">Fit graph</button><button type="button" class="secondary" data-act="graph-reset">Reset zoom</button></div>`;
    const detailOptions = model.nodes.map(node => `<option value="${esc(node.id)}"${node.id === selected ? ' selected' : ''}>${esc(node.fullLabel || node.name)}</option>`).join('');
    const picker = '<se' + `lect data-act="graph-detail-picker">${detailOptions}</se` + 'lect>';
    const pickerLabel = model.walkthrough ? 'Choose a statement' : 'Choose a claim';
    const detail = staticPage ? '' : `<aside class="proof-graph-detail" id="graph-detail" aria-label="Selected details" aria-live="polite"><label class="proof-graph-detail-picker">${pickerLabel}${picker}</label>${graphDetailHTML(model.byId.get(selected), model, false)}</aside>`;
    const staticDetails = staticPage && model.walkthrough ? `<div class="walkthrough-static-details">${model.nodes.map(node => `<section id="walkthrough-detail-${esc(node.outlineId)}">${graphDetailHTML(node, model, true)}</section>`).join('')}</div>` : '';
    const svgRole = staticPage && !model.walkthrough ? ' role="img"' : ' role="group"';
    const svgWidth = !staticPage && fit && !showFocus ? '100%' : Math.round(layout.width * (!staticPage && fit ? 1 : zoom));
    const scopeLabel = model.walkthrough ? 'Selected statement' : 'Selected claim';
    const scope = staticPage ? '' : `<div class="proof-graph-scope" role="group" aria-label="Graph scope"><button type="button" class="secondary" data-act="graph-focus" aria-pressed="${showFocus}">${scopeLabel}</button><button type="button" class="secondary" data-act="graph-full" aria-pressed="${!showFocus}">Full graph</button></div>`;
    const heading = model.walkthrough ? model.title : staticPage ? 'Recorded proof graph' : 'Claim graph';
    const arrowGuide = model.walkthrough
      ? 'Arrows show relationships mapped from the paper. Badges show saved Lean results.'
      : 'Arrows point from a supporting statement to the statement that uses it.';
    const description = showFocus ? `Shows the selection and its direct connections. ${arrowGuide}` : arrowGuide;
    const overview = model.walkthrough && model.overview ? `<p>${inlineHTML(model.overview)}</p>` : '';
    const label = model.walkthrough ? `${model.title} statement map` : heading;
    const incomplete = explanation || malformedNote || truncatedNote || bounded || outlineProblems;
    const diagnostics = explanation + malformedNote + truncatedNote + cyclicNote + bounded + outlineProblems;
    const notes = !staticPage && diagnostics ? `<details class="proof-graph-notes" data-fold-key="graph-notes"><summary>${incomplete ? 'Some links are unavailable' : 'About this graph'}</summary>${diagnostics}</details>` : '';
    return `<section class="proof-graph-view${staticPage ? ' proof-graph-export' : ''}${model.walkthrough ? ' walkthrough-map' : ''}" aria-label="${esc(label)}"><header><div><h2>${esc(heading)}</h2>${overview}<p>${description}</p>${legend}</div>${scope}</header>
      ${compatibility}${notes}<div class="proof-graph-layout">${detail}${staticPage ? '' : '<div class="proof-graph-stage">'}${toolbar}<div class="proof-graph-canvas"${staticPage ? '' : ` data-scope="${showFocus ? 'focused' : 'full'}" data-fit="${fit}"`}><svg class="proof-map" width="${svgWidth}" viewBox="0 0 ${layout.width} ${layout.height}"${svgRole} aria-label="Statement dependencies"><defs><marker id="${marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>${paths}${nodes}</svg></div>${staticPage ? '' : '</div>'}</div>${staticPage ? diagnostics : ''}${staticDetails}${staticPage ? '<p class="small muted">Static copy of the saved graph; it does not check or update results.</p>' : ''}${staticPage ? workbench.atlasStaticHTML?.(data) || '' : ''}</section>`;
  }

  function renderProofGraph({openClaimGraph = false, openExplore = false} = {}) {
    const root = document.getElementById('graph-view');
    if (!root || !UI.data) return;
    if (workbench.renderAtlas?.(UI.data, {openClaimGraph, openExplore})) return;
    const derivationAvailable = workbench.hasExploreDerivation();
    if (derivationAvailable && UI.exploreDerivation && UI.graphSelected) {
      workbench.renderDerivation(root);
      return;
    }
    const model = graphModel(UI.data, UI.graphSelected || localId(UI.selected), openClaimGraph ? UI.selected : null, UI.graphFocused);
    if (openClaimGraph || !model.byId.has(UI.graphSelected)) {
      const selected = model.walkthrough ? model.nodes.find(node => node.name === UI.selected)?.id : localId(UI.selected);
      UI.graphSelected = model.byId.has(selected) ? selected : model.nodes[0]?.id || null;
    }
    if (derivationAvailable && UI.exploreDerivation) {
      workbench.renderDerivation(root);
      return;
    }
    const readingSidebarScroll = root.querySelector('.atlas-sidebar')?.scrollTop || 0;
    const graphContent = (UI.data.walkthrough?.atlas ? workbench.atlasViewControls('graph') : '') + proofGraphHTML(UI.data, {selected: UI.graphSelected, zoom: UI.graphZoom || 1, fit: UI.graphFit === true, focused: UI.graphFocused});
    root.innerHTML = UI.data.walkthrough?.atlas ? workbench.atlasShell(UI.data, graphContent) : graphContent;
    const readingSidebar = root.querySelector('.atlas-sidebar');
    if (readingSidebar) readingSidebar.scrollTop = readingSidebarScroll;
  }
  const graphNode = id => graphModel(UI.data, UI.graphSelected, null, UI.graphFocused).byId.get(id);

  Object.assign(workbench, {claimGraphModel, graphDetailHTML, graphNode, graphStatusHTML, proofGraphHTML, renderProofGraph, walkthroughGraphModel, walkthroughId});
})();
