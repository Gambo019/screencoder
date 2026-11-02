// The remote view of the app. It knows nothing about what the app does: the app sends a description of its
// widgets (and afterwards only what changed in them), this page draws them and sends back what the viewer does.
'use strict';
(() => {
const TEXT_MS = 25;      // typing is sent this long after a key, so a burst of keys travels as one message
const PING_MS = 2000;
const SCROLL_MS = 40;    // scrolling or dragging is passed on to the app this often
const HAND_MS = 200;     // a scroll this soon after the wheel, a finger or a key is the viewer's own
const QUIET_MS = 700;    // what the app says about a view this soon after the viewer scrolled it is that scroll coming back
const MAP_HIGH = 240;    // the most height the map of the monitors takes up
const LEAF = new Set(['btn', 'chk', 'lbl', 'edit', 'text', 'combo', 'num', 'prog', 'busy', 'canvas']);
const RIGID = new Set(['btn', 'chk', 'combo', 'num', 'busy', 'prog']);  // never squeezed narrower than they are
const MONO = /mono|consol|courier|cascadia|menlo|fixed/i;
const ALIGN = {a: 'flex-start', c: 'center', e: 'flex-end'};

const N = new Map();          // id -> node {id, k(ind), p(roperties), el, ...}
const IMG = new Map();        // picture key -> data URL
const scrollers = new Set();  // nodes that scroll
const pendingText = new Map();
let roots = [], meta = {}, ws = null, key = '', retry = 300, pingTimer = 0, textTimer = 0;
let pointer = {x: 240, y: 160}, hovered = null;

const rootEl = document.getElementById('root'), layersEl = document.getElementById('layers');
const hud = document.getElementById('hud');
const appStyle = document.head.appendChild(document.createElement('style'));
const $ = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const mods = ev => (ev.shiftKey ? 1 : 0) | (ev.ctrlKey ? 2 : 0) | (ev.altKey ? 4 : 0) | (ev.metaKey ? 8 : 0);

// ---- connection

function state(text, ok) {
  hud.textContent = text;
  hud.className = ok ? 'ok' : 'bad';
  document.body.classList.toggle('stale', !ok);
}

function readKey() {
  const m = location.hash.match(/k=([^&]+)/);
  if (m) {
    key = decodeURIComponent(m[1]);
    try { localStorage.setItem('sc-key', key); } catch (e) { /* private mode: asked again next time */ }
    history.replaceState(null, '', location.pathname);  // the key does not stay in the address bar
  } else {
    try { key = localStorage.getItem('sc-key') || ''; } catch (e) { key = ''; }
  }
}

function askKey(message) {
  let gate = document.getElementById('gate');
  if (gate) gate.remove();
  gate = $('div'); gate.id = 'gate';
  const form = $('form'), note = $('div'), input = $('input'), go = $('button');
  note.textContent = message;
  input.placeholder = 'Access key'; input.autocomplete = 'off'; input.spellcheck = false;
  go.textContent = 'Connect';
  form.append(note, input, go);
  form.addEventListener('submit', ev => {
    ev.preventDefault();
    key = input.value.trim().replace(/^.*k=/, '');  // the whole link pasted in works too
    if (!key) return;
    try { localStorage.setItem('sc-key', key); } catch (e) { /* ignore */ }
    gate.remove();
    connect();
  });
  gate.append(form);
  document.body.append(gate);
  input.focus();
}

function connect() {
  if (!key) { askKey('Enter the access key of the app (Settings, Remote control).'); return; }
  state(N.size ? 'reconnecting…' : 'connecting…', false);
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws?k=${encodeURIComponent(key)}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    retry = 300;
    send(['p', performance.now()]);
    setTimeout(() => send(['p', performance.now()]), 600);  // again once the page is drawn: the true round trip
    pingTimer = setInterval(() => send(['p', performance.now()]), PING_MS);
  };
  ws.onmessage = onMessage;
  ws.onclose = ev => {
    ws = null;
    clearInterval(pingTimer);
    if (ev.code === 1008) {  // the app refused the key
      try { localStorage.removeItem('sc-key'); } catch (e) { /* ignore */ }
      key = '';
      askKey('That key was not accepted. Enter the current one.');
      return;
    }
    state(N.size ? 'reconnecting…' : 'connecting…', false);
    setTimeout(connect, retry);
    retry = Math.min(retry * 1.6, 3000);
  };
}

function send(message) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(message)); }

// Anything the viewer does goes out after what they typed, so the app sees the two in the right order.
function cmd(message) { flushText(); send(message); }

function flushText() {
  clearTimeout(textTimer);
  textTimer = 0;
  for (const make of pendingText.values()) send(make());
  pendingText.clear();
}

// `make` builds the message when it is time to send it; it numbers it, and the app confirms each number.
function queueText(n, make) {
  pendingText.set(n.id, make);
  if (!textTimer) textTimer = setTimeout(flushText, TEXT_MS);
}

function nextSeq(n) { n.seq = (n.seq || 0) + 1; return n.seq; }

// True while this viewer has typed something the app has not confirmed yet: what the app says about the field
// in the meantime is older than the typing and must not overwrite it.
function typing(n) { return pendingText.has(n.id) || n.sent != null || (n.seq || 0) !== (n.ack || 0); }

function onMessage(ev) {
  if (typeof ev.data !== 'string') return;
  const m = JSON.parse(ev.data);
  if (m[0] === 'U') apply(m[1]);
  else if (m[0] === 'S') { reset(); meta = m[1]; theme(); apply(m[2]); }
  else if (m[0] === 'P') {
    const ms = performance.now() - m[1];
    state(ms < 10 ? ms.toFixed(1) + ' ms' : Math.round(ms) + ' ms', true);
  } else if (m[0] === 'V') settle(m[1], m[2], m[3]);
  else if (m[0] === 'A') { const n = N.get(m[1]); if (n) n.ack = Math.max(n.ack || 0, m[2]); }
  else if (m[0] === 'M') snipAnswer(m[2]);
}

function reset() {
  for (const n of N.values()) { if (n.st) n.st.remove(); if (n.frame) n.frame.remove(); }
  N.clear(); IMG.clear(); scrollers.clear(); pendingText.clear();
  sized.disconnect();
  rootEl.replaceChildren(); layersEl.replaceChildren();
  roots = [];
  snipOver();
}

function theme() {
  const style = document.documentElement.style, pal = meta.pal || {};
  for (const name in pal) style.setProperty('--' + name, pal[name]);
  if (meta.font) document.body.style.font = `${meta.font[1]}pt '${meta.font[0]}', system-ui, sans-serif`;
  appStyle.textContent = qss(meta.css || '', '');
}

// ---- the app's style sheets, as CSS

const SUB = {title: ' > .gb-title', handle: ' > .handle', item: ' .row', chunk: ' > .chunk', pane: ' > .stack'};
const PSEUDO = {hover: ':hover', pressed: ':active', focus: ':focus', disabled: '.dis', enabled: ':not(.dis)',
  checked: '.on', on: '.on', unchecked: ':not(.on)', off: ':not(.on)', selected: '.sel'};

