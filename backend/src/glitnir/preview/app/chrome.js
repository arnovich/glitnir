// The workbench's chrome, keyboard shortcuts and interaction; it loads the page.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, API, HOST, LABEL, UI, assetPath, breakableName, cancelCheck, cancelGoals, cancelInspection,
    cancelView, cellView, checkBusy, checkProject, checking, claim, claimLabel, claims, closeDiagram,
    currencyNoted, currencyText, decisionAction, diagramAction, editGoalSource, esc, fetch, heldRendering, helpHTML, icon, images,
    graphNode, inlineHTML, inspectGoals, inspectProject, inspecting, inspection, leanOff, linkedPath, load, loadImage, loadResearch,
    markLiveChanges, namespaceScopes, openNotebook, openSource, projectCheckHTML, projectResolutionHTML,
    proofGraphHTML, proofProgressHTML, proofSections, readingFolds, recorded, reduced, renderDiagramDialog, renderDoc, renderInspector, researchAction,
    renderProofGraph, renderSource, renderTrail, requestedCut, restoreDiagramUI, revealSection, runEvidence, saveDiagramUI, sourceViewHTML,
    selectGoalStep, showNotebookAt, statusOf, title, toneOf, typesetMath, walkthroughMessage, walkthroughUnchecked, when, withheldBannerHTML} = workbench;
  const MAX_EXPORT_SOURCE_CHARACTERS = 1_000_000;
  const MAX_EXPORT_SOURCE_LINES = 20_000;
  let helpTimer;
  function hideHelp() {
    clearTimeout(helpTimer);
    document.querySelectorAll('.help-tooltip').forEach(tip => { tip.hidden = true; });
  }
  function showHelp(help) {
    hideHelp();
    const button = help.querySelector('.help-icon'), tip = help.querySelector('.help-tooltip');
    tip.hidden = false;
    const rect = button.getBoundingClientRect();
    tip.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - tip.offsetWidth - 12))}px`;
    tip.style.top = `${Math.max(12, rect.bottom + tip.offsetHeight + 8 < window.innerHeight ? rect.bottom + 6 : rect.top - tip.offsetHeight - 6)}px`;
  }
  document.addEventListener('focusin', event => {
    const help = event.target.closest('.context-help');
    if (help) showHelp(help);
  });
  document.addEventListener('focusout', event => {
    const help = event.target.closest('.context-help');
    if (help && !help.contains(event.relatedTarget) && !help.matches(':hover')) hideHelp();
  });
  document.addEventListener('mouseover', event => {
    const help = event.target.closest('.context-help');
    if (help) clearTimeout(helpTimer);
    if (help && !help.contains(event.relatedTarget)) showHelp(help);
  });
  document.addEventListener('mouseout', event => {
    const help = event.target.closest('.context-help');
    if (help && !help.contains(event.relatedTarget) && !help.contains(document.activeElement)) helpTimer = setTimeout(hideHelp, 200);
  });
  document.addEventListener('scroll', hideHelp, true);
  window.addEventListener('resize', hideHelp);
  $('claim-graph-shortcut').innerHTML = '<button type="button" class="ghost" data-act="open-claim-graph">View claim graph</button>'
    + helpHTML('claim-graph', 'About the claim graph', 'Open the graph in Explore. Select a statement to see its dependencies.');
  // ----- chrome -----------------------------------------------------------------------
  function renderConnection() {
    for (const shortcut of document.querySelectorAll('[data-act="open-claim-graph"]')) {
      shortcut.disabled = heldRendering();
      shortcut.title = shortcut.disabled ? 'Available when the notebook reconnects.' : '';
    }
    $('conn').dataset.state = UI.connected ? 'up' : 'down';
    $('conn-text').textContent = UI.connected ? 'Connected' : 'Reconnecting…';
    $('connection-detail').textContent = UI.connected ? 'Connected to the local notebook server' : 'Server not reachable · retrying';
    $('status-summary').setAttribute('aria-label', `Notebook status: ${[$('conn-text').textContent, $('proof-availability').hidden ? '' : $('proof-availability').textContent, $('subline').textContent, $('live-status').textContent].filter(Boolean).join(' · ')}. Show details`);
    $('status-summary').title = [
      $('connection-detail').textContent,
      $('file-state').title,
      $('lean-unsupported').hidden ? $('subline').textContent : $('lean-unsupported-text').textContent,
      $('live-status').textContent,
    ].filter(Boolean).join('\n');
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

  $('history').addEventListener('toggle', () => {
    if ($('history').open) { $('notebook-view-options').open = true; void loadGitHistory(); }
  });
  function sizeActionsMenu() {
    if (!$('notebook-view-options').open) return;
    const panel = $('notebook-view-options').querySelector('.view-options-panel');
    panel.style.maxHeight = `${Math.max(0, window.innerHeight - panel.getBoundingClientRect().top - 12)}px`;
  }
  $('notebook-view-options').addEventListener('toggle', sizeActionsMenu);
  window.addEventListener('resize', sizeActionsMenu);
  renderGitHistory();

  let outlineOwner = null, outlineCollapsed = new Set(), outlineWidth = 260;
  let outlineSelection = null, revealOutline = false;
  const sidebarHandle = $('outline-resize');
  function saveOutlineLayout() {
    const value = {width: outlineWidth, collapsed: [...outlineCollapsed].slice(0, 2000)};
    try {
      if (window.glitnirHost) window.glitnirHost.saveNotebookLayout?.(outlineOwner, value);
      else localStorage.setItem('glitnir.outline.' + outlineOwner, JSON.stringify(value));
    } catch { /* Preferences are optional. */ }
  }
  function applyOutlineWidth() {
    const available = $('workspace').clientWidth || window.innerWidth;
    const max = Math.max(200, Math.min(480, Math.floor(available * .32)));
    const width = Math.min(max, Math.max(200, outlineWidth));
    $('workspace').style.setProperty('--outline-width', width + 'px');
    sidebarHandle.setAttribute('aria-valuemax', String(max));
    sidebarHandle.setAttribute('aria-valuenow', String(width));
  }
  function readOutlineLayout() {
    const key = JSON.stringify([UI.data.source_path || UI.data.preview?.project?.root || '', UI.data.file, linkedPath() || '']);
    if (outlineOwner !== key) {
      outlineOwner = key; outlineCollapsed = new Set(); outlineWidth = 260; outlineSelection = UI.outlineTarget || UI.selected || '';
      try {
        const value = window.glitnirHost ? window.glitnirHost.readNotebookLayout?.(key) : JSON.parse(localStorage.getItem('glitnir.outline.' + key) || 'null');
        if (Number.isFinite(value?.width)) outlineWidth = Math.max(200, Math.min(480, value.width));
        if (Array.isArray(value?.collapsed)) outlineCollapsed = new Set(value.collapsed.slice(0, 2000).filter(v => typeof v === 'string' && v.length < 4096));
      } catch { /* Ignore unreadable preferences. */ }
    }
    applyOutlineWidth();
  }
  $('outline').addEventListener('click', event => {
    const toggle = event.target.closest('[data-outline-toggle]');
    if (!toggle) return;
    const key = toggle.dataset.outlineKey;
    if (outlineCollapsed.has(key)) outlineCollapsed.delete(key); else outlineCollapsed.add(key);
    saveOutlineLayout(); renderOutline();
  });
  function resizeOutline(width, persist = true) {
    const max = Number(sidebarHandle.getAttribute('aria-valuemax'));
    outlineWidth = Math.max(200, Math.min(max, width)); applyOutlineWidth();
    if (persist) saveOutlineLayout();
  }
  let sidebarDrag = null;
  sidebarHandle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    sidebarDrag = {id: event.pointerId, x: event.clientX, width: Number(sidebarHandle.getAttribute('aria-valuenow'))};
    sidebarHandle.setPointerCapture?.(event.pointerId); sidebarHandle.focus({preventScroll: true}); event.preventDefault();
  });
  sidebarHandle.addEventListener('pointermove', event => {
    if (sidebarDrag?.id === event.pointerId) resizeOutline(sidebarDrag.width + event.clientX - sidebarDrag.x, false);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) sidebarHandle.addEventListener(type, () => {if (sidebarDrag) saveOutlineLayout(); sidebarDrag = null;});
  sidebarHandle.addEventListener('keydown', event => {
    const width = Number(sidebarHandle.getAttribute('aria-valuenow'));
    const next = {ArrowLeft: width - 20, ArrowRight: width + 20, Home: 200, End: Number(sidebarHandle.getAttribute('aria-valuemax'))}[event.key];
    if (next === undefined) return;
    event.preventDefault(); event.stopPropagation(); resizeOutline(next);
  });
  sidebarHandle.addEventListener('dblclick', () => resizeOutline(260));
  window.addEventListener('resize', applyOutlineWidth);

  function renderOutline() {
    readOutlineLayout();
    const outline = $('outline'), rail = outline.closest('.rail');
    const scroll = rail.scrollTop;
    const focused = outline.contains(document.activeElement) ? {...document.activeElement.dataset} : null;
    const roots = [], stack = [{level: 1, children: roots}], declarations = [];
    const kinds = new Set(['definition', 'lemma', 'proposition', 'theorem', 'corollary', 'algorithm', 'appendix']);
    for (const [index, el] of [...$('doc').querySelectorAll('[id^="notebook-heading-"],.cell[data-claim],.definition,.context-card,.source-context')].entries()) {
      if (el.matches('[id^="notebook-heading-"]')) {
        const level = Number(el.tagName.slice(1));
        while (stack.length > 1 && stack.at(-1).level >= level) stack.pop();
        const label = (el.dataset.heading || el.textContent).replace(/\s+\{fold\}\s*$/, '').trim();
        el.tabIndex = -1;
        const prefix = /^(Definition|Lemma|Proposition|Theorem|Corollary|Setup)\b/i.exec(label)?.[1].toLowerCase();
        const entry = {id: el.id, label, kind: prefix || 'section', section: true, children: []};
        stack.at(-1).children.push(entry); stack.push({level, children: entry.children});
        continue;
      }
      if (el.closest('.cell[data-claim]') && !el.matches('.cell[data-claim]')) continue;
      const kindLabel = el.querySelector('.def-kind')?.textContent.trim() || '';
      const claimName = el.dataset.claim;
      const c = claimName ? claim(claimName) : null;
      const short = el.querySelector('.card-header dfn code')?.textContent.trim();
      const namespace = el.querySelector('.def-namespace code')?.textContent.trim();
      const name = claimName || [namespace, short].filter(Boolean).join('.');
      let kind = /theorem/i.test(kindLabel) ? 'theorem' : /lemma/i.test(kindLabel) ? 'lemma' : /example/i.test(kindLabel) ? 'example' : /assumption/i.test(kindLabel) ? 'assumption' : 'definition';
      let label = c ? (c.project ? claimLabel(c) : c.name) : short;
      if (!el.matches('.definition,.cell[data-claim]')) {
        const code = el.querySelector('pre')?.textContent.trim() || '';
        if (el.matches('.import-header')) label = el.querySelector('summary')?.firstChild?.textContent.trim();
        else if (el.matches('.source-context') && /^(namespace|section|open|variable|universe|set_option)\b/.test(code)) label = code.split('\n')[0];
        else if (el.matches('.context-card') && !['Note', 'Additional notebook content'].includes(kindLabel)) label = kindLabel;
        else continue;
        kind = 'setup';
      }
      if (!label) label = kindLabel || 'Example';
      const start = el.querySelector('.source-link')?.dataset.start;
      const cell = el.closest('.cellwrap')?.dataset.index || '0';
      if (!el.id) el.id = `notebook-entry-${cell}-${start || index}`;
      el.tabIndex = -1;
      const entry = {id: el.id, label, name, kind, c, children: []};
      stack.at(-1).children.push(entry); declarations.push(entry);
    }
    for (const node of UI.data.walkthrough?.nodes || []) {
      if (!node?.claim || !kinds.has(node.kind)) continue;
      const exact = declarations.filter(e => e.name === node.claim);
      const matches = exact.length ? exact : declarations.filter(e => e.name?.endsWith('.' + node.claim));
      if (matches.length === 1) {
        const entry = matches[0];
        entry.mappedKinds ||= new Set(); entry.mappedKinds.add(node.kind);
      }
    }
    for (const entry of declarations) if (entry.mappedKinds?.size === 1) entry.kind = [...entry.mappedKinds][0];
    const selection = UI.outlineTarget || UI.selected || '';
    const reveal = revealOutline || selection !== outlineSelection;
    outlineSelection = selection; revealOutline = false;
    const prepareAll = entries => {
      let contains = false;
      for (const entry of entries) {
        entry.key = entry.id + '|' + entry.label;
        const childCurrent = prepareAll(entry.children);
        const current = UI.outlineTarget ? UI.outlineTarget === entry.id : entry.c && UI.selected === entry.c.name;
        if (reveal && (current || childCurrent)) outlineCollapsed.delete(entry.key);
        contains ||= Boolean(current || childCurrent);
      }
      return contains;
    };
    prepareAll(roots);
    const html = entries => entries.map(entry => {
      const saved = entry.c, current = UI.outlineTarget ? UI.outlineTarget === entry.id : saved && UI.selected === saved.name;
      const action = saved ? `data-act="goto" data-id="${esc(saved.name)}"` : `data-act="scroll" data-id="${esc(entry.id)}"`;
      const kind = entry.kind[0].toUpperCase() + entry.kind.slice(1);
      const content = entry.section ? inlineHTML(entry.label) : `<span class="o-kind" title="Declaration kind: ${esc(kind)}">${esc(kind)}</span><span class="o-name${saved ? ' o-claim' : ''}">${breakableName(entry.label)}</span>`;
      const dot = saved ? `<span class="dot" data-tone="${toneOf(saved)}" title="${esc(statusOf(saved).label)}${saved.project && currencyNoted(saved.project) ? ' · ' + esc(currencyText(saved.project)) : ''}"></span>` : '';
      const folded = outlineCollapsed.has(entry.key), childrenId = 'outline-children-' + entry.id;
      const toggle = entry.children.length ? `<button type="button" class="outline-toggle" data-outline-toggle="${esc(entry.id)}" data-outline-key="${esc(entry.key)}" aria-label="${folded ? 'Expand' : 'Fold'} ${esc(entry.label)}" aria-expanded="${!folded}" aria-controls="${esc(childrenId)}">${folded ? '▸' : '▾'}</button>` : '';
      return `<li class="${entry.children.length ? 'outline-branch ' : ''}${entry.section ? 'outline-section' : 'outline-declaration'}" data-outline-kind="${entry.kind}">${toggle}<button type="button" ${action} aria-current="${Boolean(current)}" title="${esc(entry.name || entry.label)}">${content}${dot}</button>${entry.children.length ? `<ol id="${esc(childrenId)}"${folded ? ' hidden' : ''}>${html(entry.children)}</ol>` : ''}</li>`;
    }).join('');
    outline.innerHTML = html(roots);
    // Keep the shared cache's full root set, including on standalone chrome updates.
    typesetMath($('doc'), $('insp'), $('title'), outline, $('graph-view'));
    if (focused) [...outline.querySelectorAll('button')].find(b => focused.outlineToggle ? b.dataset.outlineToggle === focused.outlineToggle : b.dataset.act === focused.act && b.dataset.id === focused.id)?.focus({preventScroll: true});
    rail.scrollTop = scroll;
    if (reveal) {
      const current = outline.querySelector('button[aria-current="true"]');
      if (current) {
        const item = current.getBoundingClientRect(), bounds = rail.getBoundingClientRect();
        if (item.top < bounds.top) rail.scrollTop += item.top - bounds.top;
        else if (item.bottom > bounds.bottom) rail.scrollTop += item.bottom - bounds.bottom;
      }
    }
  }

  function renderChrome() {
    const d = UI.data, cs = claims();
    document.title = `${title()} · Glitnir`;
    $('title').innerHTML = inlineHTML(title());
    $('file-name').textContent = d.file;
    $('file-path-tooltip').textContent = d.source_path || d.file;
    $('file-name').setAttribute('aria-description', d.source_path || d.file);
    const linked = Boolean(linkedPath());
    $('location').textContent = linked ? `Linked from ${UI.main?.file || 'the notebook'} · read-only` : '';
    $('location').hidden = !linked;
    renderTrail();
    $('file-state').textContent = `Source hash: ${d.sha256.slice(0, 8)}`;
    $('file-state').title = `Source hash: ${d.sha256}\nSHA-256 of the displayed saved source (not a Git commit)`;
    const count = key => cs.filter(c => c.status === key).length;
    const stat = (n, tone, text) => `<span class="stat"><span class="dot"${tone ? ` data-tone="${tone}"` : ''}></span>${n} ${esc(text)}</span>`;
    const project = d.preview?.mode === 'project';
    const statuses = [...new Set(cs.map(c => c.status))];
    const outOfDate = cs.filter(c => c.project && c.project.current === false).length;
    const uncompared = cs.filter(c => c.project && c.project.current === null).length;
    const notRechecked = cs.filter(c => c.project?.not_rechecked).length;
    const covered = project && requestedCut(d.project_withheld) ? d.project_withheld.through_line : null;
    const bits = walkthroughUnchecked()
      ? []
      : project
      ? [...statuses.map(k => stat(count(k), statusOf({status: k, project: true}).tone, statusOf({status: k, project: true}).label.toLowerCase())), ...(outOfDate ? [stat(outOfDate, 'amber', 'out of date')] : []), ...(uncompared ? [stat(uncompared, '', 'not compared')] : []),
        ...(notRechecked ? [stat(notRechecked, 'muted', 'not rechecked')] : []), ...(covered ? [`<span class="stat project-covered">last check: lines 1–${esc(covered)}</span>`] : [])]
      : [['checked', 'green'], ['refuted', 'red'], ['conditional', 'amber'], ['stale', 'amber'], ['open', ''], ['failed', 'red'], ['unknown', 'amber']]
        .filter(([k]) => count(k)).map(([k, t]) => stat(count(k), t, LABEL[k].toLowerCase()));
    $('subline').innerHTML = (bits.length ? bits.join('') : project && !walkthroughUnchecked() && !d.preview.check && !d.checking.length ? '<span class="stat">No check results</span>' : '') + (d.checking.length ? `<span class="stat checking-badge">Checking ${d.checking.length}</span>` : '');
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
    $('proof-availability').textContent = walkthroughUnchecked() ? 'Not checked' : 'Checking unavailable';
    $('proof-availability').hidden = !unsupported;
    // The message already names any lean-toolchain read error: shown verbatim.
    $('lean-unsupported-text').textContent = unsupported ? walkthroughMessage() || d.preview.lean?.message || '' : '';
    if (unsupported) $('check-project').hidden = true;
    if (leanOff()) $('inspect-project').hidden = true;
    $('check-project').disabled = checkBusy();
    $('project-check').hidden = !project || linked || walkthroughUnchecked() || !d.preview.check;
    $('project-check').innerHTML = project ? projectCheckHTML(d.preview.check, d.sha256) : '';
    const activity = $('run-activity');
    const checkingNow = !linked && project && d.preview.check?.status === 'running';
    if (checkingNow) activity.append($('project-check'));
    else $('load-error').parentElement.insertBefore($('project-check'), $('load-error'));
    const proofRun = UI.goals?.trace?.status === 'running' ? UI.goals : null;
    const showProof = !linked && proofRun && (UI.mode !== 'doc' || !UI.panel || !$('insp').querySelector('.goal-run-progress'));
    $('proof-run-activity').innerHTML = showProof ? proofProgressHTML(proofRun) : '';
    activity.hidden = !checkingNow && !showProof;
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
      const notice = linked ? `Read-only view of ${d.file} with its own saved results${d.preview.linked?.compared === false ? ', not compared with this project’s environment because it belongs to another Lake project' : ''}. Nothing is checked, run or drawn for it here; open it in its own preview to check it.` : unsupported ? '' : d.preview.notice;
      // A check through one claim is deliberate: said, not flagged.
      const scope = covered ? `The last check covered lines 1–${covered} only.` : '';
      $('project-context').textContent = [notice, where].filter(Boolean).join(' ');
      const fallback = d.preview.notice?.match(/The file is shown as source.*$/)?.[0] || '';
      $('banner-text').textContent = [...attention, scope, linked || recorded() ? notice : fallback].filter(Boolean).join(' ');
      $('banner-text').hidden = !$('banner-text').textContent;
      $('banner').hidden = !$('banner-text').textContent && $('check-project').hidden && $('inspect-project').hidden;
    } else { $('project-context').textContent = ''; $('banner-text').hidden = false; $('banner').classList.remove('quiet', 'recorded'); $('banner').dataset.state = ''; }
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
    renderOutline();
    $('chips-row').innerHTML = cs.map(c => `<button type="button" data-act="goto" data-id="${esc(c.name)}"><span class="dot" data-tone="${toneOf(c)}"></span> ${esc(claimLabel(c))}</button>`).join('');
    document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === UI.mode)));
    $('doc').hidden = UI.mode !== 'doc';
    $('source').hidden = UI.mode !== 'source';
    $('reading-view').hidden = UI.mode === 'graph';
    $('graph-view').hidden = UI.mode !== 'graph';
    const notebookView = UI.mode === 'doc';
    document.body.dataset.view = UI.mode;
    $('workspace').classList.toggle('focused-view', !notebookView);
    document.querySelector('.rail').hidden = !notebookView;
    document.querySelector('.inspector').hidden = !notebookView || !UI.panel;
    $('workspace').classList.toggle('no-panel', !UI.panel);
    $('panel-show').hidden = !notebookView || UI.panel;
    const graphAvailable = Boolean(cs.length || d.cells.some(cell => cell.claim));
    $('claim-graph-shortcut').hidden = !graphAvailable;
    $('claim-graph-mobile').hidden = !notebookView || !graphAvailable;
    renderConnection();
  }

  // Source-based identities survive line shifts; occurrences distinguish
  // identical declarations in different scopes. No source is interpreted here.
  function readingDetails() {
    const counts = new Map();
    return [$('doc'), $('insp'), $('diagram-dialog'), $('graph-view')].filter(Boolean).flatMap(root => [...root.querySelectorAll('details[data-fold-key]')].map(fold => {
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

  function render({openClaimGraph = false, openExplore = false, inspectorOnly = false} = {}) {
    if (!UI.data) return;
    workbench.syncAtlasLocation?.(UI.data);
    const diagramUI = saveDiagramUI();
    const helpFocus = document.activeElement?.dataset.helpKey;
    const helpOpen = helpFocus && !$(helpFocus)?.hidden;
    const openHelpKey = document.querySelector('.help-tooltip:not([hidden])')?.id;
    const outlineDocumentFocus = UI.mode === 'doc' && /^notebook-(entry|heading)-/.test(document.activeElement?.id || '') ? document.activeElement.id : null;
    const documentFocus = UI.mode === 'doc' && document.activeElement?.classList.contains('document-target');
    const graphNotesFocus = document.activeElement?.matches('.proof-graph-notes > summary');
    const foldFocus = saveReadingFolds();
    const linkSelector = '.source-link,.source-edit,.source-inspect,.notebook-reference,.atlas-link,.external-link,img[data-act="image-expand"]';
    const linkFocus = $('doc').contains(document.activeElement) && document.activeElement.matches(linkSelector) ? {index: [...$('doc').querySelectorAll(linkSelector)].indexOf(document.activeElement), sha: document.activeElement.dataset.sha, href: document.activeElement.getAttribute('href')} : null;
    const sourceControl = UI.mode === 'source' && document.activeElement?.matches('#source [data-act]') ? {...document.activeElement.dataset} : null;
    const sourceFocus = UI.mode === 'source' && document.activeElement?.hasAttribute('data-source-line') ? document.activeElement.id : null;
    const activeGraphControl = UI.mode === 'graph' ? document.activeElement?.closest?.('#graph-view [data-act]') : null;
    const graphFocus = activeGraphControl ? {...activeGraphControl.dataset} : null;
    const graphHeading = UI.mode === 'graph' && document.activeElement?.matches('#graph-detail h3');
    const graphLink = UI.mode === 'graph' && document.activeElement?.matches('#graph-detail a[href]') ? document.activeElement.getAttribute('href') : null;
    const graphCanvas = $('graph-view').querySelector('.proof-graph-canvas');
    const graphScroll = graphCanvas ? {left: graphCanvas.scrollLeft, top: graphCanvas.scrollTop, sha: UI.data.sha256, scope: graphCanvas.dataset.scope, selected: graphCanvas.querySelector('.chosen')?.dataset.graphId} : null;
    const graphDetail = $('graph-detail');
    const detailScroll = graphDetail ? {left: graphDetail.scrollLeft, top: graphDetail.scrollTop} : null;
    const atlasActive = UI.mode === 'graph' && $('graph-view').contains(document.activeElement) ? document.activeElement : null;
    const atlasFocus = atlasActive ? {controlRegion: ['.atlas-flow', '.atlas-route-controls', '.atlas-visit-history', '.atlas-overview', '.atlas-details', '.atlas-content'].find(selector => atlasActive.closest(selector)), search: atlasActive.id === 'atlas-query', selection: atlasActive.id === 'atlas-query' ? [atlasActive.selectionStart, atlasActive.selectionEnd, atlasActive.selectionDirection] : null,
      summary: atlasActive.matches('.atlas-details > summary') ? '.atlas-details > summary' : atlasActive.matches('.atlas-permalink') ? '.atlas-permalink' : null,
      region: atlasActive.closest('.atlas-compare') ? '.atlas-compare' : atlasActive.closest('.atlas-matches') ? '.atlas-matches' : null, href: atlasActive.getAttribute('href'), heading: atlasActive.id === 'atlas-selected', action: atlasActive.dataset.atlas, value: atlasActive.dataset.value, route: atlasActive.hasAttribute('data-atlas-route'), source: atlasActive.dataset.atlasSource} : null;
    const activityFocus = document.activeElement?.closest('#run-activity')?.querySelector(':focus')?.dataset.act;
    const scroll = $('insp').scrollTop;
    const goalControls = '.goal-section [data-act], .insp-actions [data-act="goal"]';
    const active = document.activeElement;
    const goalFocus = $('insp').contains(active) && active.matches(goalControls) ? active.dataset.act : null;
    const tacticStates = new Map([...$('insp').querySelectorAll('.goal-full-tactic')].map(el => [el.dataset.tacticKey, {left: el.scrollLeft, top: el.scrollTop, focused: document.activeElement === el}]));
    const results = $('insp').querySelector('.goal-results');
    const resultFocus = results?.contains(document.activeElement);
    const resultsState = results ? {key: results.dataset.goalKey, top: results.scrollTop, left: results.scrollLeft, focused: document.activeElement === results} : null;
    if (!inspectorOnly && !heldRendering()) {
      if (UI.mode === 'doc') renderDoc();
      else if (UI.mode === 'source') renderSource();
      else renderProofGraph({openClaimGraph, openExplore});
    }
    renderInspector();
    if (!inspectorOnly) renderChrome();
    renderDiagramDialog();
    restoreReadingFolds(foldFocus);
    if (graphNotesFocus && UI.mode === 'graph' && !$('graph-view').querySelector('.proof-graph-notes')) {
      ($('graph-view').querySelector('[data-act="graph-detail-picker"]') || document.querySelector('[data-mode="graph"]')).focus({preventScroll: true});
    }
    if (UI.mode === 'doc' && UI.documentLine?.sha === UI.data.sha256) {
      const target = documentTarget(UI.documentLine.line);
      if (target) { target.classList.add('document-target'); target.tabIndex = -1; if (documentFocus) target.focus({preventScroll: true}); }
    }
    if (outlineDocumentFocus) $(outlineDocumentFocus)?.focus({preventScroll: true});
    if (sourceFocus) $(sourceFocus)?.focus({preventScroll: true});
    if (sourceControl?.sha === UI.data.sha256) {
      const control = [...$('source').querySelectorAll('[data-act]')].find(el => el.dataset.act === sourceControl.act && el.dataset.line === sourceControl.line);
      control?.focus({preventScroll: true});
    }
    if (atlasFocus && UI.mode === 'graph') {
      const controls = [...$('graph-view').querySelectorAll((atlasFocus.controlRegion ? atlasFocus.controlRegion + ' ' : '') + '[data-atlas]')];
      const source = [...$('graph-view').querySelectorAll('[data-atlas-source]')].find(el => el.dataset.atlasSource === atlasFocus.source);
      const atlasLink = atlasFocus.region && [...$('graph-view').querySelectorAll(atlasFocus.region + ' a[href]')].find(el => el.getAttribute('href') === atlasFocus.href);
      const control = (atlasFocus.summary && $('graph-view').querySelector(atlasFocus.summary)) || atlasLink || (atlasFocus.search ? $('atlas-query') : atlasFocus.source ? source : atlasFocus.heading ? $('atlas-selected') : atlasFocus.route ? $('graph-view').querySelector('[data-atlas-route]') : controls.find(el => el.dataset.atlas === atlasFocus.action && el.dataset.value === atlasFocus.value));
      control?.focus({preventScroll: true});
      if (atlasFocus.search && control) control.setSelectionRange(...atlasFocus.selection);
    }
    if (graphFocus && UI.mode === 'graph') {
      const control = [...$('graph-view').querySelectorAll('[data-act]')].find(el => el.dataset.act === graphFocus.act && el.dataset.graphId === graphFocus.graphId && el.dataset.graphDetail === graphFocus.graphDetail);
      control?.focus({preventScroll: true});
    }
    if (UI.mode === 'graph') {
      if (graphHeading) $('graph-detail')?.querySelector('h3')?.focus({preventScroll: true});
      if (graphLink) [...$('graph-view').querySelectorAll('#graph-detail a[href]')].find(link => link.getAttribute('href') === graphLink)?.focus({preventScroll: true});
      const canvas = $('graph-view').querySelector('.proof-graph-canvas');
      if (canvas && graphScroll?.sha === UI.data.sha256 && graphScroll.scope === canvas.dataset.scope && graphScroll.selected === canvas.querySelector('.chosen')?.dataset.graphId) {
        canvas.scrollLeft = graphScroll.left; canvas.scrollTop = graphScroll.top;
        const detail = $('graph-detail');
        if (detail && detailScroll) { detail.scrollLeft = detailScroll.left; detail.scrollTop = detailScroll.top; }
      }
      else revealGraphSelection();
    }
    if (linkFocus && UI.mode === 'doc' && (!linkFocus.sha || linkFocus.sha === UI.data.sha256)) {
      const link = $('doc').querySelectorAll(linkSelector)[linkFocus.index];
      if (link?.getAttribute('href') === linkFocus.href) link.focus({preventScroll: true});
    }
    if (activityFocus) [...$('run-activity').querySelectorAll('[data-act]')].find(el => el.dataset.act === activityFocus)?.focus({preventScroll: true});
    $('insp').scrollTop = scroll;
    const nextResults = $('insp').querySelector('.goal-results');
    if (nextResults && resultsState?.key === nextResults.dataset.goalKey) {
      nextResults.scrollTop = resultsState.top; nextResults.scrollLeft = resultsState.left;
      if (resultsState.focused) nextResults.focus({preventScroll: true});
    }
    for (const el of $('insp').querySelectorAll('.goal-full-tactic')) {
      const state = tacticStates.get(el.dataset.tacticKey);
      if (state) { el.scrollLeft = state.left; el.scrollTop = state.top; if (state.focused) el.focus({preventScroll: true}); }
    }
    if (goalFocus || (resultFocus && !$('insp').contains(document.activeElement))) {
      const controls = [...$('insp').querySelectorAll(goalControls)].filter(el => !el.disabled);
      const target = controls.find(el => el.dataset.act === goalFocus && (goalFocus !== 'goal-branch' || el.value === active.value))
        || (goalFocus === 'goal' && controls.find(el => el.dataset.act === 'goal-cancel'))
        || controls.find(el => el.dataset.act === 'goal-picker') || controls[0];
      if (target) target.focus({preventScroll: true});
      else {
        $('insp-title').tabIndex = -1;
        $('insp-title').focus({preventScroll: true});
      }
    }
    restoreDiagramUI(diagramUI);
    if (helpFocus) {
      [...document.querySelectorAll('.help-icon')].find(button => button.dataset.helpKey === helpFocus)?.focus({preventScroll: true});
      if (!helpOpen) hideHelp();
    } else if (openHelpKey) {
      const help = $(openHelpKey)?.closest('.context-help');
      if (help) showHelp(help);
    }
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

  $('export-html').onclick = () => {
    $('notebook-view-options').open = false;
    $('history').open = false;
    $('notebook-view-options').querySelector('summary').focus({preventScroll: true});
    return exportHTML();
  };

  // ----- keyboard shortcuts ------------------------------------------------------
  // The one list of shortcuts: the help dialog is drawn from it, and the
  // user guide's table (docs/guide.md) must match it (a test compares them).
  // `editor` keys exist only in the VS Code preview, whose review layer
  // (review.js) handles n/p and e.
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  const SHORTCUTS = [
    {group: 'Navigate', keys: ['j', 'k'], text: 'Next / previous claim'},
    {group: 'Navigate', keys: ['n', 'p'], editor: true,
      text: 'Next / previous claim that needs attention: out of date, not rechecked, contains sorry, additional assumptions, or where checking stopped'},
    {group: 'View', keys: ['s'], text: 'Switch between notebook and Lean source'},
    {group: 'View', keys: ['g'], text: 'Show the recorded proof graph'},
    {group: 'View', keys: ['i'], text: 'Show or hide the claim inspector in Notebook'},
    {group: 'Check and edit', keys: ['r'], text: 'Check the saved file through the selected claim'},
    {group: 'Check and edit', keys: ['e'], editor: true, text: 'Edit the selected claim in VS Code'},
    {group: 'General', keys: ['?'], text: 'Show these shortcuts'},
    {group: 'General', keys: ['Esc'], text: 'Close this dialog or the History panel'},
    {group: 'General', keys: MAC ? ['⌘K', 'G'] : ['Ctrl+K', 'G'], chord: true, editor: true,
      text: 'In the Lean editor: show the cursor’s place in Glitnir. Release the first keys before G'},
  ].filter(s => HOST || !s.editor);
  const chipsHTML = s => s.keys.map(k => `<kbd>${esc(k)}</kbd>`).join(s.chord ? ' <span class="muted">then</span> ' : ' ');
  function renderShortcuts() {
    $('keys-intro').textContent = 'Single keys work while the notebook has focus, outside text fields. Moving never starts a check.';
    const groups = [...new Set(SHORTCUTS.map(s => s.group))];
    $('keys-groups').innerHTML = groups.map(group => `<section class="keys-group"><h4 class="label">${esc(group)}</h4><dl>${SHORTCUTS.filter(s => s.group === group)
      .map(s => `<dt>${chipsHTML(s)}</dt><dd>${esc(s.text)}</dd>`).join('')}</dl></section>`).join('');
  }
  renderShortcuts();

  // ----- interaction -------------------------------------------------------------------
  function revealGraphSelection() {
    const canvas = $('graph-view').querySelector('.proof-graph-canvas');
    const chosen = canvas?.querySelector('.proof-node.chosen');
    if (!chosen || !canvas.clientWidth) return;
    const box = canvas.getBoundingClientRect(), node = chosen.getBoundingClientRect();
    canvas.scrollLeft += node.left - box.left - (canvas.clientWidth - node.width) / 2;
    canvas.scrollTop += node.top - box.top - (canvas.clientHeight - node.height) / 2;
  }
  function openExplore() {
    if (heldRendering()) return;
    hideHelp();
    const active = document.activeElement;
    UI.mode = 'graph';
    UI.graphFocused = false;
    render({openExplore: true});
    if (active && (!active.isConnected || active.closest('[hidden]'))) {
      ($('atlas-selected') || document.querySelector('[data-mode="graph"]')).focus({preventScroll: true});
    }
  }
  function zoomGraph(factor) {
    const canvas = $('graph-view').querySelector('.proof-graph-canvas');
    const svg = canvas?.querySelector('.proof-map');
    const width = Number(svg?.getAttribute('viewBox').split(' ')[2]);
    const fitScale = canvas?.clientWidth / width;
    const scale = UI.graphFit && width > 0
      ? UI.graphFocused ? Math.min(1, fitScale) : fitScale
      : UI.graphZoom || 1;
    UI.graphZoom = Math.max(.0001, Math.min(8, scale * factor));
    UI.graphFit = false;
    render();
  }
  function select(name, scroll, openInspector = false) {
    if (openInspector) UI.panel = true;
    UI.outlineTarget = null; revealOutline = true;
    UI.sourceCell = null;
    UI.selected = name;
    UI.graphSelected = `local:${name}`;
    render();
    void loadResearch(name);
    if (openInspector && UI.mode === 'doc' && window.matchMedia('(max-width:760px)').matches) {
      $('insp-title').tabIndex = -1;
      $('insp-title').focus({preventScroll: true});
      document.querySelector('.inspector').scrollIntoView({block: 'start', behavior: reduced() ? 'auto' : 'smooth'});
      return;
    }
    if (!scroll) return;
    const el = UI.mode === 'doc' ? $('cell-' + name) || [...$('doc').querySelectorAll('.source-controls[data-proof-claim]')].find(el => el.dataset.proofClaim === name)?.closest('.cell') : UI.mode === 'source' ? document.querySelector(`#source tr[data-claim="${CSS.escape(name)}"]`) : document.querySelector(`#graph-view [data-graph-name="${CSS.escape(name)}"]`);
    if (el) { revealSection(el); el.scrollIntoView?.({behavior: reduced() ? 'auto' : 'smooth', block: UI.mode === 'doc' ? 'start' : 'center'}); }
  }
  function showSourceLines(start, end) {
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return;
    if (!UI.data.cells.some(cell => start >= cell.start && start < cell.start + cell.text.split('\n').length)) return;
    UI.hideSourceComments = false;
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
    if (e.target.matches('#insp [data-act="goal-picker"]')) { selectGoalStep(e.target); return; }
    if (e.target.matches('#source-hide-comments')) { UI.hideSourceComments = e.target.checked; render(); return; }
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
      const freshScope = e.target.closest('.cell')?.querySelector('.source-controls[data-proof-claim]');
      if (freshScope && !e.target.closest('button,summary,a') && !claim(freshScope.dataset.proofClaim)) { select(freshScope.dataset.proofClaim, false, true); return; }
      const cell = e.target.closest('[data-claim]');
      if (cell && !e.target.closest('button,summary,a') && (UI.selected !== cell.dataset.claim || !UI.panel)) select(cell.dataset.claim, false, true);
      return;
    }
    if (b.closest('.proof-section > summary')) e.preventDefault();
    const id = b.dataset.id;
    if (researchAction(b)) return;
    switch (b.dataset.act) {
      case 'git-refresh': void loadGitHistory(true); break;
      case 'git-diff': void showGitDiff(b.dataset.sha); break;
      case 'diagram': diagramAction(b); break;
      case 'diagram-close': closeDiagram(); break;
      case 'diagram-condition': { e.preventDefault(); const details = document.getElementById(b.getAttribute('href').slice(1)); if (details) { details.open = true; details.querySelector('summary').focus(); details.scrollIntoView({block: 'nearest'}); } break; }
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
      case 'goto': closeDiagram(); select(id, true, true); break;
      case 'open-claim-graph': {
        if (heldRendering()) break;
        hideHelp();
        UI.mode = 'graph';
        UI.graphFocused = true;
        render({openClaimGraph: true});
        const heading = $('graph-detail')?.querySelector('h3') || $('graph-view').querySelector('h2');
        if (heading) {
          heading.tabIndex = -1;
          heading.focus({preventScroll: true});
        }
        $('graph-view').scrollIntoView?.({block: 'start'});
        revealGraphSelection();
        break;
      }
      case 'graph-select': {
        const node = graphNode(b.dataset.graphId);
        if (!node) break;
        UI.graphSelected = node.id; render();
        const heading = $('graph-detail')?.querySelector('h3');
        heading?.focus({preventScroll: true});
        if (window.matchMedia('(max-width:760px)').matches) heading?.scrollIntoView({block: 'nearest'});
        break;
      }
      case 'graph-node': {
        const node = graphNode(b.dataset.graphId);
        if (!node) break;
        UI.graphSelected = node.id;
        if (node.kind === 'walkthrough' && node.sourceLine) { showDocument(node.sourceLine, UI.data.sha256); break; }
        if (node.kind === 'local' || node.kind === 'walkthrough' && node.name) {
          const cell = UI.data.cells.find(cell => cell.claim === node.name || cell.id === node.name);
          if (cell) showDocument(cell.start, UI.data.sha256);
          break;
        }
        if (node.path) {
          const path = [...(linkedPath() || '').split('/').slice(0, -1), node.path].join('/');
          const link = path === UI.main?.file ? '' : path;
          UI.pendingClaim = node.name; openNotebook(link);
        } else render();
        break;
      }
      case 'graph-focus':
      case 'graph-full':
        UI.graphFocused = b.dataset.act === 'graph-focus';
        render();
        break;
      case 'graph-reset': UI.graphZoom = 1; UI.graphFit = false; render(); break;
      case 'graph-zoom-in': zoomGraph(1.25); break;
      case 'graph-zoom-out': zoomGraph(.8); break;
      case 'graph-fit': UI.graphZoom = 1; UI.graphFit = true; render(); break;
      case 'open-notebook': e.preventDefault(); closeDiagram(); UI.pendingClaim = b.dataset.claim || null; openNotebook(b.dataset.link || null); break;
      case 'notebook-back': showNotebookAt(UI.at - 1); break;
      case 'notebook-forward': showNotebookAt(UI.at + 1); break;
      case 'notebook-at': showNotebookAt(Number(b.dataset.at)); break;
      case 'goal': UI.selected = id; inspectGoals(b.dataset.refresh === 'true'); break;
      case 'goal-cancel': cancelGoals(); break;
      case 'goal-edit': editGoalSource(b); break;
      case 'goal-branch':
      case 'goal-prev':
      case 'goal-next': if (!b.disabled) selectGoalStep(b); break;
      // Stale buttons of an earlier saved file never check other lines.
      case 'check-through': if (b.dataset.sha === UI.data.sha256 && claim(id)) { UI.selected = id; checkProject(Number(b.dataset.line)); } break;
      case 'scroll': {
        UI.outlineTarget = id; revealOutline = true;
        UI.mode = 'doc';
        render();
        const heading = document.getElementById(id);
        const scope = heading?.querySelector('.source-controls[data-proof-claim]');
        if (scope) {
          select(scope.dataset.proofClaim, true, true);
          const target = document.getElementById(id);
          if (target && !window.matchMedia('(max-width:760px)').matches) { target.tabIndex = -1; target.focus({preventScroll: true}); }
          break;
        }
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
  document.querySelectorAll('[data-mode]').forEach(b => { b.onclick = () => { if (b.dataset.mode === 'graph') openExplore(); else if (b.dataset.mode === 'doc' && UI.mode === 'source') showDocument(); else { UI.mode = b.dataset.mode; render(); } }; });
  $('inspect-project').onclick = inspectProject;
  $('check-project').onclick = () => checkProject();
  $('panel-hide').onclick = () => { UI.panel = false; render(); };
  $('panel-show').onclick = () => { UI.panel = true; render(); };
  const keys = open => { $('keys').hidden = !open; if (open) $('keys-close').focus(); };
  $('keys-open').onclick = () => keys(true);
  $('keys-close').onclick = () => keys(false);
  $('keys').onclick = e => { if (e.target === $('keys')) keys(false); };
  document.addEventListener('click', e => {
    if (!e.composedPath().includes($('notebook-view-options'))) { $('notebook-view-options').open = false; $('history').open = false; }
    if (!$('header-status').contains(e.target)) $('header-status').open = false;
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if ($('notebook-view-options').open && $('history').open) {
        $('history').open = false;
        $('history').querySelector('summary').focus({preventScroll: true});
        e.stopImmediatePropagation();
        return;
      }
      if ($('notebook-view-options').open) {
        $('notebook-view-options').open = false;
        $('notebook-view-options').querySelector('summary').focus({preventScroll: true});
        e.stopImmediatePropagation();
        return;
      }
      if ($('header-status').open) {
        $('header-status').open = false;
        $('status-summary').focus({preventScroll: true});
        e.stopPropagation();
        return;
      }
      hideHelp();
      keys(false);
      $('history').open = false;
      return;
    }
    if ($('notebook-view-options').open || e.target.closest('#notebook-view-options,#review-queue')) return;
    if ($('header-status').open || e.target.closest('#image-dialog,.context-help,#header-status')) return;
    const graphAction = e.target.closest?.('#graph-view [data-act="graph-node"],#graph-view [data-act="graph-select"]');
    if (graphAction && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      graphAction.dispatchEvent(new MouseEvent('click', {bubbles: true}));
      return;
    }
    if (e.target.closest('input,textarea,select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const map = {
      j: () => move(1), k: () => move(-1), '?': () => keys(true),
      s: () => { if (UI.mode === 'source') showDocument(); else { UI.mode = 'source'; render(); } },
      g: openExplore,
      i: () => { if (UI.mode === 'doc') { UI.panel = !UI.panel; render(); } },
      // Check through the selected claim; Check saved file checks all of it.
      r: () => { const c = claim(UI.selected); if (c?.project && !linkedPath()) checkProject(c.project.end_line); },
    };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });

  Object.assign(workbench, {render, renderChrome, renderConnection, select, showSourceLines});

  load();
})();
