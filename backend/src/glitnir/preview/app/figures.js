// The workbench's mathematics, views and figures, and prose images.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, API, HOST, LABEL, UI, claim, esc, fetch, icon, leanOff, leanVersion, linkedPath, post,
    projectMode, recorded, refresh} = workbench;
  // Defined by a later file and bound when it loads: nothing may call one while the page is still loading.
  const leanOf = (...a) => workbench.leanOf(...a);
  const lineLinkHTML = (...a) => workbench.lineLinkHTML(...a);
  const namespaceScopes = (...a) => workbench.namespaceScopes(...a);
  const render = (...a) => workbench.render(...a);
  const select = (...a) => workbench.select(...a);
  const textbookHTML = (...a) => workbench.textbookHTML(...a);
  const trimmed = (...a) => workbench.trimmed(...a);
  // ----- mathematics ------------------------------------------------------------
  // `$…$` is inline and `$$…$$` display TeX, typeset by the vendored KaTeX
  // after each render. The HTML carries only the TeX, so without KaTeX, or
  // for TeX it cannot parse, the reader still sees the source.
  const mathHTML = (tex, display) => `<span class="math${display ? ' math-display' : ''}" data-tex="${esc(tex)}">${esc(tex)}</span>`;
  function notebookReference(target, label) {
    const module = target.replace(/^\.\//, '').replace(/\.lean$/, '');
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(module)
        || !(UI.data?.references || []).some(item => item.module === module)) return null;
    if (projectMode()) {
      // Read in this panel. The link is relative to the notebook shown; the
      // previewed notebook itself is the trail's first entry. Its href is
      // what an export keeps as plain text.
      const path = [...(linkedPath() || '').split('/').slice(0, -1), `${module}.lean`].join('/');
      const link = path === UI.main?.file ? '' : path;
      return `<a class="notebook-reference" data-act="open-notebook" data-link="${esc(link)}" data-sha="${esc(UI.data.sha256)}" href="/?reference=${encodeURIComponent(module)}" title="Read ${esc(module)}.lean in this panel">${label}</a>`;
    }
    return `<a class="notebook-reference" data-sha="${esc(UI.data.sha256)}" href="/?reference=${encodeURIComponent(module)}" target="_blank" rel="noopener" title="Open ${esc(module)}.lean in another notebook tab">${label}</a>`;
  }
  // Code and math remain atomic; only exact sibling names are navigable.
  function inlineHTML(text) {
    const plain = s => esc(s).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    let out = '';
    let at = 0;
    for (const m of text.matchAll(/`([^`]+)`|\$\$([^$]+)\$\$|\$([^$\n]+)\$|\[([^\]\n]+)\]\(([^)\s]+)\)/g)) {
      out += plain(text.slice(at, m.index));
      if (m[1] !== undefined) {
        const code = `<code>${esc(m[1])}</code>`;
        out += notebookReference(m[1], code) || code;
      } else if (m[4] !== undefined) {
        out += notebookReference(m[5], plain(m[4])) || plain(m[0]);
      } else out += mathHTML((m[2] ?? m[3]).trim(), m[2] !== undefined);
      at = m.index + m[0].length;
    }
    return out + plain(text.slice(at));
  }
  // TeX is untrusted text: agents write docstrings. Long TeX is left as
  // source rather than freeze the page, sizes are bounded, and the CSS clips
  // typeset maths to its own box so it cannot paint over a status.
  const MAX_TEX = 2000;
  const KATEX = {throwOnError: true, trust: false, maxSize: 10, maxExpand: 1000};
  const typeset = new Map();
  function typesetMath(...roots) {
    if (!window.katex) return;
    const seen = new Set();
    for (const el of roots.flatMap(root => [...root.querySelectorAll('.math[data-tex]')])) {
      const tex = el.dataset.tex;
      if (tex.length > MAX_TEX) { el.title = `Not typeset: longer than ${MAX_TEX} characters`; continue; }
      const display = el.classList.contains('math-display');
      const key = `${display ? 'D' : 'I'}${tex}`;
      seen.add(key);
      if (!typeset.has(key)) {
        const node = document.createElement('span');
        // KaTeX styles through the DOM, which the pages' CSP allows; an
        // inline style attribute in the HTML would be refused.
        try { window.katex.render(tex, node, {...KATEX, displayMode: display}); typeset.set(key, node); } catch (error) { typeset.set(key, null); }
      }
      const node = typeset.get(key);
      if (node) { el.replaceChildren(...node.cloneNode(true).childNodes); el.classList.add('typeset'); } else el.title = 'KaTeX could not typeset this TeX';
    }
    for (const key of typeset.keys()) if (!seen.has(key)) typeset.delete(key);
  }

  function markdown(text, headingPrefix = null) {
    // Blank lines separate paragraphs; headings and $$ display lines stand
    // alone; lines starting "- " form a list, continued by indented lines.
    const out = [];
    let headingIndex = 0;
    let paragraph = [];
    let items = [];
    const inline = inlineHTML;
    const flush = () => {
      if (paragraph.length) out.push(`<p>${inline(paragraph.join(' '))}</p>`);
      if (items.length) out.push(`<ul>${items.map(i => `<li>${inline(i)}</li>`).join('')}</ul>`);
      paragraph = [];
      items = [];
    };
    for (const raw of text.split('\n')) {
      const line = raw.trim();
      if (!line) { flush(); continue; }
      if (line.startsWith('- ')) { if (paragraph.length) { const kept = items; items = []; flush(); items = kept; } items.push(line.slice(2)); continue; }
      if (items.length && /^\s/.test(raw)) { items[items.length - 1] += ' ' + line; continue; }
      if (items.length) flush();
      if (line.startsWith('# ')) { flush(); continue; }
      const heading = /^(#{2,6}) (.+)$/.exec(line);
      if (heading) {
        flush();
        const folded = /\s+\{fold\}$/.test(heading[2]);
        const label = folded ? heading[2].replace(/\s+\{fold\}$/, '') : heading[2];
        const level = heading[1].length;
        out.push(`<h${level}${headingPrefix ? ` id="${headingPrefix}-${headingIndex++}"` : ''} data-heading="${esc(label)}"${folded ? ' data-section-fold="true"' : ''}>${inline(label)}</h${level}>`);
        continue;
      }
      const image = /^!\[([^\]]*)\]\((\S+)\)$/.exec(line);
      if (image) { flush(); out.push(figureHTML(image[2], image[1])); continue; }
      if (/^\$\$.*\$\$$/.test(line)) { flush(); out.push(`<div class="equation">${mathHTML(line.slice(2, -2).trim(), true)}</div>`); continue; }
      paragraph.push(line);
    }
    flush();
    return out.join('');
  }
  function title() {
    for (const cell of UI.data.cells) {
      if (cell.kind !== 'prose') continue;
      const match = /^# (.+)$/m.exec(cell.markdown);
      if (match) return match[1];
    }
    return UI.data.file;
  }

  const when = seconds => seconds ? new Date(seconds * 1000).toLocaleTimeString() : '';

  // ----- document ---------------------------------------------------------------
  // ----- views and figures ---------------------------------------------------------
  // A view is drawn by Lean from the notebook's definitions; the drawing is
  // shown as an image, so nothing in it can run.
  function viewHTML(cell, line) {
    const view = (UI.data.views || []).find(v => v.cell === cell.index && v.line === line);
    if (!view) return `<section data-document-start="${line}" data-document-end="${line}" class="cell"><div class="note-line" data-tone="red">${icon('x')}<span>${lineLinkHTML(line, line)} This view directive is incomplete. Write it as <code>${UI.data.preview?.mode === 'project' ? '-- @view &lt;kind&gt; &lt;name&gt;' : '-- @view model|circuit|dynamics &lt;name&gt;'}</code>.</span></div></section>`;
    const key = `${cell.index}:${line}:${UI.data.sha256}`;
    const drawing = UI.drawings[key];
    const linked = Boolean(linkedPath());
    if (!drawing && !linked && !leanOff()) loadView(key, view);
    const head = window.glitnirCards.header({label: `${view.kind} diagram`, name: view.name, locationHTML: lineLinkHTML(line, line)});
    let body;
    if (linked) body = `<div class="note-line" data-tone="muted">${icon('branch')}<span>Lean draws this view in the notebook's own preview.</span></div>`;
    else if (view.error) body = `<div class="note-line" data-tone="red">${icon('x')}<span>${esc(view.error)}</span></div>`;
    else if (leanOff() && (!drawing || drawing.loading || drawing.error)) body = `<div class="note-line" data-tone="muted">${icon('branch')}<span>Views need Lean ${esc(leanVersion(UI.data.preview.lean.toolchain))}.</span></div>`;
    else if (!drawing || drawing.loading) body = `<p class="small muted view-wait" role="status">${esc(drawing?.message || 'Lean is drawing it…')}</p>`
      + (drawing?.drawing ? `<div class="claim-actions"><button type="button" class="secondary" data-act="view-cancel" data-line="${line}" data-sha="${esc(UI.data.sha256)}">${icon('x')}Stop</button><span class="small muted">Drawing is bounded by the check time limit.</span></div>` : '');
    else if (drawing.error) body = `<div class="note-line" data-tone="red">${icon('x')}<span class="lean-message">No drawing: ${esc(drawing.error)}</span></div>`
      + `<div class="claim-actions"><button type="button" class="secondary" data-act="view-retry" data-key="${esc(key)}">${icon('replay')}Draw again</button></div>`;
    else if (drawing.graph || drawing.model) body = diagramHTML(key);
    else body = `<div class="view-body"><img alt="${esc(view.kind)} view of ${esc(view.name)}" src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(drawing.svg)}"></div>`;
    // Lean drew it with the notebook's proofs replaced by `sorry`; a drawing
    // never changes a status either way.
    if (drawing?.proofs_skipped && !drawing.error && !drawing.loading) body = `<div class="note-line" data-tone="muted">${icon('branch')}<span>Drawn without re-running the notebook's proofs.</span></div>` + body;
    // Drawn while a dependency's state could not be read: it is not redrawn
    // on every poll, so it may be out of date.
    if (drawing?.uncertain && !drawing.error && !drawing.loading) body = `<div class="note-line" data-tone="amber">${icon('branch')}<span>May be out of date: the state of a dependency could not be read when this was drawn.</span></div>` + body;
    return `<section data-document-start="${line}" data-document-end="${line}" class="cell box view-cell">${head}${body}</section>`;
  }

  function diagramClaim(name) {
    const scopes = namespaceScopes(UI.data.cells);
    for (const cell of UI.data.cells.filter(c => c.kind === 'claim')) {
      const c = claim(cell.id), namespace = scopes.get(cell.index)?.get(cell.start)?.namespace;
      const qualified = c?.name.startsWith('_root_.') ? c.name.slice(7) : [namespace,c?.name].filter(Boolean).join('.');
      if (c && qualified === name) return {name: c.name, label: LABEL[c.status] || c.status};
    }
    return null;
  }
  function diagramHTML(key, expanded = false) {
    return window.glitnirCircuits.render(UI.drawings[key], {key, state: UI.diagrams[key] || {}, claimFor: diagramClaim, idSuffix: expanded ? '-expanded' : ''});
  }
  function closeDiagram() {
    UI.expandedDiagram = null;
    $('diagram-dialog')?.close();
  }
  function renderDiagramDialog() {
    const key = UI.expandedDiagram;
    if (!key || !key.endsWith(UI.data.sha256) || !UI.drawings[key]) { closeDiagram(); return; }
    let dialog = $('diagram-dialog');
    if (!dialog) {
      dialog = document.createElement('dialog'); dialog.id = 'diagram-dialog'; dialog.className = 'diagram-dialog';
      dialog.setAttribute('aria-label', 'Expanded circuit diagram');
      dialog.addEventListener('close', () => {
        const old = dialog.dataset.key; UI.expandedDiagram = null;
        [...$('doc').querySelectorAll('[data-command=expand]')].find(b => b.dataset.key === old)?.focus({preventScroll: true});
      });
      document.body.append(dialog);
    }
    dialog.dataset.key = key;
    dialog.innerHTML = `<div class="diagram-dialog-head"><strong>Circuit diagram</strong><button type="button" class="secondary" data-act="diagram-close">Close</button></div>${diagramHTML(key, true)}`;
    if (!dialog.open) dialog.showModal();
  }
  function diagramAction(button) {
    const key = button.dataset.key;
    if (!key?.endsWith(UI.data.sha256)) return;
    const state = UI.diagrams[key] ||= {};
    const command = button.dataset.command;
    if (command === 'expand') UI.expandedDiagram = key;
    else if (command === 'fit') state.zoom = null;
    else if (command === 'actual') state.zoom = 1;
    else if (command === 'labels') state.labels = state.labels === false;
    else if (command === 'circuit' || command === 'overview') { state.mode = command; state.zoom = null; }
    else {
      const viewport = button.closest('.view-cell,.diagram-dialog').querySelector('.diagram-viewport');
      const svg = viewport?.querySelector('svg');
      const fitted = svg && viewport.clientWidth ? Math.min(1, viewport.clientWidth / Number(svg.getAttribute('viewBox').split(' ')[2])) : 1;
      state.zoom = Math.max(.001, Math.min(3, (state.zoom || fitted) * (command === 'in' ? 1.25 : .8)));
    }
    render();
  }
  function saveDiagramUI() {
    const active = document.activeElement;
    const region = active?.closest('.diagram-viewport');
    const wireFocus = region && (active === region || active.matches('.circuit-condition')) ? {key: region.dataset.diagramKey, dialog: !!region.closest('dialog'), href: active.getAttribute('href')} : null;
    return {wireFocus, scroll: [...document.querySelectorAll('.diagram-viewport')].map(el => ({key: el.dataset.diagramKey, dialog: !!el.closest('dialog'), left: el.scrollLeft, top: el.scrollTop})),
      focus: active?.matches('[data-act=diagram],[data-diagram-contract],[data-act=diagram-close]') ? {key: active.dataset.key || active.dataset.diagramContract, command: active.dataset.command, close: active.dataset.act === 'diagram-close', select: active.hasAttribute('data-diagram-contract'), dialog: !!active.closest('dialog')} : null};
  }
  function restoreDiagramUI(saved) {
    if (saved.wireFocus) {
      const f = saved.wireFocus;
      const region = [...document.querySelectorAll('.diagram-viewport')].find(el => el.dataset.diagramKey === f.key && !!el.closest('dialog') === f.dialog);
      const element = f.href ? [...(region?.querySelectorAll('.circuit-condition') || [])].find(el => el.getAttribute('href') === f.href) : region;
      element?.focus({preventScroll: true});
    }
    for (const state of saved.scroll) {
      const el = [...document.querySelectorAll('.diagram-viewport')].find(el => el.dataset.diagramKey === state.key && !!el.closest('dialog') === state.dialog);
      if (el) { el.scrollLeft = state.left; el.scrollTop = state.top; }
    }
    if (saved.focus) {
      const f = saved.focus;
      const el = [...document.querySelectorAll('[data-act=diagram],[data-diagram-contract],[data-act=diagram-close]')].find(el => !!el.closest('dialog') === f.dialog && (f.close ? el.dataset.act === 'diagram-close' : (el.dataset.key || el.dataset.diagramContract) === f.key && (f.select ? el.hasAttribute('data-diagram-contract') : el.dataset.command === f.command)));
      if (!(UI.expandedDiagram && !f.dialog)) el?.focus({preventScroll: true});
    }
  }

  // The server draws views in the background, one at a time. Asking for a
  // view starts it, or names the job drawing before it; that job is then
  // polled on its own cheap route, which reads no notebook, until it ends.
  const VIEW_POLL_MS = 2000;
  const JOB = /^[0-9a-f]{32}$/;
  async function viewAnswer(path) {
    try {
      const response = await fetch(path, {cache: 'no-store', headers: API});
      const answer = await response.json().catch(() => ({}));
      return response.ok ? answer : {error: answer.error || `the server answered ${response.status}`, transient: response.status >= 500};
    } catch (error) {
      return {error: 'the server did not answer', transient: true};
    }
  }
  async function loadView(key, view, job = null, mine = false) {
    UI.drawings[key] = {loading: true, ...(UI.drawings[key]?.loading ? UI.drawings[key] : {})};
    const answer = await viewAnswer(job ? `/api/view/job?id=${job}` : `/api/view?cell=${view.cell}&line=${view.line}`);
    // A drawing forgotten meanwhile (Draw again, a changed source) is not revived.
    if (!UI.drawings[key]?.loading) return;
    const waiting = answer.status === 'drawing' || answer.status === 'queued';
    let next = answer;
    if (waiting && JOB.test(String(answer.job || ''))) {
      mine = job ? mine : answer.status === 'drawing';
      // While another view draws, this card keeps saying that it waits.
      const message = mine || !job ? answer.message : UI.drawings[key].message;
      next = {loading: true, drawing: mine, message: String(message || ''), job: answer.job};
    } else if (waiting) next = {error: 'the server named no drawing to wait for', transient: true};
    else if (job && !mine) next = {loading: true, message: UI.drawings[key].message};  // the view before it ended: ask for this one
    UI.drawings[key] = next;
    render();
    if (!next.loading) return;
    const ask = next.job && waiting ? next.job : null;
    setTimeout(() => { if (UI.drawings[key]?.loading && UI.data?.sha256 && key.endsWith(UI.data.sha256)) loadView(key, view, ask, mine); }, VIEW_POLL_MS);
  }
  async function cancelView(button) {
    await post('/api/view/cancel', {sha256: button.dataset.sha, line: Number(button.dataset.line)});
    render();
  }
  async function cancelInspection() {
    await post('/api/project/inspect/cancel', {});
    await refresh();
  }
  // Also stops a check still waiting for another Lean run.
  async function cancelCheck() {
    await post('/api/project/check/cancel', {});
    await refresh();
  }

  // Evidence is what a provider computed for one listed `@evidence` directive:
  // shown apart from claims, and never a reason for any claim's status. The
  // charts are drawn here from the saved numbers; no provider markup is shown.
  const EVIDENCE_STATUS = {never: ['muted', 'Never run'], running: ['blue', 'Running'], current: ['green', 'Current'], stale: ['amber', 'Out of date'], uncompared: ['muted', 'Not compared'], failed: ['red', 'Failed']};
  const projectEvidence = cell => (UI.data.evidence || []).filter(e => e.cell === cell.index).sort((a, b) => a.line - b.line);
  // A verb no installed provider runs gets a note in the source, not a card,
  // unless saved evidence for it remains to be shown.
  const needsProvider = entry => !entry.provider && Boolean(entry.verb) && !entry.snapshot;

  function evidenceHTML(entry, staticPage = false) {
    const line = entry.line, snap = entry.snapshot;
    const running = entry.status === 'running';
    // Without a provider nothing can run it, so current saved evidence is
    // only saved; out-of-date evidence still says so.
    const [tone, label] = staticPage && running ? ['amber', 'Running at export'] : recorded() && entry.status === 'current' ? ['muted', 'As recorded'] : recorded() && entry.status === 'failed' ? ['red', 'Failed, as recorded'] : !entry.provider && snap && entry.status === 'current' ? ['muted', 'Saved'] : Object.hasOwn(EVIDENCE_STATUS, entry.status) ? EVIDENCE_STATUS[entry.status] : ['muted', entry.status];
    // A saved failed run. This session's run error takes its place, unless the
    // server marks the saved failure as earlier than that unsaved error.
    const failure = (entry.run_error && !entry.failure?.earlier) || (running && !staticPage) ? null : entry.failure;
    const what = `${esc(entry.verb || '?')} <code>${esc(entry.target || '?')}</code>${entry.provider ? ` · ${esc(entry.provider)}` : ''}`;
    const pill = entry.error && !snap ? '' : `<span class="pill" data-tone="${tone}">${esc(label)}</span>`;
    const head = `<div class="box-head"><span class="tag">Evidence · not proof</span><span class="small">${what}</span><span class="evidence-state">${pill}${lineLinkHTML(line, line)}</span></div>`;
    const note = (tone, text) => `<div class="note-line" data-tone="${tone}">${icon(tone === 'red' ? 'x' : 'branch')}<span>${text}</span></div>`;
    // A directive in error, or without its provider, keeps any saved evidence
    // visible but cannot be run.
    const runnable = !entry.error && entry.provider && !entry.unavailable;
    let body = '';
    let action = '';
    if (!entry.error) {
      if (running && !staticPage) body = '<p class="small muted evidence-wait">Running in Lean and the provider…</p>';
      else if (!snap && !failure && !entry.run_error) body = '<p class="small muted evidence-wait">Not run yet.</p>';
    }
    if (snap && (!running || staticPage)) {
      try {
        const dim = entry.status === 'stale' || running;
        body = `<div class="evidence-charts">${chartHTML(snap.series, dim)}${snap.plane ? phaseHTML(snap.series, snap.plane, dim) : ''}</div>`
          + (snap.finite ? '' : note('amber', 'Some values were not finite; they are not drawn.'))
          + provenanceHTML(snap.provenance);
      } catch (error) {
        body = note('red', `The saved evidence is unreadable.${leanOff() ? '' : ' Run it again.'}`);
      }
    }
    if (staticPage && running) body = '<p class="small muted">A run was in progress at export time. Any chart below is the previous saved evidence; its currency is not established here.</p>' + body;
    // An errored card shows its error, then at most the last run's failure;
    // a card errored only by its missing provider still says it is out of date.
    if (failure) {
      const day = /^\d{4}-\d{2}-\d{2}/.test(failure.created || '') ? ` on ${esc(failure.created.slice(0, 10))}` : '';
      body = (failure.status === 'stale'
        ? note('amber', `<span class="lean-message">An earlier run, on different source or environment, failed: ${esc(failure.message)}</span>`)
        : note('red', `<span class="lean-message">${failure.earlier ? 'An earlier run' : 'The last run'} failed${day}: ${esc(failure.message)}</span>`)) + body;
    }
    if (entry.status === 'stale' && snap && (!entry.error || !entry.provider)) body = note('amber', `Out of date: ${recorded() ? 'the saved source' : 'the source or its environment'} changed since this run.${entry.provider && !recorded() && !leanOff() ? ' Run it again to update it.' : ''}`) + body;
    if (entry.run_error) body = note('red', `<span class="lean-message">The last run failed: ${esc(entry.run_error)}</span>`) + body;
    if (entry.unavailable && !entry.error) body = note('amber', `${esc(entry.unavailable)} Saved evidence remains viewable.`) + body;
    if (entry.error) body = note('red', esc(entry.error) + (snap ? ' Saved evidence remains viewable.' : '')) + body;
    if (!staticPage && runnable && !running && !linkedPath() && !recorded() && !leanOff()) action = `<div class="claim-actions"><button type="button" class="${entry.status === 'current' ? 'secondary' : 'primary'}" data-act="evidence-run" data-line="${line}" data-sha="${esc(UI.data.sha256)}">${icon('replay')}${snap || entry.failure || entry.status === 'failed' || entry.status === 'stale' ? 'Run again' : 'Run'}</button><span class="small muted">Evidence never changes a claim's status.</span></div>`;
    return `<section data-document-start="${line}" data-document-end="${line}" class="cell box evidence">${head}${body}${action}</section>`;
  }

  // A decision is what a provider proposed for one listed `@decide` directive.
  // The provider's block becomes a candidate only when the project checker's
  // kernel check of the saved source through the directive, followed by that
  // block, checks its theorem. Nothing here writes the notebook: Accept names
  // the decision, and the editor inserts the block the server serves for it.
  const DECISION_STATUS = {never: ['muted', 'Never run'], running: ['blue', 'Running'], unknown: ['amber', 'Unknown'], rejected: ['red', 'Rejected'],
    candidate: ['blue', 'Candidate'], accepted: ['green', 'Accepted'], stale: ['amber', 'Out of date'], uncompared: ['muted', 'Not compared'], failed: ['red', 'Failed']};
  const RUN_AGAIN = new Set(['never', 'unknown', 'rejected', 'stale', 'candidate', 'failed']);
  // A goal kind no installed provider decides gets a note in the source, not
  // a card, unless a saved outcome for it remains to be shown.
  const needsDecider = entry => !entry.provider && !entry.outcome;

  function decisionHTML(entry, staticPage = false) {
    const {line, end_line: end, outcome} = entry;
    const running = entry.status === 'running';
    const [tone, label] = staticPage && running ? ['amber', 'Running at export'] : recorded() && entry.status === 'failed' ? ['red', 'Failed, as recorded'] : Object.hasOwn(DECISION_STATUS, entry.status) ? DECISION_STATUS[entry.status] : ['muted', entry.status];
    // A saved failed run. This session's run error takes its place, unless the
    // server marks the saved failure as earlier than that unsaved error.
    // An accepted block is the project check's to judge: no failure note.
    const failure = (entry.run_error && !entry.failure?.earlier) || (running && !staticPage) || entry.status === 'accepted' ? null : entry.failure;
    const what = `<code>${esc(entry.name || '?')}</code>${entry.kind ? ` · ${esc(entry.kind)}` : ''}${entry.provider ? ` · ${esc(entry.provider)}` : ''}`;
    // A directive in error has no status of its own until it is fixed.
    const pill = entry.error && !outcome ? '<span class="pill" data-tone="muted">Error</span>' : `<span class="pill" data-tone="${tone}">${esc(label)}</span>`;
    const head = `<div class="box-head"><span class="tag">Decision</span><span class="small">${what}</span><span class="evidence-state">${pill}${lineLinkHTML(line, end)}</span></div>`;
    const note = (tone, text) => `<div class="note-line" data-tone="${tone}">${icon(tone === 'red' ? 'x' : tone === 'green' ? 'check' : 'branch')}<span>${text}</span></div>`;
    const kept = outcome ? ' The saved outcome remains viewable.' : '';
    let body = '';
    if (!entry.error) {
      if (running && !staticPage) body = '<p class="small muted evidence-wait">Running the provider; the project checker then checks what it proposes…</p>';
      else if (!outcome && !failure && !entry.run_error) body = '<p class="small muted evidence-wait">Not run yet.</p>';
    }
    if (outcome && (!running || staticPage)) body = decisionOutcomeHTML(entry, note);
    if (staticPage && running) body = '<p class="small muted">A run was in progress at export time. Any outcome below is the previous saved one; its currency is not established here.</p>' + body;
    if (failure) {
      const day = /^\d{4}-\d{2}-\d{2}/.test(failure.created || '') ? ` on ${esc(failure.created.slice(0, 10))}` : '';
      body = (failure.status === 'stale'
        ? note('amber', `<span class="lean-message">An earlier run, on different source or environment, failed: ${esc(failure.message)}</span>`)
        : note('red', `<span class="lean-message">${failure.earlier ? 'An earlier run' : 'The last run'} failed${day}: ${esc(failure.message)}</span>`)) + body;
    }
    // A saved failure alone that is out of date says so in its own note.
    if (entry.status === 'stale' && outcome) body = note('amber', recorded() ? 'Out of date: the saved source through this directive changed since this run.' : `Out of date: the source through this directive or its environment changed since this run.${leanOff() ? '' : ' Run it again to update it.'}`) + body;
    if (entry.missing_imports?.length) body = note('amber', `Add ${entry.missing_imports.map(m => `<code>import ${esc(m)}</code>`).join(', ')} to the file's header before accepting.`) + body;
    if (entry.run_error) body = note('red', `The last run failed: ${esc(entry.run_error)}`) + body;
    if (entry.unavailable) body = note('amber', esc(entry.unavailable) + kept) + body;
    if (entry.error) body = note('red', esc(entry.error) + kept) + body;
    const goal = entry.goal ? `<pre class="decision-goal">${esc(entry.goal)}</pre>` : '';
    return `<section data-document-start="${line}" data-document-end="${end}" class="cell box decision">${head}${goal}${body}${staticPage || linkedPath() || recorded() ? '' : decisionActionsHTML(entry)}</section>`;
  }

  // The saved outcome as text: the provider's reason and stages, the checker's
  // verdict, the kernel's statement of the theorem (with instances explicit,
  // so notation cannot disguise it) and the whole block, readable before Accept.
  function decisionOutcomeHTML(entry, note) {
    const {outcome, verification} = entry;
    const reason = outcome.reason ? `: ${esc(outcome.reason)}` : '';
    let html;
    if (outcome.status === 'unknown') html = note('amber', `Not decided${reason}. This is not a refutation; the goal may still be true or false.`);
    else {
      const what = outcome.status === 'refuted' ? 'a refutation' : 'a proof';
      html = `<p class="decision-claim">The provider proposed ${what}, theorem <code>${esc(outcome.theorem)}</code>${reason}.</p>`;
      if (verification?.status === 'rejected') html += note('red', `The kernel check rejected it: ${esc(verification.message)}`);
      else if (verification?.status === 'checked') {
        if (entry.status === 'accepted') html += note('green', 'Accepted: this block follows the directive in the file, where the project check covers it.');
        else if (entry.status === 'candidate') html += note('muted', `The kernel checked <code>${esc(outcome.theorem)}</code> after the saved source through this directive. Nothing is written until it is accepted.`);
        if (verification.type) html += `<div class="decision-field"><div class="field-label">Kernel statement</div><pre class="decision-type">${esc(verification.type)}</pre></div>`;
        html += `<p class="small muted decision-axioms">Assumptions: ${verification.axioms?.length ? verification.axioms.map(a => `<code>${esc(a)}</code>`).join(', ') : 'none'}</p>`;
      }
    }
    if (outcome.stages?.length) html += `<table class="decision-stages"><thead><tr><th scope="col">Stage</th><th scope="col">Outcome</th><th scope="col">Detail</th></tr></thead><tbody>${outcome.stages.map(row => `<tr>${row.map(cell => `<td>${esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    if (outcome.block) html += `<div class="decision-field"><div class="field-label">Lean block${entry.status === 'accepted' ? '' : ' to insert after the directive'}</div><pre class="decision-block">${esc(outcome.block)}</pre></div>`;
    return html;
  }

  // Run and Discard name the saved text they were drawn for; the server
  // refuses them if it has changed since. Accept is handled by the editor.
  function decisionActionsHTML(entry) {
    const line = entry.line, sha = esc(UI.data.sha256);
    const served = Boolean(entry.provider) && !entry.error;
    const buttons = [];
    if (entry.status === 'running') buttons.push('<button type="button" class="secondary" data-act="decision-stop">Stop</button>');
    else {
      const acceptable = served && entry.status === 'candidate' && entry.verification?.status === 'checked' && !entry.missing_imports?.length;
      if (acceptable && HOST && !leanOff()) buttons.push(`<button type="button" class="primary" data-act="accept" data-key="${esc(entry.key)}">${icon('check')}Accept</button>`);
      if (served && !entry.unavailable && !leanOff() && RUN_AGAIN.has(entry.status)) buttons.push(`<button type="button" class="${acceptable || entry.status === 'accepted' ? 'secondary' : 'primary'}" data-act="decision-run" data-line="${line}" data-sha="${sha}">${icon('replay')}${entry.outcome || entry.failure || entry.status === 'failed' || entry.status === 'stale' ? 'Run again' : 'Run'}</button>`);
      if (entry.outcome) buttons.push(`<button type="button" class="secondary" data-act="decision-discard" data-line="${line}" data-sha="${sha}">Discard</button>`);
      if (acceptable && !HOST && !leanOff()) buttons.push('<span class="small muted">Accept it from the editor\'s preview.</span>');
    }
    return buttons.length ? `<div class="claim-actions">${buttons.join('')}<span class="small muted">A provider proposes; only the kernel check makes a candidate.</span></div>` : '';
  }

  // Decision and evidence cards listed for this project source cell, in line order.
  function projectCardsHTML(cell, staticPage = false, placed = new Set()) {
    const decisions = (UI.data.decisions || []).filter(d => d.cell === cell.index && !needsDecider(d) && !placed.has(d.line)).map(d => [d.line, decisionHTML(d, staticPage)]);
    const evidence = projectEvidence(cell).filter(e => !needsProvider(e) && !placed.has(e.line)).map(e => [e.line, evidenceHTML(e, staticPage)]);
    return [...decisions, ...evidence].sort((a, b) => a[0] - b[0]).map(([, html]) => html).join('');
  }

  // Up to 64,000 samples: too many to spread into Math.min's arguments.
  const extent = xs => xs.reduce(([lo, hi], v) => [Math.min(lo, v), Math.max(hi, v)], [Infinity, -Infinity]);
  const fmtAxis = v => Math.abs(v) >= 100 || (Math.abs(v) < 0.01 && v !== 0) ? v.toExponential(2) : v.toFixed(2);
  const CHART_COLORS = ['var(--violet)', 'var(--blue)', 'var(--green)', 'var(--amber)'];
  // A path through the finite points, broken where a value is missing. A
  // point with no finite neighbour gets a dot, which a path would not show.
  function traceHTML(points, attributes, color) {
    const segments = [[]];
    for (const [x, y] of points) {
      if (Number.isFinite(x) && Number.isFinite(y)) segments[segments.length - 1].push([x, y]);
      else if (segments[segments.length - 1].length) segments.push([]);
    }
    const d = segments.filter(run => run.length > 1).map(run => run.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('')).join('');
    const dots = segments.filter(run => run.length === 1).map(([[x, y]]) => `<circle class="dot-sample" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.5" fill="${color}"/>`).join('');
    return `<path d="${d}" ${attributes}/>${dots}`;
  }

  // Every series against time, one row of values per time.
  function chartHTML(series, dim) {
    const W = 640, H = 170, x0 = 50, x1 = W - 14, y1 = H - 30;
    const {times, values, names} = series;
    const legendRows = Math.ceil(names.length / 6);
    const y0 = 12 + 13 * Math.max(0, legendRows - 1);
    const all = values.flat().filter(Number.isFinite);
    if (!times.length || !all.length) return '<p class="small muted evidence-wait">The run produced no finite values.</p>';
    let [lo, hi] = extent(all);
    if (lo === hi) { lo -= 1; hi += 1; }
    const t0 = times[0], tEnd = times[times.length - 1];
    const t1 = tEnd === t0 ? t0 + 1 : tEnd;
    const X = t => x0 + (x1 - x0) * (t - t0) / (t1 - t0);
    const Y = v => v === null ? NaN : y1 - (y1 - y0) * (v - lo) / (hi - lo);
    const lines = names.map((_, k) => traceHTML(times.map((t, j) => [X(t), Y(values[j][k])]), `fill="none" stroke="${CHART_COLORS[k % CHART_COLORS.length]}" stroke-width="2"`, CHART_COLORS[k % CHART_COLORS.length])).join('');
    const legend = names.map((name, k) => `<text x="${x0 + 8 + (k % 6) * 95}" y="${12 + 13 * Math.floor(k / 6)}" fill="${CHART_COLORS[k % CHART_COLORS.length]}" font-size="11">${esc(name)}</text>`).join('');
    return `<div class="pane${dim ? ' dim' : ''}"><svg class="draw" viewBox="0 0 ${W} ${H}" role="img" aria-label="Evidence series over time"><g stroke="var(--border)" fill="none"><path d="M${x0} ${y0}V${y1}H${x1}"/></g>${lines}<g fill="var(--muted)" font-size="11" font-family="var(--sans)"><text x="${x0 - 6}" y="${y0 + 4}" text-anchor="end">${fmtAxis(hi)}</text><text x="${x0 - 6}" y="${y1 + 4}" text-anchor="end">${fmtAxis(lo)}</text><text x="${x0}" y="${H - 12}">${fmtAxis(t0)}</text><text x="${x1}" y="${H - 12}" text-anchor="end">${fmtAxis(t1)}</text></g>${legend}</svg></div>`;
  }

  // Two series against each other: the run's shape, not its timing.
  function phaseHTML(series, plane, dim) {
    const {values, names} = series;
    const [a, b] = plane;
    const note = text => `<div class="note-line" data-tone="amber">${icon('branch')}<span>${text}</span></div>`;
    if (![a, b].every(i => Number.isInteger(i) && i >= 0 && i < names.length)) return note('The phase plane names a series this evidence does not have.');
    const S = 300, m = 34, lo = 8;
    const xs = values.map(row => row[a]).filter(Number.isFinite), ys = values.map(row => row[b]).filter(Number.isFinite);
    if (!xs.length || !ys.length) return note(`The phase plane of ${esc(names[a])} against ${esc(names[b])} has no finite values to draw.`);
    let [xa, xb] = extent(xs), [ya, yb] = extent(ys);
    if (xa === xb) { xa -= 1; xb += 1; }
    if (ya === yb) { ya -= 1; yb += 1; }
    const X = v => v === null ? NaN : m + (S - m - lo) * (v - xa) / (xb - xa);
    const Y = v => v === null ? NaN : S - m - (S - m - lo) * (v - ya) / (yb - ya);
    const fmt = v => Math.abs(v) >= 100 || (Math.abs(v) < 0.01 && v !== 0) ? v.toExponential(1) : v.toFixed(1);
    const first = values.find(row => Number.isFinite(row[a]) && Number.isFinite(row[b]));
    const start = first ? `<circle cx="${X(first[a]).toFixed(1)}" cy="${Y(first[b]).toFixed(1)}" r="3" fill="var(--amber)"/>` : '';
    return `<div class="pane phase${dim ? ' dim' : ''}"><svg class="draw" viewBox="0 0 ${S} ${S}" role="img" aria-label="Phase plane, ${esc(names[a])} against ${esc(names[b])}"><g stroke="var(--border)" fill="none"><path d="M${m} ${lo}V${S - m}H${S - lo}"/></g>${traceHTML(values.map(row => [X(row[a]), Y(row[b])]), 'class="orbit" fill="none" stroke="var(--violet)" stroke-width="1"', 'var(--violet)')}${start}<g fill="var(--muted)" font-size="10" font-family="var(--sans)"><text x="${m}" y="${S - m + 12}">${fmt(xa)}</text><text x="${S - lo}" y="${S - m + 12}" text-anchor="end">${fmt(xb)} ${esc(names[a])}</text><text x="${m - 4}" y="${S - m}" text-anchor="end">${fmt(ya)}</text><text x="${m - 4}" y="${lo + 8}" text-anchor="end">${fmt(yb)} ${esc(names[b])}</text></g></svg></div>`;
  }

  // Provenance is the provider's own [label, value] pairs, shown as text.
  function provenanceHTML(pairs) {
    if (!pairs.length) return '';
    return `<table class="evidence-provenance"><tbody>${pairs.map(([label, value]) => `<tr><th scope="row">${esc(label)}</th><td>${esc(value)}</td></tr>`).join('')}</tbody></table>`;
  }

  // The button names the saved text it was drawn for; the server refuses a
  // run if that text has changed since.
  async function runEvidence(line, sha256) {
    if (leanOff()) return;
    await post('/api/evidence/run', {sha256, line});
    refresh();
  }
  async function decisionAction(path, body) {
    if (leanOff() && path === '/api/decide/run') return;
    await post(path, body);
    refresh();
  }

  // ----- images ------------------------------------------------------------------
  // A prose image comes from the notebook's assets/ folder, fetched through
  // the host at the hash the snapshot announced, and is shown only as a data
  // URL in an <img>, where an SVG cannot run scripts. Images illustrate; they
  // never decide anything.
  const IMAGE_DATA = /^data:image\/(png|jpeg|gif|webp|svg\+xml);base64,/;
  const images = new Map();
  const assetPath = asset => `/asset?path=${encodeURIComponent(asset.path)}&sha256=${encodeURIComponent(asset.sha256)}`;

  async function imageData(path) {
    let url;
    if (HOST) url = await HOST.asset(path);
    else {
      const response = await fetch(path, {cache: 'no-store', headers: API});
      if (!response.ok) {
        const answer = await response.json().catch(() => ({}));
        throw Object.assign(new Error(answer.error || `the server answered ${response.status}`), {status: response.status});
      }
      const blob = await response.blob();
      url = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('the image could not be read'));
        reader.readAsDataURL(blob);
      });
    }
    if (typeof url !== 'string' || !IMAGE_DATA.test(url)) throw Object.assign(new Error('the server did not send a supported image'), {status: 415});
    return url;
  }
  // Each image is asked for once per hash; the promise lets export wait for it.
  function loadImage(path) {
    if (images.has(path)) return images.get(path).promise;
    const entry = {loading: true};
    entry.promise = imageData(path).then(url => ({url}), error => ({error: error.message || String(error), transient: !error.status || error.status >= 500}))
      .then(result => {
        if (images.get(path) === entry) { images.set(path, {...result, promise: entry.promise}); render(); }
      });
    images.set(path, entry);
    return entry.promise;
  }
  // Forget images the snapshot no longer shows, and failures that may pass.
  function forgetImages(data) {
    const shown = new Set((data.assets || []).filter(a => a.status === 'ok' && a.sha256).map(assetPath));
    for (const [path, image] of images) if (!shown.has(path) || image.transient) images.delete(path);
  }

  function figureHTML(src, alt) {
    const note = (tone, text) => `<div class="note-line" data-tone="${tone}">${icon(tone === 'red' ? 'x' : 'branch')}<span>${text}</span></div>`;
    if (/^[a-z][a-z0-9+.-]*:/i.test(src)) return note('amber', 'External images are not loaded. Put the file in <code>assets/</code> and refer to it as <code>assets/…</code>.');
    if (linkedPath()) return note('muted', `The image <code>${esc(src)}</code> is shown in the notebook's own preview.`);
    const asset = (UI.data.assets || []).find(a => a.path === src);
    if (!asset) return note('red', `No image information for <code>${esc(src)}</code>. Save the file to refresh the preview.`);
    if (asset.status !== 'ok' || !asset.sha256) return note('red', `${esc(asset.message || `${src} cannot be shown`)}.`);
    const caption = alt ? `<figcaption>${esc(alt)}</figcaption>` : '';
    const path = assetPath(asset);
    if (!images.has(path)) loadImage(path);
    const image = images.get(path);
    if (image.error) return note('red', `<code>${esc(src)}</code> could not be shown: ${esc(image.error)}.`);
    if (!image.url) return `<figure class="figure image-wait"><p class="small muted">Loading image <code>${esc(src)}</code>…</p>${caption}</figure>`;
    return `<figure class="figure"><img alt="${esc(alt || src)}" src="${esc(image.url)}">${caption}</figure>`;
  }

  // Without a provider the snapshot carries no decision or evidence data:
  // the directive stays visible as its own source, with a neutral note.
  function unavailableDirectiveHTML(kind, entries) {
    const start = entries[0][0], end = entries[entries.length - 1][0];
    return `<section data-document-start="${start}" data-document-end="${end}" class="cell directive-unavailable"><pre>${esc(leanOf(entries))}</pre><div class="note-line" data-tone="muted">${icon('branch')}<span>${lineLinkHTML(start, end)} <code>@${kind}</code> needs a provider and is not available yet.</span></div></section>`;
  }
  // Notes for directives in a project source cell, whose text is shown as is.
  // They are the directives the server listed without a provider: evidence
  // whose verb no installed provider runs and that has no saved evidence, and
  // decisions whose goal kind no installed provider decides and that have no
  // saved outcome.
  function sourceDirectivesHTML(cell, placed = new Set()) {
    return directiveNotes(cell).filter(([line]) => !placed.has(line)).map(([, html]) => html).join('');
  }
  function directiveNotes(cell) {
    const note = (line, end, text) => [line, `<div class="note-line" data-tone="muted" data-document-start="${line}" data-document-end="${end}">${icon('branch')}<span>${lineLinkHTML(line, end)} ${text}</span></div>`];
    const unserved = kind => `<code>@${kind}</code> needs a provider and is not available yet.`;
    // The server says why: no provider serves it, one failed to load, or the
    // directive itself is in error.
    const decide = (UI.data.decisions || []).filter(d => d.cell === cell.index && needsDecider(d))
      .map(d => note(d.line, d.end_line, d.error ? esc(d.error) : unserved('decide')));
    const evidence = projectEvidence(cell).filter(needsProvider).map(e => note(e.line, e.line, e.error ? esc(e.error) : unserved('evidence')));
    return [...decide, ...evidence].sort((a, b) => a[0] - b[0]);
  }

  function leanCellHTML(cell, scopes = new Map(), staticPage = false) {
    const lines = cell.text.split('\n');
    // A @decide directive continues over the comment lines after it.
    const directive = [];
    lines.forEach((l, i) => {
      const t = l.trim();
      directive[i] = /^--\s*@decide\b/.test(t) || (i > 0 && directive[i - 1] && /^--\s{2,}(?!@)/.test(t));
    });
    // Code and directives are shown in file order.
    const parts = [];
    let segment = [];
    const flush = () => {
      if (leanOf(trimmed(segment))) parts.push(textbookHTML(segment, scopes));
      segment = [];
    };
    lines.forEach((l, offset) => {
      const line = cell.start + offset;
      if (/^--\s*@decide\b/.test(l.trim())) {
        let end = offset;
        while (directive[end + 1] && !/^--\s*@decide\b/.test(lines[end + 1].trim())) end++;
        flush();
        const served = (UI.data.decisions || []).find(d => d.cell === cell.index && d.line === line && !needsDecider(d));
        parts.push(served ? decisionHTML(served, staticPage) : unavailableDirectiveHTML('decide', lines.slice(offset, end + 1).map((text, i) => [line + i, text])));
        return;
      }
      if (directive[offset]) return;
      // Evidence is listed only for project source cells.
      if (/^--\s*@evidence\b/.test(l.trim())) {
        flush();
        const served = projectEvidence(cell).find(e => e.line === line && !needsProvider(e));
        parts.push(served ? evidenceHTML(served, staticPage) : unavailableDirectiveHTML('evidence', [[line, l]]));
        return;
      }
      if (!/^--\s*@view\b/.test(l.trim())) { segment.push([line, l]); return; }
      if (!/^--\s*@view\s+/.test(l.trim())) return;
      flush();
      parts.push(viewHTML(cell, line));
    });
    flush();
    return parts.join('');
  }

  Object.assign(workbench, {assetPath, cancelCheck, cancelInspection, cancelView, closeDiagram, decisionAction,
    decisionHTML, diagramAction, directiveNotes, evidenceHTML, forgetImages, images, inlineHTML, leanCellHTML,
    loadImage, markdown, needsDecider, needsProvider, projectCardsHTML, projectEvidence, renderDiagramDialog,
    restoreDiagramUI, runEvidence, saveDiagramUI, sourceDirectivesHTML, title, typesetMath, viewHTML, when});
})();