function compound(s) {
  const m = /^(\*|[A-Za-z_]\w*)?(#[\w-]+)?((?:::?[\w-]+)*)$/.exec(s);
  if (!m || !s) return null;
  let out = !m[1] || m[1] === '*' || m[1] === 'QWidget' ? '.w' : '.' + m[1], tail = '';
  if (m[2]) out += `[data-n="${m[2].slice(1)}"]`;
  for (const part of m[3].match(/::?[\w-]+/g) || []) {
    const known = part.startsWith('::') ? SUB[part.slice(2)] : PSEUDO[part.slice(1)];
    if (known === undefined) return null;  // a part of a widget that does not exist here: the rule is left out
    if (part.startsWith('::') || tail) tail += known; else out += known;
  }
  return out + tail;
}

function selector(s, scope) {
  const out = [];
  for (const part of s.trim().split(/\s*(>)\s*|\s+/)) {
    if (!part) continue;
    if (part === '>') { out.push('>'); continue; }
    const c = compound(part);
    if (c === null) return null;
    out.push(c);
  }
  if (!out.length) return null;
  const text = out.join(' ');
  return scope ? `${scope}${text}, ${scope} ${text}` : text;  // a widget's own sheet: itself and what is inside it
}

function qss(text, scope) {
  text = text.replace(/\/\*[\s\S]*?\*\//g, '');
  if (!text.includes('{')) return scope && text.trim() ? `${scope}, ${scope} .w { ${text} }` : '';
  let out = '';
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const sels = m[1].split(',').map(s => selector(s, scope)).filter(Boolean);
    if (sels.length) out += `${sels.join(', ')} { ${m[2]} }\n`;
  }
  return out;
}

function sheet(n) {
  const css = n.p.ss || '';
  if (n.inl) { for (const name of n.inl) n.el.style.removeProperty(name); n.inl = null; }
  if (n.st) { n.st.remove(); n.st = null; }
  if (!css) return;
  if (!css.includes('{') && LEAF.has(n.k)) {  // "color: x; font-size: y" on a single widget: straight onto it
    n.inl = [];
    for (const decl of css.split(';')) {
      const at = decl.indexOf(':');
      if (at < 0) continue;
      const name = decl.slice(0, at).trim().toLowerCase();
      if (!name || name.startsWith('qproperty')) continue;
      n.el.style.setProperty(name, decl.slice(at + 1).trim());
      n.inl.push(name);
    }
    return;
  }
  n.st = document.createElement('style');
  n.st.textContent = qss(css, '#n' + n.id);
  document.head.append(n.st);
}

// ---- applying what the app sends

function discard(n) {
  if (n.el) n.el.remove();
  if (n.st) n.st.remove();
  if (n.frame) n.frame.remove();
  if (scrollers.delete(n)) sized.unobserve(n.el);
  pendingText.delete(n.id);
  if (n.k === 'snip' || n.k === 'canvas') IMG.delete(n.p.img);  // its own picture, large and of no further use
  if (N.get(n.id) === n) N.delete(n.id);
}

function apply(ops) {
  const touched = new Map();  // node -> set of changed properties (null: all of them, the node is new)
  const keysOf = n => {
    let keys = touched.get(n);
    if (keys === undefined) { keys = new Set(); touched.set(n, keys); }
    return keys;
  };
  let rootsChanged = false;
  for (const op of ops) {
    if (op[0] === 0) {
      const old = N.get(op[1]);
      if (old) discard(old);
      const n = {id: op[1], k: op[2], p: op[3]};
      N.set(n.id, n);
      touched.set(n, null);
    } else if (op[0] === 1) {
      const n = N.get(op[1]);
      if (!n) continue;
      const keys = keysOf(n);
      for (const name in op[2]) {
        if (op[2][name] === null) delete n.p[name]; else n.p[name] = op[2][name];
        if (keys) keys.add(name);
      }
    } else if (op[0] === 2) {  // a long text: keep op[3] units at the start and op[4] at the end, op[5] between
      const n = N.get(op[1]);
      if (!n) continue;
      const old = n.p[op[2]] || '', mid = op[5];
      n.p[op[2]] = old.slice(0, op[3]) + mid + (op[4] ? old.slice(old.length - op[4]) : '');
      // Whole new elements after everything there was: they can be added without rebuilding the view.
      n.app = !op[4] && op[3] === old.length && /^\s*</.test(mid) && />\s*$/.test(old) ? mid : null;
      const keys = keysOf(n);
      if (keys) keys.add(op[2]);
    } else if (op[0] === 3) {
      for (const id of op[1]) { const n = N.get(id); if (n) discard(n); }
    } else if (op[0] === 4) {
      roots = op[1]; rootsChanged = true;
    } else if (op[0] === 5) {
      IMG.set(op[1], op[2]);
    }
  }
  for (const n of touched.keys()) if (!n.el && N.get(n.id) === n) create(n);
  for (const [n, keys] of touched) if (N.get(n.id) === n) update(n, keys);
  if (rootsChanged) placeRoots();
  pin();
}

function create(n) {
  const el = KIND[n.k].make(n);
  el.id = 'n' + n.id;
  el.classList.add('w', 'k-' + n.k);
  if (n.p.c) el.classList.add(...n.p.c.split(' '));
  if (n.p.n) el.dataset.n = n.p.n;
  el._n = n;
  n.el = el;
}

function update(n, keys) {
  const p = n.p, el = n.el, s = el.style, has = name => !keys || keys.has(name);
  if (has('h')) el.classList.toggle('hid', !!p.h && !n.paged);
  if (has('e')) {
    const off = p.e === 0, control = n.inp || el;
    el.classList.toggle('dis', off);
    if ('disabled' in control) control.disabled = off;
    if (n.pick) n.pick.disabled = off;
  }
  if (has('tt')) { if (p.tt) el.title = p.tt; else el.removeAttribute('title'); }
  if (has('fr')) el.classList.toggle('fr', !!p.fr);
  if (has('fs') || has('fp')) s.fontSize = p.fp ? p.fp + 'px' : p.fs ? p.fs + 'pt' : '';
  if (has('fw')) s.fontWeight = p.fw || '';
  if (has('fi')) s.fontStyle = p.fi === undefined ? '' : p.fi ? 'italic' : 'normal';
  if (has('ff')) s.fontFamily = p.ff ? `'${p.ff}', ${MONO.test(p.ff) ? 'monospace' : 'system-ui, sans-serif'}` : '';
  if (has('mn')) { s.minWidth = p.mn && p.mn[0] ? p.mn[0] + 'px' : ''; s.minHeight = p.mn && p.mn[1] ? p.mn[1] + 'px' : ''; }
  if (has('mx')) { s.maxWidth = p.mx && p.mx[0] ? p.mx[0] + 'px' : ''; s.maxHeight = p.mx && p.mx[1] ? p.mx[1] + 'px' : ''; }
  if (has('ss')) sheet(n);  // after the sizes and fonts: what the widget styles itself has the last word
  KIND[n.k].upd(n, p, has, keys);
  if (has('sy') || has('end') || has('html')) n.again = true;
  if (has('geo') && p.geo) { wm.node = n; wm.geo = p.geo; mapBtn.classList.remove('hid'); drawMap(); }
  if (n.frame && (has('wt') || has('wsz'))) frame(n);
}

// ---- layouts

// Make `host` contain exactly `want`, in that order, moving only what is out of place.
function sync(host, want) {
  let i = 0;
  for (const el of want) {
    if (host.children[i] !== el) host.insertBefore(el, host.children[i] || null);
    i++;
  }
  while (host.children.length > i) host.lastElementChild.remove();
}

function anon(n, path, cls, used) {
  n.anon = n.anon || new Map();
  let el = n.anon.get(path);
  if (!el) { el = $('div', cls); n.anon.set(path, el); }
  used.add(path);
  return el;
}

function flex(el, grow, zero, align, row, kind) {
  const s = el.style;
  s.gridArea = ''; s.justifySelf = '';
  s.flex = grow ? `${grow} 1 ${zero ? '0px' : 'auto'}` : row && !RIGID.has(kind) ? '0 1 auto' : '0 0 auto';
  s.alignSelf = ALIGN[align] || '';
}

function cell(n, item, path, used) {
  if (!item) return null;
  if (item[0] === 0) { const c = N.get(item[1]); return c ? c.el : null; }
  const sub = anon(n, path, 'lay', used);
  sync(sub, fill(n, sub, item[1], path + '.', used));
  return sub;
}

function fill(n, host, spec, path, used) {
  const type = spec[0], edge = spec[2], items = spec[3], want = [], s = host.style;
  host.dataset.l = type;
  s.padding = edge && (edge[0] || edge[1] || edge[2] || edge[3]) ? `${edge[1]}px ${edge[2]}px ${edge[3]}px ${edge[0]}px` : '';
  s.gridTemplateColumns = s.gridTemplateRows = '';
  if (type === 'g') {
    s.gap = `${spec[1][1]}px ${spec[1][0]}px`;
    s.gridTemplateColumns = spec[4][0].map(g => g ? `minmax(0, ${g}fr)` : 'auto').join(' ');
    s.gridTemplateRows = spec[4][1].map(g => g ? `minmax(0, ${g}fr)` : 'auto').join(' ');
    items.forEach((item, i) => {
      const el = cell(n, item, path + i, used);
      if (!el) return;
      el.style.flex = '';
      el.style.gridArea = `${item[2] + 1} / ${item[3] + 1} / span ${item[4]} / span ${item[5]}`;
      el.style.justifySelf = item[0] === 0 ? (ALIGN[item[6]] || '').replace('flex-', '') : '';
      el.style.alignSelf = item[0] === 0 ? (ALIGN[item[7]] || '').replace('flex-', '') : '';
      want.push(el);
    });
  } else if (type === 'f') {  // a form: captions on the left, fields on the right
    s.gap = `${spec[1][1]}px ${spec[1][0]}px`;
    s.gridTemplateColumns = 'auto minmax(0, 1fr)';
    items.forEach((row, r) => {
      row.forEach((item, j) => {
        const el = cell(n, item, path + r + '_' + j, used);
        if (!el) return;
        el.style.flex = ''; el.style.alignSelf = ''; el.style.justifySelf = '';
        el.style.gridArea = j === 2 ? `${r + 1} / 1 / span 1 / span 2` : `${r + 1} / ${j + 1}`;
        want.push(el);
      });
    });
  } else {
    const row = type !== 'v';
    s.gap = spec[1] + 'px';
    items.forEach((item, i) => {
      if (item[0] === 0) {
        const c = N.get(item[1]);
        if (!c) return;
        flex(c.el, item[2], item[4], item[3], row, c.k);
        if (type === 'k') {  // a stack: one page at a time
          c.paged = true;
          c.el.classList.remove('hid');
          c.el.classList.toggle('off', i !== spec[4]);
        }
        want.push(c.el);
      } else if (item[0] === 1) {
        const sub = cell(n, item, path + i, used);
        flex(sub, item[2], item[3], '', row);
        sub.classList.toggle('hid', !!item[4]);
        want.push(sub);
      } else if (item[0] === 2) {
        const gap = anon(n, path + i, 'sp', used);
        gap.style.flex = item[1] ? `${item[1]} 1 0px` : `0 0 ${item[2]}px`;
        want.push(gap);
      } else if (item[0] === 3) {
        want.push(anon(n, path + i, 'tsep', used));
      }
    });
  }
  return want;
}

function layout(n) {
  const host = n.body || n.el, spec = n.p.lay, used = new Set();
  let want = [];
  if (spec) want = fill(n, host, spec, 'L', used);
  else { host.removeAttribute('data-l'); host.style.padding = host.style.gap = ''; }
  for (const id of n.p.ex || []) {  // children the app placed by hand, outside any layout
    const c = N.get(id);
    if (c) { c.el.style.flex = ''; want.push(c.el); }
  }
  sync(host, want);
  if (n.anon) for (const path of [...n.anon.keys()]) if (!used.has(path)) n.anon.delete(path);
}

// ---- scrolling

// The two sides lay text out in different widths, so a view is kept the same by what is at its top and not by a
// number of pixels. A place is what the app sends as `sy`: 0 for the very start, else [node, block or line of
// its text (-1: it has none), how far down that is, from 0 to 1].
const BLOCKS = 'p,h1,h2,h3,h4,h5,h6,pre,hr,li:not(:has(> pre))';  // each of these is one block of Qt's document

// A view changed size or came into view: its text moved under it.
const sized = new ResizeObserver(entries => {
  for (const entry of entries) {
    const n = entry.target._n, view = scrollers.has(n) ? n : entry.target.parentElement && entry.target.parentElement._n;
    if (view) view.again = true;
  }
  pin();
});

function track(n, el) {
  if (n.tracked) return;
  n.tracked = true;
  n.pin = true;  // the viewer is at the end (and stays there as text arrives) until they scroll away
  scrollers.add(n);
  sized.observe(el);
  const hand = () => { n.hand = performance.now(); };
  el.addEventListener('wheel', hand, {passive: true});
  el.addEventListener('touchmove', hand, {passive: true});
  el.addEventListener('keydown', hand, {passive: true});
  el.addEventListener('pointerdown', () => { n.held = true; hand(); }, {passive: true});  // the scroll bar is in here too
  el.addEventListener('scroll', () => {
    n.pin = el.scrollHeight - el.clientHeight - el.scrollTop < 28;
    if (!n.held && performance.now() - (n.hand || 0) >= HAND_MS) return;  // the text changed, or the app moved it
    hand();
    tell(n);
  }, {passive: true});
}

function letGo() { for (const n of scrollers) n.held = false; }

function busy(n) { return n.held || performance.now() - (n.hand || 0) < QUIET_MS; }

function partsOf(n) {
  if (n.k === 'code') return n.src.children;
  if (n.k !== 'rich') return null;
  return n.blocks || (n.blocks = [...n.el.querySelectorAll(BLOCKS)]);
}

// A big text is not sent on every change, so the page can hold fewer blocks than the app for a moment.
function blocksPerPart(n, parts) { return n.k === 'rich' && n.p.bc ? n.p.bc / parts.length : 1; }

function viewTop(el) { return el.getBoundingClientRect().top + el.clientTop; }

// A list item holds the list nested in it, whose items are blocks of their own.
function partBox(el) {
  const box = el.getBoundingClientRect(), nested = el.tagName === 'LI' && el.querySelector(':scope > ul, :scope > ol');
  if (!nested) return box;
  const end = nested.getBoundingClientRect().top;
  return {top: box.top, bottom: end, height: end - box.top};
}

// How far down the content of view `n` a place is, in pixels of this page.
function offsetOf(n, at) {
  const c = N.get(at[0]);
  if (!c || !c.el) return null;
  const parts = partsOf(c);
  let box = null;
  if (at[1] >= 0 && parts && parts.length) box = partBox(parts[Math.min(Math.round(at[1] / blocksPerPart(c, parts)), parts.length - 1)]);
  else box = c.el.getBoundingClientRect();
  return box.height ? n.el.scrollTop + box.top + at[2] * box.height - viewTop(n.el) : null;
}

// The place at the top of view `n`: down through what is there to the text it lies in.
function placeOf(n) {
  const y = viewTop(n.el), far = box => Math.round(Math.min(1, Math.max(0, (y - box.top) / (box.height || 1))) * 1e4) / 1e4;
  let here = n.el, at = n;
  while (here) {
    if (here._n) at = here._n;
    const parts = here._n ? partsOf(here._n) : null;
    if (parts && parts.length) {
      let lo = 0, hi = parts.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (partBox(parts[mid]).bottom > y) hi = mid; else lo = mid + 1;
      }
      return [at.id, Math.round(lo * blocksPerPart(at, parts)), far(partBox(parts[lo]))];
    }
    here = [...here.children].find(kid => { const box = kid.getBoundingClientRect(); return box.height && box.bottom > y; });
  }
  return [at.id, -1, far(at.el.getBoundingClientRect())];
}

