// The Glitnir notebook workbench: renders what the local server reports.
// Every status shown here comes from the server, which derives it from
// sandboxed Lean checks; this page decides nothing itself.
(() => {
  'use strict';
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
    if (inspecting() || UI.data?.preview?.mode !== 'project' || linkedPath() || recorded()) return;
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
    if (UI.checkRequested || UI.data?.preview?.mode !== 'project' || UI.data.preview.check?.status === 'running' || linkedPath() || recorded()) return;
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
    if (linkedPath() || recorded() || !projectMode()) return '';
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
      : saved.current ? '' : `<div class="note-line" data-tone="amber">${icon('branch')}<span>Out of date: ${recorded() ? 'the saved source above this claim changed since it was checked.' : 'the saved source above this claim or its environment changed since it was checked. Check the saved file to update it.'}</span></div>`);
    const actions = staticPage ? '' : checkThroughHTML(saved, 'Check through here', `Lines 1–${esc(saved.end_line)} only; later claims are not rechecked`);
    return `<section class="cell project-claim" id="cell-${esc(saved.name)}" data-claim="${esc(saved.name)}">
      <article class="claim card-${presentation.kind}" data-tone="${toneOf(c)}" data-selected="${UI.selected === saved.name}" aria-label="${esc(presentation.label)} ${esc(c.label)}">
        ${window.glitnirCards.header({label: presentation.label, name: c.label, classes: ['claim-head'], locationHTML: lineLinkHTML(cell.start, cell.end), badgesHTML: `<span class="pill" data-tone="${st.tone}">${esc(st.label)}</span>${stale}`})}
        ${namespaceHTML(namespace)}
        ${reading?.doc ? `<div class="doc-line">${docHTML(reading.doc)}</div>` : ''}
        ${statement ? `<div class="claim-statement">${statement}</div>` : ''}${checked}
        ${usesHTML(saved)}
        ${saved.status === 'admitted' ? '<p class="card-notice claim-notice">Unfinished: the statement’s proof contains a placeholder.</p>' : ''}
        <p class="small muted claim-axioms">Assumptions: ${axioms}</p>
        ${source}
        ${note}
        ${actions ? `<div class="claim-actions">${actions}</div>` : ''}
      </article></section>`;
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
  const currencyPillHTML = saved => saved.current === null ? '<span class="pill" data-tone="muted">Not compared</span>' : saved.current ? '' : '<span class="pill" data-tone="amber">Out of date</span>';
  const currencyText = saved => (saved.current === null ? 'not compared' : saved.current ? (recorded() ? 'as recorded' : 'current') : 'out of date') + (saved.not_rechecked ? ' · not rechecked by the last check' : '');
  const toneOf = c => (c.project && !c.project.current ? 'amber' : statusOf(c).tone);
  // Section summaries count a project result's status and, apart, whether it
  // is out of date.
  const summariesOf = (c, staticPage) => [statusOf(c, staticPage), ...(c.project && c.project.current === false ? [{key: 'out-of-date', label: 'Out of date', tone: 'amber'}] : []),
    ...(c.project && c.project.current === null ? [{key: 'not-compared', label: 'Not compared', tone: 'muted'}] : []),
    ...(c.project?.not_rechecked ? [{key: 'not-rechecked', label: 'Not rechecked', tone: 'muted'}] : [])];
  // Colour only: the text is the exact source. Comments are matched first so
  // words inside them are never shown as keywords.
  const LEAN_TOKENS = /(\/-[\s\S]*?-\/|--[^\n]*)|\b(noncomputable|def|theorem|lemma|example|axiom|abbrev|instance|structure|inductive|where|namespace|end|import|open|by|fun|have|show|calc|let|variable|section)\b/g;
  const leanHTML = t => esc(t).replace(LEAN_TOKENS, (match, comment, keyword) => comment ? `<span class="cm">${comment}</span>` : `<span class="kw">${keyword}</span>`);

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
    if (!drawing && !linked) loadView(key, view);
    const head = window.glitnirCards.header({label: `${view.kind} diagram`, name: view.name, locationHTML: lineLinkHTML(line, line)});
    let body;
    if (linked) body = `<div class="note-line" data-tone="muted">${icon('branch')}<span>Lean draws this view in the notebook's own preview.</span></div>`;
    else if (view.error) body = `<div class="note-line" data-tone="red">${icon('x')}<span>${esc(view.error)}</span></div>`;
    else if (!drawing || drawing.loading) body = `<p class="small muted view-wait" role="status">${esc(drawing?.message || 'Lean is drawing it…')}</p>`
      + (drawing?.drawing ? `<div class="claim-actions"><button type="button" class="secondary" data-act="view-cancel" data-line="${line}" data-sha="${esc(UI.data.sha256)}">${icon('x')}Stop</button><span class="small muted">Drawing is bounded by the check time limit.</span></div>` : '');
    else if (drawing.error) body = `<div class="note-line" data-tone="red">${icon('x')}<span>No drawing: ${esc(drawing.error)}</span></div>`
      + `<div class="claim-actions"><button type="button" class="secondary" data-act="view-retry" data-key="${esc(key)}">${icon('replay')}Draw again</button></div>`;
    else if (drawing.graph || drawing.model) body = diagramHTML(key);
    else body = `<div class="view-body"><img alt="${esc(view.kind)} view of ${esc(view.name)}" src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(drawing.svg)}"></div>`;
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
  const EVIDENCE_STATUS = {never: ['muted', 'Never run'], running: ['blue', 'Running'], current: ['green', 'Current'], stale: ['amber', 'Out of date'], uncompared: ['muted', 'Not compared']};
  const projectEvidence = cell => (UI.data.evidence || []).filter(e => e.cell === cell.index).sort((a, b) => a.line - b.line);
  // A verb no installed provider runs gets a note in the source, not a card,
  // unless saved evidence for it remains to be shown.
  const needsProvider = entry => !entry.provider && Boolean(entry.verb) && !entry.snapshot;

  function evidenceHTML(entry, staticPage = false) {
    const line = entry.line, snap = entry.snapshot;
    const running = entry.status === 'running';
    // Without a provider nothing can run it, so current saved evidence is
    // only saved; out-of-date evidence still says so.
    const [tone, label] = staticPage && running ? ['amber', 'Running at export'] : recorded() && entry.status === 'current' ? ['muted', 'As recorded'] : !entry.provider && snap && entry.status === 'current' ? ['muted', 'Saved'] : Object.hasOwn(EVIDENCE_STATUS, entry.status) ? EVIDENCE_STATUS[entry.status] : ['muted', entry.status];
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
      else if (!snap) body = '<p class="small muted evidence-wait">Not run yet.</p>';
    }
    if (snap && (!running || staticPage)) {
      try {
        const dim = entry.status === 'stale' || running;
        body = `<div class="evidence-charts">${chartHTML(snap.series, dim)}${snap.plane ? phaseHTML(snap.series, snap.plane, dim) : ''}</div>`
          + (snap.finite ? '' : note('amber', 'Some values were not finite; they are not drawn.'))
          + provenanceHTML(snap.provenance);
      } catch (error) {
        body = note('red', 'The saved evidence is unreadable. Run it again.');
      }
    }
    if (staticPage && running) body = '<p class="small muted">A run was in progress at export time. Any chart below is the previous saved evidence; its currency is not established here.</p>' + body;
    // An errored card shows its error, then at most the last run's failure;
    // a card errored only by its missing provider still says it is out of date.
    if (entry.status === 'stale' && (!entry.error || !entry.provider)) body = note('amber', `Out of date: ${recorded() ? 'the saved source' : 'the source or its environment'} changed since this run.${entry.provider && !recorded() ? ' Run it again to update it.' : ''}`) + body;
    if (entry.run_error) body = note('red', `The last run failed: ${esc(entry.run_error)}`) + body;
    if (entry.unavailable && !entry.error) body = note('amber', `${esc(entry.unavailable)} Saved evidence remains viewable.`) + body;
    if (entry.error) body = note('red', esc(entry.error) + (snap ? ' Saved evidence remains viewable.' : '')) + body;
    if (!staticPage && runnable && !running && !linkedPath() && !recorded()) action = `<div class="claim-actions"><button type="button" class="${entry.status === 'current' ? 'secondary' : 'primary'}" data-act="evidence-run" data-line="${line}" data-sha="${esc(UI.data.sha256)}">${icon('replay')}${snap ? 'Run again' : 'Run'}</button><span class="small muted">Evidence never changes a claim's status.</span></div>`;
    return `<section data-document-start="${line}" data-document-end="${line}" class="cell box evidence">${head}${body}${action}</section>`;
  }

  // A decision is what a provider proposed for one listed `@decide` directive.
  // The provider's block becomes a candidate only when the project checker's
  // kernel check of the saved source through the directive, followed by that
  // block, checks its theorem. Nothing here writes the notebook: Accept names
  // the decision, and the editor inserts the block the server serves for it.
  const DECISION_STATUS = {never: ['muted', 'Never run'], running: ['blue', 'Running'], unknown: ['amber', 'Unknown'], rejected: ['red', 'Rejected'],
    candidate: ['blue', 'Candidate'], accepted: ['green', 'Accepted'], stale: ['amber', 'Out of date'], uncompared: ['muted', 'Not compared']};
  const RUN_AGAIN = new Set(['never', 'unknown', 'rejected', 'stale', 'candidate']);
  // A goal kind no installed provider decides gets a note in the source, not
  // a card, unless a saved outcome for it remains to be shown.
  const needsDecider = entry => !entry.provider && !entry.outcome;

  function decisionHTML(entry, staticPage = false) {
    const {line, end_line: end, outcome} = entry;
    const running = entry.status === 'running';
    const [tone, label] = staticPage && running ? ['amber', 'Running at export'] : Object.hasOwn(DECISION_STATUS, entry.status) ? DECISION_STATUS[entry.status] : ['muted', entry.status];
    const what = `<code>${esc(entry.name || '?')}</code>${entry.kind ? ` · ${esc(entry.kind)}` : ''}${entry.provider ? ` · ${esc(entry.provider)}` : ''}`;
    // A directive in error has no status of its own until it is fixed.
    const pill = entry.error && !outcome ? '<span class="pill" data-tone="muted">Error</span>' : `<span class="pill" data-tone="${tone}">${esc(label)}</span>`;
    const head = `<div class="box-head"><span class="tag">Decision</span><span class="small">${what}</span><span class="evidence-state">${pill}${lineLinkHTML(line, end)}</span></div>`;
    const note = (tone, text) => `<div class="note-line" data-tone="${tone}">${icon(tone === 'red' ? 'x' : tone === 'green' ? 'check' : 'branch')}<span>${text}</span></div>`;
    const kept = outcome ? ' The saved outcome remains viewable.' : '';
    let body = '';
    if (!entry.error) {
      if (running && !staticPage) body = '<p class="small muted evidence-wait">Running the provider; the project checker then checks what it proposes…</p>';
      else if (!outcome) body = '<p class="small muted evidence-wait">Not run yet.</p>';
    }
    if (outcome && (!running || staticPage)) body = decisionOutcomeHTML(entry, note);
    if (staticPage && running) body = '<p class="small muted">A run was in progress at export time. Any outcome below is the previous saved one; its currency is not established here.</p>' + body;
    if (entry.status === 'stale') body = note('amber', recorded() ? 'Out of date: the saved source through this directive changed since this run.' : 'Out of date: the source through this directive or its environment changed since this run. Run it again to update it.') + body;
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
      if (acceptable && HOST) buttons.push(`<button type="button" class="primary" data-act="accept" data-key="${esc(entry.key)}">${icon('check')}Accept</button>`);
      if (served && !entry.unavailable && RUN_AGAIN.has(entry.status)) buttons.push(`<button type="button" class="${acceptable || entry.status === 'accepted' ? 'secondary' : 'primary'}" data-act="decision-run" data-line="${line}" data-sha="${sha}">${icon('replay')}${entry.outcome ? 'Run again' : 'Run'}</button>`);
      if (entry.outcome) buttons.push(`<button type="button" class="secondary" data-act="decision-discard" data-line="${line}" data-sha="${sha}">Discard</button>`);
      if (acceptable && !HOST) buttons.push('<span class="small muted">Accept it from the editor\'s preview.</span>');
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
    await post('/api/evidence/run', {sha256, line});
    refresh();
  }
  async function decisionAction(path, body) {
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

  // ----- Lean read as a textbook ----------------------------------------------
  // Presentation only: recognized declarations get mathematical cards and an
  // exact source fold. Unknown syntax gets an explicit card, never disappears.
  const DECLARATION = /^(?:@\[.*\]\s*)?(?:(?:private|protected|noncomputable|scoped|local|nonrec|partial|unsafe)\s+)*(class\s+inductive|def|abbrev|structure|inductive|class|instance|axiom|opaque|example|theorem|lemma)\b\s*(«[^»]*»|[^\s:({[]*)/;

  const TRUST = /\b(sorry|admit|axiom|opaque|unsafe|implemented_by|extern|native_decide|set_option|variable|universe)\b|\bdebug\./;

  // Column-0 lines that continue the command above them.
  const CONTINUES = /^(\||deriving\b|where\b|termination_by\b|decreasing_by\b|with\b)/;
  // Column-0 lines that lead into the command below them.
  const LEADS = /^(@\[.*\]|.*\bin)\s*$/;

  // True when every block comment opens at the start of a line, closes at the
  // end of one and does not nest, so line shapes can find the commands.
  function plainComments(entries) {
    let inside = false;
    for (const [, text] of entries) {
      const code = inside || text.startsWith('/-') ? text : text.replace(/--.*$/, '');
      const opens = (code.match(/\/-/g) || []).length;
      const closes = (code.match(/-\//g) || []).length;
      if (inside) {
        if (opens || closes > 1 || (closes && !code.trimEnd().endsWith('-/'))) return false;
        inside = !closes;
      } else if (opens) {
        if (opens > 1 || !code.startsWith('/-') || closes > 1 || (closes && !code.trimEnd().endsWith('-/'))) return false;
        inside = !closes;
      } else if (closes) return false;
    }
    return !inside;
  }

  // Split [line, text] entries into top-level commands: {doc, notes, body}.
  // A command starts at a column-0 line; its docstring, and the comments and
  // leading lines above it, lead into it. Comments that turn out to sit inside
  // a body, because indented lines follow, stay with it.
  function commands(entries) {
    const out = [];
    let doc = [];
    let notes = [];
    let held = [];
    let current = null;
    let comment = null;
    const close = () => { if (current) out.push(current); current = null; notes.push(...held); held = []; };
    for (const entry of entries) {
      const text = entry[1];
      if (comment) { comment.push(entry); if (text.includes('-/')) comment = null; continue; }
      if (text.startsWith('/--')) { close(); notes.push(...doc); doc = [entry]; if (!text.slice(3).includes('-/')) comment = doc; continue; }
      if (text.startsWith('/-')) { close(); notes.push(entry); if (!text.slice(2).includes('-/')) comment = notes; continue; }
      if (current && (!text.trim() || text.startsWith('--'))) { held.push(entry); continue; }
      if (current && (/^\s/.test(text) || CONTINUES.test(text))) { current.body.push(...held, entry); held = []; continue; }
      close();
      if (!text.trim() || text.startsWith('--') || LEADS.test(text)) { notes.push(entry); continue; }
      current = {doc, notes, body: [entry]};
      doc = [];
      notes = [];
    }
    close();
    if (doc.length || notes.length) out.push({doc: [], notes: [...notes, ...doc].sort((a, b) => a[0] - b[0]), body: []});
    return out;
  }

  // Entries without the blank lines at either end.
  function trimmed(entries) {
    const at = entries.findIndex(e => e[1].trim());
    if (at < 0) return [];
    let to = entries.length;
    while (!entries[to - 1][1].trim()) to--;
    return entries.slice(at, to);
  }
  function lineLinkHTML(start, end) {
    const cell = UI.data.cells.find(c => c.start <= start && end <= c.end);
    const edit = HOST && cell && !linkedPath() ? `<button type="button" class="source-edit" data-act="source" data-index="${cell.index}" data-start="${start}" data-end="${end}" data-sha="${esc(UI.data.sha256)}" title="Edit in editor" aria-label="Edit in editor">${icon('edit')}</button>` : '';
    return `<span class="source-controls"><a class="lines source-link" href="#source-L${start}" data-act="source-lines" data-sha="${esc(UI.data.sha256)}" data-start="${start}" data-end="${end}" aria-label="Show lines ${start}–${end} in Source" title="Show in Source">L${start}–${end}</a>${edit}</span>`;
  }
  const leanOf = entries => entries.map(e => e[1]).join('\n');
  // A cell's [line, text] entries; a source cell's text ends with a newline.
  const cellEntries = cell => (cell.text.endsWith('\n') ? cell.text.slice(0, -1) : cell.text).split('\n').map((text, i) => [cell.start + i, text]);
  // An odd number of quotes may open a multiline string, which can imitate
  // top-level commands in the line reader.
  const unevenQuotes = line => (line.replace(/\\./g, '').replace(/--.*$/, '').match(/"/g) || []).length % 2 === 1;
  // Scaffolding such as imports, namespaces and directives: exact and quiet.
  const quietSourceHTML = entries => `<div class="cell source-context"><pre>${leanHTML(leanOf(entries))}</pre>${lineLinkHTML(entries[0][0], entries[entries.length - 1][0])}</div>`;

  // A project source cell read as the reading view reads Lean: cards for
  // declarations, prose for comments, quiet lines for scaffolding. A listed
  // directive at column 0 is replaced by its card, at its line; one no
  // provider serves keeps its exact line with a note. Returns the HTML and
  // the directive lines placed. Source the line reader cannot read safely is
  // shown whole, and so is a cell holding a `/-!` comment: the server keeps
  // prose in source cells only when it could not confirm comment boundaries.
  function projectSourceHTML(cell, scopes = new Map(), staticPage = false) {
    const entries = cellEntries(cell);
    const placed = new Set();
    if (entries.some(([, line]) => unevenQuotes(line) || line.startsWith('/-!'))) {
      const shown = trimmed(entries);
      const html = shown.length ? `<section class="cell">${lineLinkHTML(shown[0][0], shown[shown.length - 1][0])}<pre class="lean-source">${leanHTML(leanOf(shown))}</pre></section>` : '';
      return {html, placed};
    }
    const notes = new Map(directiveNotes(cell));
    const directives = new Map();
    const listed = (start, end, card) => {
      const lines = entries.filter(([n]) => n >= start && n <= end);
      if (lines.length && lines.every(([, text]) => text.startsWith('--'))) directives.set(start, {lines, card});
    };
    for (const v of UI.data.views || []) if (v.cell === cell.index) listed(v.line, v.line, () => viewHTML(cell, v.line));
    for (const e of projectEvidence(cell)) listed(e.line, e.line, () => needsProvider(e) ? null : evidenceHTML(e, staticPage));
    for (const d of UI.data.decisions || []) if (d.cell === cell.index) listed(d.line, d.end_line ?? d.line, () => needsDecider(d) ? null : decisionHTML(d, staticPage));
    const parts = [];
    let segment = [];
    const flush = () => {
      if (trimmed(segment).length) parts.push(textbookHTML(segment, scopes, true));
      segment = [];
    };
    for (let i = 0; i < entries.length; i++) {
      const directive = directives.get(entries[i][0]);
      if (!directive) { segment.push(entries[i]); continue; }
      flush();
      const {lines, card} = directive;
      const start = lines[0][0], end = lines[lines.length - 1][0];
      const html = card();
      parts.push(html ?? `<div class="cell directive-unavailable" data-document-start="${start}" data-document-end="${end}">${quietSourceHTML(lines)}${notes.get(start) || ''}</div>`);
      placed.add(start);
      i += lines.length - 1;
    }
    flush();
    return {html: parts.join(''), placed};
  }


  function kindOf(command) {
    const lines = [...command.notes, ...command.body].map(e => e[1]).filter(t => t.trim());
    if (!command.body.length) return 'context';
    const setup = lines.join('\n').replace(/\s+\([\p{L}\p{N}_'.\s]+\)$/u, '');
    if (!TRUST.test(lines.join('\n')) && /^(?:import|open(?:\s+scoped)?)(?:\s+[\p{L}_][\p{L}\p{N}_'.]*)+$/u.test(setup) && !/\bin\b/.test(lines.join('\n'))) return 'setup';
    if (lines.length === 1 && !TRUST.test(lines[0]) && !/\bin\b/.test(lines[0]) && (/^(namespace|section|end)(?:\s+(?:«[^»]+»|[\p{L}_][\p{L}\p{N}_'.]*))?\s*$/u.test(lines[0]) || /^#(?:print|check|eval)\b/.test(lines[0]))) return 'setup';
    return DECLARATION.test(command.body[0][1]) ? 'definition' : 'context';
  }

  // Namespace labels are display hints, using the same conservative command
  // boundaries as the cards. Sections affect matching `end`, not qualification.
  // If scope syntax is ambiguous, preserve it in explicit context cards.
  function namespaceScopes(cells) {
    const result = new Map();
    const stack = [];
    const ident = '(?:«[^»\\n]+»|[\\p{L}_][\\p{L}\\p{N}_\\x27]*)(?:\\.(?:«[^»\\n]+»|[\\p{L}_][\\p{L}\\p{N}_\\x27]*))*';
    const scopeHead = new RegExp(`^(namespace|section|end)(?:\\s+(${ident}))?\\s*$`, 'u');
    for (const cell of cells) {
      if (cell.kind === 'claim') {
        const namespace = stack.filter(s => s.kind === 'namespace').map(s => s.name).join('.');
        result.set(cell.index, new Map([[cell.start, {namespace}]]));
      }
      if (cell.kind !== 'lean' && cell.kind !== 'source') continue;
      const entries = cellEntries(cell);
      if (!plainComments(entries)) return new Map();
      const scopes = new Map();
      result.set(cell.index, scopes);
      for (const command of commands(entries)) {
        if (!command.body.length) continue;
        const body = leanOf(command.body).trim();
        // Multiline strings can imitate top-level commands in the line reader.
        if (command.body.some(([, line]) => (line.replace(/\\./g, '').replace(/--.*$/, '').match(/"/g) || []).length % 2)) return new Map();
        const namespace = stack.filter(s => s.kind === 'namespace').map(s => s.name).join('.');
        const info = {namespace, hide: false};
        scopes.set(command.body[0][0], info);
        if (!/^(namespace|section|end)\b/.test(body)) continue;
        if (command.notes.some(([, line]) => LEADS.test(line))) return new Map();
        const match = scopeHead.exec(body);
        if (!match) return new Map();
        const [, kind, name = ''] = match;
        if (kind === 'end') {
          const scope = stack.pop();
          if (!scope || (name && name !== scope.name)) return new Map();
          info.hide = scope.kind === 'namespace';
        } else {
          if (kind === 'namespace' && (!name || name.includes('_root_'))) return new Map();
          stack.push({kind, name});
          info.hide = kind === 'namespace';
        }
      }
    }
    return result;
  }

  const namespaceHTML = name => name ? `<p class="def-namespace">Namespace <code>${esc(name)}</code></p>` : '';

  // With `quiet`, scaffolding is shown as exact quiet lines instead of being
  // left out, and its comments as notes.
  function textbookHTML(entries, scopes = new Map(), quiet = false) {
    if (!plainComments(entries)) {
      const all = trimmed(entries);
      return all.length ? contextHTML(null, all, '') : '';
    }
    return commands(entries).map(command => {
      const list = trimmed([...command.notes, ...command.doc, ...command.body].sort((a, b) => a[0] - b[0]));
      if (!list.length) return '';
      if (quiet && command.body.length && kindOf({doc: [], notes: [], body: command.body}) === 'setup') {
        const notes = trimmed([...command.notes, ...command.doc].sort((a, b) => a[0] - b[0]));
        // A leading line such as `@[…]` belongs to the command: keep it exact.
        if (notes.some(([, line]) => LEADS.test(line))) return quietSourceHTML(list);
        return (notes.length ? contextHTML({doc: command.doc, notes: command.notes, body: []}, notes, '') : '') + quietSourceHTML(trimmed(command.body));
      }
      const kind = kindOf(command);
      const scope = scopes.get(command.body[0]?.[0]);
      // Hide only recognized scaffolding; preserve attached comments as notes.
      if (kind === 'setup' && /^(namespace|section|end)\b/.test(command.body[0][1]) && !scope) return contextHTML(command, list, '');
      if (scope?.hide || kind === 'setup') {
        const notes = trimmed([...command.notes, ...command.doc]);
        return notes.length ? contextHTML({doc: command.doc, notes: command.notes, body: []}, notes, scope?.namespace || '') : '';
      }
      if (kind === 'definition') return definitionHTML(command, list, scope?.namespace || '');
      return contextHTML(command, list, scope?.namespace || '');
    }).join('');
  }

  const sourceFoldHTML = (entries, key = 'definition', label = 'Lean source') => `<details class="def-lean" data-fold-key="${key}:${esc(leanOf(entries))}"><summary class="small muted">${esc(label)} · ${entries.length} line${entries.length === 1 ? '' : 's'}</summary><pre>${leanHTML(leanOf(entries))}</pre></details>`;

  // Translate glyphs, not meaning: identifiers, application order and grouping
  // are retained. Exact Lean stays in the source fold; no elaboration is inferred.
  function expressionHTML(text) {
    const symbols = {'∑': '\\sum', '∏': '\\prod', '*': '\\cdot', '→': '\\to', '←': '\\leftarrow', '↔': '\\leftrightarrow', '∀': '\\forall', '∃': '\\exists', '∧': '\\land', '∨': '\\lor', '¬': '\\neg', '≤': '\\le', '≥': '\\ge', '≠': '\\ne', '∈': '\\in', '∉': '\\notin', '⊆': '\\subseteq', '∪': '\\cup', '∩': '\\cap', 'ℕ': '\\mathbb{N}', 'ℤ': '\\mathbb{Z}', 'ℚ': '\\mathbb{Q}', 'ℝ': '\\mathbb{R}', '⟨': '\\langle', '⟩': '\\rangle', '×': '\\times', '∞': '\\infty', 'λ': '\\lambda', 'Σ': '\\Sigma', 'ω': '\\omega', 'ρ': '\\rho', 'ε': '\\varepsilon', 'ν': '\\nu', 'α': '\\alpha', 'β': '\\beta', 'γ': '\\gamma', 'δ': '\\delta', 'θ': '\\theta', 'π': '\\pi'};
    // Strings are typewriter text: KaTeX's text font curls a straight quote.
    const literal = (token, font = 'text') => `\\${font}{` + token.replace(/[\\{}$&#%_^~]/g, ch => ({'\\':'\\textbackslash{}','{':'\\{','}':'\\}','$':'\\$','&':'\\&','#':'\\#','%':'\\%','_':'\\_','^':'\\textasciicircum{}','~':'\\textasciitilde{}'}[ch])) + '}';
    const tokenTex = token => {
      if (Object.hasOwn(symbols, token)) return symbols[token] + ' ';
      const sub = /^(.*?)([₀-₉]+)$/.exec(token);
      if (sub) return `${tokenTex(sub[1])}_{${[...sub[2]].map(ch => '₀₁₂₃₄₅₆₇₈₉'.indexOf(ch)).join('')}}`;
      if (/^[\p{L}\p{N}_']+$/u.test(token) && /[α-ωΑ-Ω]/u.test(token)) return [...token].map(ch => Object.hasOwn(symbols, ch) ? symbols[ch] + ' ' : literal(ch)).join('');
      return /^[0-9]+$/.test(token) ? token : literal(token, token.startsWith('"') ? 'texttt' : 'text');
    };
    return text.trim().split('\n').filter(line => line.trim()).map(line => {
      const tokens = line.match(/«[^»]*»|"(?:\\.|[^"\\])*"|[\p{L}_][\p{L}\p{N}_']*|[0-9]+|\s+|./gu) || [];
      const atom = '[\\p{L}_][\\p{L}\\p{N}_\\x27.]*|[0-9]+';
      const power = new RegExp(`^(${atom})\\s*\\^\\s*(${atom})(?:\\s*/\\s*(${atom}))?$`, 'u').exec(line.trim());
      const fraction = new RegExp(`^(${atom})\\s*/\\s*(${atom})$`, 'u').exec(line.trim());
      const baseTex = tokens.map(token => /^\s+$/.test(token) ? '\\;' : /^[=+(),.:;<>!|/\-]$/.test(token) ? token : tokenTex(token)).join('');
      const tex = power ? (power[3] ? `\\frac{${tokenTex(power[1])}^{${tokenTex(power[2])}}}{${tokenTex(power[3])}}` : `${tokenTex(power[1])}^{${tokenTex(power[2])}}`) : fraction ? `\\frac{${tokenTex(fraction[1])}}{${tokenTex(fraction[2])}}` : baseTex;
      // Keep the original expression as the fallback if KaTeX is unavailable.
      return `<span class="formal-line math" data-tex="${esc(tex)}">${esc(line.trim())}</span>`;
    }).join('');
  }

  const fieldHTML = (label, value) => value ? `<div class="formal-field"><div class="field-label">${esc(label)}</div><div class="formal-expression">${expressionHTML(value)}</div></div>` : '';

  function contextHTML(command, entries, namespace) {
    const body = command ? leanOf(command.body).trim() : '';
    const context = /^(variable|universe|set_option|attribute|notation|infix[lr]?|prefix|postfix)\b\s*(.*)$/s.exec(body);
    const labels = {variable: 'Parameters', universe: 'Universes', set_option: 'Option', attribute: 'Attributes', notation: 'Notation', infix: 'Notation', infixl: 'Notation', infixr: 'Notation', prefix: 'Notation', postfix: 'Notation'};
    const note = command && !body ? leanOf([...command.notes, ...command.doc]).replace(/^\s*-- ?/gm, '').replace(/\/--?|-\//g, '').trim() : '';
    const label = context ? labels[context[1]] : note ? 'Note' : 'Additional notebook content';
    const sensitive = [...new Set(leanOf(entries).match(/\b(sorry|admit|axiom|unsafe|implemented_by|extern|native_decide)\b/g) || [])];
    return window.glitnirCards.render({
      kind: 'context', label, classes: ['context-card', ...(!context && !note ? ['unrendered'] : [])],
      locationHTML: lineLinkHTML(entries[0][0], entries[entries.length - 1][0]),
      sourceHTML: sourceFoldHTML(entries, 'context'),
      bodyHTML: `${namespaceHTML(namespace)}
      ${context ? fieldHTML('Context', context[2]) : note ? `<div class="def-body">${docHTML(note)}</div>` : '<p class="small muted">This content needs its Lean source to be read accurately; it has not been omitted.</p>'}
      ${sensitive.length ? `<p class="card-notice">Source contains ${sensitive.map(esc).join(', ')}; inspect its assumptions or unfinished construction.</p>` : ''}
      `,
    });
  }

  const charLiteralLength = (text, at) => text[at] === "'" && !/[\p{L}\p{N}_']/u.test(text[at - 1] || '') ? (/^'(?:\\[^'\n]+|[^'\\\n])'/u.exec(text.slice(at))?.[0].length || 0) : 0;

  // Remove comments only outside strings and quoted identifiers. Keeping
  // whitespace and offsets inside literals matters to the displayed meaning.
  function presentationCode(text) {
    let out = '', quoted = '', block = 0;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i], pair = text.slice(i, i + 2);
      if (quoted) {
        out += ch;
        if (quoted === '"' && ch === '\\') { out += text[++i] || ''; continue; }
        if (ch === quoted) quoted = '';
      } else if (block) {
        if (pair === '/-') { block++; i++; }
        else if (pair === '-/') { block--; i++; }
        else if (ch === '\n') out += '\n';
      } else if (charLiteralLength(text, i)) { const length = charLiteralLength(text, i); out += text.slice(i, i + length); i += length - 1; }
      else if (pair === '/-') { block++; i++; out += ' '; }
      else if (pair === '--') { while (i < text.length && text[i] !== '\n') i++; out += '\n'; }
      else { out += ch; if (ch === '"' || ch === '«') quoted = ch === '«' ? '»' : '"'; }
    }
    return out;
  }

  // Find a declaration's outer value delimiter, ignoring brackets and markers
  // inside quoted text. This is presentation only, not a Lean parser.
  function declarationParts(command) {
    const text = presentationCode(leanOf(command.body));
    let depth = 0, end = text.length, quoted = '';
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (quoted === '"' && ch === '\\') { i++; continue; }
        if (ch === quoted) quoted = '';
        continue;
      }
      if (charLiteralLength(text, i)) { i += charLiteralLength(text, i) - 1; continue; }
      if (ch === '"' || ch === '«') { quoted = ch === '«' ? '»' : '"'; continue; }
      if ('([{⟨'.includes(ch)) depth++;
      else if (')]}⟩'.includes(ch)) depth--;
      else if (depth === 0 && (text.startsWith(':=', i) || /^\n\s*\|/.test(text.slice(i, i + 40)) || (/\s/.test(text[i - 1] || ' ') && /^where(\s|$)/.test(text.slice(i, i + 6))))) { end = i; break; }
    }
    const tail = text.slice(end).trim();
    return {head: text.slice(0, end).trim(), value: tail.replace(/^(?::=|where)\s*/, ''), label: tail.startsWith('where') ? 'Fields' : tail.startsWith('|') ? 'Constructors' : 'Value'};
  }

  // A docstring is not a prose cell: paragraphs, code fences and inline code
  // only, so it adds nothing to the outline and drops no line.
  function docHTML(text) {
    const blocks = [];
    let paragraph = [];
    let fence = null;
    const inline = inlineHTML;
    const flush = () => { if (paragraph.length) blocks.push(`<p>${inline(paragraph.join(' '))}</p>`); paragraph = []; };
    for (const raw of text.split('\n')) {
      if (/^\s*```/.test(raw)) {
        if (fence) { blocks.push(`<pre>${esc(fence.join('\n'))}</pre>`); fence = null; } else { flush(); fence = []; }
        continue;
      }
      if (fence) { fence.push(raw); continue; }
      if (raw.trim()) paragraph.push(raw.trim()); else flush();
    }
    if (fence) blocks.push(`<pre>${esc(fence.join('\n'))}</pre>`);
    flush();
    return blocks.join('');
  }

  function definitionHTML(command, entries, namespace = '') {
    const match = DECLARATION.exec(command.body[0][1]);
    const [, keyword, name] = match;
    const presentation = window.glitnirCards.declarations[keyword.replace(/\s+/g, ' ')];
    const doc = leanOf(command.doc).trim().replace(/^\/--/, '').replace(/-\/$/, '').trim();
    const parts = declarationParts(command);
    const type = parts.head.slice(match[0].length).trim().replace(/^:\s*/, '');
    const text = leanOf(entries);
    const warnings = [];
    if (/\b(sorry|admit)\b/.test(text)) warnings.push('Unfinished: contains a proof placeholder.');
    if (keyword === 'axiom') warnings.push('Assumed without a proof.');
    if (/\b(unsafe|implemented_by|extern|native_decide)\b/.test(text)) warnings.push('Uses an unsafe, external or native implementation; inspect the source.');
    const proof = ['example', 'theorem', 'lemma'].includes(keyword) || /^(by\b|sorry\b|admit\b)/.test(parts.value);
    const notes = command.notes.filter(([, line]) => line.trim());
    const context = [match[0].slice(0, match[0].indexOf(keyword)).trim(), ...notes.filter(([, line]) => LEADS.test(line)).map(([, line]) => line)].filter(Boolean).join('\n');
    const comments = leanOf(notes.filter(([, line]) => !LEADS.test(line))).replace(/^\s*-- ?/gm, '').replace(/\/--?|-\//g, '').trim();
    const compact = presentation.kind === 'definition';
    const signature = parts.head.slice(match[0].length).trim();
    const headerType = compact && signature.length <= 100 && !signature.includes('\n') ? signature : '';
    const shortDoc = compact && doc.length <= 100 && !doc.includes('\n') && !doc.includes('```') ? doc : '';
    const details = compact ? `${!headerType ? fieldHTML('Type and parameters', type) : ''}
      ${doc && !shortDoc ? `<div class="def-body">${docHTML(doc)}</div>` : ''}
      ${comments ? `<div class="def-body">${docHTML(comments)}</div>` : ''}${fieldHTML('Declaration context', context)}`.trim() : '';
    const value = proof ? '<p class="small muted">Construction in Lean source.</p>'
      : parts.label === 'Value' ? `<div class="formal-expression">${expressionHTML(parts.value)}</div>` : fieldHTML(parts.label, parts.value);
    const mathematical = compact ? value
      : keyword === 'example' ? `<div class="formal-expression">${expressionHTML(type)}</div>`
      : fieldHTML(['theorem', 'lemma', 'axiom'].includes(keyword) ? 'Statement' : 'Type and parameters', type)
        + (proof ? '' : fieldHTML(/inductive/.test(keyword) ? 'Constructors' : parts.label, parts.value));
    return window.glitnirCards.render({
      kind: presentation.kind, label: presentation.label, name,
      signature: headerType, descriptionHTML: shortDoc ? inlineHTML(shortDoc) : '',
      classes: ['definition', ...(compact ? ['compact-definition'] : [])],
      locationHTML: lineLinkHTML(entries[0][0], entries[entries.length - 1][0]),
      sourceHTML: sourceFoldHTML(entries, 'definition', ['example', 'theorem', 'lemma'].includes(keyword) ? 'Lean source & proof' : 'Lean source')
        + (details ? `<details class="def-lean definition-details" data-fold-key="definition-details:${esc(leanOf(entries))}"><summary>Details</summary>${details}</details>` : ''),
      bodyHTML: `${namespaceHTML(namespace)}
      ${!compact && doc ? `<div class="def-body">${docHTML(doc)}</div>` : ''}
      ${warnings.map(warning => `<p class="card-notice">${esc(warning)}</p>`).join('')}
      ${mathematical}
      ${!compact && comments ? `<div class="def-body">${docHTML(comments)}</div>` : ''}${!compact ? fieldHTML('Declaration context', context) : ''}`,
    });
  }

  function noteFor(c) {
    const record = c.record;
    switch (c.status) {
      case 'checked': return ['green', 'check', `Checked · context ${c.context.slice(0, 8)} is current.`];
      case 'refuted': return ['red', 'x', `Refuted · Lean checked a proof of its negation in context ${c.context.slice(0, 8)}; the claim is false as stated.`];
      case 'conditional': return c.reason.startsWith('refuted assuming ')
        ? ['amber', 'branch', `Its negation checks, but only assuming ${esc(c.reason.slice('refuted assuming '.length))}.`]
        : ['amber', 'branch', `The proof checks, but it ${esc(c.reason)}.`];
      case 'stale': return ['amber', 'branch', record ? `Out of date: ${esc(c.reason)}.` : 'Not checked yet.'];
      case 'failed': return ['red', 'x', esc(c.reason)];
      case 'unknown': return ['amber', 'x', `Lean could not decide: ${esc(c.reason)}.`];
      default: return null;
    }
  }

  function claimHTML(cell, c, staticPage = false, namespace = '') {
    const st = statusOf(c, staticPage);
    const failure = UI.data.check_errors && UI.data.check_errors[c.name];
    const note = !staticPage && checking(c.name) ? null : failure ? ['red', 'x', `The last check could not run: ${esc(failure)}`] : noteFor(c);
    const uses = c.uses.length ? `<div class="uses">uses ${c.uses.map(useHTML).join('')}</div>` : '';
    return `<section class="cell" id="cell-${esc(c.name)}" data-claim="${esc(c.name)}">
      <article class="claim card-proposition" data-tone="${st.tone}" data-selected="${UI.selected === c.name}" aria-label="${esc(c.keyword)} ${esc(c.name)}">
        ${window.glitnirCards.header({label: window.glitnirCards.declarations[c.keyword]?.label || c.keyword, name: c.name, classes: ['claim-head'], locationHTML: lineLinkHTML(cell.start, cell.end), badgesHTML: `${!staticPage && checking(c.name) ? '<span class="checking-badge">Checking in Lean</span>' : ''}<span class="pill" data-tone="${st.tone}">${st.label}</span>`})}
        ${namespaceHTML(namespace)}
        ${c.doc ? `<div class="doc-line">${inlineHTML(c.doc)}</div>` : ''}
        <div class="claim-statement">${fieldHTML('Statement', c.statement.replace(/^:\s*/, ''))}</div>
        ${uses}
        ${/\b(sorry|admit)\b/.test(presentationCode(c.proof)) ? '<p class="card-notice claim-notice">Unfinished: the statement’s proof contains a placeholder.</p>' : ''}
        <div class="card-source">${sourceFoldHTML(cell.text.split('\n').map((text, i) => [cell.start + i, text]), `claim:${c.name}`, 'Lean source & proof')}${c.refutation ? `<details class="def-lean" data-fold-key="refutation:${esc(c.name)}:${esc(c.refutation.script.join('\n'))}"><summary>Recorded refutation</summary>${refutationHTML(c)}</details>` : ''}</div>
        ${note ? `<div class="note-line" data-tone="${note[0]}">${icon(note[1])}<span>${note[2]}</span></div>` : ''}

        <div class="claim-actions"><span class="small muted">${checking(c.name) ? 'Fresh sandboxed check with the independent axiom check' : c.proof.trim() === 'sorry' ? 'No proof yet' : ''}</span></div>
      </article></section>`;
  }

  // Provider views listed for this project source cell, in line order.
  function projectViewsHTML(cell, placed = new Set()) {
    return (UI.data.views || []).filter(v => v.cell === cell.index && !placed.has(v.line)).sort((a, b) => a.line - b.line).map(v => viewHTML(cell, v.line)).join('');
  }

  function cellView(cell, staticPage = false, scopes = new Map()) {
    if (cell.kind === 'prose') {
      const html = markdown(cell.markdown, `notebook-heading-${cell.index}`);
      return html ? `<section class="cell prose">${html}</section>` : '<section class="cell prose"><p class="muted">(empty prose)</p></section>';
    }
    // Blank lines between cells stay in the data for exact line ranges but are not shown.
    if (cell.kind === 'source' && !cell.text.trim()) return '';
    if (cell.kind === 'source' && cell.claim) {
      const saved = (UI.data.project_claims || []).find(c => c.name === cell.claim);
      if (saved) return projectClaimHTML(cell, saved, scopes.get(cell.index), staticPage) + sourceDirectivesHTML(cell) + projectViewsHTML(cell) + projectCardsHTML(cell, staticPage);
    }
    if (cell.kind === 'source') {
      const {html, placed} = projectSourceHTML(cell, scopes.get(cell.index), staticPage);
      const notes = nonClaimsHTML(cell) + sourceDirectivesHTML(cell, placed);
      return html + (notes ? `<div class="cell project-notes">${notes}</div>` : '') + projectViewsHTML(cell, placed) + projectCardsHTML(cell, staticPage, placed);
    }
    if (cell.kind === 'lean') return leanCellHTML(cell, scopes.get(cell.index), staticPage);
    const c = claim(cell.id);
    return c ? claimHTML(cell, c, staticPage, scopes.get(cell.index)?.get(cell.start)?.namespace) : '';
  }

  // Section structure belongs to the reading view, never to Lean checking.
  // Split prose at headings so multiple sections in one comment keep their
  // source-cell identity while their following claim/definition cells can nest.
  function proofSections(root, staticPage = false) {
    if (!root.querySelector('[data-section-fold]')) return;
    const parts = [];
    for (const node of [...root.children]) {
      const prose = node.matches('.prose') ? node : node.querySelector(':scope > .prose');
      if (!prose?.querySelector('[data-heading]')) { parts.push(node); continue; }
      let fragment, body;
      for (const child of [...prose.children]) {
        if (!body || child.hasAttribute('data-heading')) {
          body = prose.cloneNode(false);
          fragment = node === prose ? body : node.cloneNode(false);
          if (node !== prose) {
            const tools = node.querySelector(':scope > .cell-tools');
            if (tools) fragment.append(tools.cloneNode(true));
            fragment.append(body);
          }
          parts.push(fragment);
        }
        body.append(child);
      }
    }
    root.replaceChildren();
    const stack = [];
    for (const part of parts) {
      const heading = part.querySelector('[data-heading]');
      if (heading) {
        const level = Number(heading.tagName.slice(1));
        while (stack.length && stack.at(-1).level >= level) stack.pop();
        if (heading.dataset.sectionFold) {
          const section = document.createElement('details');
          section.className = 'proof-section';
          section.dataset.foldKey = 'section:' + JSON.stringify([...stack.map(s => s.title), heading.dataset.heading]);
          section.open = staticPage;
          const summary = document.createElement('summary');
          // The heading still navigates to its original source comment.
          if (part.dataset.index !== undefined) { summary.className = 'cell cellwrap'; summary.dataset.index = part.dataset.index; }
          const first = heading.nextElementSibling;
          heading.classList.add('section-title');
          summary.append(heading);
          if (first?.tagName === 'P') {
            const abstract = document.createElement('span');
            abstract.className = 'section-summary';
            abstract.append(...first.childNodes);
            summary.append(abstract);
            first.remove();
          }
          const body = document.createElement('div');
          body.className = 'proof-section-body';
          const tools = part.querySelector(':scope > .cell-tools');
          if (tools) { tools.className = 'section-tools'; summary.append(tools); }
          section.append(summary, body);
          (stack.at(-1)?.body || root).append(section);
          stack.push({level, title: heading.dataset.heading, body});
        }
      }
      const prose = part.matches('.prose') ? part : part.querySelector(':scope > .prose');
      if (!prose || prose.children.length) (stack.at(-1)?.body || root).append(part);
    }
    const fragments = new Map();
    for (const wrap of root.querySelectorAll('.cellwrap')) {
      const index = wrap.dataset.index;
      const fragment = fragments.get(index) || 0;
      wrap.dataset.fragment = fragment;
      fragments.set(index, fragment + 1);
    }
    for (const section of root.querySelectorAll('details.proof-section')) {
      const counts = new Map();
      for (const card of section.querySelectorAll('[data-claim]')) {
        const c = claim(card.dataset.claim);
        if (!c) continue;
        for (const status of summariesOf(c, staticPage)) {
          const count = counts.get(status.key);
          counts.set(status.key, {...status, count: (count?.count || 0) + 1});
        }
      }
      const badges = document.createElement('span');
      badges.className = 'section-statuses';
      badges.setAttribute('aria-label', 'Claim observations in this section, including subsections');
      badges.innerHTML = counts.size ? [...counts.values()].map(s => `<span class="pill" data-tone="${esc(s.tone)}">${s.count} ${esc(s.label)}</span>`).join(' ') : '<span class="muted">No claim observations</span>';
      section.querySelector(':scope > summary').append(badges);
    }
  }

  function revealSection(target) {
    for (let node = target; node; node = node.parentElement) {
      if (node.matches('details.proof-section')) node.open = true;
    }
  }

  function openSource(index) {
    const cell = UI.data && UI.data.cells.find(c => c.index === index);
    if (cell && HOST) HOST.openSource({index: cell.index, sha256: UI.data.sha256});
  }
  if (HOST) {
    HOST.openSelectedSource = () => {
      if (linkedPath()) { UI.notice = `${linkedPath()} is read-only here. Open it in its own preview to edit or check it.`; render(); return; }
      const cells = UI.data ? UI.data.cells : [];
      const cell = cells.find(c => UI.sourceCell && UI.sourceCell.sha256 === UI.data.sha256 && c.index === UI.sourceCell.index)
        || cells.find(c => c.id === UI.selected || (c.claim && c.claim === UI.selected)) || cells[0];
      if (cell) openSource(cell.index); else HOST.openSource({});
    };
    document.addEventListener('click', event => {
      const wrap = event.target.closest('.cellwrap');
      if (wrap) UI.sourceCell = {index: Number(wrap.dataset.index), sha256: UI.data.sha256};
    }, true);
    document.addEventListener('glitnir-reveal-cell', async event => {
      HOST.pendingReveal = null;
      // A reveal names a place in the previewed notebook.
      if (linkedPath()) showNotebookAt(0);
      if (!UI.data || UI.data.sha256 !== event.detail.sha256) await refresh();
      if (!UI.data || UI.data.sha256 !== event.detail.sha256) return;
      UI.mode = 'doc';
      UI.sourceCell = {index: event.detail.index, sha256: event.detail.sha256};
      const cell = UI.data.cells.find(c => c.index === UI.sourceCell.index);
      if (!cell) return;
      if (cell.kind === 'claim') UI.selected = cell.id;
      else if (cell.claim && claim(cell.claim)) UI.selected = cell.claim;
      render();
      const target = document.querySelector(`.cellwrap[data-index="${cell.index}"]`);
      if (target) { revealSection(target); target.classList.add('source-selected'); target.scrollIntoView({block: 'center'}); }
      else showSourceLines(cell.start, cell.end);
    });
    if (HOST.pendingReveal) document.dispatchEvent(new CustomEvent('glitnir-reveal-cell', {detail: HOST.pendingReveal}));
  }

  function toolsHTML(cell) {
    return `<div class="cell-tools">${lineLinkHTML(cell.start, cell.end)}</div>`;
  }

  function renderDoc() {
    const scopes = namespaceScopes(UI.data.cells);
    $('doc').innerHTML = withheldBannerHTML(UI.data) + UI.data.cells.map(cell => {
      const body = cellView(cell, false, scopes);
      return body ? `<div class="cellwrap" data-index="${cell.index}">${toolsHTML(cell)}${body}</div>` : '';
    }).join('');
    proofSections($('doc'));
    if (HOST) for (const wrap of $('doc').querySelectorAll('.cellwrap')) {
      if (wrap.querySelector('.cell .source-controls')) wrap.querySelector(':scope > .cell-tools')?.remove();
    }
  }

  function renderSource() {
    if (UI.sourceRange?.sha !== UI.data.sha256) UI.sourceRange = null;
    const range = UI.sourceRange;
    const rows = [];
    UI.data.cells.forEach(cell => {
      const c = cell.kind === 'claim' ? claim(cell.id) : cell.claim ? claim(cell.claim) : null;
      cell.text.split('\n').slice(0, cell.end - cell.start + 1).forEach((line, i) => rows.push({n: cell.start + i, line, c}));
    });
    const back = `<button type="button" class="secondary" data-act="document-return" data-sha="${esc(UI.data.sha256)}">← Back to Document</button>`;
    $('source').innerHTML = `${range ? '' : `<p>${back}</p>`}<p class="source-note">${esc(UI.data.file)} as it is on disk ${UI.data.preview?.mode === 'project' ? '· source preview; the gutter marks saved results · use Check saved file to check it' : '· the gutter shows each claim\'s status'} · click a line number to show its place in Document.</p>
      <div class="source"><table>${rows.map(r => `${range && r.n === range.start ? `<tr class="source-return"><td colspan="3">${back}</td></tr>` : ''}<tr id="source-L${r.n}" tabindex="-1" data-source-line="${r.n}" data-line-target="${Boolean(range && r.n >= range.start && r.n <= range.end)}" ${r.c ? `data-claim="${esc(r.c.name)}" data-tone="${toneOf(r.c)}" data-selected="${UI.selected === r.c.name}"` : ''}><td class="gut"></td><td class="ln"><a href="#doc" data-act="document-line" data-line="${r.n}" data-sha="${esc(UI.data.sha256)}" aria-label="Show line ${r.n} in Document">${r.n}</a></td><td>${leanHTML(r.line) || ' '}</td></tr>`).join('')}</table></div>`;
  }

  // ----- inspector ------------------------------------------------------------------
  function refutationHTML(c) {
    return c.refutation ? `<div class="insp-section"><h5>Refutation${c.refutation.context === c.context ? '' : ' · old context'}</h5><p class="small">A proof of <code>¬ ${esc(c.name)}</code>, recorded beside the notebook; last check ${esc(c.refutation.verdict)}.</p><pre>${esc(':= by\n  ' + c.refutation.script.join('\n  '))}</pre></div>` : '';
  }

  // A saved project claim: its record's status, currency and assumptions by
  // module, and the docstring of its exact source.
  function projectInspectorHTML(c) {
    const saved = c.project;
    const st = statusOf(c);
    const cell = UI.data.cells.find(cell => cell.claim === c.name);
    const reading = cell ? claimReading(trimmed(cellEntries(cell))) : null;
    const modules = new Map();
    for (const axiom of saved.axioms) modules.set(axiom.module, [...(modules.get(axiom.module) || []), axiom.name]);
    const statement = saved.example === true ? checkedStatementHTML(saved, true) : '';
    const checked = statement ? `<div class="insp-section">${statement}</div>` : '';
    return `<div class="focus"><div class="row"><span class="pill" data-tone="${st.tone}">${esc(st.label)}</span>${currencyPillHTML(saved)}</div>${reading?.doc ? `<div class="eq">${docHTML(reading.doc)}</div>` : ''}
        <dl><dt>name</dt><dd>${esc(saved.example === true ? c.label : c.name)}</dd><dt>lines</dt><dd>${cell ? lineLinkHTML(saved.start_line, saved.end_line) : '—'}</dd><dt>result</dt><dd>${currencyText(saved)}</dd><dt>uses</dt><dd class="insp-uses">${!Array.isArray(saved.uses) ? '<span class="muted">not recorded by this check</span>' : saved.uses.length ? `<span class="uses">${useChipsHTML(saved)}</span>` : '—'}</dd></dl></div>
      ${checked}
      ${linkedPath() ? '' : `<div class="insp-section insp-check">${checkThroughHTML(saved, 'Check through this claim', `<kbd>r</kbd> · compiles and kernel-replays lines 1–${esc(saved.end_line)} of the saved file; later claims are not rechecked. <b>Check saved file</b> checks all of it.`)}</div>`}
      <div class="insp-section"><h5>Assumptions from the last check</h5>${modules.size ? [...modules].map(([module, names]) => `<p class="small muted axiom-module">${esc(module)}</p><div class="axioms">${names.map(name => `<code>${esc(name)}</code>`).join('')}</div>`).join('') : '<p class="small muted">None.</p>'}</div>
      ${goalSectionHTML(c)}`;
  }

  // ----- proof goals ------------------------------------------------------------------
  // The goals Lean shows at each tactic step of a claim's saved proof, from the
  // same observation as Proof steps: one runs at a time and records the whole
  // saved file, so one observation serves every claim. It is historical: it
  // never changes a status, and a source or dependency change makes it stale.
  let goalTimer, goalEpoch = 0;
  const MAX_GOAL_STEPS = 200;
  const traceRunning = trace => trace?.status === 'running';
  const traceFinished = trace => ['ready', 'partial'].includes(trace?.status);
  // The dependency epoch in which this page first saw each observation. An
  // observation seen only before a dependency change describes the old state.
  const traceSeen = new Map();
  async function readTrace() {
    const response = await fetch('/api/proof/trace', {cache: 'no-store', headers: API});
    const answer = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(answer.error || `the server answered ${response.status}`);
    // First seen after a dependency change: when it was recorded is unknown.
    if (answer.id && !traceSeen.has(answer.id)) traceSeen.set(answer.id, environmentEpoch === 0 ? 0 : -1);
    return answer;
  }
  // A finished observation of this saved text is reused only while no
  // dependency changed since this page first saw it, or since it started it.
  // The server and this page start together, so one first seen before any
  // change was recorded under the current dependencies.
  const reusable = (trace, sha256) => traceFinished(trace) && trace.source_sha256 === sha256 && trace.id && traceSeen.get(trace.id) === environmentEpoch;
  async function inspectGoals(refresh = false) {
    if (!UI.data || linkedPath() || !projectMode() || UI.goals?.starting || traceRunning(UI.goals?.trace)) return;
    const sha256 = UI.data.sha256, epoch = ++goalEpoch, environment = environmentEpoch;
    clearTimeout(goalTimer);
    UI.goals = {starting: true, environment, trace: {status: 'running', source_sha256: sha256, message: 'Preparing proof goals', nodes: []}};
    render();
    try {
      // Reuse a finished observation of this saved text, or join a running
      // one (for example one Proof steps started); otherwise start one. Only
      // an observation started here is cancelled from here.
      let trace = await readTrace();
      let owned = false;
      const joined = traceRunning(trace) && trace.source_sha256 === sha256 && trace.id;
      if (!joined && (refresh || !reusable(trace, sha256))) {
        const response = await fetch('/api/proof/start', {method: 'POST', headers: {...API, 'Content-Type': 'application/json'}, body: JSON.stringify({sha256})});
        trace = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(trace.error || `the server answered ${response.status}`);
        if (trace.id) traceSeen.set(trace.id, environmentEpoch);
        owned = true;
      }
      if (epoch !== goalEpoch) return;
      const cancel = UI.goals.cancel;
      UI.goals = {environment, trace, owned};
      render();
      if (cancel && traceRunning(trace)) { cancelGoals(); if (!owned) return; }
      if (traceRunning(trace)) goalTimer = setTimeout(() => pollGoals(trace.id, epoch), 750);
    } catch (error) {
      if (epoch !== goalEpoch) return;
      UI.goals = {environment, trace: {status: 'unavailable', source_sha256: sha256, message: error.message || String(error), nodes: []}};
      render();
    }
  }
  async function pollGoals(id, epoch) {
    try {
      const trace = await readTrace();
      if (epoch !== goalEpoch) return;
      if (trace.id !== id) throw new Error('Another proof observation replaced this one. Inspect the goals again.');
      UI.goals = {...UI.goals, trace};
      render();
      if (traceRunning(trace)) goalTimer = setTimeout(() => pollGoals(id, epoch), 750);
    } catch (error) {
      if (epoch !== goalEpoch) return;
      UI.goals = {...UI.goals, trace: {status: 'unavailable', source_sha256: UI.goals.trace.source_sha256, message: error.message || String(error), nodes: []}};
      render();
    }
  }
  async function cancelGoals() {
    if (!traceRunning(UI.goals?.trace)) return;
    // Asked before the observation started: cancel it once it has.
    if (UI.goals.starting) { UI.goals = {...UI.goals, cancel: true, trace: {...UI.goals.trace, message: 'Cancelling'}}; render(); return; }
    if (!UI.goals.owned) {
      // Joined, not started here: stop following it and leave it running.
      goalEpoch++;
      clearTimeout(goalTimer);
      UI.goals = {...UI.goals, trace: {status: 'detached', source_sha256: UI.goals.trace.source_sha256, nodes: []}};
      render();
      return;
    }
    UI.goals = {...UI.goals, trace: {...UI.goals.trace, message: 'Cancelling'}};
    render();
    // Cancelling only stops work, so unsaved edits never hold it back.
    await fetch('/api/proof/cancel', {method: 'POST', headers: {...API, 'Content-Type': 'application/json'}, body: '{}'}).catch(() => null);
  }
  const goalBlocksHTML = goals => goals.length
    ? goals.map(goal => `<pre class="goal goal-block">${esc(goal)}</pre>`).join('')
    : '<pre class="goal" data-tone="green">No goals remain in this tactic’s scope.</pre>';
  function goalSectionHTML(c) {
    const head = '<h5>Proof goals</h5>';
    if (linkedPath()) return `<div class="insp-section goal-section">${head}<p class="small muted">Open this notebook in its own preview to inspect its proof goals.</p></div>`;
    const goals = UI.goals, trace = goals?.trace;
    const button = label => `<button type="button" class="secondary" data-act="goal" data-id="${esc(c.name)}"${label.startsWith('Refresh') ? ' data-refresh="true"' : ''}>${label}</button>`;
    const note = (tone, text) => `<div class="note-line goal-note" data-tone="${tone}">${icon(tone === 'red' ? 'x' : 'branch')}<span>${text}</span></div>`;
    const historical = '<p class="small muted goal-caption">A historical observation of the saved proof: it never changes a status.</p>';
    let body;
    if (!trace) body = `${button('Inspect proof goals')}<p class="small muted goal-caption">Shows the goals Lean reports at each tactic step of this claim’s saved proof. A historical observation: it never changes a status.</p>`;
    else if (traceRunning(trace)) body = `<p class="small muted goal-state" role="status">Asking Lean… ${esc(trace.message || '')}</p><button type="button" class="secondary" data-act="goal-cancel">Cancel</button>${historical}`;
    else if (trace.source_sha256 !== UI.data.sha256) body = note('amber', 'The saved source changed since these goals were recorded.') + button('Refresh proof goals');
    else if (goals.environment !== environmentEpoch) body = note('amber', 'The source or its dependencies changed since these goals were recorded.') + button('Refresh proof goals');
    else if (trace.status === 'detached') body = note('muted', 'Stopped following the proof observation Proof steps started; it keeps running there.') + button('Inspect proof goals');
    else if (trace.status === 'cancelled') body = note('muted', 'The proof observation was cancelled. No goals were recorded.') + button('Inspect proof goals');
    else if (!['ready', 'partial'].includes(trace.status)) body = note('red', `No proof goals: ${esc(trace.message || trace.status)}`) + button('Inspect proof goals');
    else body = goalStepsHTML(c, trace) + button('Refresh proof goals');
    return `<div class="insp-section goal-section">${head}${body}</div>`;
  }
  function goalStepsHTML(c, trace) {
    const {start_line: start, end_line: end} = c.project;
    const byId = new Map(trace.nodes.map(n => [n.id, n]));
    const nodes = trace.nodes.filter(n => n.range.start.line >= start && n.range.start.line <= end);
    const messages = [...(trace.errors || []), ...(trace.warnings || [])];
    const messagesHTML = messages.length ? `<details class="goal-messages" data-fold-key="goal-messages"><summary class="small muted">Lean messages for the file · ${messages.length}</summary><pre>${esc(messages.join('\n\n'))}</pre></details>` : '';
    const provenance = `<p class="small muted goal-caption">Recorded from source ${esc(String(trace.source_sha256).slice(0, 8))}${trace.environment?.toolchain ? ` with ${esc(trace.environment.toolchain)}` : ''}. Dependencies were read-only live inputs. Recorded elaboration attempts, not a proof certificate: this never changes a status.</p>`;
    if (!nodes.length) {
      return `<div class="note-line goal-note" data-tone="muted">${icon('branch')}<span>No tactic steps were recorded for this claim. It may be a term proof${trace.status === 'partial' ? ', or Lean stopped before it' : ''}.</span></div>${messagesHTML}${provenance}`;
    }
    const depth = n => { let d = 0; for (let p = n.parent; p !== null && p !== undefined && d < 6; p = byId.get(p)?.parent) d++; return d; };
    const shown = nodes.slice(0, MAX_GOAL_STEPS);
    const steps = shown.map((n, i) => {
      const lines = n.text.split('\n');
      const tactic = lines[0] + (lines.length > 1 ? ' …' : '');
      return `<li class="goal-step" data-depth="${depth(n)}"><div class="goal-tactic"><span class="goal-number">${i + 1}</span><code title="${esc(n.text)}">${esc(tactic)}</code><span class="small muted">L${esc(n.range.start.line)}</span></div>${goalBlocksHTML(n.after)}</li>`;
    }).join('');
    return (trace.status === 'partial' ? `<div class="note-line goal-note" data-tone="amber">${icon('branch')}<span>Lean reported errors in the file; some steps may be missing.</span></div>` : '')
      + `<div class="goal-initial"><div class="field-label">Initial goal</div>${goalBlocksHTML(nodes[0].before)}</div>`
      + `<ol class="goal-steps" aria-label="Goals after each tactic step">${steps}</ol>`
      + (nodes.length > shown.length ? `<p class="small muted">Showing the first ${shown.length} of ${nodes.length} steps. Proof steps shows them all.</p>` : '')
      + messagesHTML + provenance;
  }

  function renderInspector() {
    const c = claim(UI.selected);
    if (!c) {
      const project = projectMode();
      $('insp-title').textContent = project && UI.data.cells.length ? 'No saved results' : 'No claims';
      $('insp-sub').textContent = '';
      $('insp').innerHTML = project
        ? '<p class="small muted">No claim of this file has a saved result. Check the saved file to see each claim\'s status here.</p>'
        : '<p class="small muted">This notebook has no theorems yet.</p>';
      return;
    }
    if (c.project) {
      $('insp-title').textContent = c.label;
      $('insp-sub').textContent = `${c.keyword || 'claim'} · ${statusOf(c).label.toLowerCase()}${c.project.current ? '' : ` · ${currencyText(c.project)}`}`;
      $('insp').innerHTML = projectInspectorHTML(c);
      return;
    }
    const st = statusOf(c);
    const r = c.record;
    $('insp-title').textContent = c.name;
    $('insp-sub').textContent = `${c.keyword} · ${st.label.toLowerCase()}`;
    $('insp').innerHTML = `
      <div class="focus"><div class="row"><span class="pill" data-tone="${st.tone}">${st.label}</span></div>${c.doc ? `<div class="eq">${inlineHTML(c.doc)}</div>` : ''}
        <dl><dt>context</dt><dd>${c.context.slice(0, 12)}</dd><dt>last check</dt><dd>${r ? `${esc(r.verdict)}${r.context === c.context ? '' : ' · old context'}` : 'none'}</dd><dt>uses</dt><dd>${esc(c.uses.join(', ') || '—')}</dd></dl></div>
      ${c.reason ? `<div class="insp-section"><h5>Why</h5><p class="small">${esc(c.reason)}</p></div>` : ''}
      ${refutationHTML(c)}
      ${r && r.axioms.length ? `<div class="insp-section"><h5>Axioms from the last check</h5><div class="axioms">${r.axioms.map(a => `<code>${esc(a)}</code>`).join('')}</div></div>` : ''}
`;
  }


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
    const color = t => ({green: 'var(--green)', amber: 'var(--amber)', red: 'var(--red)'})[t] || 'var(--blue)';
    const arcs = cs.flatMap(c => c.uses.filter(u => pos.has(u) && u !== c.name).map(u => {
      const y1 = pos.get(u), y2 = pos.get(c.name), bend = x - 10 - (y2 - y1) / 5;
      return `<path d="M${x - 7} ${y1}C${bend} ${y1} ${bend} ${y2} ${x - 9} ${y2}" fill="none" stroke="var(--border)" stroke-width="1.5" marker-end="url(#g-arr)"/>`;
    })).join('');
    svg.innerHTML = `<defs><marker id="g-arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L8 4L0 8" fill="var(--faint)"/></marker></defs>${arcs}` +
      cs.map(c => {
        const tone = toneOf(c), y = pos.get(c.name), sel = UI.selected === c.name;
        return `<g class="node" data-act="goto" data-id="${esc(c.name)}"><title>${esc(claimLabel(c))}: ${esc(statusOf(c).label)}${c.project && c.project.current !== true ? ` · ${currencyText(c.project)}` : ''}</title><rect x="0" y="${y - 12}" width="${W}" height="24" fill="transparent"/><circle cx="${x}" cy="${y}" r="${sel ? 6.5 : 5.5}" fill="${tone === 'blue' ? 'var(--paper)' : color(tone)}" stroke="${color(tone)}" stroke-width="${sel ? 3 : 1.8}"/><text x="${x + 13}" y="${y + 3.5}" font-size="10.5" font-family="var(--mono)" fill="${sel ? 'var(--ink)' : 'var(--muted)'}">${esc(claimLabel(c))}</text></g>`;
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
    $('title').textContent = title();
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
    const bits = project
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
    $('check-project').disabled = checkBusy();
    $('project-check').hidden = !project || linked || !d.preview.check;
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
      const notice = linked ? `Read-only view of ${d.file} with its own saved results${d.preview.linked?.compared === false ? ', not compared with this project’s environment because it belongs to another Lake project' : ''}. Nothing is checked, run or drawn for it here; open it in its own preview to check it.` : d.preview.notice;
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
        return c ? [`<li><button type="button" data-act="goto" data-id="${esc(c.name)}" aria-current="${UI.selected === c.name}" title="${esc(c.name)}"><span class="o-claim">${breakableName(claimLabel(c))}</span><span class="dot" data-tone="${toneOf(c)}" title="${esc(statusOf(c).label)}${c.project.current ? '' : ` · ${currencyText(c.project)}`}"></span></button></li>`] : [];
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
    const linkFocus = document.activeElement?.matches('#doc .source-link, #doc .source-edit, #doc .notebook-reference') ? {index: [...$('doc').querySelectorAll('.source-link,.source-edit,.notebook-reference')].indexOf(document.activeElement), sha: document.activeElement.dataset.sha, href: document.activeElement.getAttribute('href')} : null;
    const sourceControl = UI.mode === 'source' && document.activeElement?.matches('#source [data-act]') ? {...document.activeElement.dataset} : null;
    const sourceFocus = UI.mode === 'source' && document.activeElement?.hasAttribute('data-source-line') ? document.activeElement.id : null;
    const scroll = $('insp').scrollTop;
    const goalFocus = Boolean(document.activeElement?.matches('#insp .goal-section [data-act]'));
    if (!heldRendering) { if (UI.mode === 'doc') renderDoc(); else renderSource(); }
    renderInspector();
    renderChrome();
    typesetMath($('doc'), $('insp'));
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
    if (linkFocus && UI.mode === 'doc' && linkFocus.sha === UI.data.sha256) {
      const link = $('doc').querySelectorAll('.source-link,.source-edit,.notebook-reference')[linkFocus.index];
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
      const exportTitle = title();
      const content = document.createElement('div');
      const scopes = namespaceScopes(snapshot.cells);
      content.innerHTML = withheldBannerHTML(snapshot) + snapshot.cells.map(cell => cellView(cell, true, scopes)).join('');
      proofSections(content, true);
      if (snapshot.preview?.check?.status === 'checked_snapshot') content.insertAdjacentHTML('beforeend', projectCheckHTML(snapshot.preview.check, snapshot.sha256));
      if (content.querySelector('.view-wait')) throw new Error('Wait for the diagrams to finish drawing, then export again.');
      if (content.querySelector('.image-wait')) throw new Error('An image is still loading. Export again in a moment.');
      const defs = document.querySelector('svg.defs');
      if (defs) content.append(defs.cloneNode(true));
      typesetMath(content);
      const response = await fetch('/api/export/styles', {headers: API});
      if (!response.ok) throw new Error('The server could not supply export styles. Restart the notebook server after updating Glitnir.');
      const {css} = await response.json();
      const html = await window.glitnirExport({content, title: exportTitle, file: snapshot.file, sha256: snapshot.sha256, preview: snapshot.preview, css});
      if (window.glitnirHost) await window.glitnirHost.saveHTML(html);
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
  // from it, and the README's table must match it (a test compares them).
  // `editor` keys exist only in the VS Code preview, whose review layer
  // (review.js) handles n/p, c/t and e; `editorText` is their wording there.
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  const SHORTCUTS = [
    {group: 'Navigate', keys: ['j', 'k'], hint: 'claims', text: 'Next / previous claim'},
    {group: 'Navigate', keys: ['n', 'p'], hint: 'needs attention', editor: true,
      text: 'Next / previous claim that needs attention: out of date, not rechecked, contains sorry, additional assumptions, or where checking stopped. In Proof steps: next / previous step'},
    {group: 'Navigate', keys: ['c', 't'], hint: 'claims / proof steps', editor: true, text: 'Claims / Proof steps'},
    {group: 'View', keys: ['s'], hint: 'document / source', text: 'Switch between document and Lean source'},
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
    render();
    if (!scroll) return;
    const el = UI.mode === 'doc' ? $('cell-' + name) : document.querySelector(`#source tr[data-claim="${CSS.escape(name)}"]`);
    if (el) { revealSection(el); el.scrollIntoView({behavior: reduced() ? 'auto' : 'smooth', block: UI.mode === 'doc' ? 'start' : 'center'}); }
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
      case 'evidence-run': runEvidence(Number(b.dataset.line), b.dataset.sha); break;
      case 'view-cancel': cancelView(b); break;
      case 'inspect-cancel': cancelInspection(); break;
      case 'check-cancel': cancelCheck(); break;
      case 'view-retry': if (UI.drawings[b.dataset.key]?.error) { delete UI.drawings[b.dataset.key]; render(); } break;
      case 'decision-run': decisionAction('/api/decide/run', {sha256: b.dataset.sha, line: Number(b.dataset.line)}); break;
      case 'decision-stop': decisionAction('/api/decide/stop', {}); break;
      case 'decision-discard': decisionAction('/api/decide/discard', {sha256: b.dataset.sha, line: Number(b.dataset.line)}); break;
      case 'goto': closeDiagram(); select(id, true); break;
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
    if (e.target.closest('input,textarea,select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const map = {
      j: () => move(1), k: () => move(-1), '?': () => keys(true),
      s: () => { if (UI.mode === 'source') showDocument(); else { UI.mode = 'source'; render(); } },
      i: () => { UI.panel = !UI.panel; render(); },
      // Check through the selected claim; Check saved file checks all of it.
      r: () => { const c = claim(UI.selected); if (c?.project && !linkedPath()) checkProject(c.project.end_line); },
    };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });

  load();
})();
