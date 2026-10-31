// Serialize the shared renderer's document as an inert, self-contained page.
// No notebook data or scripts are bootstrapped into the exported HTML.
(() => {
  'use strict';
  window.glitnirExport = async ({content, source, title, renderedTitle, file, sha256, preview, css, availableExports = []}) => {
    const page = document.implementation.createHTMLDocument(title);
    page.documentElement.lang = 'en';
    const meta = (name, value, attribute = 'name') => {
      const node = page.createElement('meta');
      node.setAttribute(attribute, name); node.content = value; page.head.append(node);
    };
    const charset = page.createElement('meta'); charset.setAttribute('charset', 'utf-8'); page.head.prepend(charset);
    meta('viewport', 'width=device-width, initial-scale=1');
    meta('Content-Security-Policy', "default-src 'none'; script-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'", 'http-equiv');
    const style = page.createElement('style'); style.textContent = css; page.head.append(style);
    page.body.className = 'static-notebook';
    const app = page.createElement('div'); app.className = 'app'; page.body.append(app);
    const heading = page.createElement('header'); heading.className = 'heading'; app.append(heading);
    const h1 = renderedTitle ? page.importNode(renderedTitle, true) : page.createElement('h1');
    if (!renderedTitle) h1.textContent = title;
    h1.removeAttribute('id'); heading.append(h1);
    const snapshot = page.createElement('p'); snapshot.className = 'snapshot';
    // Saved results shown as recorded say so in the snapshot's own words.
    const recorded = preview?.recorded === true;
    if (recorded) snapshot.classList.add('recorded');
    const words = recorded ? `${preview.notice || 'Saved results, shown as recorded.'} This static page does not check or update results.`
      : preview?.mode === 'project' ? 'Saved source and any historical compiled observations shown below. This static page does not check or update results.' : 'Statuses describe recorded Lean observations at export time; this page does not re-check them.';
    snapshot.textContent = `Static snapshot of ${file} · exported ${new Date().toISOString()} · source SHA-256 ${sha256}. ${words}`;
    heading.append(snapshot);
    const tabs = page.createElement('nav'); tabs.className = 'export-tabs'; tabs.setAttribute('aria-label', 'Notebook views');
    tabs.innerHTML = `<a class="document-tab" id="document-tab" href="#document-view">Document</a>${source ? '<a class="source-tab" id="source-tab" href="#source-view">Lean source</a>' : ''}`;
    app.append(tabs);
    let sourceBody = null;
    if (source) {
      const sourcePane = page.createElement('section'); sourcePane.className = 'export-pane source-pane'; sourcePane.id = 'source-view';
      sourcePane.setAttribute('aria-labelledby', 'source-tab');
      sourceBody = page.importNode(source, true); sourceBody.className = 'source-inner'; sourcePane.append(sourceBody); app.append(sourcePane);
    }
    const main = page.createElement('main'); main.className = 'document export-pane document-pane'; main.id = 'document-view'; app.append(main);
    main.setAttribute('aria-labelledby', 'document-tab');
    const body = page.importNode(content, true); body.className = 'doc-inner'; main.append(body);
    const roots = sourceBody ? [body, sourceBody] : [body];
    const all = selector => roots.flatMap(root => [...root.querySelectorAll(selector)]);

    // Drop actions with their contextual instructions, keeping claim references.
    for (const image of all('img[data-act="image-expand"]')) {
      for (const name of ['role', 'tabindex', 'data-act', 'aria-label', 'title']) image.removeAttribute(name);
    }
    all('.diagram-toolbar,.claim-actions,.copy-result,.cell-tools,.section-tools,.lean-tabs,.lean-pane,script,style,link,meta,iframe,object,embed,form,input,textarea,select').forEach(el => el.remove());
    for (const scene of all('.circuit-scene')) { scene.classList.add('diagram-fit'); scene.setAttribute('width', scene.getAttribute('viewBox').split(' ')[2]); }
    for (const button of all('button')) {
      if (button.dataset.act === 'goto') {
        const link = page.createElement('a'); link.href = '#cell-' + button.dataset.id;
        link.append(...button.childNodes); button.replaceWith(link);
      } else button.remove();
    }
    // Keep per-card source optional; expand supporting replay information.
    for (const details of all('details')) {
      if (details.matches('.proof-section')) { details.open = true; continue; }
      if (details.matches('.def-lean,.export-source,.import-header')) { details.open = false; continue; }
      const section = page.createElement('section'); section.className = details.className + ' export-fold';
      section.append(...details.childNodes); details.replaceWith(section);
    }
    for (const summary of all('summary')) {
      if (summary.parentElement.tagName === 'DETAILS') continue;
      const label = page.createElement('h3'); label.append(...summary.childNodes); summary.replaceWith(label);
    }
    for (const img of all('img')) {
      if (!/^data:image\/(png|jpeg|gif|webp|svg\+xml)[;,]/i.test(img.getAttribute('src') || '')) img.remove();
      img.removeAttribute('srcset');
      img.loading = 'eager';
    }
    const siblingLinks = new Set();
    const exportedModules = new Set(availableExports.filter(module => typeof module === 'string' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(module)));
    const textOnly = a => {
      const span = page.createElement('span'); span.className = a.className;
      span.append(...a.childNodes); a.replaceWith(span);
    };
    for (const a of all('a')) {
      const match = /^\/\?(?:reference|notebook)=([^&]+)$/.exec(a.getAttribute('href') || '');
      if (!match) continue;
      let module;
      try { module = decodeURIComponent(match[1]); } catch { textOnly(a); continue; }
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(module) || !exportedModules.has(module)) { textOnly(a); continue; }
      const claim = a.dataset.claim;
      const href = `${module}.html${claim ? `#cell-${encodeURIComponent(claim)}` : ''}`;
      a.setAttribute('href', href); siblingLinks.add(href);
    }
    const safeLink = href => {
      if (href.startsWith('#')) return true;
      if (siblingLinks.has(href)) return true;
      if (/\s/.test(href)) return false;
      try {
        const url = new URL(href);
        return url.protocol === 'https:' && !!url.hostname
          || url.protocol === 'mailto:' && !!url.pathname;
      } catch { return false; }
    };
    for (const a of all('a')) {
      if (!safeLink(a.getAttribute('href') || '')) {
        textOnly(a);
      } else { a.removeAttribute('target'); a.rel = 'noopener noreferrer'; }
    }
    for (const el of roots.flatMap(root => [root, ...root.querySelectorAll('*')])) {
      for (const attr of [...el.attributes]) {
        if (/^on/i.test(attr.name) || (attr.name.startsWith('data-') && attr.name !== 'data-tone') || ['tabindex', 'contenteditable', 'autofocus'].includes(attr.name)) el.removeAttribute(attr.name);
        // SVG icons may refer to this file's symbols, never an external resource.
        if (el.namespaceURI === 'http://www.w3.org/2000/svg' && ['href', 'xlink:href'].includes(attr.name) && !attr.value.startsWith('#')) el.removeAttribute(attr.name);
      }
    }
    const footer = page.createElement('footer'); footer.className = 'footer';
    footer.textContent = 'Exported from Glitnir. Evidence and figures are not proof.'; app.append(footer);
    return '<!doctype html>\n' + page.documentElement.outerHTML;
  };
})();
