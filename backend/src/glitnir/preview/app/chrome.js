// The workbench's chrome, keyboard shortcuts and interaction; it loads the page.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, API, HOST, LABEL, UI, assetPath, breakableName, cancelCheck, cancelGoals, cancelInspection,
    cancelView, cellView, checkBusy, checkProject, checking, claim, claimLabel, claims, closeDiagram,
    currencyNoted, currencyText, decisionAction, diagramAction, esc, fetch, heldRendering, icon, images,
    graphNode, inlineHTML, inspectGoals, inspectProject, inspecting, inspection, leanOff, linkedPath, load, loadImage,
    markLiveChanges, namespaceScopes, openNotebook, openSource, projectCheckHTML, projectResolutionHTML,
    proofGraphHTML, proofSections, readingFolds, recorded, reduced, renderDiagramDialog, renderDoc, renderInspector,
    renderProofGraph, renderSource, renderTrail, requestedCut, restoreDiagramUI, revealSection, runEvidence, saveDiagramUI, sourceViewHTML,
    showNotebookAt, statusOf, title, toneOf, typesetMath, walkthroughMessage, walkthroughUnchecked, when, withheldBannerHTML} = workbench;
  const MAX_EXPORT_SOURCE_CHARACTERS = 1_000_000;
  const MAX_EXPORT_SOURCE_LINES = 20_000;
  // ----- chrome -----------------------------------------------------------------------
  function renderConnection() {
    const busy = UI.data && UI.data.checking.length;
    $('conn').dataset.state = !UI.connected ? 'down' : busy ? 'busy' : 'up';
    $('conn-text').textContent = !UI.connected ? 'Server not reachable · retrying' : busy ? `Lean is checking ${UI.data.checking.join(', ')}` : 'Connected to the local notebook server';
  }

  function drawGraph() {
    const svg = $('graph'), cs = claims(), rowH = 30, x = 34, W = 172, pos = new Map();
    cs.forEach((c, i) => { pos.set(c.name, 14 + i * rowH); });
    const H = Math.max(28, 14 + (cs.length - 1) * rowH + 14);
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('height', H);
    const color = t => ({green: 'var(--green)', amber: 'var(--amber)', red: 'var(--red)', muted: 'var(--faint)'})[t] || 'var(--blue)';
    const arcs = cs.flatMap(c => (Array.isArray(c.uses) ? c.uses : []).filter(u => pos.has(u) && u !== c.name).map(u => {
      const y1 = pos.get(u), y2 = pos.get(c.name), bend = x - 10 - (y2 - y1) / 5;
      return `<path d="M${x - 7} ${y1}C${bend} ${y1} ${bend} ${y2} ${x - 9} ${y2}" fill="none" stroke="var(--border)" stroke-width="1.5" marker-end="url(#g-arr)"/>`;
    })).join('');
    svg.innerHTML = `<defs><marker id="g-arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8" fill="var(--faint)"/></marker></defs>${arcs}` +
      cs.map(c => {
        const tone = toneOf(c), y = pos.get(c.name), sel = UI.selected === c.name;
        return `<g class="node" data-act="goto" data-id="${esc(c.name)}"><title>${esc(claimLabel(c))}: ${esc(statusOf(c).label)}${c.project && currencyNoted(c.project) ? ` · ${currencyText(c.project)}` : ''}</title><rect x="0" y="${y - 12}" width="${W}" height="24" fill="transparent"/><circle cx="${x}" cy="${y}" r="${sel ? 6.5 : 5.5}" fill="${tone === 'blue' ? 'var(--paper)' : color(tone)}" stroke="${color(tone)}" stroke-width="${sel ? 3 : 1.8}"/><text x="${x + 13}" y="${y + 3.5}" font-size="10.5" font-family="var(--mono)" fill="${sel ? 'var(--ink)' : 'var(--muted)'}">${esc(claimLabel(c))}</text></g>`;
      }).join('');
  }

  // Git is an on-demand host observation, independent of notebook check records.
  let gitLoading = false;
  let gitSnapshot = null;
  let gitReadAt = '';
  let gitDiffLoading = false;
  let gitGeneration = 0;

  function renderGitHistory() {
    const panel = $('git-history');
    panel.hidden = !HOST?.gitHistory;
    if (panel.hidden) return;
    panel.innerHTML = '<div class="history-heading"><h3>Git history</h3><button type="button" class="secondary" data-act="git-refresh"'
      + (gitLoading ? ' disabled' : '') + '>Refresh</button></div>';
    if (gitLoading) { panel.innerHTML += '<p role="status">Loading Git history…</p>'; return; }
    if (!gitSnapshot) { panel.innerHTML += '<p class="small muted">Open History to read this file\'s local Git history.</p>'; return; }
    if (gitSnapshot.state !== 'repository') {
      panel.innerHTML += `<p role="status">${esc(gitSnapshot.reason)}</p><p class="small muted">Source hashes and notebook activity work without Git.</p>`;
      return;
    }
    const d = gitSnapshot;
    const states = {clean: 'No uncommitted changes to this file', modified: 'Unstaged changes to this file', staged: 'Staged changes to this file', staged_and_modified: 'Staged and unstaged changes to this file', conflicted: 'Unresolved conflicts in this file', untracked: 'This file is not tracked by Git', ignored: 'This file is ignored by Git', filtered: 'This file uses a Git content filter; change status is unavailable without running it'};
    panel.innerHTML += `<p class="git-location">${d.branch ? `Branch <b>${esc(d.branch)}</b>` : 'Detached HEAD'} · ${d.head ? `Commit <code title="${esc(d.head)}">${esc(d.head.slice(0, 8))}</code>` : 'No commits yet'}</p>`
      + `<p>${esc(states[d.fileState] || 'File state unavailable')}.</p>`
      + `<p class="small muted">Read at ${esc(gitReadAt)}. Refresh to update. Commits shown affect this file at its current path; earlier names are not followed. Diffs compare each commit with its first parent.</p>`
      + (d.commits.length ? '<ol class="git-commits">' + d.commits.map(c => `<li><button type="button" class="git-commit" data-act="git-diff" data-sha="${esc(c.sha)}" aria-label="View diff: ${esc(c.subject)}"><code>${esc(c.sha.slice(0, 8))}</code> ${esc(c.subject)}</button><div class="small muted">${esc(c.author)} · ${esc(new Date(c.date).toLocaleString())}</div></li>`).join('') + '</ol>' : '<p class="small muted">No commits for this file at its current path.</p>')
      + (d.more ? '<p class="small muted">Showing the 30 most recent commits. Older commits are available in your Git client.</p>' : '')
      + '<div id="git-diff" tabindex="0" hidden></div>';
  }

  async function loadGitHistory(restoreFocus = false) {
    if (!HOST?.gitHistory || gitLoading) return;
    const generation = ++gitGeneration;
    gitLoading = true;
    renderGitHistory();
    try { gitSnapshot = await HOST.gitHistory(); gitReadAt = new Date().toLocaleTimeString(); }
    catch (error) { gitSnapshot = {state: 'unavailable', reason: String(error.message || error)}; }
    finally {
      if (generation === gitGeneration) {
        gitLoading = false;
        renderGitHistory();
        if (restoreFocus && $('history').open && (document.activeElement === document.body || $('git-history').contains(document.activeElement))) $('git-history').querySelector('[data-act="git-refresh"]').focus();
      }
    }
  }

  async function showGitDiff(sha) {
    if (gitLoading || gitDiffLoading || !gitSnapshot?.commits?.some(c => c.sha === sha)) return;
    const generation = gitGeneration;
    const target = $('git-diff');
    gitDiffLoading = true;
    target.hidden = false;
    target.innerHTML = '<p role="status">Loading commit diff…</p>';
    target.focus({preventScroll: true});
    target.scrollIntoView({block: 'nearest'});
    try {
      const diff = await HOST.gitDiff(sha);
      if (generation !== gitGeneration) return;
      target.innerHTML = `<h4>Commit ${esc(sha.slice(0, 8))}</h4><p class="small muted">Historical changes to this file; your checkout is unchanged.</p><pre class="git-diff-text">${esc(diff || 'No textual changes for this file in this commit.')}</pre>`;
    } catch (error) {
      if (generation === gitGeneration) target.innerHTML = `<p role="status">${esc(error.message || error)}</p>`;
    } finally { gitDiffLoading = false; }
  }

  $('history').addEventListener('toggle', () => { if ($('history').open) void loadGitHistory(); });
  renderGitHistory();

  function renderChrome() {
    const d = UI.data, cs = claims();
    document.title = `${title()} · Glitnir`;
    $('title').innerHTML = inlineHTML(title());
    $('file-name').textContent = d.file;
    const linked = Boolean(linkedPath());
    $('location').textContent = linked ? `Linked from ${UI.main?.file || 'the notebook'} · read-only` : 'Notebook';
    renderTrail();
    $('file-state').textContent = `Source hash: ${d.sha256.slice(0, 8)}`;
    $('file-state').title = `SHA-256 of the saved Lean source (not a Git commit): ${d.sha256}`;
    const count = key => cs.filter(c => c.status === key).length;
    const stat = (n, tone, text) => `<span class="stat"><span class="dot"${tone ? ` data-tone="${tone}"` : ''}></span>${n} ${esc(text)}</span>`;
    const project = d.preview?.mode === 'project';
    const statuses = [...new Set(cs.map(c => c.status))];
    const outOfDate = cs.filter(c => c.project && c.project.current === false).length;
    const uncompared = cs.filter(c => c.project && c.project.current === null).length;
    const notRechecked = cs.filter(c => c.project?.not_rechecked).length;
    const covered = project && requestedCut(d.project_withheld) ? d.project_withheld.through_line : null;
    const bits = walkthroughUnchecked()
      ? [stat(cs.length, 'muted', 'unchecked')]
      : project
      ? [...statuses.map(k => stat(count(k), statusOf({status: k, project: true}).tone, statusOf({status: k, project: true}).label.toLowerCase())), ...(outOfDate ? [stat(outOfDate, 'amber', 'out of date')] : []), ...(uncompared ? [stat(uncompared, '', 'not compared')] : []),
        ...(notRechecked ? [stat(notRechecked, 'muted', 'not rechecked')] : []), ...(covered ? [`<span class="stat project-covered">last check: lines 1–${esc(covered)}</span>`] : [])]
      : [['checked', 'green'], ['refuted', 'red'], ['conditional', 'amber'], ['stale', 'amber'], ['open', ''], ['failed', 'red'], ['unknown', 'amber']]
        .filter(([k]) => count(k)).map(([k, t]) => stat(count(k), t, LABEL[k].toLowerCase()));
    $('subline').innerHTML = bits.join('') + (d.checking.length ? `<span class="stat checking-badge">Checking ${d.checking.length}</span>` : '');
    const stale = cs.filter(c => c.status === 'stale' && !checking(c.name) && c.record);
    $('banner').hidden = !stale.length;
    $('banner-text').textContent = `${stale.length} result${stale.length > 1 ? 's' : ''} no longer match${stale.length > 1 ? '' : 'es'} the file: ${stale.map(c => c.name).join(', ')}.`;
    const problem = d.error ? `The notebook could not be read: ${d.error}` : d.metadata_error ? `The check records could not be read, so no result is shown: ${d.metadata_error}` : UI.notice;
    $('load-error').hidden = !problem;
    $('load-error').textContent = problem || '';
    // A linked notebook is checked in its own preview, never from this one.
    $('inspect-project').hidden = !project || linked;
    $('inspect-project').disabled = checkBusy();
    $('check-project').hidden = !project || linked;
    // One banner for a project Glitnir does not check; never on a static page.
    const unsupported = leanOff() || walkthroughUnchecked();
    $('lean-unsupported').hidden = !unsupported;
    // The message already names any lean-toolchain read error: shown verbatim.
    $('lean-unsupported-text').textContent = unsupported ? walkthroughMessage() || d.preview.lean?.message || '' : '';
    if (unsupported) $('check-project').hidden = true;
    if (leanOff()) $('inspect-project').hidden = true;
    $('check-project').disabled = checkBusy();
    $('project-check').hidden = !project || linked || (!d.preview.check && !walkthroughUnchecked());
    $('project-check').innerHTML = project ? projectCheckHTML(d.preview.check, d.sha256) : '';
    const inspected = inspection();
    $('inspect-project').textContent = inspecting() ? 'Inspecting…' : 'Inspect project environment';
    $('project-inspection').hidden = !project || linked || (!inspected && !inspecting());
    $('project-inspection').innerHTML = inspecting() ? `Inspecting the saved project setup… <button type="button" class="secondary" data-act="inspect-cancel">${icon('x')}Stop</button>` : inspected ? `Last inspection: ${esc(inspected.message)}` + projectResolutionHTML(inspected.resolution) : '';
    if (project) {
      // The check actions live in the banner, so it stays; it is amber only
      // when a result needs attention: out of date, withheld or unavailable.
      $('banner').hidden = false;
      const env = d.preview.project;
      const where = `${env.kind === 'lake' ? 'Project: ' + env.root : 'Loose Lean file'}. ${env.toolchain ? 'Declared toolchain: ' + env.toolchain : 'No toolchain declared.'}`;
      const unavailable = d.preview.check && !['running', 'checked_snapshot'].includes(d.preview.check.status);
      const attention = [
        outOfDate ? `${outOfDate} saved result${outOfDate > 1 ? 's are' : ' is'} out of date.` : '',
        d.project_withheld && !covered ? 'Checking stopped before the end of the file.' : '',
        unavailable ? 'The last check could not run.' : '',
      ].filter(Boolean);
      $('banner').classList.toggle('quiet', !attention.length);
      // Saved results shown as recorded: nothing here checks or compares them.
      $('banner').classList.toggle('recorded', recorded());
      $('banner').dataset.state = recorded() ? 'recorded' : '';
      if (recorded()) { $('check-project').hidden = true; $('inspect-project').hidden = true; $('project-check').hidden = true; }
      const notice = linked ? `Read-only view of ${d.file} with its own saved results${d.preview.linked?.compared === false ? ', not compared with this project’s environment because it belongs to another Lake project' : ''}. Nothing is checked, run or drawn for it here; open it in its own preview to check it.` : unsupported ? 'Saved-source preview.' : d.preview.notice;
      // A check through one claim is deliberate: said, not flagged.
      const scope = covered ? `The last check covered lines 1–${covered} only.` : '';
      $('banner-text').textContent = [...attention, scope, notice, where].filter(Boolean).join(' ');
    } else { $('banner').classList.remove('quiet', 'recorded'); $('banner').dataset.state = ''; }
    $('export-html').disabled = linked || Boolean(UI.exporting);
    $('export-html').title = linked ? `Go back to ${UI.main?.file || 'the notebook'} to export it` : '';
    $('preview-footer').textContent = project ? 'Checks are explicit and describe a captured saved file and its dependencies. Results are shown separately from source.' : 'Statuses describe saved files checked by sandboxed Lean.';
    const revisions = d.revisions || [];
    const effect = h => (h.made_stale || []).length ? `Made stale: ${h.made_stale.join(', ')}` : h.prose_only ? 'Prose only · nothing went stale' : 'No checked result went stale';
    $('notebook-activity').innerHTML = '<h3>Notebook activity</h3><div class="small muted">Saved activity from the notebook\'s metadata file. These records summarize changes; they do not contain restorable source versions. Saves in VS Code are not added to this log.</div>'
      + (d.unrecorded_change ? '<p class="small warn-text">The saved source differs from the last activity record.</p>' : '')
      + (revisions.length
        ? [...revisions].reverse().map(h => `<div class="rev"><b>r${esc(h.rev)}</b><span>${esc(h.label)} <span class="muted">${when(h.time)}</span></span><span class="effect" data-hit="${(h.made_stale || []).length > 0}">${esc(effect(h))}</span></div>`).join('')
        : '<p class="small muted">No recorded notebook activity.</p>');
    $('outline').innerHTML = d.cells.flatMap(cell => {
      if (cell.kind === 'claim') {
        const c = claim(cell.id);
        return c ? [`<li><button type="button" data-act="goto" data-id="${esc(c.name)}" aria-current="${UI.selected === c.name}" title="${esc(c.name)}"><span class="o-claim">${breakableName(c.name)}</span><span class="dot" data-tone="${statusOf(c).tone}" title="${statusOf(c).label}"></span></button></li>`] : [];
      }
      if (cell.kind === 'source' && cell.claim) {
        const c = claim(cell.claim);
        return c ? [`<li><button type="button" data-act="goto" data-id="${esc(c.name)}" aria-current="${UI.selected === c.name}" title="${esc(c.name)}"><span class="o-claim">${breakableName(claimLabel(c))}</span><span class="dot" data-tone="${toneOf(c)}" title="${esc(statusOf(c).label)}${currencyNoted(c.project) ? ` · ${currencyText(c.project)}` : ''}"></span></button></li>`] : [];
      }
      if (cell.kind === 'prose') {
        return [...cell.markdown.matchAll(/^\s*(#{2,6}) (.+)$/gm)].map((m, i) => `<li class="outline-level-${m[1].length}"><button type="button" data-act="scroll" data-id="notebook-heading-${cell.index}-${i}">${inlineHTML(m[2].trim().replace(/\s+\{fold\}$/, ''))}</button></li>`);
      }
      return [];
    }).join('');
    $('chips-row').innerHTML = cs.map(c => `<button type="button" data-act="goto" data-id="${esc(c.name)}"><span class="dot" data-tone="${toneOf(c)}"></span> ${esc(claimLabel(c))}</button>`).join('');
    document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === UI.mode)));
    $('doc').hidden = UI.mode !== 'doc';
    $('source').hidden = UI.mode !== 'source';
    $('reading-view').hidden = UI.mode === 'graph';
    $('graph-view').hidden = UI.mode !== 'graph';
    $('workspace').classList.toggle('no-panel', !UI.panel);
    $('panel-show').hidden = UI.panel;
    // Project records hold uses only from checks that recorded them: without
    // any, show no graph rather than a graph without its arcs.
    const graph = !project || cs.some(c => Array.isArray(c.project?.uses));
    for (const id of ['graph-label', 'graph', 'graph-note']) $(id).hidden = !graph;
    $('graph-note').textContent = project
      ? 'Arcs come from Lean: the claims each claim’s statement or proof names directly, as its last check recorded them.'
      : 'Arcs come from Lean: the earlier claims a proof\'s axioms name.';
    if (graph) drawGraph();
    renderConnection();
  }

  // Source-based identities survive line shifts; occurrences distinguish
  // identical declarations in different scopes. No source is interpreted here.
  function readingDetails() {
    const counts = new Map();
    return [$('doc'), $('insp'), $('diagram-dialog')].filter(Boolean).flatMap(root => [...root.querySelectorAll('details[data-fold-key]')].map(fold => {
      const identity = JSON.stringify([root.id, fold.dataset.foldKey]);
      const occurrence = counts.get(identity) || 0;
      counts.set(identity, occurrence + 1);
      return {fold, key: JSON.stringify([identity, occurrence])};
    }));
  }
  // Save synchronously before replacing nodes: native toggle events can arrive
  // after a refresh. Remember closed folds too, even when their default is open.
  function saveReadingFolds() {
    let focus = null;
    for (const {fold, key} of readingDetails()) {
      readingFolds.set(key, fold.open);
      if (document.activeElement === fold.querySelector('summary')) focus = key;
    }
    return focus;
  }
  function restoreReadingFolds(focus) {
    for (const {fold, key} of readingDetails()) {
      if (readingFolds.has(key)) fold.open = readingFolds.get(key);
      if (key === focus) fold.querySelector('summary').focus({preventScroll: true});
    }
  }

  function render() {
    if (!UI.data) return;
    const diagramUI = saveDiagramUI();
    const documentFocus = UI.mode === 'doc' && document.activeElement?.classList.contains('document-target');
    const foldFocus = saveReadingFolds();
    const linkSelector = '.source-link,.source-edit,.notebook-reference,.external-link';
    const linkFocus = $('doc').contains(document.activeElement) && document.activeElement.matches(linkSelector) ? {index: [...$('doc').querySelectorAll(linkSelector)].indexOf(document.activeElement), sha: document.activeElement.dataset.sha, href: document.activeElement.getAttribute('href')} : null;
    const sourceControl = UI.mode === 'source' && document.activeElement?.matches('#source [data-act]') ? {...document.activeElement.dataset} : null;
    const sourceFocus = UI.mode === 'source' && document.activeElement?.hasAttribute('data-source-line') ? document.activeElement.id : null;
    const activeGraphControl = UI.mode === 'graph' ? document.activeElement?.closest?.('#graph-view [data-act]') : null;
    const graphFocus = activeGraphControl ? {...activeGraphControl.dataset} : null;
    const scroll = $('insp').scrollTop;
    const goalFocus = Boolean(document.activeElement?.matches('#insp .goal-section [data-act]'));
    if (!heldRendering()) {
      if (UI.mode === 'doc') renderDoc();
      else if (UI.mode === 'source') renderSource();
      else renderProofGraph();
    }
    renderInspector();
    renderChrome();
    typesetMath($('doc'), $('insp'), $('title'), $('outline'), $('graph-view'));
    renderDiagramDialog();
    restoreReadingFolds(foldFocus);
    if (UI.mode === 'doc' && UI.documentLine?.sha === UI.data.sha256) {
      const target = documentTarget(UI.documentLine.line);
      if (target) { target.classList.add('document-target'); target.tabIndex = -1; if (documentFocus) target.focus({preventScroll: true}); }
    }
    if (sourceFocus) $(sourceFocus)?.focus({preventScroll: true});
    if (sourceControl?.sha === UI.data.sha256) {
      const control = [...$('source').querySelectorAll('[data-act]')].find(el => el.dataset.act === sourceControl.act && el.dataset.line === sourceControl.line);
      control?.focus({preventScroll: true});
    }
    if (graphFocus && UI.mode === 'graph') {
      const control = [...$('graph-view').querySelectorAll('[data-act]')].find(el => el.dataset.act === graphFocus.act && el.dataset.graphId === graphFocus.graphId && el.dataset.graphDetail === graphFocus.graphDetail);
      control?.focus({preventScroll: true});
    }
    if (linkFocus && UI.mode === 'doc' && (!linkFocus.sha || linkFocus.sha === UI.data.sha256)) {
      const link = $('doc').querySelectorAll(linkSelector)[linkFocus.index];
      if (link?.getAttribute('href') === linkFocus.href) link.focus({preventScroll: true});
    }
    $('insp').scrollTop = scroll;
    if (goalFocus) $('insp').querySelector('.goal-section [data-act]')?.focus({preventScroll: true});
    restoreDiagramUI(diagramUI);
    markLiveChanges();

  }

  // Export from the saved snapshot, never an editor draft or the selected tab.
  async function exportHTML() {
    if (!UI.data || UI.exporting || linkedPath()) return;
    UI.exporting = true;
    $('export-html').disabled = true;
    try {
      if (!window.katex) throw new Error('The math renderer is still loading. Reopen the preview and try again.');
      // Embed every image the snapshot shows, so the page works offline. A
      // poll may replace the snapshot meanwhile, so wait until the current
      // one's images are loaded, then render that one snapshot without pause.
      const unloaded = () => (UI.data.assets || []).filter(a => a.status === 'ok' && a.sha256 && !(images.has(assetPath(a)) && !images.get(assetPath(a)).loading));
      for (let round = 0; round < 3 && unloaded().length; round++) await Promise.all(unloaded().map(a => loadImage(assetPath(a))));
      const snapshot = UI.data;
      if (snapshot.error) throw new Error(snapshot.error);
      if (snapshot.metadata_error) throw new Error(snapshot.metadata_error);
      const sourceCharacters = snapshot.cells.reduce((total, cell) => total + cell.text.length, 0);
      const sourceLines = snapshot.cells.reduce((total, cell) => total + cell.end - cell.start + 1, 0);
      if (sourceCharacters > MAX_EXPORT_SOURCE_CHARACTERS || sourceLines > MAX_EXPORT_SOURCE_LINES) {
        throw new Error('The saved source is too large to export safely as an interactive HTML page.');
      }
      const exportTitle = title();
      const renderedTitle = document.createElement('h1');
      renderedTitle.innerHTML = inlineHTML(exportTitle);
      const content = document.createElement('div');
      const scopes = namespaceScopes(snapshot.cells);
      content.innerHTML = proofGraphHTML(snapshot, {staticPage: true}) + withheldBannerHTML(snapshot) + snapshot.cells.map(cell => cellView(cell, true, scopes)).join('');
      const source = document.createElement('section');
      source.innerHTML = sourceViewHTML(true);
      proofSections(content, true);
      if (snapshot.preview?.check?.status === 'checked_snapshot') content.insertAdjacentHTML('beforeend', projectCheckHTML(snapshot.preview.check, snapshot.sha256));
      if (content.querySelector('.view-wait')) throw new Error('Wait for the diagrams to finish drawing, then export again.');
      if (content.querySelector('.image-wait')) throw new Error('An image is still loading. Export again in a moment.');
      const defs = document.querySelector('svg.defs');
      if (defs) content.append(defs.cloneNode(true));
      typesetMath(content, renderedTitle);
      const response = await fetch('/api/export/styles', {headers: API});
      if (!response.ok) throw new Error('The server could not supply export styles. Restart the notebook server after updating Glitnir.');
      const {css} = await response.json();
      const modules = [...new Set((snapshot.references || []).map(item => item.module).filter(module => /^[A-Za-z_][A-Za-z0-9_]*$/.test(module)))].sort();
      const target = HOST?.prepareHTMLExport ? await HOST.prepareHTMLExport(modules) : {saved: true, siblings: []};
      if (!target.saved) return;
      const html = await window.glitnirExport({content, source, title: exportTitle, renderedTitle, file: snapshot.file, sha256: snapshot.sha256, preview: snapshot.preview, css, availableExports: target.siblings || []});
      if (window.glitnirHost) await window.glitnirHost.saveHTML(html, target.token);
      else {
        const url = URL.createObjectURL(new Blob([html], {type: 'text/html;charset=utf-8'}));
        const link = document.createElement('a'); link.href = url; link.download = snapshot.file.replace(/\.lean$/i, '') + '.html';
        document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { UI.notice = `Export failed: ${error.message || error}`; render(); }
    finally { UI.exporting = false; $('export-html').disabled = false; }
  }

  $('export-html').onclick = exportHTML;

  // ----- keyboard shortcuts ------------------------------------------------------
  // The one list of shortcuts: the help dialog and the contents hint are drawn
  // from it, and the user guide's table (docs/guide.md) must match it (a test compares them).
  // `editor` keys exist only in the VS Code preview, whose review layer
  // (review.js) handles n/p, c/t and e; `editorText` is their wording there.
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  const SHORTCUTS = [
    {group: 'Navigate', keys: ['j', 'k'], hint: 'claims', text: 'Next / previous claim'},
    {group: 'Navigate', keys: ['n', 'p'], hint: 'needs attention', editor: true,
      text: 'Next / previous claim that needs attention: out of date, not rechecked, contains sorry, additional assumptions, or where checking stopped. In Proof steps: next / previous step'},
    {group: 'Navigate', keys: ['c', 't'], hint: 'claims / proof steps', editor: true, text: 'Claims / Proof steps'},
    {group: 'View', keys: ['s'], hint: 'document / source', text: 'Switch between document and Lean source'},
    {group: 'View', keys: ['g'], hint: 'proof graph', text: 'Show the recorded proof graph'},
    {group: 'View', keys: ['i'], hint: 'inspector', text: 'Show or hide the claim inspector'},
    {group: 'Check and edit', keys: ['r'], hint: 'check through claim', text: 'Check the saved file through the selected claim',
      editorText: 'Check the saved file through the selected claim (in Proof steps: all of it)'},
    {group: 'Check and edit', keys: ['e'], hint: 'edit in VS Code', editor: true, text: 'Edit the selected claim or proof step in VS Code'},
    {group: 'General', keys: ['?'], hint: 'all shortcuts', text: 'Show these shortcuts'},
    {group: 'General', keys: ['Esc'], text: 'Close this dialog or the History panel',
      editorText: 'Close the dialog; in Proof steps, return to Claims'},
    {group: 'General', keys: MAC ? ['⌘K', 'G'] : ['Ctrl+K', 'G'], chord: true, editor: true,
      text: 'In the Lean editor: show the cursor’s place in Glitnir. Release the first keys before G'},
  ].filter(s => HOST || !s.editor);
  const chipsHTML = s => s.keys.map(k => `<kbd>${esc(k)}</kbd>`).join(s.chord ? ' <span class="muted">then</span> ' : ' ');
  function renderShortcuts() {
    $('keys-intro').textContent = 'Single keys work while the notebook has focus, outside text fields. Moving never starts a check.';
    const groups = [...new Set(SHORTCUTS.map(s => s.group))];
    $('keys-groups').innerHTML = groups.map(group => `<section class="keys-group"><h4 class="label">${esc(group)}</h4><dl>${SHORTCUTS.filter(s => s.group === group)
      .map(s => `<dt>${chipsHTML(s)}</dt><dd>${esc((HOST && s.editorText) || s.text)}</dd>`).join('')}</dl></section>`).join('');
    $('key-hints').innerHTML = SHORTCUTS.filter(s => s.hint).map(s => `<dt>${chipsHTML(s)}</dt><dd>${esc(s.hint)}</dd>`).join('');
  }
  renderShortcuts();

  // ----- interaction -------------------------------------------------------------------
  function select(name, scroll) {
    UI.sourceCell = null;
    UI.selected = name;
    if (UI.mode === 'graph') UI.graphSelected = `local:${name}`;
    render();
    if (!scroll) return;
    const el = UI.mode === 'doc' ? $('cell-' + name) : UI.mode === 'source' ? document.querySelector(`#source tr[data-claim="${CSS.escape(name)}"]`) : document.querySelector(`#graph-view [data-graph-name="${CSS.escape(name)}"]`);
    if (el) { revealSection(el); el.scrollIntoView?.({behavior: reduced() ? 'auto' : 'smooth', block: UI.mode === 'doc' ? 'start' : 'center'}); }
  }
  function showSourceLines(start, end) {
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return;
    if (!UI.data.cells.some(cell => start >= cell.start && start < cell.start + cell.text.split('\n').length)) return;
    UI.sourceRange = {start, end, sha: UI.data.sha256};
    UI.mode = 'source';
    render();
    const row = $('source-L' + start);
    row?.focus({preventScroll: true});
    row?.scrollIntoView({behavior: reduced() ? 'auto' : 'smooth', block: 'center'});
  }

  // Prefer the individual card's range over its enclosing multi-command cell.
  function documentTarget(line) {
    const directive = [...$('doc').querySelectorAll('.cell[data-document-start]')].find(card => Number(card.dataset.documentStart) <= line && line <= Number(card.dataset.documentEnd));
    if (directive) return directive;
    const links = [...$('doc').querySelectorAll('.cell .source-link')]
      .filter(link => Number(link.dataset.start) <= line && line <= Number(link.dataset.end))
      .sort((a, b) => (a.dataset.end - a.dataset.start) - (b.dataset.end - b.dataset.start));
    if (links.length) return links[0].closest('.cell');
    const cells = UI.data.cells;
    const cell = cells.find(c => c.start <= line && line <= c.end);
    const wrap = cell && $('doc').querySelector(`.cellwrap[data-index="${cell.index}"]`);
    const own = wrap?.matches('.cell') ? wrap : wrap?.querySelector('.cell');
    if (own) return own;
    // Imports and setup have no reading card: return to the nearest visible cell.
    return [...$('doc').querySelectorAll('.cellwrap')].sort((a, b) => {
      const distance = wrap => { const c = cells.find(c => c.index === Number(wrap.dataset.index)); return Math.min(Math.abs(c.start - line), Math.abs(c.end - line)); };
      return distance(a) - distance(b);
    })[0]?.querySelector('.cell');
  }
  function showDocument(line = UI.sourceRange?.start, sha = UI.sourceRange?.sha) {
    const valid = sha === UI.data.sha256 && Number.isSafeInteger(line) && line > 0;
    UI.documentLine = valid ? {line, sha} : null;
    UI.mode = 'doc';
    const cell = valid && UI.data.cells.find(c => c.start <= line && line <= c.end);
    if (cell) { UI.sourceCell = {index: cell.index, sha256: sha}; if (cell.kind === 'claim') UI.selected = cell.id; else if (cell.claim && claim(cell.claim)) UI.selected = cell.claim; }
    render();
    const target = valid && documentTarget(line);
    if (target) { revealSection(target); target.focus({preventScroll: true}); target.scrollIntoView({behavior: reduced() ? 'auto' : 'smooth', block: 'center'}); }
    else document.querySelector('[data-mode="doc"]').focus({preventScroll: true});
  }

  function move(step) {
    const names = claims().map(c => c.name);
    if (!names.length) return;
    const i = names.indexOf(UI.selected);
    select(names[(i + step + names.length) % names.length], true);
  }

  document.addEventListener('change', e => {
    if (!e.target.matches('[data-diagram-contract]')) return;
    const key = e.target.dataset.diagramContract;
    if (!key.endsWith(UI.data.sha256)) return;
    (UI.diagrams[key] ||= {}).contract = Number(e.target.value); render();
  });

  document.addEventListener('click', e => {
    const history = $('history');
    if (history.open && !history.contains(e.target)) history.open = false;
    const b = e.target.closest('[data-act]');
    if (!b) {
      const cell = e.target.closest('[data-claim]');
      if (cell && !e.target.closest('button,summary,a') && UI.selected !== cell.dataset.claim) select(cell.dataset.claim, false);
      return;
    }
    if (b.closest('.proof-section > summary')) e.preventDefault();
    const id = b.dataset.id;
    switch (b.dataset.act) {
      case 'git-refresh': void loadGitHistory(true); break;
      case 'git-diff': void showGitDiff(b.dataset.sha); break;
      case 'diagram': diagramAction(b); break;
      case 'diagram-close': closeDiagram(); break;
      case 'diagram-condition': { e.preventDefault(); const details = document.getElementById(b.getAttribute('href').slice(1)); if (details) { details.open = true; details.querySelector('summary').focus(); details.scrollIntoView({block: 'nearest'}); } break; }
      case 'document-return': showDocument(); break;
      case 'document-line': e.preventDefault(); showDocument(Number(b.dataset.line), b.dataset.sha); break;
      case 'source-lines': e.preventDefault(); showSourceLines(Number(b.dataset.start), Number(b.dataset.end)); break;
      case 'source': if (linkedPath()) break; if (b.dataset.start && HOST) HOST.openSource({index: Number(b.dataset.index), sha256: b.dataset.sha, range: {start: Number(b.dataset.start), end: Number(b.dataset.end)}}); else openSource(Number(b.dataset.index)); break;
      case 'evidence-run': if (leanOff()) break; runEvidence(Number(b.dataset.line), b.dataset.sha); break;
      case 'view-cancel': cancelView(b); break;
      case 'inspect-cancel': cancelInspection(); break;
      case 'check-cancel': cancelCheck(); break;
      case 'view-retry': if (UI.drawings[b.dataset.key]?.error) { delete UI.drawings[b.dataset.key]; render(); } break;
      case 'decision-run': if (leanOff()) break; decisionAction('/api/decide/run', {sha256: b.dataset.sha, line: Number(b.dataset.line)}); break;
      case 'decision-stop': decisionAction('/api/decide/stop', {}); break;
      case 'decision-discard': decisionAction('/api/decide/discard', {sha256: b.dataset.sha, line: Number(b.dataset.line)}); break;
      case 'goto': closeDiagram(); select(id, true); break;
      case 'graph-node': {
        const node = graphNode(b.dataset.graphId);
        if (!node) break;
        UI.graphSelected = node.id;
        if (node.kind === 'walkthrough' && node.sourceLine) { showDocument(node.sourceLine, UI.data.sha256); break; }
        if (node.kind === 'local' || node.kind === 'walkthrough' && node.name) { UI.mode = 'doc'; select(node.name, true); break; }
        if (node.path) {
          const path = [...(linkedPath() || '').split('/').slice(0, -1), node.path].join('/');
          const link = path === UI.main?.file ? '' : path;
          UI.pendingClaim = node.name; openNotebook(link);
        } else render();
        break;
      }
      case 'graph-zoom-in': UI.graphZoom = Math.min(8, (UI.graphZoom || 1) + .25); render(); break;
      case 'graph-zoom-out': UI.graphZoom = Math.max(.5, (UI.graphZoom || 1) - .25); render(); break;
      case 'graph-fit': UI.graphZoom = 1; render(); break;
      case 'open-notebook': e.preventDefault(); closeDiagram(); UI.pendingClaim = b.dataset.claim || null; openNotebook(b.dataset.link || null); break;
      case 'notebook-back': showNotebookAt(UI.at - 1); break;
      case 'notebook-forward': showNotebookAt(UI.at + 1); break;
      case 'notebook-at': showNotebookAt(Number(b.dataset.at)); break;
      case 'goal': UI.selected = id; inspectGoals(b.dataset.refresh === 'true'); break;
      case 'goal-cancel': cancelGoals(); break;
      // Stale buttons of an earlier saved file never check other lines.
      case 'check-through': if (b.dataset.sha === UI.data.sha256 && claim(id)) { UI.selected = id; checkProject(Number(b.dataset.line)); } break;
      case 'scroll': {
        UI.mode = 'doc';
        render();
        const heading = document.getElementById(id);
        if (heading) {
          revealSection(heading);
          const target = heading.closest('summary') || heading;
          if (target === heading) target.tabIndex = -1;
          target.focus({preventScroll: true});
          target.scrollIntoView({block: 'start', behavior: reduced() ? 'auto' : 'smooth'});
        }
        break;
      }
    }
  });
  $('source').addEventListener('click', e => { const tr = e.target.closest('tr[data-claim]'); if (tr && !e.target.closest('[data-act]')) select(tr.dataset.claim, false); });
  $('graph-view').addEventListener('change', e => {
    const picker = e.target.closest('[data-act="graph-detail-picker"]');
    if (!picker) return;
    UI.graphSelected = picker.value;
    render();
  });
  document.querySelectorAll('[data-mode]').forEach(b => { b.onclick = () => { if (b.dataset.mode === 'doc' && UI.mode === 'source') showDocument(); else { UI.mode = b.dataset.mode; render(); } }; });
  $('inspect-project').onclick = inspectProject;
  $('check-project').onclick = () => checkProject();
  $('panel-hide').onclick = () => { UI.panel = false; render(); };
  $('panel-show').onclick = () => { UI.panel = true; render(); };
  const keys = open => { $('keys').hidden = !open; if (open) $('keys-close').focus(); };
  $('keys-open').onclick = () => keys(true);
  $('keys-close').onclick = () => keys(false);
  $('keys').onclick = e => { if (e.target === $('keys')) keys(false); };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      keys(false);
      $('history').open = false;
      return;
    }
    const graphAction = e.target.closest?.('#graph-view [data-act="graph-node"]');
    if (graphAction && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      graphAction.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      return;
    }
    if (e.target.closest('input,textarea,select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const map = {
      j: () => move(1), k: () => move(-1), '?': () => keys(true),
      s: () => { if (UI.mode === 'source') showDocument(); else { UI.mode = 'source'; render(); } },
      g: () => { UI.mode = 'graph'; render(); },
      i: () => { UI.panel = !UI.panel; render(); },
      // Check through the selected claim; Check saved file checks all of it.
      r: () => { const c = claim(UI.selected); if (c?.project && !linkedPath()) checkProject(c.project.end_line); },
    };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });

  Object.assign(workbench, {render, renderChrome, renderConnection, select, showSourceLines});

  load();
})();