function tell(n) {
  if (n.telling) return;
  n.telling = setTimeout(() => {
    n.telling = 0;
    if (N.get(n.id) !== n) return;
    const el = n.el, max = el.scrollHeight - el.clientHeight;
    if (el.scrollTop <= 0) cmd(['y', n.id, 0]);
    else if (el.scrollTop >= max - 1) cmd(['y', n.id, -1]);
    else cmd(['y', n.id, ...placeOf(n)]);
  }, SCROLL_MS);
}

// Show what the app shows, unless the viewer has the view in hand right now.
function pin() {
  for (const n of scrollers) {
    const el = n.el;
    if (!el.clientHeight) { n.again = true; continue; }  // not on show: put right as soon as it is
    const max = el.scrollHeight - el.clientHeight;
    if (n.p.sy != null) {
      if (!n.again) continue;
      n.again = false;
      if (busy(n)) continue;
      const to = n.p.end ? max : n.p.sy === 0 ? 0 : offsetOf(n, n.p.sy);
      if (to != null && Math.abs(el.scrollTop - to) > 1) el.scrollTop = to;
    } else if (n.p.end && n.pin && el.scrollTop < max - 1) el.scrollTop = max;  // a tab that is not open in the app
  }
}

// ---- text fields

function settle(id, value, caret) {
  // The app's answer to a key it was sent (Enter may have submitted the text, or broken the line).
  const n = N.get(id);
  if (!n) return;
  const el = n.inp || n.el, sent = n.sent;
  n.sent = null;
  if (sent != null && el.value !== sent) {
    // More was typed while the key was on its way: do to the newer text what the app did to the older one.
    let a = 0;
    const most = Math.min(sent.length, value.length);
    while (a < most && sent[a] === value[a]) a++;
    let b = 0;
    while (b < most - a && sent[sent.length - 1 - b] === value[value.length - 1 - b]) b++;
    const cut = sent.slice(a, sent.length - b), put = value.slice(a, value.length - b);
    if (el.value.slice(0, a) === sent.slice(0, a) && el.value.slice(a, a + cut.length) === cut) {
      const at = el.selectionStart;
      el.value = el.value.slice(0, a) + put + el.value.slice(a + cut.length);
      if (at != null) { const to = Math.max(a, at + put.length - cut.length); el.setSelectionRange(to, to); }
      n.p.v = el.value;
      queueText(n, () => ['t', n.id, el.value, el.selectionStart || 0, nextSeq(n)]);
    }
    return;
  }
  pendingText.delete(id);
  n.p.v = value;
  if (el.value !== value) el.value = value;
  if (document.activeElement === el) el.setSelectionRange(caret, caret);
}

