// Complete structured text capture of one frame, as ordered blocks with heading paths.
// Unlike snapshot.js (bounded, for Jev's action choice) this reads the whole document, visible or not,
// so the LLM reading path and the answer-evidence quotes see everything on the page.
(() => {
  if (!document.body) return null;
  const HEADINGS = { H1: 1, H2: 2, H3: 3, H4: 4, H5: 5, H6: 6 };
  const blocks = [];
  const path = [];

  const textOf = e => (e.innerText ?? e.textContent ?? '').trim();

  const cellsOf = row => [...row.querySelectorAll(':scope > th, :scope > td')].map(c => textOf(c).replace(/\|/g, '\\|'));

  const renderTable = table => {
    const rows = [...table.querySelectorAll(':scope > tr, :scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr')];
    if (!rows.length) return null;
    const lines = rows.map(cellsOf).filter(cells => cells.length);
    if (!lines.length) return null;
    const header = lines[0];
    const body = ['| ' + header.join(' | ') + ' |', '| ' + header.map(() => '---').join(' | ') + ' |'];
    for (const row of lines.slice(1)) body.push('| ' + row.join(' | ') + ' |');
    return body.join('\n');
  };

  // Skip a subtree once its container has been rendered as one block (table, code, or a link's own text).
  const SKIP_DESCEND = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TABLE', 'PRE']);

  function walk(node) {
    for (const child of node.children) {
      if (SKIP_DESCEND.has(child.tagName)) {
        if (child.tagName === 'TABLE') {
          const rendered = renderTable(child);
          if (rendered) blocks.push({ kind: 'table', text: rendered, heading_path: [...path] });
        } else if (child.tagName === 'PRE') {
          const text = textOf(child);
          if (text) blocks.push({ kind: 'code', text, heading_path: [...path] });
        }
        continue;
      }
      const level = HEADINGS[child.tagName];
      if (level) {
        const text = textOf(child);
        while (path.length >= level) path.pop();
        if (text) {
          path.push(text);
          blocks.push({ kind: 'heading', text, heading_path: path.slice(0, -1) });
        }
        continue;
      }
      if (child.tagName === 'LI') {
        const text = textOf(child);
        if (text) blocks.push({ kind: 'list_item', text, heading_path: [...path] });
        walk(child);
        continue;
      }
      if (child.tagName === 'A' && child.href) {
        const text = textOf(child);
        if (text) {
          const u = new URL(child.href, location.href);
          const href = u.origin === location.origin ? u.pathname + u.search : u.href;
          blocks.push({ kind: 'link', text, href, heading_path: [...path] });
        }
        continue;
      }
      if (['P', 'ARTICLE', 'SECTION', 'MAIN', 'BLOCKQUOTE'].includes(child.tagName)) {
        const ownText = [...child.childNodes]
          .filter(n => n.nodeType === 3)
          .map(n => n.textContent.trim())
          .filter(Boolean)
          .join(' ');
        if (ownText) blocks.push({ kind: 'paragraph', text: ownText, heading_path: [...path] });
        walk(child);
        continue;
      }
      walk(child);
    }
  }
  walk(document.body);
  return { url: location.href, title: document.title, blocks };
})()
