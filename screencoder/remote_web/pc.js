// The app's PC itself (pc_control.py on the PC): a picture of one of its monitors, and what is done on the picture
// is done there - a click, a drag, a held press, the wheel, keys. Nothing is streamed: each action goes as one
// message, and the answer is a new picture of the monitor - only the parts that changed since the last one.
(() => {
  'use strict';
  const $id = id => document.getElementById(id);
  const btn = $id('pcbtn'), box = $id('pc'), stage = $id('pcstage'), canvas = $id('pccanvas'), dot = $id('pccur');
  const info = $id('pcinfo'), mons = $id('pcmons'), form = $id('pcform'), typed = $id('pctype'), enter = $id('pcenter');
  const ctx = canvas.getContext('2d');
  const DOUBLE_MS = 300;   // a second click this soon, near the first, makes a double-click
  const KEYS_MS = 250;     // keys typed in a burst go together (one picture), this long after the last
  const WHEEL_MS = 220;    // the same for the wheel
  const MOVE_PX = 6;       // moved less than this (in the picture's pixels), a press is a click
  const LONG_MS = 600;     // held longer than this, it is a held press, not a click
  const MODIFIERS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock', 'ScrollLock']);
  let mon = 0, waiting = 0, sentAt = 0, gotAt = 0, lastBytes = 0, lastMs = 0, chain = Promise.resolve();
  let press = null, pending = null, wheel = null, keys = [], keyTimer = 0, lastPoint = [0.5, 0.5], monitors = [];

  const open = () => !box.classList.contains('hid');
  const width = () => Math.round(Math.min(2560, Math.max(480, stage.clientWidth * (window.devicePixelRatio || 1))));
  // Ctrl / Alt / Shift / Win buttons in the bar: held for the next key or click (then let go), for combinations the
  // viewer's own browser or computer keeps for itself - Ctrl+W, Alt+Tab, the Windows key.
  const modButtons = [...document.querySelectorAll('#pcmods button')];
  let sticky = 0;
  const held = ev => (ev.shiftKey ? 1 : 0) | (ev.ctrlKey ? 2 : 0) | (ev.altKey ? 4 : 0) | (ev.metaKey ? 8 : 0);
  function mods(ev) {
    const m = held(ev) | sticky;
    if (sticky) { sticky = 0; modButtons.forEach(b => b.classList.remove('on')); }
    return m;
  }
  modButtons.forEach(b => b.addEventListener('click', () => {
    sticky ^= +b.dataset.m;
    b.classList.toggle('on', !!(sticky & +b.dataset.m));
    canvas.focus();
  }));
  const keyList = $id('pckeys');
  keyList.addEventListener('change', () => {
    const [key, m] = keyList.value.split('|');
    keyList.value = '';
    canvas.focus();
    if (!key) return;
    keys.push(['k', key, +m | sticky]);
    if (sticky) { sticky = 0; modButtons.forEach(b => b.classList.remove('on')); }
    flushKeys();
  });

  function ask(message) {
    waiting++;
    sentAt = performance.now();
    show();
    window.scSend(message);  // app.js: the page's one connection to the app
  }

  function show(text, kind) {
    info.className = kind || (waiting ? 'busy' : '');
    if (text != null) { info.textContent = text; return; }
    if (waiting) { info.textContent = 'sending…'; return; }
    const ago = gotAt ? Math.round((performance.now() - gotAt) / 1000) : 0;
    const kb = lastBytes ? ` · ${Math.max(1, Math.round(lastBytes * 0.75 / 1024))} KB` : '';
    info.textContent = gotAt ? `Monitor ${mon + 1} · ${(lastMs / 1000).toFixed(1)} s${kb}` + (ago > 2 ? ` · ${ago} s ago` : '') : '';
  }

  function toggle(on) {
    box.classList.toggle('hid', !on);
    btn.classList.toggle('on', on);
    if (on) { fit(); canvas.focus(); ask(['pcv', mon, width()]); }
  }
  btn.addEventListener('click', () => toggle(!open()));
  $id('pcx').addEventListener('click', () => toggle(false));
  $id('pcrefresh').addEventListener('click', () => ask(['pcv', mon, width()]));
  $id('pcup').addEventListener('click', () => ask(['pcw', mon, width(), lastPoint[0], lastPoint[1], 0, -300]));
  $id('pcdown').addEventListener('click', () => ask(['pcw', mon, width(), lastPoint[0], lastPoint[1], 0, 300]));
  form.addEventListener('submit', ev => {
    ev.preventDefault();
    const events = typed.value ? [['t', typed.value]] : [];
    if (enter.checked) events.push(['k', 'Enter', 0]);
    if (!events.length) return;
    typed.value = '';
    ask(['pck', mon, width(), events]);
  });

  // ---- the picture: whole, or the pieces that changed, drawn in the order they came
  function load(url) {
    return new Promise(done => { const img = new Image(); img.onload = () => done(img); img.onerror = () => done(null); img.src = url; });
  }

  window.pcAnswer = a => {
    waiting = Math.max(0, waiting - 1);
    if (a.err) { show(a.err, 'err'); return; }
    if (a.mons) monitorButtons(a.mons);
    const urls = a.whole ? [a.whole] : (a.pieces || []).map(p => p[4]);
    lastBytes = urls.reduce((n, u) => n + u.length, 0);
    lastMs = performance.now() - sentAt;
    chain = chain.then(async () => {
      if (a.whole) {
        const img = await load(a.whole);
        if (!img) return;
        if (canvas.width !== a.w || canvas.height !== a.h) { canvas.width = a.w; canvas.height = a.h; fit(); }
        ctx.drawImage(img, 0, 0);
      } else if (a.pieces) {
        if (canvas.width !== a.w || canvas.height !== a.h) { ask(['pcv', mon, width()]); return; }  // out of step
        for (const [x, y, , , url] of a.pieces) {
          const img = await load(url);
          if (img) ctx.drawImage(img, x, y);
        }
      }
      cursor(a.cur);
      gotAt = performance.now();
      show();
    });
  };

  function monitorButtons(list) {
    monitors = list;
    if (mons.children.length !== list.length) {
      mons.textContent = '';
      list.forEach((m, i) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = `${i + 1}${m[3] ? ' ★' : ''}`;
        b.title = `Monitor ${i + 1} · ${m[1]} × ${m[2]}${m[3] ? ' · the main one' : ''}`;
        b.addEventListener('click', () => { if (i !== mon) { mon = i; ask(['pcv', mon, width()]); } });
        mons.append(b);
      });
    }
    [...mons.children].forEach((b, i) => b.classList.toggle('on', i === mon));
  }

  // The picture as large as the room allows, keeping its shape; places on it are fractions of it.
  function fit() {
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    const scale = Math.min(w / canvas.width, h / canvas.height);
    canvas.style.width = Math.floor(canvas.width * scale) + 'px';
    canvas.style.height = Math.floor(canvas.height * scale) + 'px';
  }
  let resized = 0;
  window.addEventListener('resize', () => {
    if (!open()) return;
    fit();
    clearTimeout(resized);
    resized = setTimeout(() => { if (Math.abs(width() - canvas.width) > canvas.width * 0.2) ask(['pcv', mon, width()]); }, 600);
  });

  function cursor(at) {
    if (!at) { dot.style.display = 'none'; return; }
    const r = canvas.getBoundingClientRect(), s = stage.getBoundingClientRect();
    dot.style.left = (r.left - s.left + at[0] / canvas.width * r.width) + 'px';
    dot.style.top = (r.top - s.top + at[1] / canvas.height * r.height) + 'px';
    dot.style.display = 'block';
  }

  function where(ev, ms) {
    const r = canvas.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
    const y = Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height));
    return [Math.round(x * 1e4) / 1e4, Math.round(y * 1e4) / 1e4, Math.round(ms)];
  }

  function tap(ev) {  // seen at once, before the picture comes back
    const s = stage.getBoundingClientRect(), ring = document.createElement('div');
    ring.className = 'pc-tap';
    ring.style.left = (ev.clientX - s.left) + 'px';
    ring.style.top = (ev.clientY - s.top) + 'px';
    stage.append(ring);
    setTimeout(() => ring.remove(), 700);
  }

  // ---- the mouse: pressed, maybe moved while held, let go - all of it one message
  canvas.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    canvas.focus();
    canvas.setPointerCapture(ev.pointerId);
    press = {button: ev.button === 2 ? 2 : ev.button === 1 ? 1 : 0, t0: performance.now(), mods: mods(ev),
             points: [where(ev, 0)], lastAt: 0};
    tap(ev);
  });
  canvas.addEventListener('pointermove', ev => {
    if (!press) return;
    const ms = performance.now() - press.t0, p = where(ev, ms), last = press.points[press.points.length - 1];
    const px = Math.hypot((p[0] - last[0]) * canvas.width, (p[1] - last[1]) * canvas.height);
    if (px >= 3 && ms - press.lastAt >= 15) { press.points.push(p); press.lastAt = ms; }
  });
  canvas.addEventListener('pointerup', ev => {
    if (!press) return;
    const ms = performance.now() - press.t0, end = where(ev, ms), first = press.points[0];
    press.points.push(end);
    lastPoint = [end[0], end[1]];
    const moved = Math.hypot((end[0] - first[0]) * canvas.width, (end[1] - first[1]) * canvas.height);
    const done = press;
    press = null;
    if (moved >= MOVE_PX || ms >= LONG_MS) { flushPending(); mouse(done, 1); return; }  // a drag, or held
    // A click: it may be the first of a double-click, so it waits a moment for the second.
    if (pending && pending.done.button === done.button && performance.now() - pending.at < DOUBLE_MS
        && Math.hypot((first[0] - pending.done.points[0][0]) * canvas.width,
                      (first[1] - pending.done.points[0][1]) * canvas.height) < MOVE_PX * 2) {
      clearTimeout(pending.timer);
      const was = pending.done;
      pending = null;
      mouse(was, 2);
      return;
    }
    flushPending();
    pending = {done, at: performance.now(), timer: setTimeout(() => { const d = pending.done; pending = null; mouse(d, 1); }, DOUBLE_MS)};
  });
  canvas.addEventListener('pointercancel', () => { press = null; });
  canvas.addEventListener('contextmenu', ev => ev.preventDefault());  // the right click is the PC's

  function flushPending() {
    if (!pending) return;
    clearTimeout(pending.timer);
    const d = pending.done;
    pending = null;
    mouse(d, 1);
  }

  function mouse(done, clicks) {
    flushKeys();
    ask(['pcm', mon, width(), done.button, clicks, done.points, done.mods]);
  }

  // ---- the wheel: a burst of it as one message
  canvas.addEventListener('wheel', ev => {
    ev.preventDefault();
    const k = ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? 800 : 1, p = where(ev, 0);
    if (!wheel) wheel = {x: p[0], y: p[1], dx: 0, dy: 0, timer: setTimeout(flushWheel, WHEEL_MS)};
    wheel.dx += ev.deltaX * k;
    wheel.dy += ev.deltaY * k;
    lastPoint = [p[0], p[1]];
  }, {passive: false});

  function flushWheel() {
    if (!wheel) return;
    const w = wheel;
    wheel = null;
    if (Math.abs(w.dx) < 1 && Math.abs(w.dy) < 1) return;
    flushPending();
    flushKeys();
    ask(['pcw', mon, width(), w.x, w.y, Math.round(w.dx), Math.round(w.dy)]);
  }

  // ---- keys: while the picture is open, every key is the PC's (the text box above types normally)
  window.addEventListener('keydown', ev => {
    if (!open() || ev.target === typed) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (MODIFIERS.has(ev.key) || ev.isComposing) return;
    // AltGr arrives as Ctrl+Alt and types a character (@ on many keyboards); a real Ctrl+Alt+key is a combination.
    const altGr = !!(ev.getModifierState && ev.getModifierState('AltGraph'));
    const m = mods(ev);
    if (ev.key.length === 1 && (!(m & 14) || altGr)) {
      const last = keys[keys.length - 1];
      if (last && last[0] === 't') last[1] += ev.key; else keys.push(['t', ev.key]);
    } else {
      keys.push(['k', ev.key, m]);
    }
    clearTimeout(keyTimer);
    keyTimer = setTimeout(flushKeys, KEYS_MS);
    show('typing…', 'busy');
  }, true);
  window.addEventListener('keyup', ev => { if (open() && ev.target !== typed) { ev.preventDefault(); ev.stopImmediatePropagation(); } }, true);

  function flushKeys() {
    clearTimeout(keyTimer);
    keyTimer = 0;
    if (!keys.length) return;
    const events = keys;
    keys = [];
    ask(['pck', mon, width(), events]);
  }
})();