function wireText(n, el, multi) {
  el.spellcheck = false;
  el.autocomplete = 'off';
  el.addEventListener('input', () => {
    n.p.v = el.value;
    queueText(n, () => ['t', n.id, el.value, el.selectionStart || 0, nextSeq(n)]);
  });
  el.addEventListener('keydown', ev => {
    if (ev.isComposing || (ev.key !== 'Enter' && ev.key !== 'Escape')) return;
    ev.stopPropagation();
    if (ev.key === 'Enter') ev.preventDefault();  // what Enter means here is the app's decision
    pendingText.delete(n.id);
    flushText();
    n.sent = el.value;
    send(['t', n.id, el.value, el.selectionStart || 0, nextSeq(n)]);
    send(['k', n.id, ev.key, mods(ev)]);
  });
  if (!multi) el.addEventListener('change', () => cmd(['b', n.id]));
}

function textProps(n, el, p, has) {
  if (has('v') && !typing(n) && el.value !== (p.v || '')) {
    const at = el.selectionStart;
    el.value = p.v || '';
    if (document.activeElement === el && at != null) { const to = Math.min(at, el.value.length); el.setSelectionRange(to, to); }
  }
  if (has('ph')) el.placeholder = p.ph || '';
  if (has('ro')) el.readOnly = !!p.ro;
}

function image(el, key) {
  const url = key && IMG.get(key);
  if (url) el.src = url; else el.removeAttribute('src');
}

function link(n, href) {
  if (/^https?:/i.test(href)) window.open(href, '_blank', 'noopener');  // a web link opens on this machine
  else if (href) cmd(['a', n.id, href]);
}

function anchors(n, el) {
  el.addEventListener('click', ev => {
    const a = ev.target.closest && ev.target.closest('a');
    if (a) { ev.preventDefault(); link(n, a.getAttribute('href') || ''); }
  });
  el.addEventListener('auxclick', ev => { if (ev.target.closest && ev.target.closest('a')) ev.preventDefault(); });
}

// A caption that is rich text comes as the app wrote it. Only what formats text is let through.
function safeHtml(html) {
  const holder = document.createElement('template');
  holder.innerHTML = html;
  for (const el of holder.content.querySelectorAll('script,style,meta,link,iframe,object,embed,form,base,svg,math')) el.remove();
  for (const el of holder.content.querySelectorAll('*')) {
    for (const attr of [...el.attributes]) if (/^on/i.test(attr.name)) el.removeAttribute(attr.name);
  }
  return holder.content;
}

function bodyStyle(el, style) {
  const get = name => { const m = new RegExp(name + '\\s*:\\s*([^;]+)').exec(style || ''); return m ? m[1].trim() : ''; };
  const family = get('font-family');
  el.style.fontFamily = family ? `${family}, ${MONO.test(family) ? 'monospace' : 'system-ui, sans-serif'}` : '';
  el.style.fontSize = get('font-size');
  el.style.fontWeight = get('font-weight');
  el.style.fontStyle = get('font-style');
}

function showTab(n, cur) {
  const p = n.p;
  n.bar.replaceChildren(...(p.tabs || []).map((t, i) => {
    const tab = $('div', 'tab' + (i === cur ? ' sel' : '') + (t[1] ? '' : ' hid') + (t[2] ? '' : ' dis'));
    tab.textContent = t[0];
    if (t[3]) tab.title = t[3];
    tab.addEventListener('click', () => {
      if (!t[2] || n.el.classList.contains('dis')) return;
      n.localCur = i;
      showTab(n, i);  // shown at once; when syncing, the app is told and has the last word
      pin();
      if (p.sync) cmd(['s', n.id, i]);  // only drive the app (and the other viewers) while syncing
    });
    return tab;
  }));
  (p.ch || []).forEach((id, i) => { const c = N.get(id); if (c) c.el.classList.toggle('off', i !== cur); });
}

function handle(n, i) {
  n.handles = n.handles || [];
  if (n.handles[i]) return n.handles[i];
  const h = n.handles[i] = $('div', 'handle');
  h.addEventListener('pointerdown', ev => {
    const kids = (n.p.ch || []).map(id => N.get(id)).filter(Boolean);
    const before = kids.findIndex(c => c.el === h.previousElementSibling);
    const after = kids.findIndex(c => c.el === h.nextElementSibling);
    if (before < 0 || after < 0) return;
    ev.preventDefault();
    h.setPointerCapture(ev.pointerId);
    const tall = n.p.o === 'v', size = c => tall ? c.el.offsetHeight : c.el.offsetWidth;
    const from = tall ? ev.clientY : ev.clientX, sizes = kids.map(size), a = sizes[before], b = sizes[after];
    const move = e => {
      const d = Math.max(40 - a, Math.min(b - 40, (tall ? e.clientY : e.clientX) - from));
      sizes[before] = a + d; sizes[after] = b - d;
      n.local = sizes;  // this viewer's own division of the space, kept until the page is reloaded
      kids.forEach((c, j) => { c.el.style.flex = `${sizes[j]} 1 0px`; });
    };
    const up = () => { h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); };
    h.addEventListener('pointermove', move);
    h.addEventListener('pointerup', up);
  });
  return h;
}

// ---- one entry per kind of widget: how it is made, and how its properties show

