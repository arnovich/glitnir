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
  const nav = document.createElement('nav');
  nav.className = 'review-nav'; nav.setAttribute('aria-label', 'Review navigation');
  nav.innerHTML = `<div class="seg" role="group" aria-label="Navigation mode"><button id="review-claims" aria-pressed="true">Claims <kbd>c</kbd></button><button id="review-proof" aria-pressed="false">Proof steps <kbd>t</kbd></button></div>
    <label id="review-filter-label">Show <select id="review-filter" aria-label="Claims to review"><option value="attention">Needs attention</option><option value="all">All claims</option></select></label>
    <span id="review-position" class="small" role="status"></span><div class="review-controls"><button class="secondary" id="review-previous">Previous <kbd>p</kbd></button></div>`;
  const next = $('next-unresolved') || document.createElement('button');
  next.id = 'next-unresolved'; next.className = 'secondary'; next.innerHTML = 'Next <kbd>n</kbd>';
  nav.querySelector('.review-controls').append(next);
  app.querySelector('.bar').after(nav);
  const proof = document.createElement('section');
  proof.id = 'proof-review'; proof.hidden = true;
  proof.innerHTML = `<div class="proof-heading"><div><h2 id="proof-title"></h2><p id="proof-status" class="small"></p></div><div class="review-controls"><button id="proof-refresh" class="secondary">Refresh steps</button><button id="proof-cancel" class="secondary" hidden>Cancel</button><button id="proof-back" class="secondary">Back to claims <kbd>Esc</kbd></button></div></div>
    <p id="proof-message" role="status"></p><div class="proof-grid"><div><pre id="proof-statement"></pre><div id="proof-tactics" role="group" aria-label="Recorded tactics"></div></div><div class="proof-goals"><h3 id="proof-goal-title"></h3><details id="proof-before"><summary>Goals before this tactic</summary><pre id="proof-before-text"></pre></details><pre id="proof-after"></pre><p class="small" id="proof-scope"></p><details id="proof-diagnostics" hidden><summary>Lean messages</summary><pre></pre></details><p id="proof-provenance" class="small muted"></p></div></div>`;
  nav.after(proof);
  const shortcuts = [
    ['n / p', 'Next / previous in the active mode'], ['c / t', 'Claims / Proof steps'],
    ['j / k', 'Next / previous of all claims (returns to Claims)'],
    ['e', 'Edit the selected claim or tactic in VS Code'], ['r', 'Check the saved file through the selected claim (in Proof steps: all of it)'],
    ['s', 'Switch document / source (returns to Claims)'], ['i', 'Show or hide the claim inspector'],
    ['Esc', 'Close the top dialog; otherwise return to Claims'], ['?', 'Keyboard shortcuts'],
    ['⌘K, then G (Mac) / Ctrl+K, then G (Windows/Linux)', 'From Lean source: reveal this location in Glitnir. Release the first combination before pressing G.'],
  ];
  const help = $('keys');
  const list = help.querySelector('dl'); list.replaceChildren();
  for (const [key, description] of shortcuts) {
    const term = document.createElement('dt'), desc = document.createElement('dd'), kbd = document.createElement('kbd');
    kbd.textContent = key; term.append(kbd); desc.textContent = description; list.append(term, desc);
  }
  const setting = document.createElement('label');
  setting.innerHTML = '<input id="review-letter-keys" type="checkbox" checked> Enable single-letter shortcuts in this notebook';
  list.after(setting);
  const explanation = document.createElement('p');
  explanation.textContent = 'These keys work only while Glitnir has focus, outside text fields and dialogs. Navigation does not run checks. Review → explore steps → edit → return → check. Live view follows saved edits from any editor or agent; unsaved drafts are not shown. Changed sections are highlighted briefly. Enable Follow edits to scroll to them without moving keyboard focus. Checking and proof-step refresh remain explicit.';
  list.before(explanation);
  help.querySelector('.dialog').setAttribute('aria-labelledby', 'keys-title');
  const labels = {checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', unknown: 'Unknown', failed: 'Failed', open: 'Open goal'};
  function currentCell() { return snapshot?.cells?.find(c => c.index === selected); }
  function currentClaim() { return snapshot?.claims?.find(c => c.name === currentCell()?.id); }
  function project() { return snapshot?.preview?.mode === 'project'; }
  function cells() {
    const claims = snapshot?.claims || [];
    const names = new Set(claims.filter(c => $('review-filter').value === 'all' || !['checked', 'refuted'].includes(c.status) || snapshot.check_errors?.[c.name]).map(c => c.name));
    return (snapshot?.cells || []).filter(c => c.kind === 'claim' && names.has(c.id));
  }
  function nodes() {
    if (!trace || !['ready', 'partial'].includes(trace.status) || trace.source_sha256 !== sha || (!project() && trace.claim !== currentClaim()?.name)) return [];
    const cell = currentCell();
    return (trace.nodes || []).filter(n => !cell || (n.range.start.line >= cell.start && n.range.start.line <= cell.end));
  }
  function remember() { if (trace?.id) positions.set(`${trace.id}:${selected}`, step); }
  function focusAfterRender(previous) {
    if (previous?.dataset.proofNode) {
      const found = [...$('proof-tactics').children].find(b => b.dataset.proofNode === String(nodes()[step]?.id));
      (found || $('review-proof')).focus({preventScroll: true});
    } else if (previous && (previous.hidden || previous.disabled || previous.closest('[hidden]') || (mode === 'proof' && previous.closest('.workspace')))) {
      $(mode === 'proof' ? 'review-proof' : 'review-claims').focus({preventScroll: true});
    }
  }
  function update() {
    const focused = document.activeElement;
    const inProof = mode === 'proof', queue = cells(), nodeList = nodes(), claim = currentClaim();
    nav.hidden = !snapshot || !!snapshot.error;
    proof.hidden = !inProof;
    document.body.classList.toggle('glitnir-proof-mode', inProof);
    $('review-claims').setAttribute('aria-pressed', String(!inProof));
    $('review-proof').setAttribute('aria-pressed', String(inProof));
    $('review-proof').disabled = dirty || (!project() && !claim);
    $('review-proof').title = dirty ? 'Save or revert edits before viewing proof steps' : 'Explore saved tactics (t)';
    $('review-filter-label').hidden = inProof || project();
    const previous = $('review-previous');
    next.hidden = previous.hidden = !inProof && project();
    previous.disabled = dirty || (inProof ? step < 0 : !queue.some(c => selected === null || c.index < selected));
    next.disabled = dirty || (inProof ? !nodeList.length || step >= nodeList.length - 1 : !queue.some(c => selected === null || c.index > selected));
    previous.title = inProof ? 'Previous recorded proof state (p)' : 'Previous matching claim (p)';
    next.title = inProof ? 'Next recorded proof state (n)' : queue.length ? `${queue.length} matching claims; next in source order (n)` : 'No matching claims';
    const index = queue.findIndex(c => c.index === selected);
    $('review-position').textContent = inProof ? nodeList.length ? step < 0 ? 'Initial goals' : `Step ${step + 1} of ${nodeList.length}` : 'No recorded steps' : project() ? 'Project source · whole-file checking' : index >= 0 ? `Claim ${index + 1} of ${queue.length}` : `${queue.length} matching claims`;
    if (!inProof) { focusAfterRender(focused); return; }
    $('proof-title').textContent = claim?.name || currentCell()?.id || snapshot?.file || 'Saved source unavailable';
    $('proof-status').textContent = claim ? `${labels[claim.status] || claim.status}${claim.reason ? ' · ' + claim.reason : ''}` : 'Project observation · use Check saved file for proof checking';
    $('proof-statement').textContent = claim ? `${claim.keyword} ${claim.name} ${claim.statement}` : currentCell()?.text || '';
    $('proof-refresh').disabled = dirty || !snapshot || !!snapshot.error || trace?.status === 'running';
    $('proof-cancel').hidden = trace?.status !== 'running';
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
    $('proof-after').textContent = node ? (step < 0 ? node.before : node.after).join('\n\n') || 'No goals in this tactic scope.' : trace?.status === 'running' ? 'Asking Lean…' : 'No tactic observations. This may be a term proof or an unavailable trace.';
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
    if (dirty) return;
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
    if (dirty || !snapshot) return;
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
    const epoch = ++generation; clearTimeout(polling); step = -1;
    trace = {status: 'running', claim: claim?.name, message: 'Preparing proof steps', nodes: []}; update();
    try {
      if (project()) {
        // Join an observation of this saved file that is already running,
        // such as one the inspector's proof goals started.
        const current = await host.fetch('/api/proof/trace');
        const running = await current.json().catch(() => ({}));
        if (epoch !== generation) return;
        if (current.ok && running.status === 'running' && running.source_sha256 === sha && running.id) { trace = running; update(); await poll(running.id, epoch); return; }
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
  function proofMode() {
    if (dirty || (!project() && !currentClaim()) || linkedView()) return;
    mode = 'proof'; step = positions.get(`${trace?.id}:${selected}`) ?? -1; update();
    if (!trace || trace.status === 'stale' || (!project() && trace.claim !== currentClaim()?.name)) void refresh();
  }
  function edit() {
    const node = nodes()[step];
    const cell = node ? snapshot?.cells?.find(c => c.start <= node.range.start.line && node.range.end.line <= c.end) : currentCell();
    if (mode === 'proof' && node && cell && !dirty) {
      // The host fetches this observation by id again before trusting its range.
      host.openSource({index: cell.index, sha256: sha, trace: {id: trace.id, node: node.id}});
    } else host.openSelectedSource();
  }
  $('review-claims').onclick = $('proof-back').onclick = claimsMode;
  $('review-proof').onclick = proofMode; $('review-filter').onchange = update;
  $('review-previous').onclick = () => move(-1); next.onclick = () => move(1);
  $('proof-refresh').onclick = () => void refresh();
  $('proof-cancel').onclick = () => { pendingRefresh = false; void host.fetch('/api/proof/cancel', {method: 'POST', body: '{}'}).catch(() => {}); update(); };
  $('open-source').onclick = edit; $('open-source').title = 'Edit the selected claim or tactic in VS Code (e)';
  document.addEventListener('glitnir-snapshot', event => {
    const nextSnapshot = event.detail;
    if (sha && nextSnapshot?.sha256 !== sha) { selected = null; invalidate('Saved source changed. Refresh proof steps.'); }
    snapshot = nextSnapshot; sha = snapshot?.sha256;
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
    if (message.type === 'reviewCommand' && (!linkedView() || message.action === 'help')) ({next: () => move(1), previous: () => move(-1), claims: claimsMode, proof: proofMode, help: helpOpen})[message.action]?.();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-mode]')) claimsMode();
    if (event.target.closest('#keys-open')) helpFocus = document.activeElement;
    if (event.target.closest('#keys-close') || event.target === help) queueMicrotask(restoreHelpFocus);
  }, true);
  document.addEventListener('click', event => {
    if (linkedView()) return;
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
    if (!app.contains(event.target) || event.target.isContentEditable || event.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) { if ('npctejksir?'.includes(event.key) && event.key.length === 1) event.stopImmediatePropagation(); return; }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const overlay = [...document.querySelectorAll('.overlay')].some(e => e !== help && !e.hidden);
    if (overlay || $('history')?.open) { if (event.key !== 'Escape') event.stopImmediatePropagation(); return; }
    if (event.key === 'Escape' && mode === 'proof') { event.preventDefault(); event.stopImmediatePropagation(); claimsMode(); return; }
    if (!$('review-letter-keys').checked) { if (event.key.length === 1) event.stopImmediatePropagation(); return; }
    if (linkedView()) {
      // Only help and the renderer's own keys (j/k claims, s source, i inspector)
      // work here; they never touch this review's state for the previewed notebook.
      if (event.key === '?') { event.preventDefault(); event.stopImmediatePropagation(); helpOpen(); }
      else if (event.key.length === 1 && !'jksi'.includes(event.key)) event.stopImmediatePropagation();
      return;
    }
    const actions = {n: () => move(1), p: () => move(-1), c: claimsMode, t: proofMode, e: edit, '?': helpOpen};
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
