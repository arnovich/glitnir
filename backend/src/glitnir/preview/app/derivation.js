// Explore renders the saved tactic observations as inference bars.
(() => {
  'use strict';
  const workbench = window.glitnirWorkbench;
  const {$, UI, claim, esc, goalBlocksHTML, goalSourceHTML, projectMode, proofGoalsHTML, sourceClaimRanges} = workbench;
  const SCOPED_WRAPPERS = new Set(['Lean.Parser.Tactic.case', 'Lean.Parser.Tactic.allGoals']);
  let position, renderedHTML;

  function exploreTabsHTML() {
    if (!projectMode() || UI.data.walkthrough || !sourceClaimRanges(UI.data.cells).size) return '';
    return `<nav class="explore-tabs" aria-label="Explore views">
      <button type="button" class="secondary" data-act="explore-graph" aria-pressed="${!UI.exploreDerivation}">Claim graph</button>
      <button type="button" class="secondary" data-act="explore-derivation" aria-pressed="${Boolean(UI.exploreDerivation)}">Derivation</button>
    </nav>`;
  }

  function transition(node) {
    const before = node.before_ids, after = node.after_ids;
    if (!before?.length || !after) return null;
    const prior = new Set(before), remaining = new Set(after);
    if (prior.size !== before.length || remaining.size !== after.length) return null;
    if (remaining.has(before[0]) || before.slice(1).some(id => !remaining.has(id))) return null;
    return {id: before[0], premises: after.flatMap((id, index) => prior.has(id) ? [] : [{id, text: node.after[index]}])};
  }

  const observedGoals = (texts, ids) => texts.map((text, index) => ({id: ids?.[index], text}));

  function applyFragment(pending, fragment) {
    if (!fragment || fragment.roots.some(goal => !pending.has(goal.id))) return false;
    const consumed = new Set(fragment.roots.map(goal => goal.id));
    if (fragment.outputs.some(goal => pending.has(goal.id) && !consumed.has(goal.id))) return false;
    for (const goal of fragment.roots) {
      Object.assign(pending.get(goal.id), {step: goal.step, premises: goal.premises});
      pending.delete(goal.id);
    }
    for (const goal of fragment.outputs) pending.set(goal.id, goal);
    return true;
  }

  function nestedFragment(node, children, fragments) {
    if (!node.before_ids?.length || !node.after_ids) return null;
    const goals = observedGoals(node.before, node.before_ids);
    const pending = new Map(goals.map(goal => [goal.id, goal]));
    if (pending.size !== goals.length || new Set(node.after_ids).size !== node.after_ids.length) return null;
    for (const child of children) if (!applyFragment(pending, fragments.get(child.id))) return null;
    // Child tactics run in their own focus. Untouched siblings remain in the
    // wrapper's pending set even when they are absent from a child's snapshot.
    if (pending.size !== node.after_ids.length || node.after_ids.some(id => !pending.has(id))) return null;
    const roots = goals.filter(goal => goal.step);
    if (!roots.length) return null;
    const unchanged = new Set(goals.filter(goal => !goal.step).map(goal => goal.id));
    return {roots, outputs: [...pending.values()].filter(goal => !unchanged.has(goal.id))};
  }

  function derivation(nodes) {
    const children = new Map();
    for (const node of nodes) {
      if (!children.has(node.parent)) children.set(node.parent, []);
      children.get(node.parent).push(node);
    }
    const fragments = new Map();
    const expanded = new Set();
    for (let index = nodes.length - 1; index >= 0; index--) {
      const node = nodes[index], nested = children.get(node.id);
      // Refine a wrapper only when all direct children can be replayed inside
      // its scope and the resulting goal IDs match its recorded exit.
      const refinement = nested && SCOPED_WRAPPERS.has(node.kind) && nestedFragment(node, nested, fragments);
      if (refinement) {
        fragments.set(node.id, refinement);
        expanded.add(index + 1);
        continue;
      }
      const change = transition(node);
      if (change) fragments.set(node.id, {
        roots: [{id: change.id, step: index + 1, premises: change.premises}],
        outputs: change.premises,
      });
    }

    const ids = new Set(nodes.map(node => node.id));
    const top = nodes.filter(node => !ids.has(node.parent));
    const roots = observedGoals(top[0].before, top[0].before_ids);
    const pending = new Map(roots.filter(goal => goal.id).map(goal => [goal.id, goal]));
    for (const node of top) {
      if (applyFragment(pending, fragments.get(node.id))) continue;
      // A known disappearance in an ambiguous step breaks the chain.
      if (node.before_ids && node.after_ids) {
        const after = new Set(node.after_ids);
        for (const id of node.before_ids) if (!after.has(id)) pending.delete(id);
      }
    }
    const linked = new Set(), queue = [...roots];
    while (queue.length) {
      const goal = queue.pop();
      if (!goal.step) continue;
      linked.add(goal.step);
      for (const premise of goal.premises) queue.push(premise);
    }
    const unlinked = nodes.flatMap((_, index) => {
      const step = index + 1;
      return linked.has(step) ? [] : [{step, note: expanded.has(step) ? 'Enclosing tactic' : 'Goal link unavailable'}];
    });
    return {roots, unlinked};
  }

  function target(text) {
    const marker = text.indexOf('\n⊢');
    const goal = marker < 0 ? text : text.slice(marker + 1);
    return goal.length > 240 ? goal.slice(0, 240) + '…' : goal;
  }

  function treeHTML(roots, button) {
    const output = [], stack = roots.slice().reverse(), continuations = [];
    while (stack.length || continuations.length) {
      if (!stack.length) {
        const part = continuations.shift();
        output.push(`<section class="derivation-continuation" aria-label="Continued branch ${part.number}"><p class="small muted">Continued branch ${part.number}</p>`);
        stack.push('</section>', {goal: part.goal, depth: 0});
      }
      const entry = stack.pop();
      if (typeof entry === 'string') { output.push(entry); continue; }
      let goal = entry.goal || entry;
      const depth = entry.depth || 0;
      if (depth >= 24 && goal.step) {
        const number = goal.step;
        output.push(`<div class="derivation-gap"><code>${esc(target(goal.text))}</code><span>Continued in branch ${number} below</span></div>`);
        continuations.push({goal, number});
        continue;
      }
      output.push('<div class="derivation-inference">');
      stack.push('</div>');
      // Unary chains share a column and do not add DOM nesting per tactic.
      while (goal.step) {
        stack.push(button(goal.step, 'derivation-rule')
          + `<div class="derivation-conclusion"><code>${esc(target(goal.text))}</code></div>`);
        if (goal.premises.length === 1) { goal = goal.premises[0]; continue; }
        if (goal.premises.length) {
          output.push('<div class="derivation-premises">');
          stack.push('</div>');
          for (let i = goal.premises.length - 1; i >= 0; i--) stack.push({goal: goal.premises[i], depth: depth + 1});
        }
        break;
      }
      if (!goal.step) output.push(`<div class="derivation-gap"><code>${esc(target(goal.text))}</code><span>No linked tactic captured</span></div>`);
    }
    return output.join('');
  }

  function stepsHTML(claim, trace, key, range) {
    const inRange = trace.nodes.filter(node => node.range.start.line >= range.start_line && node.range.start.line <= range.end_line);
    const exact = inRange.filter(node => node.declaration === claim.name);
    const nodes = exact.length ? exact : inRange;
    if (!nodes.length) return '<p class="muted">No tactics were captured for this claim. It may use a term proof.</p>';
    const {roots, unlinked} = derivation(nodes);
    const firstStep = roots.find(goal => goal.step)?.step ?? 1;
    const step = position?.key === key ? position.step : firstStep;
    const node = nodes[step - 1];
    const button = (value, css) => {
      const tactic = nodes[value - 1].text.trim().split(/\s+/, 1)[0] || 'Tactic';
      return `<button type="button" class="secondary ${css}" data-act="derivation-step" value="${value}" aria-pressed="${step === value}" aria-controls="derivation-details"><span class="small">Step ${value}</span> <code>${esc(tactic.slice(0, 64))}</code></button>`;
    };
    const extra = unlinked.length ? `<section class="derivation-unlinked"><h4>Other tactics in the recording</h4><ul>${unlinked.map(item =>
      `<li>${button(item.step, '')} <span class="small muted">${item.note}</span></li>`).join('')}</ul></section>` : '';
    const partial = trace.status === 'partial' ? '<p class="note-line" data-tone="amber">Lean reported errors. This recording may be incomplete.</p>' : '';
    const empty = '<p class="small muted">No goals captured.</p>';
    return `${partial}<div class="derivation-layout"><div class="derivation-diagram">
      <p class="small muted derivation-selection"><span role="status">Selected step ${step} · line ${node.range.start.line}</span> <button type="button" class="ghost" data-act="derivation-details">Go to details</button></p>
      <div class="derivation-tree" tabindex="0" role="region" aria-label="Tactic derivation">${treeHTML(roots, button) || '<p class="muted">No starting goals were captured.</p>'}</div></div>
      <section class="derivation-details" id="derivation-details" aria-label="Selected tactic">
        <div class="goal-tactic-heading"><h4 tabindex="-1">Step ${step}</h4>${goalSourceHTML(trace, node)}</div>
        <pre class="goal-full-tactic"><code>${esc(node.text)}</code></pre>
        <h5>Goals before</h5>${goalBlocksHTML(node.before, empty)}
        <h5>Goals after</h5>${goalBlocksHTML(node.after, empty)}
      </section>${extra}</div>`;
  }

  function renderDerivation(root, tabs) {
    const cells = sourceClaimRanges(UI.data.cells);
    const name = UI.graphSelected?.startsWith('local:') ? UI.graphSelected.slice(6) : null;
    const cell = cells.get(name);
    const options = [...cells.keys()].map(value => `<option value="${esc(value)}"${name === value ? ' selected' : ''}>${esc(value)}</option>`).join('');
    const pickerTag = 'select';
    const picker = cells.size ? `<label>Claim <${pickerTag} data-act="derivation-claim">${cell ? '' : '<option value="" selected disabled>Choose a claim</option>'}${options}</${pickerTag}></label>` : '';
    const body = cell ? proofGoalsHTML(claim(name) || {name}, {start_line: cell.start, end_line: cell.end}, stepsHTML)
      : '<p class="muted">Choose a claim with source in this notebook to see its derivation.</p>';
    const html = `${tabs}<section class="proof-graph-view derivation-view" aria-label="Derivation"><header>
      <div><h2>Derivation</h2><p>Read from the original goal at the bottom to its subgoals above. Select a tactic to see its source and goals.</p></div>${picker}
      </header>${body}</section>`;
    if (renderedHTML === html && root.querySelector('.derivation-view')) return;
    const tree = root.querySelector('.derivation-tree');
    const key = tree?.closest('.goal-results')?.dataset.goalKey;
    const scroll = tree ? [tree.scrollLeft, tree.scrollTop] : null;
    const focused = tree && document.activeElement === tree;
    root.innerHTML = html;
    renderedHTML = html;
    const next = root.querySelector('.derivation-tree');
    if (next && key === next.closest('.goal-results')?.dataset.goalKey) {
      [next.scrollLeft, next.scrollTop] = scroll;
      if (focused) next.focus({preventScroll: true});
    } else if (next) {
      next.scrollTop = next.scrollHeight;
    }
  }

  function selectDerivationStep(button) {
    const key = button.closest('.goal-results')?.dataset.goalKey;
    if (!key) return;
    position = {key, step: Number(button.value)};
    workbench.render();
  }

  Object.assign(workbench, {exploreTabsHTML, renderDerivation, selectDerivationStep});
})();