const KIND = {
  box: {
    make(n) {
      const el = $('div');
      if (n.p.gt !== undefined) {  // a group box: a caption on the border, the content inside
        n.title = $('div', 'gb-title'); n.body = $('div', 'gb-body');
        el.append(n.title, n.body);
      }
      return el;
    },
    upd(n, p, has) {
      if (n.title && (has('gt') || has('ck') || has('on'))) {
        n.title.replaceChildren();
        if (p.ck) {
          const box = $('input'); box.type = 'checkbox'; box.checked = !!p.on;
          box.addEventListener('click', ev => { ev.preventDefault(); cmd(['c', n.id]); });
          n.title.append(box);
        }
        n.title.append(p.gt || '');
        n.title.classList.toggle('hid', !p.gt && !p.ck);
      }
      if (has('sep')) { if (p.sep) n.el.dataset.sep = p.sep; else delete n.el.dataset.sep; }
      if (has('msg')) { if (p.msg) n.el.dataset.msg = p.msg; else delete n.el.dataset.msg; }
      if (has('lay') || has('ex')) layout(n);
    },
  },
  tabs: {
    make(n) {
      const el = $('div');
      n.bar = $('div', 'tabbar'); n.stack = $('div', 'stack');
      el.append(n.bar, n.stack);
      return el;
    },
    upd(n, p, has) {
      if (has('ch')) {
        const want = [];
        for (const id of p.ch || []) {
          const c = N.get(id);
          if (!c) continue;
          c.paged = true;  // which page shows is the tab bar's business, not the page's own "hidden"
          c.el.classList.remove('hid');
          c.el.style.flex = '';
          want.push(c.el);
        }
        sync(n.stack, want);
      }
      if (has('tabs') || has('cur') || has('ch') || has('sync')) {
        if (p.sync) n.localCur = p.cur;  // in sync: follow the app, and snap to it the moment sync is turned on
        else if (n.localCur == null) n.localCur = p.cur;  // first paint while unsynced: start from the app's tab
        showTab(n, p.sync ? p.cur : n.localCur);
      }
    },
  },
  split: {
    make() { return $('div'); },
    upd(n, p, has) {
      if (has('o')) n.el.classList.toggle('v', p.o === 'v');
      if (has('ch') || has('sz')) {
        const want = [], sizes = n.local || p.sz || [];
        (p.ch || []).forEach((id, i) => {
          const c = N.get(id);
          if (!c) return;
          if (want.length) want.push(handle(n, i));
          c.el.style.flex = `${Math.max(sizes[i] || 0, 0)} 1 0px`;
          want.push(c.el);
        });
        sync(n.el, want);
      }
    },
  },
  scroll: {
    make(n) { const el = $('div'); track(n, el); return el; },
    upd(n, p, has) {
      if (has('ch')) {
        const c = N.get((p.ch || [])[0]);
        if (c) { c.el.style.flex = ''; sized.observe(c.el); }  // what is inside grows here at its own pace
        sync(n.el, c ? [c.el] : []);
      }
      if (has('fit')) n.el.classList.toggle('fit', !!p.fit);
    },
  },
  btn: {
    make(n) {
      const el = $('button');
      el.type = 'button';
      n.ic = $('img', 'ic'); n.tx = $('span');
      el.append(n.ic, n.tx);
      el.addEventListener('click', () => cmd(['c', n.id]));
      return el;
    },
    upd(n, p, has) {
      if (has('t') || has('io') || has('ic')) n.tx.textContent = p.io && p.ic ? '' : (p.t || '');
      if (has('ic')) image(n.ic, p.ic);
      if (has('on')) n.el.classList.toggle('on', !!p.on);
      if (has('fl')) n.el.classList.toggle('fl', !!p.fl);
      if (has('df')) n.el.classList.toggle('df', !!p.df);
    },
  },
  chk: {
    make(n) {
      const el = $('label');
      n.inp = $('input'); n.inp.type = 'checkbox'; n.tx = $('span');
      el.append(n.inp, n.tx);
      // The tick follows the app, not the click: the click is sent, the app answers with the new state.
      n.inp.addEventListener('click', ev => { ev.preventDefault(); cmd(['c', n.id]); });
      return el;
    },
    upd(n, p, has) {
      if (has('rd')) n.inp.type = p.rd ? 'radio' : 'checkbox';
      if (has('t')) n.tx.textContent = p.t || '';
      if (has('on')) n.inp.checked = !!p.on;
    },
  },
  lbl: {
    make(n) {
      const el = $('div');
      n.tx = $('span', 'lt');
      el.append(n.tx);
      anchors(n, el);
      return el;
    },
    upd(n, p, has) {
      if (has('t') || has('r') || has('img') || has('isz')) {
        if (p.img) {
          if (!n.im) { n.im = $('img'); n.im.draggable = false; }
          image(n.im, p.img);
          if (p.isz) { n.im.width = p.isz[0]; n.im.height = p.isz[1]; }
          n.tx.replaceChildren(n.im);
        } else if (p.r) n.tx.replaceChildren(safeHtml(p.t || ''));
        else n.tx.textContent = p.t || '';
      }
      if (has('r')) n.el.classList.toggle('r', !!p.r);
      if (has('wr')) n.el.classList.toggle('wr', !!p.wr);
      if (has('al')) { n.el.classList.toggle('al-c', p.al === 'c'); n.el.classList.toggle('al-r', p.al === 'r'); }
    },
  },
  edit: {
    make(n) { const el = $('input'); el.type = 'text'; wireText(n, el, false); return el; },
    upd(n, p, has) {
      textProps(n, n.el, p, has);
      if (has('pw')) n.el.type = p.pw ? 'password' : 'text';
    },
  },
  text: {
    make(n) { const el = $('textarea'); wireText(n, el, true); return el; },
    upd(n, p, has) { textProps(n, n.el, p, has); },
  },
  rich: {
    make(n) { const el = $('div'); anchors(n, el); return el; },
    upd(n, p, has, keys) {
      if (has('au')) { n.el.classList.toggle('auto', !!p.au); if (!p.au) track(n, n.el); }
      if (has('bs')) bodyStyle(n.el, p.bs);
      if (has('html')) {
        if (keys && n.app != null) n.el.insertAdjacentHTML('beforeend', n.app);
        else n.el.innerHTML = p.html || '';
        n.blocks = null;
      }
      n.app = null;
    },
  },
  code: {
    make(n) {
      const el = $('div');
      n.src = $('pre', 'src');
      el.append(n.src);
      track(n, el);
      return el;
    },
    upd(n, p, has) {
      // A line of its own: its number stays beside it when it wraps, and it can be scrolled to.
      if (has('html')) n.src.innerHTML = (p.html || '').split('\n').map(line => `<div class="cl">${line || '<br>'}</div>`).join('');
      if (has('ln')) {
        n.el.classList.toggle('ln', !!p.ln);
        n.el.style.setProperty('--gw', String(p.ln || 0).length + 'ch');
      }
      if (has('wr')) n.el.classList.toggle('wr', !!p.wr);
      n.app = null;
    },
  },
  combo: {
    make(n) {
      if (!n.p.ed) {
        const el = $('select');
        el.addEventListener('change', () => cmd(['s', n.id, el.selectedIndex]));
        return el;
      }
      // One you can also type into: the text, and next to it the list to pick from.
      const el = $('span', 'ed');
      n.inp = $('input'); n.inp.type = 'text'; n.inp.spellcheck = false;
      n.pick = $('select');
      el.append(n.inp, n.pick);
      n.inp.addEventListener('input', () => { n.p.v = n.inp.value; queueText(n, () => ['e', n.id, n.inp.value, nextSeq(n)]); });
      n.pick.addEventListener('change', () => { n.inp.value = n.pick.value; cmd(['s', n.id, n.pick.selectedIndex]); });
      return el;
    },
    upd(n, p, has) {
      const list = n.pick || n.el;
      if (has('it')) list.replaceChildren(...(p.it || []).map(t => { const o = $('option'); o.textContent = t; return o; }));
      if (has('it') || has('cur')) list.selectedIndex = p.cur;
      if (n.inp && has('v') && !typing(n) && n.inp.value !== (p.v || '')) n.inp.value = p.v || '';
    },
  },
  num: {
    make(n) {
      const el = $('input');
      el.type = n.p.ty === 'range' ? 'range' : 'number';
      el.addEventListener('pointerdown', () => { n.held = true; });
      el.addEventListener('pointerup', () => setTimeout(() => { n.held = false; }, 300));
      el.addEventListener('input', () => { n.p.v = +el.value; send(['v', n.id, +el.value, 0]); });
      el.addEventListener('change', () => { n.held = false; cmd(['v', n.id, +el.value, 1]); });
      return el;
    },
    upd(n, p, has) {
      const el = n.el;
      if (has('lo')) el.min = p.lo;
      if (has('hi')) el.max = p.hi;
      if (has('st') || has('dec')) el.step = p.dec ? Math.pow(10, -p.dec) : (p.st || 1);
      // While the viewer drags it or types in it, their value stands; the app's echo would only fight them.
      if (has('v') && !n.held && document.activeElement !== el) el.value = p.v;
    },
  },
  prog: {
    make(n) {
      const el = $('div');
      n.chunk = $('div', 'chunk'); n.pt = $('div', 'pt');
      el.append(n.chunk, n.pt);
      return el;
    },
    upd(n, p, has) {
      if (has('v') || has('lo') || has('hi')) {
        n.chunk.style.width = Math.max(0, Math.min(100, (p.v - p.lo) * 100 / ((p.hi - p.lo) || 1))) + '%';
      }
      if (has('t')) n.pt.textContent = p.t || '';
    },
  },
  busy: {
    make() { return $('div'); },
    upd(n, p, has) { if (has('col')) n.el.style.color = p.col || ''; },
  },
  priv: {  // this viewer's own private message box: its text lives only here until a Send button is pressed
    make(n) {
      const el = $('div', 'privbox');
      n.title = $('div', 'priv-title');
      n.ta = $('textarea', 'priv-text');
      n.ta.placeholder = 'Type a message — “Send to AI” asks the developer; “Send to local user” shows it to them';
      const row = $('div', 'priv-row');
      n.toAi = $('button', 'priv-ai'); n.toAi.type = 'button'; n.toAi.textContent = '↪ Send to AI';
      n.toLocal = $('button'); n.toLocal.type = 'button'; n.toLocal.textContent = '💬 Send to local user';
      const post = action => {
        if (!n.ta.value.trim()) return;
        send(['post', n.id, action, n.ta.value]);  // nothing is sent while typing: the box is this viewer's alone
        n.ta.value = '';
      };
      n.toAi.addEventListener('click', () => post(0));
      n.toLocal.addEventListener('click', () => post(1));
      n.ta.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); post(0); }
      });
      row.append(n.toAi, n.toLocal);
      el.append(n.title, n.ta, row);
      return el;
    },
    upd(n, p, has) { if (has('t')) n.title.textContent = p.t || 'Your message'; },
  },
  tree: {
    make(n) {
      const el = $('div');
      let lastRow = -1, lastAt = 0;
      el.addEventListener('click', ev => {
        const row = ev.target.closest('.row');
        if (!row) return;
        const i = +row.dataset.i, now = performance.now();
        if (ev.target.classList.contains('tw') && row.classList.contains('par')) { cmd(['r', n.id, i, 2]); return; }
        const twice = i === lastRow && now - lastAt < 450;  // the rows are redrawn between the two clicks
        lastRow = twice ? -1 : i; lastAt = now;
        cmd(['r', n.id, i, twice ? 1 : 0]);
      });
      el.addEventListener('contextmenu', ev => {
        const row = ev.target.closest('.row');
        if (!row) return;
        ev.preventDefault(); ev.stopPropagation();
        cmd(['r', n.id, +row.dataset.i, 3]);
      });
      track(n, el);
      return el;
    },
    upd(n, p, has) {
      if (!has('rows') && !has('hd')) return;
      let out = p.hd ? '<div class="hdr">' + p.hd.map(t => `<span class="cell">${esc(t)}</span>`).join('') + '</div>' : '';
      (p.rows || []).forEach((r, i) => {
        const f = r[1];
        out += `<div class="row${f & 4 ? ' sel' : ''}${f & 8 ? ' dis' : ''}${f & 1 ? ' par' : ''}" data-i="${i}" ` +
          `style="padding-left:${6 + r[0] * 14}px"><span class="tw">${f & 1 ? (f & 2 ? '▾' : '▸') : ''}</span>` +
          r[2].map((t, j) => `<span class="cell"${r[3] && r[3][j] ? ` style="color:${esc(r[3][j])}"` : ''}>${esc(t)}</span>`).join('') +
          '</div>';
      });
      n.el.innerHTML = out;
    },
  },
  menu: {
    make(n) {
      const el = $('div');
      el.addEventListener('click', ev => {
        const item = ev.target.closest('.mi');
        if (item && !item.classList.contains('dis') && !item.classList.contains('sub')) cmd(['n', n.id, +item.dataset.i]);
      });
      return el;
    },
    upd(n, p, has) {
      if (!has('items')) return;
      n.el.innerHTML = (p.items || []).map((it, i) => it === 0 ? '<div class="msep"></div>' :
        `<div class="mi${it[1] & 1 ? '' : ' dis'}${it[1] & 8 ? ' sub' : ''}" data-i="${i}" style="padding-left:${22 + it[0] * 16}px">` +
        `<span>${it[1] & 4 ? '✓ ' : ''}${esc(it[2])}</span><span class="sc">${esc(it[3] || '')}</span></div>`).join('');
    },
  },
  canvas: {  // something the app paints itself: its picture, with the mouse passed through
    make(n) {
      const el = $('img');
      el.draggable = false;
      const at = ev => {
        const r = el.getBoundingClientRect(), size = n.p.isz || [r.width, r.height];
        return [Math.round((ev.clientX - r.left) * size[0] / (r.width || 1)), Math.round((ev.clientY - r.top) * size[1] / (r.height || 1))];
      };
      let frame = 0, last = null;
      el.addEventListener('pointerdown', ev => {
        ev.preventDefault();
        el.setPointerCapture(ev.pointerId);
        const [x, y] = at(ev);
        cmd(['m', n.id, 0, x, y, ev.button]);
      });
      el.addEventListener('pointermove', ev => {
        if (!ev.buttons) return;
        last = at(ev);
        if (!frame) frame = requestAnimationFrame(() => { frame = 0; send(['m', n.id, 1, last[0], last[1], 0]); });
      });
      el.addEventListener('pointerup', ev => { const [x, y] = at(ev); cmd(['m', n.id, 2, x, y, ev.button]); });
      el.addEventListener('dblclick', ev => { ev.stopPropagation(); const [x, y] = at(ev); cmd(['m', n.id, 3, x, y, 0]); });
      el.addEventListener('contextmenu', ev => ev.preventDefault());
      return el;
    },
    upd(n, p, has) {
      if (!has('img')) return;
      image(n.el, p.img);
      if (n.shown && n.shown !== p.img) IMG.delete(n.shown);
      n.shown = p.img;
      // Tell the app once this picture is on screen: it sends the next one only then, so a slow connection
      // gets fewer pictures that are current instead of a queue of old ones.
      const key = p.img, seen = () => send(['i', n.id, key]);
      if (n.el.decode) n.el.decode().then(seen, seen); else seen();
    },
  },
  snip: {  // a region picker: the app's frozen screenshot, with the rectangle drawn here as the pointer moves
    make(n) {
      const el = $('div');
      n.im = $('img'); n.im.draggable = false;
      n.im.addEventListener('load', snipMore);
      n.dim = $('div', 'snip-dim'); n.rect = $('div', 'snip-sel'); n.tag = $('div', 'snip-size');
      el.append(n.im, n.dim, n.rect, n.tag);
      const at = ev => {  // the pointer in the app's own screen points, kept inside the picture
        const r = n.im.getBoundingClientRect(), size = n.p.isz || [r.width, r.height];
        const inside = (v, most) => Math.max(0, Math.min(most - 1, Math.round(v)));
        return [inside((ev.clientX - r.left) * size[0] / (r.width || 1), size[0]),
                inside((ev.clientY - r.top) * size[1] / (r.height || 1), size[1])];
      };
      el.addEventListener('pointerdown', ev => {
        ev.preventDefault();
        if (snip.busy) return;
        if (ev.button === 2) { snipCancel(); return; }
        if (ev.button !== 0 || !n.p.img) return;
        el.setPointerCapture(ev.pointerId);
        const p = at(ev);
        n.drag = [p[0], p[1], p[0], p[1]];
        snipDraw(n);
      });
      el.addEventListener('pointermove', ev => {
        if (!n.drag) return;
        const p = at(ev);
        n.drag[2] = p[0]; n.drag[3] = p[1];
        snipDraw(n);  // nothing is sent while dragging: the rectangle follows the pointer at once
      });
      el.addEventListener('pointerup', ev => {
        const d = n.drag;
        if (!d || ev.button !== 0) return;
        const p = at(ev);
        d[2] = p[0]; d[3] = p[1];
        n.drag = null;
        const r = snipRect(d);
        if (r[2] > SNIP_MIN && r[3] > SNIP_MIN) {
          n.done = d;  // stays as drawn, whatever the pointer does next, until the app has answered
          snipSend(['m', n.id, 4, d[0], d[1], d[2], d[3]], 'Capturing…');
        }
        snipDraw(n);
      });
      el.addEventListener('pointercancel', () => { n.drag = null; snipDraw(n); });
      return el;
    },
    upd(n, p, has) {
      if (has('img')) { image(n.im, p.img); n.el.classList.toggle('wait', !p.img); }
      if (has('sel') || has('isz') || has('dpr')) snipDraw(n);
    },
  },
};

