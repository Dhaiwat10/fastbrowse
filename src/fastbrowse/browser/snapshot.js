// Ported from jev-ultrafast (MIT): jev_ultrafast/snapshot.js.
// Atomic single-evaluate observation of one frame's controls, viewport text, and a freshness fingerprint.
// Adapted to fastbrowse's Control shape (role + operation set, not per-kind actions) and to traverse
// open shadow roots. Runs once per frame session (main frame or an OOPIF); the Python side merges frames.
(() => {
  if (!document.body) return null;
  const registry = window.__fastbrowse ||= { ids: new WeakMap(), nodes: new Map(), next: 1 };
  const identity = e => {
    if (!registry.ids.has(e)) registry.ids.set(e, registry.next++);
    const id = registry.ids.get(e);
    registry.nodes.set(id, e);
    return id;
  };
  for (const [id, e] of registry.nodes) if (!e.isConnected) registry.nodes.delete(id);

  const safe = e => e.type !== 'hidden';
  // Password values never leave the page: only their length is observed, to mask them and detect edits.
  const reveal = e => typeof e.value !== 'string' ? null : e.type === 'password' ? '•'.repeat(e.value.length) : e.value;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });

  const labelOf = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const root = e.getRootNode instanceof Function ? e.getRootNode() : document;
    const byId = id => (root.getElementById ? root.getElementById(id) : document.getElementById(id));
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => labelOf(byId(id), seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => labelOf(l, seen)).filter(Boolean).join(' ') ||
      (['button', 'submit', 'reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n => n.nodeType === 3 ? n.textContent :
        n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? labelOf(n, seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };

  const ARIA_ROLES = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio',
    'option', 'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'];
  const SELECTOR = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' +
    ARIA_ROLES.map(role => `[role="${role}"]`).join(',');

  const roleOf = e => {
    const explicit = e.getAttribute('role');
    if (ARIA_ROLES.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (e.type === 'file') return 'textbox';
      if (['checkbox', 'radio'].includes(e.type)) return e.type;
      if (['button', 'submit', 'reset', 'image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text', 'email', 'url', 'tel', 'password'].includes(e.type)) return 'textbox';
    }
    return null;
  };

  // Traverse light DOM plus any open shadow roots, and same-origin same-process nested iframes.
  function* walk(root) {
    for (const e of root.querySelectorAll(SELECTOR)) yield e;
    for (const e of root.querySelectorAll('*')) {
      if (e.shadowRoot) yield* walk(e.shadowRoot);
      if (e.tagName === 'IFRAME') {
        let inner = null;
        try { inner = e.contentDocument; } catch { inner = null; }
        if (inner && inner.body) yield* walk(inner);
      }
    }
  }

  registry.pageKey = () => [performance.timeOrigin, location.href, scrollX, scrollY, innerWidth, innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e => [identity(e), reveal(e), e.checked, e.selectedIndex, e.disabled, e.readOnly])];
  registry.guard = e => {
    if (!e?.isConnected || !visible(e)) return null;
    const scope = e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e), roleOf(e), labelOf(e), reveal(e), e.checked ?? null, e.selectedIndex ?? null,
      e.readOnly ?? null, e.matches(':disabled'), e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'), e.getAttribute('aria-checked'), e.getAttribute('aria-selected'),
      e.getAttribute('href'), scope?.innerText?.slice(0, 6000) || ''];
  };

  const controls = [];
  for (const e of walk(document)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) continue;
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2, rname = roleOf(e);
    if (!rname || r.width <= 0 || r.height <= 0 || x < 0 || x >= innerWidth) continue;
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;
    const id = identity(e);
    const base = {
      id, role: rname, label: labelOf(e) || rname, offscreen: y < 0 || y >= innerHeight,
      distance: (y < 0 || y >= innerHeight) ? 1 + Math.abs(y - innerHeight / 2) : 0,
      sensitive: e.type === 'password', input_type: e.type || null,
    };
    if (rname === 'link' && e.href) {
      const u = new URL(e.href, location.href);
      base.href = (u.origin === location.origin ? u.pathname + u.search : u.host + u.pathname).slice(0, 200);
    }
    for (const key of ['checked', 'selected', 'expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value === 'true';
    }
    if (['checkbox', 'radio'].includes(e.type)) base.checked = e.checked;
    if (e.tagName === 'SELECT') {
      base.operations = ['select'];
      base.options = [...e.options].filter(o => !o.disabled && !o.closest('optgroup[disabled]')).map(o => o.label);
      base.value = [...e.selectedOptions].map(o => o.label).join(', ');
    } else if (e.type === 'file') {
      base.operations = ['upload'];
      base.value = null;
    } else {
      const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
        (['textbox', 'searchbox', 'spinbutton'].includes(rname) ||
          (rname === 'combobox' && ['INPUT', 'TEXTAREA'].includes(e.tagName)));
      // reveal() is null for <li>, <progress> and <meter>, whose numeric `value`s are not field contents.
      base.value = reveal(e) ?? (e.isContentEditable || rname === 'combobox' ? e.innerText.trim() : null);
      base.operations = editable ? ['fill', 'click', 'enter'] : ['click'];
    }
    controls.push(base);
  }

  // Within the cap, prefer what is on screen, then what is nearest to it.
  controls.sort((a, b) => a.distance - b.distance);
  const kept = [], offscreenIds = new Set();
  for (const c of controls) {
    if (c.offscreen) {
      if (offscreenIds.size >= 40 && !offscreenIds.has(c.id)) continue;
      offscreenIds.add(c.id);
    }
    kept.push(c);
  }
  const omitted = controls.length - kept.length;

  const words = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node, length = 0;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim(), parent = node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
      words.push(value);
      length += value.length;
    }
  }
  const viewport_text = words.join('\n').slice(0, 6000);

  const guards = {};
  for (const c of kept) guards[c.id] = registry.guard(registry.nodes.get(c.id));
  const page_key_raw = registry.pageKey();
  // Compare meaning and identity for the page_key fingerprint; geometry is re-resolved just before input.
  const semantics = kept.map(({ distance, ...c }) => c);
  const page_key = JSON.stringify([location.href, scrollX, scrollY, innerWidth, innerHeight,
    document.title, viewport_text, semantics, page_key_raw[6]]);

  let dialog = null;
  if (window.__fastbrowseDialog) dialog = window.__fastbrowseDialog;

  return {
    url: location.href, title: document.title, viewport_text,
    controls: kept.map(({ distance, ...c }) => c), omitted_controls: omitted,
    page_key, guards, scroll_bottom: scrollY + innerHeight >= document.documentElement.scrollHeight - 2,
    scroll_top: scrollY <= 0,
  };
})()
