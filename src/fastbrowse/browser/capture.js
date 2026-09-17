// Complete structured text capture of one frame, as ordered blocks with heading paths.
// Unlike snapshot.js (bounded, for Jev's action choice) this reads the whole rendered document, on screen or not,
// so the reading path and answer-evidence quotes see everything a user could scroll to.
// Adjacent inline content (text, <strong>, <a>, <span>...) is one block, so a value inside markup stays with its label.
(() => {
  if (!document.body) return null;
  const HEADINGS = { H1: 1, H2: 2, H3: 3, H4: 4, H5: 5, H6: 6 };
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'CANVAS', 'IFRAME']);
  const blocks = [];
  const path = [];

  const clean = s => s.replace(/[ \t\r\f\v]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const textOf = e => clean(e.innerText ?? e.textContent ?? '');
  const hidden = e => e.hidden || getComputedStyle(e).display === 'none';
  const isBlock = e => {
    const display = getComputedStyle(e).display;
    return !(display.startsWith('inline') || display === 'contents') || e.tagName === 'BR';
  };
  const hrefOf = a => {
    const u = new URL(a.href, location.href);
    return u.origin === location.origin ? u.pathname + u.search : u.href;
  };
  const push = (kind, text, extra = {}) => {
    if (text) blocks.push({ kind, text, heading_path: [...path], ...extra });
  };

  const renderTable = table => {
    const rows = [...table.querySelectorAll(':scope > tr, :scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr')]
      .map(row => [...row.querySelectorAll(':scope > th, :scope > td')].map(c => textOf(c).replace(/\|/g, '\\|')))
      .filter(cells => cells.length);
    if (!rows.length) return '';
    const lines = ['| ' + rows[0].join(' | ') + ' |', '| ' + rows[0].map(() => '---').join(' | ') + ' |'];
    for (const cells of rows.slice(1)) lines.push('| ' + cells.join(' | ') + ' |');
    return lines.join('\n');
  };

  function flush(run) {
    if (!run.length) return;
    const elements = run.filter(n => n.nodeType === 1);
    const text = clean(run.map(n => (n.nodeType === 3 ? n.textContent : n.innerText ?? '')).join(''));
    const onlyLink = elements.length === 1 && elements[0].tagName === 'A' && elements[0].href
      && clean(elements[0].innerText ?? '') === text;
    if (onlyLink) push('link', text, { href: hrefOf(elements[0]) });
    else push('paragraph', text);
    run.length = 0;
  }

  function leaf(el) {
    return ![...el.children].some(c => !SKIP.has(c.tagName) && !hidden(c) && isBlock(c));
  }

  function walk(el) {
    const run = [];
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {
        run.push(node);
        continue;
      }
      if (node.nodeType !== 1 || SKIP.has(node.tagName) || hidden(node)) continue;
      if (!isBlock(node)) {
        run.push(node);
        continue;
      }
      flush(run);
      block(node);
    }
    flush(run);
  }

  function block(el) {
    const level = HEADINGS[el.tagName];
    if (level) {
      const text = textOf(el);
      while (path.length >= level) path.pop();
      if (text) {
        push('heading', text);
        path.push(text);
      }
      return;
    }
    if (el.tagName === 'TABLE') return push('table', renderTable(el));
    if (el.tagName === 'PRE') return push('code', textOf(el));
    if (el.tagName === 'LI' && leaf(el)) {
      const links = el.querySelectorAll('a[href]');
      return push('list_item', textOf(el), links.length === 1 ? { href: hrefOf(links[0]) } : {});
    }
    walk(el);
  }

  walk(document.body);
  return { url: location.href, title: document.title, blocks };
})()