// ---- picking a region of the app machine's screen

const SNIP_MIN = 4;          // a drag no larger than this picks nothing (the app's own rule)
const SNIP_PATIENCE = 4000;  // no answer from the app by then: stop waiting and ask it where things stand
const snip = {layer: null, picks: [], ids: '', cur: 0, busy: false, timer: 0};

// The rectangle the app makes of a drag from (d[0], d[1]) to (d[2], d[3]), so that what is drawn here is exactly
// what gets captured there. (Its rule: both end points belong to it, except along a side dragged backwards.)
function snipRect(d) {
  const side = (from, to) => to >= from ? [from, to - from + 1] : [to + 1, from - to - 1];
  const across = side(d[0], d[2]), down = side(d[1], d[3]);
  return [across[0], down[0], across[1], down[1]];
}

function snipDraw(n) {
  const size = n.p.isz || [1, 1], d = n.drag || n.done;
  let r = d ? snipRect(d) : n.p.sel || null;  // `sel`: a rectangle being dragged on the app's machine itself
  if (r && (r[2] < 1 || r[3] < 1)) r = null;
  n.dim.classList.toggle('hid', !!r);
  n.rect.classList.toggle('hid', !r);
  n.tag.classList.toggle('hid', !r);
  if (!r) return;
  const part = (v, of) => (v * 100 / of) + '%', dpr = n.p.dpr || 1;
  n.rect.style.left = n.tag.style.left = part(r[0], size[0]);
  n.rect.style.top = part(r[1], size[1]);
  n.rect.style.width = part(r[2], size[0]);
  n.rect.style.height = part(r[3], size[1]);
  n.tag.style.top = `max(0px, calc(${part(r[1], size[1])} - 1.6em))`;
  n.tag.textContent = `${Math.round(r[2] * dpr)} x ${Math.round(r[3] * dpr)}`;
}

