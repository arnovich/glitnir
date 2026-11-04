// Navigation and display of historical Lean observations. Checking stays explicit.
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const app = document.querySelector('.app');
  if (!app || !window.glitnirHost) return;
  app.tabIndex = -1;
  const host = window.glitnirHost;
  let snapshot, sha, selected = null, dirty = false, mode = 'claims', trace, step = -1;
  let generation = 0, polling, helpFocus, sourceLine, sourceCharacter, pendingRefresh = false, pendingReveal;
  const positions = new Map();
  let returnPosition, inspection, proofStarted, proofRefreshing = false;
  const nav = document.createElement('nav');
  nav.className = 'review-nav'; nav.setAttribute('aria-label', 'Review navigation');
  nav.innerHTML = `<label id="review-filter-label">Show <select id="review-filter" aria-label="Claims to review"><option value="attention">Needs attention</option><option value="all">All claims</option></select></label>
    <details id="review-queue"><summary id="review-queue-summary"></summary><div id="review-items" aria-label="Items to review"></div></details>
    <div class="review-controls"><button class="secondary" id="review-previous">Previous <kbd>p</kbd></button><span id="review-position" role="status"></span></div>`;
  const next = $('next-unresolved') || document.createElement('button');
  next.id = 'next-unresolved'; next.className = 'secondary'; next.innerHTML = 'Next <kbd>n</kbd>'; next.hidden = false;
  nav.querySelector('.review-controls').append(next);
  const activity = app.querySelector('.live-bar');
  activity.prepend(nav);
  $('notebook-view-options').querySelector('.notebook-preferences').prepend($('review-filter-label'));
  const queueMenu = $('review-queue');
  const queueSummary = $('review-queue-summary');
  function closeQueue() { queueMenu.open = false; queueSummary.focus({preventScroll: true}); }
  document.addEventListener('click', event => { if (!queueMenu.contains(event.target)) queueMenu.open = false; });
  queueMenu.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeQueue(); }
    else if (event.key.length === 1) event.stopPropagation();
  });
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => {
    document.documentElement.style.setProperty('--review-offset', `${activity.getBoundingClientRect().height + 12}px`);
  }).observe(activity);
  const proof = document.createElement('section');
  proof.id = 'proof-review'; proof.hidden = true;
  proof.innerHTML = `<div class="proof-heading"><div><h2 id="proof-title"></h2><p id="proof-status" class="small"></p></div><div class="review-controls"><button id="proof-refresh" class="secondary">Refresh steps</button><button id="proof-back" class="secondary">Back to claim <kbd>Esc</kbd></button></div></div>
    <section id="proof-progress" class="proof-progress" hidden><div class="check-progress-copy"><span class="check-progress-indicator" aria-hidden="true"></span><div><div role="status"><strong id="proof-progress-title" class="proof-progress-title">Recording proof goals</strong><div id="proof-progress-stage" class="proof-progress-stage"></div></div><div id="proof-progress-time" class="small muted"></div><div class="small muted">Counts update after each command.</div></div></div><button id="proof-cancel" class="secondary">Cancel</button></section>
    <p id="proof-message" role="status"></p><div class="proof-grid"><div><pre id="proof-statement"></pre><div id="proof-tactics" role="group" aria-label="Recorded tactics"></div></div><div class="proof-goals"><h3 id="proof-goal-title"></h3><details id="proof-before"><summary>Goals before this tactic</summary><pre id="proof-before-text"></pre></details><pre id="proof-after"></pre><p class="small" id="proof-scope"></p><details id="proof-diagnostics" hidden><summary>Lean messages</summary><pre></pre></details><p id="proof-provenance" class="small muted"></p></div></div>`;
  activity.after(proof);
  // The renderer draws the shortcut list (one source for every listing); this
  // layer adds only its own setting.
  const help = $('keys');
  const setting = document.createElement('label');
  setting.innerHTML = '<input id="review-letter-keys" type="checkbox" checked> Enable single-letter shortcuts in this notebook';
  $('keys-foot').prepend(setting);
  help.querySelector('.dialog').setAttribute('aria-labelledby', 'keys-title');
  const labels = {checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', unknown: 'Unknown', failed: 'Failed', open: 'Open goal'};
  function currentCell() { return snapshot?.cells?.find(c => c.index === selected); }
  function currentClaim() { return snapshot?.claims?.find(c => c.name === currentCell()?.id); }
  function project() { return snapshot?.preview?.mode === 'project'; }
  function proofUnavailable() {
    if (snapshot?.preview?.lean?.supported === false) return snapshot.preview.lean.message || 'Proof steps are unavailable for this Lean toolchain.';
    const workbench = window.glitnirWorkbench;
    if (snapshot?.walkthrough && workbench?.walkthroughUnchecked(snapshot)) return workbench.walkthroughMessage(snapshot) || 'Proof steps are unavailable for this walkthrough.';
    return '';
  }
  // A saved project result needs attention unless it is kernel checked and
  // current: out of date, not compared, not rechecked, containing sorry or
  // resting on additional assumptions.
  const needsAttention = saved => saved.status !== 'checked' || saved.current !== true || saved.not_rechecked === true;
  // Where the last check of this saved file stopped at a failing command
  // (not a requested check through a claim), as a place to review.
  function stoppedCell() {
    const withheld = snapshot?.project_withheld;
    if (!withheld || withheld.stale || !Number.isSafeInteger(withheld.from_line)) return null;
    if (Number.isSafeInteger(withheld.through_line) && withheld.from_line === withheld.through_line + 1) return null;
    return (snapshot.cells || []).find(c => c.start <= withheld.from_line && withheld.from_line <= c.end) || null;
  }
  function cells() {
    if (project()) {
      const saved = new Map((snapshot.project_claims || []).map(c => [c.name, c]));
      const stopped = stoppedCell();
      return (snapshot.cells || []).filter(c => c === stopped || (c.claim && saved.has(c.claim) && needsAttention(saved.get(c.claim))));
    }
    const claims = snapshot?.claims || [];
    const names = new Set(claims.filter(c => $('review-filter').value === 'all' || !['checked', 'refuted'].includes(c.status) || snapshot.check_errors?.[c.name]).map(c => c.name));
    return (snapshot?.cells || []).filter(c => c.kind === 'claim' && names.has(c.id));
  }
  function reviewReason(cell) {
    if (cell === stoppedCell()) return `Checking stopped: ${snapshot.project_withheld.message || 'Lean command failed'}`;
    if (!project()) {
      const claim = snapshot.claims.find(c => c.name === cell.id);
      return snapshot.check_errors?.[cell.id] || claim?.reason || labels[claim?.status] || 'Needs review';
    }
    const saved = snapshot.project_claims.find(c => c.name === cell.claim);
    const reasons = [];
    if (saved.current === false) reasons.push('Out of date');
    else if (saved.current !== true) reasons.push('Not compared');
    if (saved.not_rechecked) reasons.push('Not rechecked');
    if (saved.status !== 'checked') reasons.push(({admitted: 'Contains sorry', assumptions: 'Additional assumptions'})[saved.status] || labels[saved.status] || 'Not checked');
    return reasons.join(' · ');
  }
  function updateQueue(queue, inProof) {
    queueMenu.hidden = inProof || !queue.length;
    if (queueMenu.hidden) queueMenu.open = false;
    queueSummary.textContent = `${project() || $('review-filter').value !== 'all' ? 'Needs review' : 'All claims'} · ${queue.length} ${queue.length === 1 ? 'item' : 'items'}`;
    // Keep focused list buttons intact during polling and selection updates.
    const signature = JSON.stringify(queue.map(cell => [cell.index, cell.claim || cell.id, reviewReason(cell)]));
    if ($('review-items').dataset.signature !== signature) {
      const hadFocus = $('review-items').contains(document.activeElement);
      $('review-items').dataset.signature = signature;
      $('review-items').replaceChildren(...queue.map(cell => {
        const button = document.createElement('button');
        button.type = 'button'; button.dataset.index = cell.index;
        const name = document.createElement('span');
        name.textContent = cell.claim || cell.id || `Line ${cell.start}`;
        const reason = document.createElement('span'); reason.className = 'review-item-reason';
        reason.textContent = reviewReason(cell);
        button.append(name, reason);
        button.onclick = () => { if (!dirty) { closeQueue(); reveal(cell); } };
        return button;
      }));
      if (hadFocus && !nav.hidden) closeQueue();
    }
    for (const button of $('review-items').children) {
      button.disabled = dirty;
      if (Number(button.dataset.index) === selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  }
  function nodes() {
    if (!trace || !['ready', 'partial'].includes(trace.status) || trace.source_sha256 !== sha || (!project() && trace.claim !== currentClaim()?.name)) return [];
    const scope = inspection?.sha === sha ? inspection : null;
    return (trace.nodes || []).filter(n => scope && n.range.start.line >= scope.start && n.range.start.line <= scope.end);
  }
  const positionKey = () => `${trace?.id}:${inspection?.sha}:${inspection?.name}:${inspection?.start}:${inspection?.end}`;
  function remember() { if (trace?.id) positions.set(positionKey(), step); }
  function focusAfterRender(previous) {
    if (previous?.dataset.proofNode) {
      const found = [...$('proof-tactics').children].find(b => b.dataset.proofNode === String(nodes()[step]?.id));
      (found || $('proof-back')).focus({preventScroll: true});
    } else if (previous && (previous.hidden || previous.disabled || previous.closest('[hidden]') || (mode === 'proof' && previous.closest('.workspace')))) {
      if (mode === 'proof') $('proof-back').focus({preventScroll: true});
    }
  }
  function competingRun() {
    const ui = window.glitnirWorkbench?.UI;
    return ui?.checkRequested || ui?.data?.preview?.check?.status === 'running' || ui?.inspecting || ui?.data?.preview?.inspection?.status === 'running';
  }
  function cancelProof() { pendingRefresh = false; void host.fetch('/api/proof/cancel', {method: 'POST', body: '{}'}).catch(() => {}); }
  function update() {
    const focused = document.activeElement;
    const inProof = mode === 'proof', queue = cells(), nodeList = nodes(), claim = currentClaim();
    nav.hidden = !snapshot || !!snapshot.error || (!inProof && !queue.length && (project() || $('review-filter').value !== 'all'));
    activity.hidden = nav.hidden;
    if (nav.hidden && nav.contains(focused)) document.querySelector('[data-mode="doc"]').focus({preventScroll: true});
    updateQueue(queue, inProof);
    proof.hidden = !inProof;
    document.body.classList.toggle('glitnir-proof-mode', inProof);
    const unavailable = proofUnavailable();
    const workbench = window.glitnirWorkbench;
    if (workbench) {
      const busy = trace?.status === 'running';
      const changed = workbench.UI.proofBusy !== busy;
      workbench.UI.proofBusy = busy;
      workbench.UI.proofActivity = busy ? {trace, started: proofStarted, refreshing: proofRefreshing, expanded: true} : null;
      if (changed || (busy && !inProof)) workbench.render();
    }
    for (const button of document.querySelectorAll('[data-act="inspect-proof"],#insp [data-act="goal"]')) {
      const reason = dirty ? 'Save or revert edits before viewing proof steps.' : unavailable || (competingRun() ? 'Wait for the current check to finish.' : button.dataset.act === 'goal' && workbench?.UI.proofBusy ? 'Proof goals are already being recorded.' : '');
      button.disabled = !!reason;
      button.title = reason || 'Inspect this claim’s saved tactic goals (t)';
      if (reason) button.setAttribute('aria-description', reason);
      else button.removeAttribute('aria-description');
    }
    $('review-filter-label').hidden = inProof || project();
    const previous = $('review-previous');
    previous.disabled = dirty || (inProof ? step < 0 : !queue.some(c => selected === null || c.index < selected));
    next.disabled = dirty || (inProof ? !nodeList.length || step >= nodeList.length - 1 : !queue.some(c => selected === null || c.index > selected));
    const index = queue.findIndex(c => c.index === selected);
    if (inProof) {
      previous.title = 'Previous recorded proof state (p)'; next.title = 'Next recorded proof state (n)';
      $('review-position').textContent = nodeList.length ? step < 0 ? 'Initial goals' : `Step ${step + 1} of ${nodeList.length}` : 'No recorded steps';
    } else if (project()) {
      previous.title = 'Previous claim that needs attention (p)';
      next.title = queue.length ? `${queue.length} need attention; next in source order (n)` : 'No claims queued for review';
      $('review-position').textContent = index >= 0 ? `Item ${index + 1} of ${queue.length}` : '';
    } else {
      previous.title = 'Previous matching claim (p)';
      next.title = queue.length ? `${queue.length} matching claims; next in source order (n)` : 'No matching claims';
      $('review-position').textContent = index >= 0 ? `Claim ${index + 1} of ${queue.length}` : queue.length ? `${queue.length} matching claims` : '';
    }
    $('review-position').hidden = !$('review-position').textContent;
    if (!inProof) { focusAfterRender(focused); return; }
    $('proof-title').textContent = !snapshot ? 'Saved source unavailable' : inspection?.name || 'Selected claim';
    $('proof-status').textContent = claim ? `${labels[claim.status] || claim.status}${claim.reason ? ' · ' + claim.reason : ''}` : 'Project observation · use Check saved file for proof checking';
    $('proof-statement').textContent = claim ? `${claim.keyword} ${claim.name} ${claim.statement}` : currentCell()?.text.split('\n').slice(Math.max(0, (inspection?.start || currentCell().start) - currentCell().start), (inspection?.end || currentCell().end) - currentCell().start + 1).join('\n') || '';
    $('proof-refresh').disabled = dirty || !!unavailable || !snapshot || !!snapshot.error || trace?.status === 'running' || !!competingRun();
    const recording = trace?.status === 'running';
    $('proof-progress').hidden = !recording;
    $('proof-cancel').hidden = !recording;
    $('proof-progress-title').textContent = proofRefreshing ? 'Refreshing proof goals' : 'Recording proof goals';
    $('proof-progress-stage').textContent = trace?.message || 'Preparing the saved proof';
    $('proof-progress-time').textContent = `${Math.max(0, Math.floor((Date.now() - (proofStarted ?? Date.now())) / 1000))}s waiting here`;
    $('proof-message').hidden = recording;
    $('proof-message').textContent = dirty ? 'Unsaved changes — save or revert, then refresh proof steps.' : (pendingRefresh ? 'Finishing the previous observation before loading this claim…' : trace?.message) || 'Load the saved proof to see its tactic goals.';
    const buttons = nodeList.map((n, i) => {
      const b = document.createElement('button'); b.className = 'proof-tactic'; b.dataset.proofNode = String(n.id);
      b.setAttribute('aria-current', String(i === step));
      b.setAttribute('aria-label', `Step ${i + 1}: ${n.text}`);
      let depth = 0, parent = n.parent;
      while (parent !== null && depth < 12) { depth++; parent = trace.nodes[parent]?.parent ?? null; }
      b.style.paddingInlineStart = `${12 + depth * 12}px`;
      b.textContent = `${i + 1}  ${n.text}`;
      b.onclick = () => { step = i; remember(); update(); };
      return b;
    });
    $('proof-tactics').replaceChildren(...buttons);
    const node = nodeList[step < 0 ? 0 : step];
    $('proof-goal-title').textContent = step < 0 ? 'Initial goals in this scope' : `Goals after step ${step + 1}`;
    $('proof-before').hidden = step < 0 || !node;
    $('proof-before-text').textContent = node?.before?.join('\n\n') || 'No goals in this tactic scope.';
    document.querySelector('.proof-goals').hidden = recording && !node;
    document.querySelector('.proof-grid').classList.toggle('proof-loading', recording && !node);
    $('proof-after').textContent = node ? (step < 0 ? node.before : node.after).join('\n\n') || 'No goals in this tactic scope.' : trace?.status === 'running' ? '' : 'No tactic observations. This may be a term proof or an unavailable trace.';
    $('proof-scope').textContent = node ? 'Goals belong to this tactic scope. Other branches may remain; the claim’s check status is unchanged.' : '';
    const matching = project() || trace?.claim === claim?.name;
    const messages = matching ? [...(trace?.errors || []), ...(trace?.warnings || [])] : [];
    $('proof-diagnostics').hidden = !messages.length;
    $('proof-diagnostics').querySelector('pre').textContent = messages.join('\n\n');
    $('proof-provenance').textContent = matching && trace?.environment ? `Recorded source ${trace.source_sha256.slice(0, 8)} · ${trace.environment.toolchain}. Dependencies were read-only live inputs; refresh after changes. Recorded elaboration attempts, not a proof certificate.` : '';
    focusAfterRender(focused);
  }
  function reveal(cell) {
    selected = cell.index;
    document.dispatchEvent(new CustomEvent('glitnir-reveal-cell', {detail: {index: cell.index, sha256: sha}}));
    update();
  }
  function move(direction) {
    if (dirty || linkedView()) return;
    if (mode === 'proof') { step = Math.max(-1, Math.min(nodes().length - 1, step + direction)); remember(); update(); return; }
    const queue = cells();
    const cell = direction > 0 ? queue.find(c => selected === null || c.index > selected) : queue.findLast(c => selected === null || c.index < selected);
    if (cell) reveal(cell);
  }
  function claimsMode() { pendingRefresh = false; remember(); mode = 'claims'; update(); }
  function invalidate(message) {
    generation++; pendingRefresh = false; clearTimeout(polling);
    if (trace?.status === 'running') void host.fetch('/api/proof/cancel', {method: 'POST', body: '{}'}).catch(() => {});
    trace = {status: 'stale', nodes: [], message}; positions.clear(); step = -1; update();
  }
  async function poll(id, epoch) {
    try {
      const response = await host.fetch('/api/proof/trace');
      const answer = await response.json();
      if (epoch !== generation) return;
      if (!response.ok) throw new Error(answer.error || 'Proof observation unavailable.');
      if (answer.id !== id || answer.source_sha256 !== sha) throw new Error('Proof observation changed. Refresh the steps.');
      trace = answer;
      alignSource();
      if (answer.status !== 'running' && pendingRefresh && mode === 'proof') { pendingRefresh = false; void refresh(); return; }
      update();
      if (answer.status === 'running') polling = setTimeout(() => void poll(id, epoch), 750);
    } catch (error) { if (epoch === generation) { trace = {status: 'unavailable', message: String(error), nodes: []}; update(); } }
  }
  async function refresh() {
    if (dirty || competingRun() || !snapshot || proofUnavailable() || !inspection || inspection.sha !== sha) return;
    if (trace?.status === 'running') {
      if (!project() && trace.claim !== currentClaim()?.name) {
        pendingRefresh = true;
        void host.fetch('/api/proof/cancel', {method: 'POST', body: '{}'}).catch(() => {});
        update();
      }
      return;
    }
    const claim = currentClaim(); if (!project() && !claim) return;
    pendingRefresh = false;
    proofRefreshing = !!trace && ['ready', 'partial'].includes(trace.status);
    proofStarted = Date.now();
    const epoch = ++generation; clearTimeout(polling); step = -1;
    trace = {status: 'running', claim: claim?.name, message: 'Preparing proof steps', nodes: []}; update();
    try {
      if (project()) {
        // Join an observation of this saved file that is already running,
        // such as one the inspector's proof goals started.
        const current = await host.fetch('/api/proof/trace');
        const running = await current.json().catch(() => ({}));
        if (epoch !== generation) return;
        if (current.ok && running.status === 'running' && running.source_sha256 === sha && running.id) {
          trace = running;
          const inline = window.glitnirWorkbench?.UI.goals;
          if (inline?.trace?.id === running.id) { proofStarted = inline.started ?? proofStarted; proofRefreshing = !!inline.refreshing; }
          update(); await poll(running.id, epoch); return;
        }
      }
      const response = await host.fetch('/api/proof/start', {method: 'POST', body: JSON.stringify({sha256: sha, ...(claim ? {claim: claim.name} : {})})});
      const answer = await response.json();
      if (epoch !== generation) return;
      if (!response.ok) throw new Error(answer.error || 'Could not start proof observation.');
      trace = answer; update(); await poll(answer.id, epoch);
    } catch (error) { if (epoch === generation) { trace = {status: 'unavailable', message: String(error), nodes: []}; update(); } }
  }
  // Another notebook of the project read in this panel: review, proof steps
  // and editing belong to the previewed notebook, so they stay off.
  const linkedView = () => document.body.classList.contains('glitnir-linked-view');
  function proofMode(action) {
    if (dirty || competingRun() || proofUnavailable() || linkedView() || !snapshot) return;
    const workbench = window.glitnirWorkbench;
    const buttons = [...document.querySelectorAll('#doc .source-controls[data-proof-claim]')].filter(b => b.dataset.sha === sha);
    let cell = currentCell();
    if (!action && mode === 'proof' && !sourceLine && inspection) action = buttons.find(b => b.dataset.proofClaim === inspection.name && Number(b.dataset.start) === inspection.start);
    if (!action) {
      action = buttons.find(b => Number(b.dataset.index) === selected && (!sourceLine || Number(b.dataset.start) <= sourceLine && sourceLine <= Number(b.dataset.end)));
      if (action && !cell?.claim && cell?.kind !== 'claim' && buttons.filter(b => Number(b.dataset.index) === selected).length > 1 && !sourceLine) action = null;
      action ||= buttons.find(b => b.dataset.proofClaim === workbench?.UI.selected);
    }
    if (action) cell = snapshot.cells?.find(c => c.index === Number(action.dataset.index));
    const name = action?.dataset.proofClaim || cell?.claim || (cell?.kind === 'claim' ? cell.id : null);
    if (!cell || !name) return;
    selected = cell.index;
    inspection = {name, index: selected, start: action ? Number(action.dataset.start) : cell.start, end: action ? Number(action.dataset.end) : cell.end, sha};
    if (mode !== 'proof') returnPosition = {claim: name, sha, scroll: workbench?.UI.mode === 'doc' ? [window.scrollX, window.scrollY] : null};
    else if (returnPosition?.claim !== name) returnPosition = {claim: name, sha, scroll: null};
    if (workbench) { workbench.UI.selected = name; workbench.UI.mode = 'doc'; workbench.UI.panel = true; workbench.render(); }
    const inlineTrace = workbench?.currentGoalTrace?.();
    if (project() && inlineTrace) trace = inlineTrace;
    else if (project() && workbench?.UI.goals?.trace?.status === 'running') trace = {status: 'stale', nodes: []};
    mode = 'proof'; step = Math.min(positions.get(positionKey()) ?? -1, nodes().length - 1); update();
    $('proof-back').focus({preventScroll: true});
    if (!trace || trace.status === 'stale' || (!project() && trace.claim !== currentClaim()?.name)) void refresh();
  }
  function backToClaim(focus = true) {
    if (mode !== 'proof') return;
    const origin = returnPosition;
    returnPosition = null;
    claimsMode();
    const workbench = window.glitnirWorkbench;
    if (workbench) { workbench.UI.mode = 'doc'; workbench.render(); }
    const action = [...document.querySelectorAll('#insp [data-act="inspect-proof"]')].find(b => b.dataset.proofClaim === origin?.claim);
    const scope = [...document.querySelectorAll('#doc .source-controls[data-proof-claim]')].find(el => el.dataset.proofClaim === origin?.claim);
    const target = scope?.closest('.cell');
    if (origin?.sha === sha && origin.scroll) window.scrollTo(...origin.scroll);
    else if (target) { workbench?.revealSection?.(target); target.scrollIntoView?.({block: 'center'}); }
    if (focus) {
      if (action && !action.disabled) action.focus({preventScroll: true});
      else if (target) { target.tabIndex = -1; target.classList.add('document-target'); if (workbench) workbench.UI.documentLine = {line: inspection?.start, sha}; target.focus({preventScroll: true}); }
      else document.querySelector('[data-mode="doc"]')?.focus({preventScroll: true});
    }
  }
  function edit() {
    const node = nodes()[step];
    const cell = node ? snapshot?.cells?.find(c => c.start <= node.range.start.line && node.range.end.line <= c.end) : currentCell();
    if (mode === 'proof' && node && cell && !dirty) {
      // The host fetches this observation by id again before trusting its range.
      host.openSource({index: cell.index, sha256: sha, trace: {id: trace.id, node: node.id}});
    } else host.openSelectedSource();
  }
  $('proof-back').onclick = () => backToClaim();
  $('review-filter').onchange = update;
  $('review-previous').onclick = () => move(-1); next.onclick = () => move(1);
  $('proof-refresh').onclick = () => void refresh();
  $('proof-cancel').onclick = cancelProof;
  $('open-source').onclick = edit; $('open-source').title = 'Edit the selected claim or tactic in VS Code (e)';
  document.addEventListener('glitnir-snapshot', event => {
    const nextSnapshot = event.detail;
    const inspectedName = mode === 'proof' ? inspection?.name : null;
    if (sha && nextSnapshot?.sha256 !== sha) { selected = null; invalidate('Saved source changed. Refresh proof steps.'); }
    snapshot = nextSnapshot; sha = snapshot?.sha256;
    if (mode === 'proof' && inspectedName && inspection.sha !== sha && snapshot) {
      const cell = snapshot.cells?.find(c => c.claim === inspectedName || c.kind === 'claim' && c.id === inspectedName);
      if (cell) { selected = cell.index; inspection = {name: inspectedName, index: selected, start: cell.start, end: cell.end, sha}; }
      else backToClaim(proof.contains(document.activeElement));
    }
    if (mode === 'proof' && proofUnavailable()) {
      const focused = proof.contains(document.activeElement);
      invalidate(proofUnavailable());
      backToClaim(focused);
    }
    if (selected !== null && !currentCell()) selected = null;
    if (selected === null) selected = snapshot?.cells?.find(c => c.kind === (project() ? 'source' : 'claim'))?.index ?? null;
    if (pendingReveal && pendingReveal.sha256 === sha) { const reveal = pendingReveal; pendingReveal = undefined; applySourceReveal(reveal); }
    update();
  });
  function applySourceReveal(message) {
    selected = message.index; sourceLine = message.line; sourceCharacter = message.character;
    if (mode === 'proof') { alignSource(); proofMode(); }
    update();
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if (message.type === 'editorState') { const changed = message.dirty && !dirty; dirty = message.dirty; if (changed) invalidate('Source has unsaved edits.'); update(); }
    if (message.type === 'environmentChanged') invalidate('Source or dependencies changed. Refresh proof steps.');
    if (message.type === 'revealCell') {
      if (message.sha256 !== sha) {
        pendingReveal = message;
        invalidate('Saved source changed. Refreshing the notebook…');
      } else applySourceReveal(message);
    }
    if (message.type === 'reviewCommand' && (!linkedView() || message.action === 'help')) ({next: () => move(1), previous: () => move(-1), claims: () => backToClaim(), proof: proofMode, help: helpOpen})[message.action]?.();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-mode]')) claimsMode();
    if (event.target.closest('#keys-open')) helpFocus = document.activeElement;
    if (event.target.closest('#keys-close') || event.target === help) queueMicrotask(restoreHelpFocus);
  }, true);
  document.addEventListener('click', event => {
    if (linkedView()) return;
    if (event.target.closest('[data-act="proof-cancel"]')) { cancelProof(); return; }
    const inspect = event.target.closest('[data-act="inspect-proof"]');
    if (inspect) {
      if (inspect.disabled || inspect.dataset.sha !== sha) return;
      selected = Number(inspect.dataset.index);
      proofMode(inspect);
      return;
    }
    const link = event.target.closest('[data-act="goto"]'), wrap = event.target.closest('.cellwrap'), row = event.target.closest('#source tr[data-claim]');
    if (link) selected = snapshot?.cells?.find(c => c.id === link.dataset.id)?.index ?? null;
    else if (wrap) selected = Number(wrap.dataset.index);
    else if (row) selected = snapshot?.cells?.find(c => c.id === row.dataset.claim)?.index ?? null;
    if (link || wrap || row) { claimsMode(); update(); }
  });
  function helpOpen() { helpFocus = document.activeElement; $('keys-open').click(); }
  function restoreHelpFocus() { (helpFocus?.isConnected && !helpFocus.closest('[hidden]') ? helpFocus : $('keys-open')).focus(); }
  function helpClose() { $('keys-close').click(); restoreHelpFocus(); }
  function alignSource() {
    if (!sourceLine || !['ready', 'partial'].includes(trace?.status)) return;
    const matching = nodes().map((n, i) => ({n, i})).filter(({n}) => n.range.start.line <= sourceLine && sourceLine <= n.range.end.line
      && (sourceLine !== n.range.start.line || (sourceCharacter ?? 0) >= n.range.start.character)
      && (sourceLine !== n.range.end.line || (sourceCharacter ?? 0) < n.range.end.character));
    if (matching.length) {
      matching.sort((a, b) => (a.n.end - a.n.start) - (b.n.end - b.n.start) || b.i - a.i);
      step = matching[0].i; remember(); sourceLine = undefined;
    }
  }
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing) return;
    if (!help.hidden) {
      if (event.key === 'Escape') { event.preventDefault(); helpClose(); }
      if (event.key === 'Tab') {
        const controls = [...help.querySelectorAll('button,input')].filter(e => !e.disabled);
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
      event.stopImmediatePropagation(); return;
    }
    if (queueMenu.open || event.target.closest('#review-queue,#notebook-view-options')) {
      if (event.key === 'Escape' && queueMenu.open) { event.preventDefault(); event.stopImmediatePropagation(); closeQueue(); }
      else if (event.key !== 'Escape' && event.key.length === 1 && ![' ', 'Enter'].includes(event.key)) event.stopImmediatePropagation();
      return;
    }
    if ($('header-status')?.open || event.target.closest('#header-status')) {
      if (event.key !== 'Escape') event.stopImmediatePropagation();
      return;
    }
    if (!app.contains(event.target) || event.target.isContentEditable || event.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) { if ('npctejksir?'.includes(event.key) && event.key.length === 1) event.stopImmediatePropagation(); return; }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const overlay = [...document.querySelectorAll('.overlay')].some(e => e !== help && !e.hidden);
    if (overlay || $('history')?.open) { if (event.key !== 'Escape') event.stopImmediatePropagation(); return; }
    if (event.key === 'Escape' && mode === 'proof') { event.preventDefault(); event.stopImmediatePropagation(); backToClaim(); return; }
    if (!$('review-letter-keys').checked) { if (event.key.length === 1) event.stopImmediatePropagation(); return; }
    if (linkedView()) {
      // Only help and the renderer's own keys (j/k claims, s source, i inspector)
      // work here; they never touch this review's state for the previewed notebook.
      if (event.key === '?') { event.preventDefault(); event.stopImmediatePropagation(); helpOpen(); }
      else if (event.key.length === 1 && !'jksi'.includes(event.key)) event.stopImmediatePropagation();
      return;
    }
    const actions = {n: () => move(1), p: () => move(-1), c: () => backToClaim(), t: proofMode, e: edit, '?': helpOpen};
    if (actions[event.key]) { event.preventDefault(); event.stopImmediatePropagation(); actions[event.key](); return; }
    if (['j', 'k', 's', 'i'].includes(event.key)) {
      claimsMode();
      queueMicrotask(() => {
        const sourceMode = document.querySelector('[data-mode="source"]')?.getAttribute('aria-pressed') === 'true';
        const chosen = document.querySelector(sourceMode ? '#source tr[data-selected="true"]' : '.claim[data-selected="true"]');
        if (sourceMode && chosen) selected = snapshot?.cells?.find(c => c.id === chosen.dataset.claim)?.index ?? selected;
        else if (chosen?.closest('.cellwrap')) selected = Number(chosen.closest('.cellwrap').dataset.index);
        update();
      });
    }
    if (event.key === 'r' && mode === 'proof') {
      event.preventDefault(); event.stopImmediatePropagation();
      if (dirty) return;
      $('check-project').click();
    }
  }, true);
  // Opening or leaving a linked notebook always leaves proof mode, which
  // would otherwise hide the notebook now shown behind the previewed one's steps.
  let wasLinked = linkedView();
  new MutationObserver(() => {
    if (linkedView() === wasLinked) return;
    wasLinked = linkedView();
    if (mode === 'proof') claimsMode();
  }).observe(document.body, {attributes: true, attributeFilter: ['class']});
  update();
})();
