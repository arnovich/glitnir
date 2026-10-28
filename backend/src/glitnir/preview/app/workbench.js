// The Glitnir notebook workbench: renders what the local server reports.
// Every status shown here comes from the server, which derives it from
// sandboxed Lean checks; this page decides nothing itself.
// This file holds the shared state, the server, linked notebooks and the helpers.
// The workbench files share names through `window.glitnirWorkbench`: the workbench's
// own namespace, not an interface; it may change with any release.
(() => {
  'use strict';
  window.glitnirWorkbench = {};
  const workbench = window.glitnirWorkbench;
  // Defined by a later file and bound when it loads: nothing may call one while the page is still loading.
  const cellEntries = (...a) => workbench.cellEntries(...a);
  const closeDiagram = (...a) => workbench.closeDiagram(...a);
  const commands = (...a) => workbench.commands(...a);
  const declarationParts = (...a) => workbench.declarationParts(...a);
  const docHTML = (...a) => workbench.docHTML(...a);
  const fieldHTML = (...a) => workbench.fieldHTML(...a);
  const forgetImages = (...a) => workbench.forgetImages(...a);
  const leanOf = (...a) => workbench.leanOf(...a);
  const lineLinkHTML = (...a) => workbench.lineLinkHTML(...a);
  const namespaceHTML = (...a) => workbench.namespaceHTML(...a);
  const plainComments = (...a) => workbench.plainComments(...a);
  const render = (...a) => workbench.render(...a);
  const renderChrome = (...a) => workbench.renderChrome(...a);
  const renderConnection = (...a) => workbench.renderConnection(...a);
  const revealSection = (...a) => workbench.revealSection(...a);
  const sourceFoldHTML = (...a) => workbench.sourceFoldHTML(...a);
  const title = (...a) => workbench.title(...a);
  const trimmed = (...a) => workbench.trimmed(...a);
  const unevenQuotes = (...a) => workbench.unevenQuotes(...a);
  // VS Code uses the same renderer with a restricted, host-owned transport.
  const HOST = window.glitnirHost;
  const fetch = window.glitnirHost ? window.glitnirHost.fetch : window.fetch.bind(window);
  const $ = id => document.getElementById(id);
  const esc = t => String(t).replace(/[&<>"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[ch]);
  const icon = n => `<svg class="ic"><use href="#i-${n}"/></svg>`;
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  const TONE = {checked: 'green', refuted: 'red', conditional: 'amber', stale: 'amber', unknown: 'amber', failed: 'red', open: 'blue'};
  const LABEL = {checked: 'Checked', refuted: 'Refuted', conditional: 'Conditional', stale: 'Out of date', unknown: 'Unknown', failed: 'Failed', open: 'Open goal'};

  const UI = {drawings: {}, diagrams: {}, expandedDiagram: null, data: null, selected: null, mode: 'doc', panel: true, connected: true, notice: '', sourceCell: null, sourceRange: null, documentLine: null,
    // Notebooks read in this panel: null is the previewed notebook, a string
    // the link to another notebook of its project. `at` is the one shown.
    trail: [null], at: 0, main: null, goals: null};
  const readingFolds = new Map();
  // Every API call carries this header; a cross-site page cannot send it.
  // In project mode a prose link to another notebook of the project is read
  // in this panel, read-only, from /api/linked; elsewhere /?reference=Module
  // asks the host to open that sibling in its own preview.
  const LINKED = new URLSearchParams(location.search).get('notebook');
  const REFERENCE = new URLSearchParams(location.search).get('reference');
  const API = {'X-Glitnir': '1'};

  // A use of an imported claim (Module:claim) links to its notebook and shows
  // the status its own notebook's records give it.
  function importedClaim(use) {
    for (const link of (UI.data && UI.data.links) || []) {
      const found = link.claims.find(c => c.use === use);
      if (found) return {...found, module: link.module, file: link.file};
    }
    return null;
  }
  function useHTML(u) {
    if (!u.includes(':')) return `<button type="button" data-act="goto" data-id="${esc(u)}"><span class="dot" data-tone="${TONE[(claim(u) || {}).status] || 'blue'}"></span> ${esc(u)}</button>`;
    const found = importedClaim(u);
    const module = u.slice(0, u.indexOf(':'));
    const title = found ? `${found.file}: ${LABEL[found.status] || found.status}${found.reason ? ' · ' + found.reason : ''}` : 'no longer in the imported notebook';
    return `<a class="use-link" href="/?${REFERENCE ? 'reference' : 'notebook'}=${encodeURIComponent(module)}" target="_blank" rel="noopener" title="${esc(title)}"><span class="dot" data-tone="${TONE[(found || {}).status] || 'amber'}"></span> ${esc(u)}</a>`;
  }

  // ----- server ---------------------------------------------------------------
  let loadTimer, inFlight, reloadPending = false, sourceEpoch = 0, opening = false;
  let environmentEpoch = 0, mainChanged = false;
  const linkedPath = () => UI.trail[UI.at];
  // A snapshot from saved results (a static export) compares each result
  // with its saved source only, never with an environment: its results are
  // shown as recorded, never called current, and nothing checks or runs them.
  const recorded = () => UI.data?.preview?.recorded === true;
  // Glitnir checks one Lean version; a project on another reads as usual but
  // runs no Lean (the server refuses every such request with the same words).
  // Saved results shown as recorded run nothing and say nothing about it.
  const leanOff = () => UI.data?.preview?.lean?.supported === false && !recorded();
  const leanVersion = toolchain => String(toolchain || '').replace(/^leanprover\/lean4:/, '').replace(/^v(?=\d)/, '');
  const upToDate = () => (linkedPath() && mainChanged ? `${UI.main?.file || 'The notebook'} changed on disk · go back to read the saved edits` : 'Saved file is up to date');
  let readableSnapshot, heldRendering = false, highlightTimer;
  let highlighted = new Set(), highlightUntil = 0;

  function liveMessage(text) {
    if ($('live-status').textContent !== text) $('live-status').textContent = text;
  }
  function scheduleLoad(delay) {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(load, delay);
  }
  document.addEventListener('glitnir-source-changed', () => {
    // A linked notebook stays on screen; its own polls keep it current.
    if (linkedPath()) { mainChanged = true; liveMessage(upToDate()); return; }
    sourceEpoch++;
    reloadPending = true;
    liveMessage('Saved file changing…');
    scheduleLoad(250);
  });

  document.addEventListener('glitnir-environment-changed', () => {
    // Recorded proof goals describe the dependencies as they were.
    environmentEpoch++;
    if (UI.goals) render();
    if (UI.data?.preview?.mode === 'project') scheduleLoad(500);
  });

  function readingAnchor() {
    if (!readableSnapshot || UI.mode !== 'doc') return null;
    const top = Math.max(0, document.querySelector('.live-bar').getBoundingClientRect().bottom);
    const wrap = [...$('doc').querySelectorAll('.cellwrap')].find(el => el.getBoundingClientRect().bottom > top);
    if (!wrap) return null;
    const cell = readableSnapshot.cells.find(c => c.index === Number(wrap.dataset.index));
    const occurrence = readableSnapshot.cells.filter(c => c.index < cell.index && c.kind === cell.kind && c.text === cell.text).length;
    return {cell, occurrence, fragment: wrap.dataset.fragment, top: wrap.getBoundingClientRect().top};
  }
  function restoreAnchor(anchor) {
    if (!anchor?.cell || UI.mode !== 'doc') return;
    const old = anchor.cell;
    const cell = (old.kind === 'claim' && UI.data.cells.find(c => c.kind === 'claim' && c.id === old.id))
      || UI.data.cells.filter(c => c.kind === old.kind && c.text === old.text)[anchor.occurrence]
      || UI.data.cells.find(c => c.index === old.index);
    const wraps = cell ? [...$('doc').querySelectorAll(`.cellwrap[data-index="${cell.index}"]`)] : [];
    const target = wraps.find(wrap => wrap.dataset.fragment === anchor.fragment) || wraps[0];
    if (target) {
      const delta = target.getBoundingClientRect().top - anchor.top;
      if (delta) window.scrollBy(0, delta);
    }
  }
  function changedCells(before, after) {
    const remaining = new Map();
    const key = c => JSON.stringify([c.kind, c.text]);
    for (const cell of before.cells) remaining.set(key(cell), (remaining.get(key(cell)) || 0) + 1);
    const changed = [];
    for (const cell of after.cells) {
      const count = remaining.get(key(cell)) || 0;
      if (count) remaining.set(key(cell), count - 1); else changed.push(cell.index);
    }
    // Pure deletions have no new text: indicate the nearest surviving section.
    if (!changed.length && before.cells.length > after.cells.length && after.cells.length) {
      let i = 0;
      while (i < after.cells.length && key(before.cells[i]) === key(after.cells[i])) i++;
      changed.push(after.cells[Math.min(i, after.cells.length - 1)].index);
    }
    return new Set(changed);
  }
  function markLiveChanges() {
    for (const wrap of $('doc').querySelectorAll('.cellwrap')) {
      wrap.classList.toggle('live-changed', !heldRendering && Date.now() < highlightUntil && highlighted.has(Number(wrap.dataset.index)));
    }
  }
  function canFollow() {
    return $('live-follow').checked && UI.mode === 'doc' && !document.body.classList.contains('glitnir-proof-mode')
      && !document.querySelector('.overlay:not([hidden]), #history[open]');
  }
  function holdRendering(message) {
    heldRendering = !!readableSnapshot;
    liveMessage(message);
    for (const id of ['doc', 'source', 'insp']) {
      $(id).inert = heldRendering;
      $(id).classList.toggle('live-held', heldRendering);
    }
    markLiveChanges();
  }
  // A transient view failure (busy checker, cancellation, timeout) is asked
  // again on the next snapshot; any failure is asked again once a check
  // starts or finishes, because the environment it saw may have changed.
  // A drawing still being polled for another saved source is forgotten, so
  // returning to that source asks for it again.
  function forgetFailedDrawings(all, sha256) {
    for (const [key, drawing] of Object.entries(UI.drawings)) {
      if (drawing.error && (all || drawing.transient)) delete UI.drawings[key];
      else if (drawing.loading && sha256 && !key.endsWith(sha256)) delete UI.drawings[key];
    }
  }

  function acceptSnapshot(data) {
    const anchor = readingAnchor();
    const changed = readableSnapshot && readableSnapshot.sha256 !== data.sha256;
    forgetFailedDrawings(Boolean(UI.data) && UI.data.preview?.check?.status !== data.preview?.check?.status, data.sha256);
    forgetImages(data);
    UI.data = data;
    UI.connected = true;
    // Only the previewed notebook's snapshots reach the editor integration.
    if (!linkedPath()) { UI.main = data; document.dispatchEvent(new CustomEvent('glitnir-snapshot', {detail: data})); }
    if (data.error) {
      holdRendering('File is not readable yet · showing the previous rendering');
      render();
      return;
    }
    if (changed) {
      highlighted = changedCells(readableSnapshot, data);
      highlightUntil = Date.now() + 2400;
      clearTimeout(highlightTimer);
      highlightTimer = setTimeout(markLiveChanges, 2450);
      liveMessage('Saved edits received · checking is manual');
    } else if (opening) liveMessage(linkedPath() && !mainChanged ? `Reading ${data.file} read-only · its saved results only` : upToDate());
    else if (heldRendering) liveMessage('Saved source is readable again');
    else if (readableSnapshot) liveMessage(upToDate());
    opening = false;
    heldRendering = false;
    for (const id of ['doc', 'source', 'insp']) { $(id).inert = false; $(id).classList.remove('live-held'); }
    readableSnapshot = data;
    snapshots.set(linkedPath() ?? '', data);
    if (!UI.selected || !claim(UI.selected)) UI.selected = claims().length ? claims()[0].name : null;
    const pending = takePendingClaim();
    render();
    if (pending) scrollToClaim(pending);
    else if (changed && canFollow() && highlighted.size) {
      const target = $('doc').querySelector('.live-changed');
      revealSection(target);
      target?.scrollIntoView({block: 'nearest', behavior: 'auto'});
    } else restoreAnchor(anchor);
  }
  function load() {
    clearTimeout(loadTimer);
    if (inFlight) { reloadPending = true; return inFlight; }
    reloadPending = false;
    const epoch = sourceEpoch;
    const link = linkedPath();
    inFlight = (async () => {
      try {
        const response = await fetch(link ? `/api/linked?path=${encodeURIComponent(link)}` : '/api/notebook', {cache: 'no-store', headers: API});
        if (!response.ok) {
          const answer = await response.json().catch(() => ({}));
          throw Object.assign(new Error(answer.error || `server said ${response.status}`), {refused: response.status >= 400 && response.status < 500});
        }
        const data = await response.json();
        // A newer write was observed while reading, or another notebook was
        // opened: never flash the old reply.
        if (epoch === sourceEpoch) acceptSnapshot(data);
      } catch (error) {
        if (epoch === sourceEpoch && link && (opening || error.refused)) {
          // The linked notebook cannot be read: say why and return.
          leaveLinked(`${link} could not be opened here: ${error.message}`);
        } else if (epoch === sourceEpoch) {
          UI.connected = false;
          if (!link) document.dispatchEvent(new CustomEvent('glitnir-snapshot', {detail: undefined}));
          holdRendering(`Cannot read ${link || 'saved file'} · showing the last readable version; retrying`);
          renderConnection();
        }
      } finally {
        inFlight = null;
        const busy = UI.data && (UI.data.checking.length || UI.data.preview?.check?.status === 'running' || UI.data.preview?.inspection?.status === 'running' || [...(UI.data.decisions || []), ...(UI.data.evidence || [])].some(e => e.status === 'running'));
        scheduleLoad(reloadPending ? 250 : busy ? 1000 : 4000);
      }
    })();
    return inFlight;
  }

  async function post(path, body) {
    let response;
    try {
      response = await fetch(path, {method: 'POST', headers: {...API, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    } catch (error) {
      UI.notice = 'The server did not answer.';
      render();
      return null;
    }
    const answer = await response.json().catch(() => ({}));
    UI.notice = response.ok ? '' : `Refused: ${answer.error || response.status}.`;
    return response.ok ? answer : null;
  }
  // The server inspects in the background, as it checks, and reports the last
  // inspection in each snapshot; it is shown for the saved source it read.
  function inspection() {
    const found = UI.data?.preview?.inspection;
    return found && (found.status === 'running' || found.source_sha256 === UI.data.sha256) ? found : null;
  }
  const inspecting = () => Boolean(UI.inspecting || inspection()?.status === 'running');
  async function inspectProject() {
    if (inspecting() || UI.data?.preview?.mode !== 'project' || linkedPath() || recorded() || leanOff()) return;
    UI.inspecting = true;
    const sha256 = UI.data.sha256;
    renderChrome();
    try {
      await post('/api/project/inspect', {sha256});
      await refresh();
    } finally {
      UI.inspecting = false;
      renderChrome();
    }
  }
  // A check of the whole saved file, or, with `throughLine`, of its lines 1
  // through that line only: the end of a chosen claim.
  async function checkProject(throughLine = null) {
    if (UI.checkRequested || UI.data?.preview?.mode !== 'project' || UI.data.preview.check?.status === 'running' || linkedPath() || recorded() || leanOff()) return;
    UI.checkRequested = true;
    render();
    const body = {sha256: UI.data.sha256};
    // A line with nothing after it asks for the whole file.
    const lastLine = Math.max(0, ...UI.data.cells.map(cell => cell.end));
    if (Number.isSafeInteger(throughLine) && throughLine > 0 && throughLine < lastLine) body.through_line = throughLine;
    try { await post('/api/project/check', body); await refresh(); }
    finally { UI.checkRequested = false; render(); }
  }
  const checkBusy = () => Boolean(inspecting() || UI.checkRequested || UI.data?.preview?.check?.status === 'running');
  // A check through a claim compiles and kernel-replays the saved file's lines
  // 1 through the claim's last line; later claims are not checked by it.
  function checkThroughHTML(saved, label, note) {
    if (linkedPath() || recorded() || leanOff() || !projectMode()) return '';
    return `<button type="button" class="secondary" data-act="check-through" data-id="${esc(saved.name)}" data-line="${esc(saved.end_line)}" data-sha="${esc(UI.data.sha256)}"${checkBusy() ? ' disabled' : ''} title="Compile and kernel-replay lines 1–${esc(saved.end_line)} of the saved file">${icon('replay')}${label}</button><span class="small muted">${note}</span>`;
  }
  // Where a check stopped because it was asked to, not at a failing command.
  const requestedCut = w => Number.isSafeInteger(w?.through_line) && w.from_line === w.through_line + 1;
  const coveredText = (w, stale) => `${stale ? 'The last check, of an earlier version of this file, covered' : 'The last check covered'} lines 1–${esc(w.through_line)} only, on request. Claims after line ${esc(w.through_line)} were not rechecked: each keeps its earlier saved result, if it has one, marked Not rechecked.`;
  function projectResolutionHTML(resolution) {
    if (!resolution || resolution.kind !== 'prepared_local') return '';
    return '<details><summary>Prepared local dependencies</summary>'
      + '<p>These are the local contents used for this observation. Git revision and worktree cleanliness were not verified.</p><ul>'
      + resolution.packages.map(p => `<li><code>${esc(p.name)}</code>: ${esc(p.path)}${p.entry?.rev ? ` · declared revision ${esc(p.entry.rev)} (not verified)` : ''}</li>`).join('') + '</ul></details>';
  }
  const PROJECT_STATUS = {
    checked: ['green', 'Kernel checked'],
    admitted: ['amber', 'Contains sorry'],
    assumptions: ['amber', 'Additional assumptions'],
  };
  // An example is checked under a name of the checker's, never shown.
  const projectClaimLabel = claim => claim.example === true ? `example (line ${claim.start_line})` : claim.name;
  function withheldText(withheld, lead) {
    return `${lead} at line ${esc(withheld.from_line)}: ${esc(withheld.message)}. Claims from there on were not checked.`;
  }
  function withheldBannerHTML(data) {
    const withheld = data.project_withheld;
    if (!withheld) return '';
    if (requestedCut(withheld)) return `<p class="banner quiet project-covered" role="status">${coveredText(withheld, withheld.stale)}</p>`;
    const lead = withheld.stale ? 'In the last check, checking stopped' : 'Checking stopped';
    const asked = Number.isSafeInteger(withheld.through_line) ? ` It was asked to check lines 1–${esc(withheld.through_line)}.` : '';
    return `<p class="banner" role="status">${withheldText(withheld, lead)}${asked}</p>`;
  }
  // The declaration a claim cell holds, when the line reader can find it
  // safely: its docstring becomes the card's prose and its statement a field.
  function claimReading(entries) {
    if (!plainComments(entries) || entries.some(([, line]) => unevenQuotes(line))) return null;
    const declarations = commands(entries).filter(command => command.body.length);
    if (declarations.length !== 1 || !DECLARATION.test(declarations[0].body[0][1])) return null;
    const command = declarations[0];
    return {command, doc: leanOf(command.doc).trim().replace(/^\/--/, '').replace(/-\/$/, '').trim()};
  }
  // A saved project claim as the reading view's claim card. The status,
  // currency, assumptions and checked statement are the project record's;
  // the prose and the statement field are presentation of the exact source,
  // which stays in the card's source fold.
  function projectClaimHTML(cell, saved, scopes = new Map(), staticPage = false) {
    const c = claim(saved.name) || {name: saved.name, label: projectClaimLabel(saved), keyword: '', project: saved, status: saved.status};
    const st = statusOf(c);
    const axioms = saved.axioms.length ? saved.axioms.map(a => esc(a.name)).join(', ') : 'none';
    // An example is checked as a private theorem; its status is about that
    // theorem's statement, shown as Lean prints it where the example stands.
    const checked = saved.example === true ? `<div class="claim-statement">${checkedStatementHTML(saved)}</div>` : '';
    const entries = trimmed(cellEntries(cell));
    const reading = claimReading(entries);
    const presentation = window.glitnirCards.declarations[c.keyword] || {kind: 'proposition', label: 'Claim'};
    let statement = '';
    let namespace = '';
    if (reading) {
      const match = DECLARATION.exec(reading.command.body[0][1]);
      const type = declarationParts(reading.command).head.slice(match[0].length).trim().replace(/^:\s*/, '');
      statement = saved.example === true ? '' : fieldHTML('Statement', type);
      namespace = scopes.get(reading.command.body[0][0])?.namespace || '';
    }
    // Without a reading, the exact source is the card's body, not a fold.
    const source = reading
      ? `<div class="card-source">${sourceFoldHTML(entries, `claim:${saved.name}`, 'Lean source & proof')}</div>`
      : `<div class="claim-statement"><pre class="lean-code">${leanHTML(leanOf(entries))}</pre></div>`;
    const stale = currencyPillHTML(saved) + (saved.not_rechecked ? '<span class="pill" data-tone="muted">Not rechecked</span>' : '');
    const through = UI.data.project_withheld?.through_line;
    const notRechecked = saved.not_rechecked ? `<div class="note-line" data-tone="muted">${icon('branch')}<span>Not rechecked: ${Number.isSafeInteger(through) ? `the last check covered lines 1–${esc(through)} only, so this` : 'this'} is the result of an earlier check.</span></div>` : '';
    const note = notRechecked + (saved.current === null ? `<div class="note-line" data-tone="muted">${icon('branch')}<span>${NOT_COMPARED}</span></div>`
      : saved.current ? '' : `<div class="note-line" data-tone="amber">${icon('branch')}<span>Out of date: ${recorded() ? 'the saved source above this claim changed since it was checked.' : `the saved source above this claim or its environment changed since it was checked.${leanOff() ? '' : ' Check the saved file to update it.'}`}</span></div>`);
    const actions = staticPage ? '' : checkThroughHTML(saved, 'Check through here', `Lines 1–${esc(saved.end_line)} only; later claims are not rechecked`);
    return `<section class="cell project-claim" id="cell-${esc(saved.name)}" data-claim="${esc(saved.name)}">
      <article class="claim card-${presentation.kind}" data-tone="${toneOf(c)}" data-selected="${UI.selected === saved.name}" aria-label="${esc(presentation.label)} ${esc(c.label)}">
        ${window.glitnirCards.header({label: presentation.label, name: c.label, classes: ['claim-head'], locationHTML: lineLinkHTML(cell.start, cell.end), badgesHTML: `<span class="pill" data-tone="${st.tone}">${esc(st.label)}</span>${stale}`})}
        ${namespaceHTML(namespace)}
        ${reading?.doc ? `<div class="doc-line">${docHTML(reading.doc)}</div>` : ''}
        ${statement ? `<div class="claim-statement">${statement}</div>` : ''}${checked}
        ${statementExpansionHTML(saved)}${usesHTML(saved)}
        ${saved.status === 'admitted' ? '<p class="card-notice claim-notice">Unfinished: the statement’s proof contains a placeholder.</p>' : ''}
        <p class="small muted claim-axioms">Assumptions: ${axioms}</p>
        ${source}
        ${note}
        ${actions ? `<div class="claim-actions">${actions}</div>` : ''}
      </article></section>`;
  }
  // Optional display from the last compiled observation, with the same currency
  // as its claim. Never used for statuses, source binding or proof navigation.
  function statementExpansionHTML(saved) {
    const expansion = saved.statement_expansion;
    if (!expansion) return '';
    const label = expansion.kind === 'constructor' ? 'Constructor signature · display only'
      : expansion.kind === 'unavailable' ? 'Statement expansion unavailable · display only'
      : 'One definition unfolded · display only';
    const bound = expansion.kind === 'unavailable' ? 'Expansion stopped; no expanded statement is available.'
      : expansion.kind === 'constructor' ? 'The constructor describes the fields; their definitions are not unfolded.'
      : 'Stopped after one definition; further names are not unfolded.';
    return `<div class="statement-expansion">${fieldHTML(label, expansion.text)}<p class="small muted">${bound}${expansion.truncated ? ' Display shortened to 4096 characters.' : ''}</p></div>`;
  }

  // The statement a checked example's status is about: as Lean prints it in
  // the example's own scope, with this project's notation (project code, so
  // its text is the project's), typeset like a theorem's Statement field, with
  // the kernel form (no notation, every name in full) one fold away. Records
  // of older checks hold only the kernel form. Both are display only. `kind`
  // is 'card', 'inspector' or 'panel'.
  const PRINTED_LABEL = 'Checked statement, as this project’s notation prints it';
  function statementFormsHTML({printed, kernel, printedCut, kernelCut, key}, kind) {
    printed = typeof printed === 'string' ? printed : null;
    kernel = typeof kernel === 'string' ? kernel : null;
    if (!printed && !kernel) return '';
    const cut = shortened => (shortened ? ' (shortened)' : '');
    const label = text => (kind === 'inspector' ? `<h5>${text}</h5>` : `<p class="small muted">${text}</p>`);
    const kernelHTML = kernel ? `<pre class="checked-statement">${esc(kernel)}</pre>` : '';
    if (!printed) return label(`Checked statement, kernel form${cut(kernelCut)}`) + kernelHTML;
    const fold = kernel && kernel !== printed ? `<details class="def-lean checked-kernel" data-fold-key="kernel:${esc(key)}"><summary>Kernel form, without notation${cut(kernelCut)}</summary>${kernelHTML}</details>` : '';
    const shown = kind === 'card' ? fieldHTML(`${PRINTED_LABEL}${cut(printedCut)}`, printed) : `${label(`${PRINTED_LABEL}${cut(printedCut)}`)}<pre class="checked-statement">${esc(printed)}</pre>`;
    return shown + fold;
  }
  const checkedStatementHTML = (saved, heading = false) => statementFormsHTML({printed: saved.statement, kernel: saved.kernel_statement,
    printedCut: saved.statement_shortened === true, kernelCut: saved.kernel_statement_shortened === true, key: saved.name}, heading ? 'inspector' : 'card');
  // The claims a project claim uses, as its last check recorded them: Lean's
  // own record of the theorems its statement and proof name directly. A claim
  // of this notebook is a chip that selects it; another notebook's opens that
  // notebook in this panel, read-only, at the claim. A target that is not
  // among this notebook's results, or whose notebook cannot be read here, is
  // plain text. Uses are display only.
  const shortName = name => name.split('.').pop();
  function useChipHTML(use) {
    if (use.in_notebook) {
      const target = claim(use.name);
      if (!target) return `<span class="use-plain" title="${esc(use.name)}: no saved result in this notebook">${esc(shortName(use.name))}</span>`;
      return `<button type="button" data-act="goto" data-id="${esc(target.name)}" title="${esc(target.name)}"><span class="dot" data-tone="${toneOf(target)}"></span> ${esc(claimLabel(target))}</button>`;
    }
    const text = `${use.module}:${shortName(use.name)}`;
    if (typeof use.path !== 'string' || !use.path) return `<span class="use-plain" title="${esc(use.name)} in ${esc(use.module)}: its notebook cannot be read here">${esc(text)}</span>`;
    // Relative to the notebook shown, as for prose links; the previewed
    // notebook itself is the trail's first entry.
    const path = [...(linkedPath() || '').split('/').slice(0, -1), use.path].join('/');
    const link = path === UI.main?.file ? '' : path;
    return `<a class="use-link" data-act="open-notebook" data-link="${esc(link)}" data-claim="${esc(use.name)}" href="/?reference=${encodeURIComponent(use.module)}" title="Read ${esc(use.name)} in ${esc(use.path)}, in this panel"><span class="dot" data-tone="muted"></span> ${esc(text)}</a>`;
  }
  const useChipsHTML = saved => saved.uses.map(useChipHTML).join('') + (saved.uses_truncated === true ? '<span class="use-plain" title="The check recorded only the first uses">and more</span>' : '');
  const usesHTML = saved => (Array.isArray(saved.uses) && saved.uses.length ? `<div class="uses">uses ${useChipsHTML(saved)}</div>` : '');
  // Examples without a proposition claim nothing; say so beside their source.
  function nonClaimsHTML(cell) {
    return (UI.data.project_non_claims || []).filter(n => n.start_line >= cell.start && n.start_line <= cell.end)
      .map(n => `<p class="small muted project-non-claim">Line ${esc(n.start_line)}: example without a proposition; not a claim.</p>`).join('');
  }
  function projectCheckHTML(result, sourceSha) {
    if (!result) return '';
    const through = Number.isSafeInteger(result.through_line) ? result.through_line : null;
    if (result.status === 'running') return `<p role="status">${through ? `Checking lines 1–${esc(through)} · ` : ''}${esc(result.message)}… <button type="button" class="secondary" data-act="check-cancel">${icon('x')}Stop</button></p>`;
    if (result.status !== 'checked_snapshot') return `<h3>Check unavailable${through ? ` · lines 1–${esc(through)}` : ''}</h3><pre>${esc(result.message)}</pre>`;
    const labels = {checked: 'Kernel checked', admitted: 'Contains sorry', assumptions: 'Additional assumptions'};
    const stopped = !result.withheld ? '' : requestedCut(result.withheld)
      ? `<p role="status">Checked lines 1–${esc(result.withheld.through_line)} on request; nothing after line ${esc(result.withheld.through_line)} was checked.</p>`
      : `<p role="status">${withheldText(result.withheld, 'Checking stopped')}</p>`;
    return `<h3>Last checked snapshot${through ? ` · lines 1–${esc(through)}` : ''}</h3>${stopped}<p>${esc(result.message)}</p>`
      + `<p>${result.source_sha256 !== sourceSha ? 'The saved source has changed since this check. ' : ''}Results describe the captured environment; dependencies may have changed. Run a new check to update them.</p>`
      + `<p class="small muted">Source ${esc(result.source_sha256)} · Environment ${esc(result.environment_sha256)}</p>`
      + projectResolutionHTML(result.resolution)
      + (result.theorems || []).map(t => `<article><h4>${esc(projectClaimLabel(t))} ·${esc(labels[t.status] || t.status)}</h4>${statementFormsHTML({printed: t.statement, kernel: t.type, key: `panel:${t.name}`}, 'panel')}<p>Assumptions: ${t.axioms.length ? t.axioms.map(a => esc(a.name)).join(', ') : 'none'}</p></article>`).join('');
  }
  const refresh = async () => {
    // Explicit actions need a read started after the action, not an older poll.
    if (inFlight) await inFlight;
    return load();
  };

  // ----- linked notebooks ---------------------------------------------------------
  // Another notebook of the project, read in this panel from its saved file
  // with its own saved results. Nothing is checked, observed, drawn or run
  // for it here: "Open in its own preview" does that. While it is shown, polls
  // read it, so live updates of the previewed notebook never replace it.
  const snapshots = new Map();
  function showNotebookAt(at) {
    if (!Number.isSafeInteger(at) || at < 0 || at >= UI.trail.length || at === UI.at) return;
    UI.at = at;
    switchNotebook();
  }
  function openNotebook(link) {
    // A notebook already on the way here is returned to, not stacked again.
    const earlier = UI.trail.slice(0, UI.at + 1).indexOf(link);
    if (earlier === UI.at) {
      const pending = takePendingClaim();
      render();
      if (pending) scrollToClaim(pending);
      return;
    }
    if (earlier >= 0) { showNotebookAt(earlier); return; }
    UI.trail = [...UI.trail.slice(0, UI.at + 1), link];
    UI.at += 1;
    switchNotebook();
  }
  function leaveLinked(message) {
    UI.pendingClaim = null;
    UI.trail.splice(UI.at, 1);
    UI.at = Math.max(0, UI.at - 1);
    switchNotebook();
    UI.notice = message;
    render();
  }
  function switchNotebook() {
    sourceEpoch++;
    opening = true;
    if (!linkedPath()) mainChanged = false;
    clearTimeout(highlightTimer);
    highlighted = new Set();
    Object.assign(UI, {selected: null, mode: 'doc', sourceCell: null, sourceRange: null, documentLine: null, notice: ''});
    closeDiagram();
    document.body.classList.toggle('glitnir-linked-view', Boolean(linkedPath()));
    const cached = snapshots.get(linkedPath() ?? '');
    readableSnapshot = cached || null;
    heldRendering = !cached;
    for (const id of ['doc', 'source', 'insp']) { $(id).inert = heldRendering; $(id).classList.toggle('live-held', heldRendering); }
    liveMessage(`Opening ${trailName(linkedPath())}…`);
    if (cached) {
      UI.data = cached;
      UI.selected = claims()[0]?.name ?? null;
    }
    const pending = cached ? takePendingClaim() : null;
    render();
    renderTrail();
    window.scrollTo?.(0, 0);
    if (pending) scrollToClaim(pending);
    load();
  }
  // A uses chip opened this notebook at one of its claims: select it once
  // the notebook is shown, or say it has no saved result for that claim.
  function takePendingClaim() {
    const name = UI.pendingClaim;
    if (!name) return null;
    UI.pendingClaim = null;
    if (claim(name)) { UI.selected = name; return name; }
    UI.notice = `${name} has no saved result in ${UI.data.file}.`;
    return null;
  }
  function scrollToClaim(name) {
    const el = $('cell-' + name);
    if (el) { revealSection(el); el.scrollIntoView?.({behavior: 'auto', block: 'start'}); }
  }
  const trailName = link => (link ? link.split('/').pop() : UI.main?.file || 'the notebook');
  function renderTrail() {
    const bar = $('notebook-trail');
    bar.hidden = UI.trail.length < 2;
    if (bar.hidden) { bar.replaceChildren(); return; }
    const link = linkedPath();
    const back = UI.trail[UI.at - 1], forward = UI.trail[UI.at + 1];
    const step = (act, target, text) => `<button type="button" class="secondary" data-act="${act}"${target === undefined ? ' disabled' : ` title="${esc(`${act === 'notebook-back' ? 'Back' : 'Forward'} to ${trailName(target)}`)}"`}>${text}</button>`;
    const crumbs = UI.trail.slice(0, UI.at + 1).map((entry, i) => i === UI.at
      ? `<li><span aria-current="page">${esc(trailName(entry))}</span></li>`
      : `<li><button type="button" class="link" data-act="notebook-at" data-at="${i}">${esc(trailName(entry))}</button></li>`).join('');
    // The editor opens a sibling in its own preview, where it can be checked.
    const module = link && /^[A-Za-z_][A-Za-z0-9_]*\.lean$/.test(link) ? link.slice(0, -5) : null;
    const own = HOST && module ? `<a class="notebook-own" href="/?reference=${encodeURIComponent(module)}">Open in its own preview</a>` : '';
    bar.innerHTML = step('notebook-back', back, '← Back') + step('notebook-forward', forward, 'Forward →')
      + `<ol class="trail-crumbs">${crumbs}</ol>`
      + (link ? `<span class="trail-note">Read-only · its own saved results · nothing is checked here</span>${own}` : '');
  }

  // ----- helpers --------------------------------------------------------------
  // A declaration command's modifiers, keyword and name; reading.js reads the rest.
  const DECLARATION = /^(?:@\[.*\]\s*)?(?:(?:private|protected|noncomputable|scoped|local|nonrec|partial|unsafe)\s+)*(class\s+inductive|def|abbrev|structure|inductive|class|instance|axiom|opaque|example|theorem|lemma)\b\s*(«[^»]*»|[^\s:({[]*)/;

  // In project mode the claims are the saved project records, named as the
  // source names them; an example is "example (line N)".
  const projectClaimCache = new WeakMap();
  function projectClaims(data) {
    if (!projectClaimCache.has(data)) {
      const cells = data.cells.filter(cell => cell.kind === 'source' && cell.claim);
      projectClaimCache.set(data, (data.project_claims || []).map(saved => {
        const cell = cells.find(c => c.claim === saved.name);
        const head = cell ? cellEntries(cell).map(([, line]) => line).find(line => DECLARATION.test(line)) : undefined;
        const match = head ? DECLARATION.exec(head) : null;
        const label = saved.example === true ? projectClaimLabel(saved) : match?.[2] || saved.name;
        // The graph's arcs: the claims of this notebook it uses.
        const uses = Array.isArray(saved.uses) ? saved.uses.filter(u => u.in_notebook === true).map(u => u.name) : [];
        return {name: saved.name, label, keyword: match ? match[1].replace(/\s+/g, ' ') : '', status: saved.status, uses, project: saved};
      }));
    }
    return projectClaimCache.get(data);
  }
  const projectMode = () => UI.data?.preview?.mode === 'project';
  const claims = () => (!UI.data ? [] : projectMode() ? projectClaims(UI.data) : UI.data.claims);
  const claim = name => claims().find(c => c.name === name);
  const claimLabel = c => c.label || c.name;
  // A long Lean name in the narrow contents wraps after its `_` and `.` parts.
  const breakableName = name => esc(name).replace(/[_.]/g, '$&<wbr>');
  const checking = name => UI.data && UI.data.checking.includes(name);
  function statusOf(c, staticPage = false) {
    if (c.project) {
      const [tone, label] = Object.hasOwn(PROJECT_STATUS, c.status) ? PROJECT_STATUS[c.status] : ['blue', c.status];
      return {key: c.status, label, tone};
    }
    if (!staticPage && checking(c.name)) return {key: 'checking', label: 'Checking', tone: 'blue'};
    return {key: c.status, label: LABEL[c.status] || c.status, tone: TONE[c.status] || 'blue'};
  }
  // An out-of-date project result is shown amber wherever only a colour
  // stands for the claim: dots, the gutter and the card's edge.
  // A linked notebook of another Lake project shows its saved results without
  // comparing them with any environment: their currency is unknown (null).
  const NOT_COMPARED = 'Saved result, not compared with this project’s environment: this notebook belongs to another Lake project. Open it in its own preview to check it.';
  // A result current only as recorded says so wherever a current one says nothing.
  const asRecorded = saved => recorded() && saved.current === true;
  const currencyPillHTML = saved => saved.current === null ? '<span class="pill" data-tone="muted">Not compared</span>' : asRecorded(saved) ? '<span class="pill" data-tone="muted">As recorded</span>' : saved.current ? '' : '<span class="pill" data-tone="amber">Out of date</span>';
  const currencyNoted = saved => saved.current !== true || recorded();
  const currencyText = saved => (saved.current === null ? 'not compared' : saved.current ? (recorded() ? 'as recorded' : 'current') : 'out of date') + (saved.not_rechecked ? ' · not rechecked by the last check' : '');
  const toneOf = c => (c.project && !c.project.current ? 'amber' : c.project && asRecorded(c.project) ? 'muted' : statusOf(c).tone);
  // Section summaries count a project result's status and, apart, whether it
  // is out of date.
  const summariesOf = (c, staticPage) => [statusOf(c, staticPage), ...(c.project && c.project.current === false ? [{key: 'out-of-date', label: 'Out of date', tone: 'amber'}] : []),
    ...(c.project && c.project.current === null ? [{key: 'not-compared', label: 'Not compared', tone: 'muted'}] : []),
    ...(c.project && asRecorded(c.project) ? [{key: 'as-recorded', label: 'As recorded', tone: 'muted'}] : []),
    ...(c.project?.not_rechecked ? [{key: 'not-rechecked', label: 'Not rechecked', tone: 'muted'}] : [])];
  // Colour only: the text is the exact source. Comments are matched first so
  // words inside them are never shown as keywords.
  const LEAN_TOKENS = /(\/-[\s\S]*?-\/|--[^\n]*)|\b(noncomputable|def|theorem|lemma|example|axiom|abbrev|instance|structure|inductive|where|namespace|end|import|open|by|fun|have|show|calc|let|variable|section)\b/g;
  const leanHTML = t => esc(t).replace(LEAN_TOKENS, (match, comment, keyword) => comment ? `<span class="cm">${comment}</span>` : `<span class="kw">${keyword}</span>`);

  Object.assign(workbench, {$, API, DECLARATION, HOST, LABEL, UI, breakableName, checkBusy, checkProject,
    checkThroughHTML, checkedStatementHTML, checking, claim, claimLabel, claimReading, claims, currencyNoted,
    currencyPillHTML, currencyText, esc, fetch, icon, inspectProject, inspecting, inspection, leanHTML,
    leanOff, leanVersion, linkedPath, load, markLiveChanges, nonClaimsHTML, openNotebook, post,
    projectCheckHTML, projectClaimHTML, projectClaims, projectMode, projectResolutionHTML, readingFolds, recorded, reduced,
    refresh, renderTrail, requestedCut, showNotebookAt, statusOf, summariesOf, toneOf, useChipsHTML, useHTML,
    withheldBannerHTML, environmentEpoch: () => environmentEpoch, heldRendering: () => heldRendering});
})();