function snipCurrent() { return snip.picks.find(n => n.id === snip.cur && N.get(n.id) === n) || null; }

// Send what the viewer decided and hold the picker still until the app says what came of it.
function snipSend(message, note) {
  snip.busy = true;
  snip.note.textContent = note;
  snip.layer.classList.add('busy');
  clearTimeout(snip.timer);
  snip.timer = setTimeout(() => { snipFree(); send(['z']); }, SNIP_PATIENCE);
  cmd(message);
}

function snipFree() {  // picking is possible again; a rectangle that was sent is forgotten
  clearTimeout(snip.timer);
  snip.busy = false;
  if (snip.layer) snip.layer.classList.remove('busy');
  for (const n of snip.picks) if (n.done) { n.done = null; snipDraw(n); }
}

function snipCancel() { const n = snipCurrent(); if (n && !snip.busy) snipSend(['k', n.id, 'Escape', 0], 'Cancelling…'); }

function snipWhole() { const n = snipCurrent(); if (n && !snip.busy) snipSend(['k', n.id, 'Enter', 0], 'Capturing…'); }

// The app's answer: the picking is over (the picker goes at once), or it is still up (too small a drag).
function snipAnswer(over) {
  if (!over) { snipFree(); return; }
  for (const n of N.values()) if (n.k === 'snip') n.over = true;
  placeRoots();
}

function snipOver() {
  snip.picks = []; snip.ids = '';
  snipFree();
}

// One layer for all monitors: one is shown at a time, as large as the page allows.
function snipLayer(picks) {
  if (!snip.layer) {
    const layer = snip.layer = $('div', 'layer snip'), bar = $('div', 'snip-bar'), hint = $('span', 'snip-hint');
    const cancel = $('button', 'snip-btn');
    snip.tabs = $('span', 'snip-tabs'); snip.note = $('span', 'snip-note'); snip.stage = $('div', 'snip-stage');
    hint.textContent = 'Drag to select an area  ·  Enter: whole screen  ·  Esc / right-click: cancel';
    cancel.type = 'button'; cancel.textContent = 'Cancel';
    cancel.addEventListener('click', snipCancel);
    bar.append(snip.tabs, hint, snip.note, cancel);
    layer.append(bar, snip.stage);
    layer.addEventListener('contextmenu', ev => ev.preventDefault());
  }
  const where = n => n.p.geo || [0, 0];
  picks.sort((a, b) => where(a)[0] - where(b)[0] || where(a)[1] - where(b)[1]);  // as the monitors stand
  const ids = picks.map(n => n.id).join(',');
  snip.picks = picks;
  if (ids !== snip.ids) {
    snip.ids = ids;
    snipFree();
    if (!snipCurrent()) snip.cur = picks[0].id;
    snip.tabs.replaceChildren(...(picks.length < 2 ? [] : picks.map((n, i) => {
      const tab = $('button', 'snip-btn');
      tab.type = 'button';
      tab.textContent = `Monitor ${i + 1}`;
      tab.dataset.id = n.id;
      tab.addEventListener('click', () => { if (!snip.busy) { snip.cur = n.id; snipShow(); } });
      return tab;
    })));
    // Keys now belong to the picker (Enter, Esc), not to whatever had the focus behind it.
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  }
  snipShow();
  return snip.layer;
}

function snipShow() {
  const n = snipCurrent();
  if (n && snip.stage.firstChild !== n.el) snip.stage.replaceChildren(n.el);
  if (n) snipFetch(n);
  for (const tab of snip.tabs.children) tab.classList.toggle('sel', +tab.dataset.id === snip.cur);
}

// Only the first monitor's screenshot comes unasked, so the picker is there quickly. The one on show is asked
// for at once if it is missing; the others when the one on show has arrived.
function snipFetch(n) { if (!n.p.img && !n.asked) { n.asked = true; send(['g', n.id]); } }

function snipMore() {
  const n = snipCurrent();
  if (n && n.im.complete && n.im.naturalWidth) snip.picks.forEach(snipFetch);
}

// ---- windows: the main one fills the page, every other one lies on top of it

function frame(n) {
  if (!n.frame) {
    const layer = n.frame = $('div', 'layer');
    if (n.k === 'menu') {
      layer.classList.add('pop');
      layer.append(n.el);
      n.el.style.left = Math.max(4, Math.min(pointer.x, innerWidth - 240)) + 'px';
      n.el.style.top = Math.max(4, Math.min(pointer.y, innerHeight - 40 - 24 * ((n.p.items || []).length))) + 'px';
      layer.addEventListener('pointerdown', ev => { if (ev.target === layer) cmd(['q', n.id]); });
    } else if (n.k === 'canvas') {
      layer.classList.add('bare');
      layer.append(n.el);
    } else {
      const box = n.box = $('div', 'dlg'), bar = $('div', 'dlg-bar'), close = $('button', 'dlg-x'), body = $('div', 'dlg-body');
      n.caption = $('span');
      close.type = 'button'; close.textContent = '✕'; close.title = 'Close';
      close.addEventListener('click', () => cmd(['q', n.id]));
      bar.append(n.caption, close);
      body.append(n.el);
      box.append(bar, body);
      layer.append(box);
    }
  }
  if (n.caption) n.caption.textContent = n.p.wt || '';
  if (n.box && n.p.wsz) {  // as wide as it is in the app; at least as tall, taller if the text needs it here
    n.box.style.width = n.p.wsz[0] + 'px';
    n.box.style.minHeight = `min(${n.p.wsz[1] + 32}px, 94vh)`;
  }
  return n.frame;
}

function placeRoots() {
  const main = N.get(roots[0]);
  if (main && main.el.parentNode !== rootEl) rootEl.replaceChildren(main.el);
  document.title = (main && main.p.wt) || meta.title || 'Remote';
  const want = [], picks = [];
  for (const id of roots.slice(1)) {
    const n = N.get(id);
    if (!n) continue;
    if (n.k !== 'snip') want.push(frame(n));
    else if (!n.over) picks.push(n);
  }
  if (picks.length) want.push(snipLayer(picks));
  else if (snip.ids) snipOver();
  sync(layersEl, want);
}

// ---- where the app is on its PC: the monitors to scale, with the window on them to drag and resize

const mapEl = document.getElementById('map'), mapBtn = document.getElementById('mapbtn');
const mapInfo = document.getElementById('mapinfo'), mapStage = document.getElementById('mapstage');
const mapWin = $('div');
const wm = {node: null, geo: null, scale: 1, pad: 0, left: 0, top: 0, right: 0, bottom: 0, drawn: '', drag: null, sending: 0, done: 0};
mapWin.id = 'mapwin';
for (const edge of ['n', 'e', 's', 'w', 'ne', 'nw', 'se', 'sw']) {
  const handle = $('div', 'map-h');
  handle.dataset.e = edge;
  mapWin.append(handle);
}

function showMap(open) {
  mapEl.classList.toggle('hid', !open);
  mapBtn.classList.toggle('on', open);
  try { localStorage.setItem('sc-map', open ? '1' : ''); } catch (e) { /* only the choice is lost */ }
  drawMap();
}

function mapSpot(el, r) {
  const s = el.style;
  s.left = wm.pad + (r[0] - wm.left) * wm.scale + 'px';
  s.top = (r[1] - wm.top) * wm.scale + 'px';
  s.width = r[2] * wm.scale + 'px';
  s.height = r[3] * wm.scale + 'px';
}

