// Review navigation and keyboard controls for the VS Code preview.
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const app = document.querySelector('.app');
  if (!app || !window.glitnirHost) return;
  app.tabIndex = -1;
  const host = window.glitnirHost;
  let snapshot, sha, selected = null, dirty = false, helpFocus, pendingReveal;
  const nav = document.createElement('nav');
  nav.className = 'review-nav'; nav.setAttribute('aria-label', 'Review navigation');
  nav.innerHTML = `<label id="review-filter-label">Show <select id="review-filter" aria-label="Claims to review"><option value="attention">Needs attention</option><option value="all">All claims</option></select></label>
    <details id="review-queue"><summary id="review-queue-summary"></summary><div id="review-items" aria-label="Items to review"></div></details>
    <div class="review-controls"><button class="secondary" id="review-previous">Previous <kbd>p</kbd></button><span id="review-position" role="status"></span></div>`;
  const next = $('next-unresolved') || document.createElement('button');
  next.id = 'next-unresolved'; next.className = 'secondary'; next.innerHTML = 'Next <kbd>n</kbd>'; next.hidden = false;
  const navigation = nav.querySelector('.review-controls');
  navigation.append(next);
  const activity = app.querySelector('.live-bar');
  activity.prepend(nav);
  $('notebook-view-options').querySelector('.notebook-preferences').prepend($('review-filter-label'));
  const queueMenu = $('review-queue');
  const queueSummary = $('review-queue-summary');
  function closeQueue() { queueMenu.open = false; queueSummary.focus({preventScroll: true}); }
  document.addEventListener('click', event => { if (!queueMenu.contains(event.target)) queueMenu.open = false; });
  queueMenu.addEventListener('keydown', event => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeQueue(); }
    else if (event.key.length === 1) event.stopPropagation();
  });
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => {
    document.documentElement.style.setProperty('--review-offset', `${activity.getBoundingClientRect().height + 12}px`);
  }).observe(activity);
  // The renderer draws the shortcut list (one source for every listing); this
  // layer adds only its own setting.
  const help = $('keys');
  const setting = document.createElement('label');
  setting.innerHTML = '<input id="review-letter-keys" type="checkbox" checked> Enable single-letter shortcuts in this notebook';
  $('keys-foot').prepend(setting);
  help.querySelector('.dialog').setAttribute('aria-labelledby', 'keys-title');
  const labels = {checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', unknown: 'Unknown', failed: 'Failed', open: 'Open goal'};
  function currentCell() { return snapshot?.cells?.find(c => c.index === selected); }
  function project() { return snapshot?.preview?.mode === 'project'; }
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
  function updateQueue(queue) {
    queueMenu.hidden = !queue.length;
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
  function update() {
    const focused = document.activeElement;
    const queue = cells();
    nav.hidden = !snapshot || !!snapshot.error || (!queue.length && (project() || $('review-filter').value !== 'all'));
    activity.hidden = nav.hidden;
    if (nav.hidden && nav.contains(focused)) document.querySelector('[data-mode="doc"]').focus({preventScroll: true});
    updateQueue(queue);
    $('review-filter-label').hidden = project();
    const previous = $('review-previous');
    previous.disabled = dirty || !queue.some(c => selected === null || c.index < selected);
    next.disabled = dirty || !queue.some(c => selected === null || c.index > selected);
    const index = queue.findIndex(c => c.index === selected);
    if (project()) {
      previous.title = 'Previous claim that needs attention (p)';
      next.title = queue.length ? `${queue.length} need attention; next in source order (n)` : 'No claims queued for review';
      $('review-position').textContent = index >= 0 ? `Item ${index + 1} of ${queue.length}` : '';
    } else {
      previous.title = 'Previous matching claim (p)';
      next.title = queue.length ? `${queue.length} matching claims` : 'No matching claims';
      $('review-position').textContent = index >= 0 ? `Claim ${index + 1} of ${queue.length}` : queue.length ? `${queue.length} matching claims` : '';
    }
    $('review-position').hidden = !$('review-position').textContent;
  }
  function reveal(cell) {
    selected = cell.index;
    document.dispatchEvent(new CustomEvent('glitnir-reveal-cell', {detail: {index: cell.index, sha256: sha}}));
    update();
  }
  function move(direction) {
    if (dirty || linkedView()) return;
    const queue = cells();
    const cell = direction > 0 ? queue.find(c => selected === null || c.index > selected) : queue.findLast(c => selected === null || c.index < selected);
    if (cell) reveal(cell);
  }
  const linkedView = () => document.body.classList.contains('glitnir-linked-view');
  const edit = () => host.openSelectedSource();
  $('review-filter').onchange = update;
  $('review-previous').onclick = () => move(-1); next.onclick = () => move(1);
  $('open-source').onclick = edit; $('open-source').title = 'Edit the selected claim in VS Code (e)';
  document.addEventListener('glitnir-snapshot', event => {
    const nextSnapshot = event.detail;
    if (sha && nextSnapshot?.sha256 !== sha) selected = null;
    snapshot = nextSnapshot; sha = snapshot?.sha256;
    if (selected !== null && !currentCell()) selected = null;
    if (selected === null) selected = snapshot?.cells?.find(c => c.kind === (project() ? 'source' : 'claim'))?.index ?? null;
    if (pendingReveal && pendingReveal.sha256 === sha) { const reveal = pendingReveal; pendingReveal = undefined; applySourceReveal(reveal); }
    update();
  });
  function applySourceReveal(message) {
    selected = message.index;
    update();
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if (message.type === 'editorState') {
      dirty = message.dirty;
      window.glitnirWorkbench?.render({inspectorOnly: true});
      update();
    }
    if (message.type === 'revealCell') {
      if (message.sha256 !== sha) {
        pendingReveal = message;
      } else applySourceReveal(message);
    }
    if (message.type === 'reviewCommand' && (!linkedView() || message.action === 'help')) ({next: () => move(1), previous: () => move(-1), help: helpOpen})[message.action]?.();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('#keys-open')) helpFocus = document.activeElement;
    if (event.target.closest('#keys-close') || event.target === help) queueMicrotask(restoreHelpFocus);
  }, true);
  document.addEventListener('click', event => {
    if (linkedView()) return;
    const link = event.target.closest('[data-act="goto"]'), wrap = event.target.closest('.cellwrap'), row = event.target.closest('#source tr[data-claim]');
    if (link) selected = snapshot?.cells?.find(c => c.id === link.dataset.id)?.index ?? null;
    else if (wrap) selected = Number(wrap.dataset.index);
    else if (row) selected = snapshot?.cells?.find(c => c.id === row.dataset.claim)?.index ?? null;
    if (link || wrap || row) update();
  });
  function helpOpen() { helpFocus = document.activeElement; $('keys-open').click(); }
  function restoreHelpFocus() { (helpFocus?.isConnected && !helpFocus.closest('[hidden]') ? helpFocus : $('keys-open')).focus(); }
  function helpClose() { $('keys-close').click(); restoreHelpFocus(); }
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
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
    if (!app.contains(event.target) || event.target.isContentEditable || event.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) { if ('npejksirg?'.includes(event.key) && event.key.length === 1) event.stopImmediatePropagation(); return; }
    const overlay = [...document.querySelectorAll('.overlay')].some(e => e !== help && !e.hidden);
    if (overlay || $('history')?.open) { if (event.key !== 'Escape') event.stopImmediatePropagation(); return; }
    if (!$('review-letter-keys').checked) { if (event.key.length === 1) event.stopImmediatePropagation(); return; }
    if (linkedView()) {
      // Only help and the renderer's own keys (j/k claims, s source, i inspector)
      // work here; they never touch this review's state for the previewed notebook.
      if (event.key === '?') { event.preventDefault(); event.stopImmediatePropagation(); helpOpen(); }
      else if (event.key.length === 1 && !'jksi'.includes(event.key)) event.stopImmediatePropagation();
      return;
    }
    const actions = {n: () => move(1), p: () => move(-1), e: edit, '?': helpOpen};
    if (actions[event.key]) { event.preventDefault(); event.stopImmediatePropagation(); actions[event.key](); return; }
    if (['j', 'k', 's', 'i'].includes(event.key)) {
      queueMicrotask(() => {
        const sourceMode = document.querySelector('[data-mode="source"]')?.getAttribute('aria-pressed') === 'true';
        const chosen = document.querySelector(sourceMode ? '#source tr[data-selected="true"]' : '.claim[data-selected="true"]');
        if (sourceMode && chosen) selected = snapshot?.cells?.find(c => c.id === chosen.dataset.claim)?.index ?? selected;
        else if (chosen?.closest('.cellwrap')) selected = Number(chosen.closest('.cellwrap').dataset.index);
        update();
      });
    }
  }, true);
  update();
})();