function drawMap() {
  const g = wm.geo;
  if (!g || mapEl.classList.contains('hid')) return;
  const wide = mapStage.clientWidth, drawn = JSON.stringify(g.m) + wide;
  if (drawn !== wm.drawn) {  // and only then: taking the window off the map would end a drag
    wm.drawn = drawn;
    wm.left = Math.min(...g.m.map(s => s[0]));
    wm.top = Math.min(...g.m.map(s => s[1]));
    wm.right = Math.max(...g.m.map(s => s[0] + s[2]));
    wm.bottom = Math.max(...g.m.map(s => s[1] + s[3]));
    wm.scale = Math.min(wide / (wm.right - wm.left), MAP_HIGH / (wm.bottom - wm.top));
    wm.pad = (wide - (wm.right - wm.left) * wm.scale) / 2;
    mapStage.style.height = Math.round((wm.bottom - wm.top) * wm.scale) + 'px';
    mapStage.replaceChildren(...g.m.map((s, i) => {
      const tile = $('div', 'map-scr' + (s[4] ? ' pri' : ''));
      tile.textContent = `${i + 1}  ·  ${s[2]} × ${s[3]}`;
      mapSpot(tile, s);
      return tile;
    }), mapWin);
  }
  drawWin();
}

function drawWin() {
  const g = wm.geo, r = wm.drag ? wm.drag.now : g.r, x = r[0] + r[2] / 2, y = r[1] + r[3] / 2;
  mapSpot(mapWin, r);
  mapWin.classList.toggle('min', g.st === 'min');
  const on = Math.max(0, g.m.findIndex(s => x >= s[0] && x < s[0] + s[2] && y >= s[1] && y < s[1] + s[3])), s = g.m[on];
  const state = {min: ' (minimized)', max: ' (maximized)', full: ' (full screen)'}[g.st] || '';
  mapInfo.textContent = `Monitor ${on + 1}: x ${r[0] - s[0]}, y ${r[1] - s[1]}, ${r[2]} × ${r[3]}${state}`;
}

function limit(value, low, high) { return Math.max(low, Math.min(value, high)); }

function dragged(d, dx, dy) {
  let [x, y, w, h] = d.from;
  const [leastW, leastH] = wm.geo.min, how = d.how;
  if (!how) {
    x = limit(x + dx, wm.left, wm.right - w);
    y = limit(y + dy, wm.top, wm.bottom - h);
  } else {
    if (how.includes('e')) w = limit(w + dx, leastW, wm.right - x);
    if (how.includes('s')) h = limit(h + dy, leastH, wm.bottom - y);
    if (how.includes('w')) { const end = x + w; x = limit(x + dx, wm.left, end - leastW); w = end - x; }
    if (how.includes('n')) { const end = y + h; y = limit(y + dy, wm.top, end - leastH); h = end - y; }
  }
  return [x, y, w, h].map(Math.round);
}

function sendWin() {
  clearTimeout(wm.sending);
  wm.sending = 0;
  const d = wm.drag;
  if (d && wm.node && d.now.some((value, i) => value !== d.shown[i])) cmd(['w', wm.node.id, ...d.now]);
}

function dropWin() {
  if (!wm.drag) return;
  sendWin();
  wm.done = setTimeout(() => { wm.drag = null; drawWin(); }, QUIET_MS);  // the app's answer is on its way: no jump back first
}

mapWin.addEventListener('pointerdown', ev => {
  if (!wm.geo) return;
  ev.preventDefault();
  clearTimeout(wm.done);
  mapWin.setPointerCapture(ev.pointerId);
  const g = wm.geo, from = (wm.drag ? wm.drag.now : g.r).slice(), shown = from.slice();
  if (g.st === 'max' || g.st === 'full') { from[2] = g.n[0]; from[3] = g.n[1]; }  // comes off at its own size
  wm.drag = {x: ev.clientX, y: ev.clientY, from, shown, now: shown, how: ev.target.dataset.e || ''};
});
mapWin.addEventListener('pointermove', ev => {
  const d = wm.drag;
  if (!d || !mapWin.hasPointerCapture(ev.pointerId)) return;
  d.now = dragged(d, (ev.clientX - d.x) / wm.scale, (ev.clientY - d.y) / wm.scale);
  drawWin();
  if (!wm.sending) wm.sending = setTimeout(sendWin, SCROLL_MS);
});
mapWin.addEventListener('pointerup', dropWin);
mapWin.addEventListener('pointercancel', dropWin);
mapBtn.addEventListener('click', () => showMap(mapEl.classList.contains('hid')));
document.getElementById('mapx').addEventListener('click', () => showMap(false));
window.addEventListener('resize', drawMap);
try { showMap(localStorage.getItem('sc-map') === '1'); } catch (e) { /* closed until asked for */ }

// ---- things that concern the page as a whole

function topWindow() { return roots.length > 1 ? N.get(roots[roots.length - 1]) : null; }

// While a region is being picked, Enter and Esc are the picker's, before anything else on the page sees them.
document.addEventListener('keydown', ev => {
  if (!snip.ids || (ev.key !== 'Escape' && ev.key !== 'Enter')) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (ev.repeat) return;
  if (ev.key === 'Escape') snipCancel(); else snipWhole();
}, true);

document.addEventListener('keydown', ev => {
  const top = topWindow();
  if (!top || top.k === 'snip') return;
  if (top.k === 'canvas') {
    if (['Shift', 'Control', 'Alt', 'Meta'].includes(ev.key)) return;
    ev.preventDefault();
    cmd(['k', top.id, ev.key, mods(ev)]);
  } else if (ev.key === 'Escape') {
    ev.preventDefault();
    cmd(['k', top.id, 'Escape', 0]);
  }
});

document.addEventListener('pointermove', ev => {
  pointer = {x: ev.clientX, y: ev.clientY};
  hovered = ev.target;
  if (!ev.buttons) letGo();  // letting go of a scroll bar is not always reported as such
}, {passive: true});
document.addEventListener('pointerdown', ev => { pointer = {x: ev.clientX, y: ev.clientY}; hovered = ev.target; }, true);
window.addEventListener('pointerup', letGo, true);
window.addEventListener('pointercancel', letGo, true);

function nodeAt(el) {
  const w = el && el.closest ? el.closest('.w') : null;
  return w ? w._n : null;
}

// A picture pasted or dropped here goes to the text box it was pasted into, or the one nearest the pointer.
function pictureTarget() {
  const a = document.activeElement;
  if (a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT')) return nodeAt(a);
  return nodeAt(hovered) || nodeAt(a);
}

// A file travels as: where it goes (4 bytes), the length of its name (2), the name, the file itself.
function upload(n, file) {
  if (file.size > 30 * 1024 * 1024) return;
  file.arrayBuffer().then(buffer => {
    if (!ws || ws.readyState !== 1) return;
    const name = new TextEncoder().encode((file.name || 'pasted').slice(0, 200));
    const out = new Uint8Array(6 + name.length + buffer.byteLength), view = new DataView(out.buffer);
    view.setUint32(0, n.id);
    view.setUint16(4, name.length);
    out.set(name, 6);
    out.set(new Uint8Array(buffer), 6 + name.length);
    flushText();
    ws.send(out);
  });
}

document.addEventListener('paste', ev => {
  const files = [...((ev.clipboardData && ev.clipboardData.files) || [])];
  const n = pictureTarget();
  if (!files.length || !n) return;  // plain text: pasted into the field as usual
  ev.preventDefault();
  for (const file of files) upload(n, file);
});
document.addEventListener('dragover', ev => { if (ev.dataTransfer && [...ev.dataTransfer.types].includes('Files')) ev.preventDefault(); });
document.addEventListener('drop', ev => {
  const files = [...((ev.dataTransfer && ev.dataTransfer.files) || [])];
  if (!files.length) return;
  ev.preventDefault();
  const n = nodeAt(ev.target);
  if (n) for (const file of files) upload(n, file);
});

// A double click or a right click on something that is not a control of its own (a tile, a card).
function plainNode(target) {
  let n = nodeAt(target);
  if (!n || (n.k !== 'box' && n.k !== 'lbl')) return null;
  while (n && n.k !== 'box') n = nodeAt(n.el.parentElement);
  return n;
}
document.addEventListener('dblclick', ev => { const n = plainNode(ev.target); if (n) cmd(['d', n.id]); });
document.addEventListener('contextmenu', ev => {
  const n = plainNode(ev.target);
  if (!n) return;  // text keeps the browser's own menu (copy, paste)
  ev.preventDefault();
  cmd(['x', n.id]);
});

readKey();
connect();
})();
