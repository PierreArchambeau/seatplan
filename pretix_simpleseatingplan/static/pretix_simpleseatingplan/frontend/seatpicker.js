/* seatpicker.js — Plan unique multi-billets (Pretix checkout)
 * - Un seul plan
 * - Plusieurs champs "Seat/Siège/Place..." gérés
 * - Clic = remplit le champ actif (ou le premier vide)
 * - Anti-doublon local + (optionnel) hold côté serveur
 * - sold/held refresh (status_url)
 */

(function () {
  'use strict';

  // Liste des "shape" SVG usuelles à colorer
  const SHAPES = ['path','circle','ellipse','rect','polygon','polyline','line','text','g','use'];

  /**
   * Applique un fill/ stroke direct aux éléments graphiques ciblés.
   * - Si le style inline existe -> el.style.fill / el.style.stroke
   * - Sinon -> attributs de présentation 'fill' / 'stroke'
   */
  function applyDirectColor(el, { fill = null, stroke = null } = {}, { includeSelf = true } = {}) {
    const targets = [];
    if (includeSelf && SHAPES.includes(el.tagName?.toLowerCase())) targets.push(el);
    // Descendants graphiques
    targets.push(...el.querySelectorAll(SHAPES.join(',')));

    targets.forEach(node => {
      // Clean previous fill/stroke via DOM API (CSP-safe, no setAttribute('style'))
      if (node.style) {
        node.style.removeProperty('fill');
        node.style.removeProperty('stroke');
      }

      // Apply fill/stroke via presentation attributes + DOM style API
      if (fill != null) {
        node.setAttribute('fill', fill);
        if (node.style) node.style.fill = fill;
      }
      if (stroke != null) {
        node.setAttribute('stroke', stroke);
        if (node.style) node.style.stroke = stroke;
      }
    });
  }

  /**
   * Met a jour l'apparence d'un siege selon les etats sold/held/selected/free.
   */
  function setSeatVisualState(el, { isSold, isHeld, isSelected }, palette = {
    sold:     { fill: '#ea0d0d', stroke: '#bbbbbb' },
    held:     { fill: '#f2b705', stroke: '#f2b705' },
    selected: { fill: '#2563eb', stroke: '#1e40af' },
    free:     { fill: '#22c55e', stroke: '#0f5f96' },
  }) {
    // Save original colors the first time we see this element
    if (!el.hasAttribute('data-orig-fill')) {
      const shapes = [el, ...el.querySelectorAll(SHAPES.join(','))];
      const firstShape = shapes.find(s => s.getAttribute('fill'));
      const origFill = firstShape ? firstShape.getAttribute('fill') : '';
      const origStroke = firstShape ? (firstShape.getAttribute('stroke') || '') : '';
      el.setAttribute('data-orig-fill', origFill);
      el.setAttribute('data-orig-stroke', origStroke);
    }

    if (isSold) {
      applyDirectColor(el, palette.sold, { includeSelf: true });
      el.style.pointerEvents = 'none';
      el.style.opacity = '0.55';
    } else if (isHeld) {
      applyDirectColor(el, palette.held, { includeSelf: true });
      el.style.pointerEvents = 'none';
      el.style.opacity = '0.75';
    } else if (isSelected) {
      applyDirectColor(el, palette.selected, { includeSelf: true });
      el.style.pointerEvents = '';
      el.style.opacity = '';
    } else {
      // Free seat: use the free palette color
      applyDirectColor(el, palette.free, { includeSelf: true });
      el.style.pointerEvents = '';
      el.style.opacity = '';
    }
  }

  // ========= Logs & garde-fous =========
  const logW = (...a) => console.warn('[seatpicker]', ...a);
  const logE = (...a) => console.error('[seatpicker]', ...a);

  window.addEventListener('error', e => logE('window.onerror', e.message, e.filename, e.lineno));
  window.addEventListener('unhandledrejection', e => logE('unhandledrejection', e.reason));

  // ========= CSRF / fetch =========
  function getCSRFFromMeta() {
    const m = document.querySelector('meta[name="csrf-token"], meta[name="csrf"]');
    return m ? m.getAttribute('content') : null;
  }
  function getCSRFFromForm() {
    const i = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return i ? i.value : null;
  }
  function getCSRFFromCookie() {
    const cs = (document.cookie || '').split(';');
    const pick = (n) => {
      const r = cs.find((c) => c.trim().startsWith(n + '='));
      return r ? decodeURIComponent(r.split('=')[1]) : null;
    };
    return pick('csrftoken') || pick('pretix_csrftoken') || pick('csrf');
  }
  function getCSRF() { return getCSRFFromMeta() || getCSRFFromForm() || getCSRFFromCookie(); }

  async function getJSON(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  async function postForm(url, data) {
    const b = new URLSearchParams(data);
    const h = {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
    };
    const t = getCSRF();
    if (t) h['X-CSRFToken'] = t;
    const r = await fetch(url, { method: 'POST', headers: h, credentials: 'same-origin', body: b });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, json: j };
  }

  // ========= Détection des champs Seat =========
  /**
   * Extrait le cart position ID depuis le name d'un champ input.
   * Pretix checkout: name="{cartpos_id}-question_{question_id}"
   * Ex: "42-question_7" => 42
   */
  function posIdFromName(name) {
    if (!name) return null;
    // Pretix standard: {cartpos_id}-question_{qid}
    const m = name.match(/^(\d+)-question_\d+$/);
    if (m) return parseInt(m[1], 10);
    // Fallback: premier nombre dans le name
    const nums = name.match(/\d+/g);
    return nums ? parseInt(nums[0], 10) : null;
  }

  /**
   * Trouve le vrai cart position ID Pretix pour un champ input donné.
   * Cherche dans le DOM un data-cartpos, un hidden input, ou le name du champ.
   */
  function findCartPosId(inputEl) {
    // 1) data-cartpos sur un conteneur parent
    const container = inputEl.closest('[data-cartpos], [data-id], [data-position], .cart-position, .product-row');
    if (container) {
      const cid = container.getAttribute('data-cartpos')
                || container.getAttribute('data-id')
                || container.getAttribute('data-position');
      if (cid && /^\d+$/.test(cid)) {
        return parseInt(cid, 10);
      }
    }
    // 2) hidden input dans le même form-group
    const group = inputEl.closest('form, .questions-form, .cart-position, .product-row');
    if (group) {
      const hidden = group.querySelector('input[name*="cartpos"], input[name*="position_id"]');
      if (hidden && hidden.value && /^\d+$/.test(hidden.value)) {
        return parseInt(hidden.value, 10);
      }
    }
    // 3) Fallback: extraire du name
    const fromName = posIdFromName(inputEl.name);
    return fromName;
  }

  /**
   * Vérifie si la question identifiée par qid est présente sur la page.
   * Utilise les conventions de nommage Pretix pour détecter précisément.
   */
  function isQuestionOnPage(qid) {
    if (!qid) return true;
    const q = String(qid);
    // Pretix checkout: name="{cartpos_id}-question_{question_id}"
    const re = new RegExp('^\\d+-question_' + q + '$');
    for (const el of document.querySelectorAll('input, textarea, select')) {
      const name = el.name || '';
      if (name && re.test(name)) return true;
    }
    return false;
  }

  function findAllSeatInputs() {
    const langs = /(seat|si\u00e8ge|siege|place|platz|asiento)/i;
    const guidExclude = /(guid|identifiant)/i;
    const set = new Set();
    const cfg = window.SimpleSeatingPlanCfg || {};

    // 1) Via la question_label_id si disponible dans la config
    //    Pretix checkout: name="{cartpos_id}-question_{question_id}"
    if (cfg.question_label_id) {
      const q = String(cfg.question_label_id);
      const re = new RegExp('^\\d+-question_' + q + '$');
      document.querySelectorAll('input, textarea, select').forEach(el => {
        if (el.type === 'hidden') return;
        const name = el.name || '';
        if (re.test(name)) set.add(el);
      });
    }

    // 2) Via <label for="...">
    document.querySelectorAll('label[for]').forEach(lab => {
      const txt = (lab.textContent || '').trim();
      if (langs.test(txt) && !guidExclude.test(txt)) {
        const inp = document.getElementById(lab.getAttribute('for'));
        if (inp && /^(INPUT|SELECT|TEXTAREA)$/.test(inp.tagName) && inp.type !== 'hidden') set.add(inp);
      }
    });

    // 3) Fallback: chercher dans name/id (exclude GUID and hidden inputs)
    document.querySelectorAll('input,select,textarea').forEach(el => {
      if (el.type === 'hidden') return;
      const key = (el.name || '') + ' ' + (el.id || '');
      if (langs.test(key) && !guidExclude.test(key)) set.add(el);
    });

    return Array.from(set);
  }

  function groupForInput(input) {
    return input.closest('.form-group, .question, .control-group, .row') || input.parentElement || input;
  }

  // ========= Conteneur unique =========
  function ensureSingleContainer() {
    let cont = document.querySelector('[data-seatmap]');
    if (cont) return cont;

    // Place the plan BEFORE the seat fields (standard UX: pick then fill)
    const seats = findAllSeatInputs();
    let anchor = seats.length ? groupForInput(seats[0]) : (document.querySelector('.product-container') || document.body);

    // Remonter au-dessus d'un éventuel <table> pour ne pas injecter un <div> dans un tableau
    let node = anchor;
    while (node && node !== document.body) {
      const tag = node.tagName && node.tagName.toLowerCase();
      if (tag === 'table' || tag === 'tbody' || tag === 'thead' || tag === 'tr' || tag === 'td' || tag === 'th') {
        // Remonter jusqu'à trouver le <table> englobant, puis s'ancrer juste avant
        const table = node.closest('table');
        if (table) { anchor = table; break; }
      }
      break;
    }

    const wrap = document.createElement('div');
    wrap.className = 'my-seatmap-wrapper';
    // Insert BEFORE the anchor (first seat field group or table)
    if (anchor.parentElement) {
      anchor.parentElement.insertBefore(wrap, anchor);
    } else {
      document.body.prepend(wrap);
    }

    cont = document.createElement('div');
    cont.className = 'my-seatmap';
    cont.setAttribute('data-seatmap', '');
    wrap.appendChild(cont);

    return cont;
  }

  function ensureLegend(container) {
    let lg = container.querySelector('.legend');
    if (!lg) {
      lg = document.createElement('div');
      lg.className = 'legend';
      container.appendChild(lg);
    }
    return lg;
  }
  function setLegend(container, msg) {
    ensureLegend(container).textContent = msg;
  }

  // ========= SVG helpers =========
  function svgFromStringInto(el, svgText) {
    try {
      el.innerHTML = svgText;
      const svg = el.querySelector('svg');
      if (svg) return svg;
      throw new Error('No <svg>');
    } catch (e) { logE('Injection SVG (string) échouée', e); return null; }
  }

  async function svgFromUrlInto(el, url) {
    try {
      const r = await fetch(url, { credentials: 'same-origin' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const txt = await r.text();
      el.innerHTML = txt;
      const svg = el.querySelector('svg');
      if (svg) return svg;
      throw new Error('No <svg> root');
    } catch (e) { logE('Chargement SVG URL échoué', url, e); return null; }
  }

  // ========= État global multi-billets =========
  const g = {
    svg: null,
    container: null,
    inputs: [],                 // tous les champs Seat
    activeInput: null,          // champ Seat actuellement actif (focus)
    seatToInput: new Map(),     // seatId -> input (anti-doublon)
    inputToGuid: new Map(),     // input -> seatGuid currently held for that input
  };

  function setActiveInput(inp) {
    g.activeInput = inp || null;
    g.inputs.forEach(i => i.classList.remove('seat-active'));
    if (g.activeInput) g.activeInput.classList.add('seat-active');
  }

  function firstEmptyInput() {
    return g.inputs.find(i => !i.value?.trim());
  }

  function nextInput(current) {
    if (!current) return g.inputs[0];
    const idx = g.inputs.indexOf(current);
    if (idx === -1 || idx === g.inputs.length - 1) return null;
    return g.inputs[idx + 1];
  }


    // ========= Extraction id/label à partir du noeud cliqué =========
  function makeExtractors(cfg) {
    const prefix = cfg.prefix || '';
    const labelMap = cfg.label_map || null;

    const idFromEl = (el) => {
      // const ds = el.getAttribute('data-seat-id');
      // if (ds && ds.trim()) return ds.trim();
      const raw = el.getAttribute('id') || '';
      if (!raw) return null;
      return prefix ? (raw.startsWith(prefix) ? raw.substring(prefix.length) : null) : raw;
    };

    const labelFromEl = (el, fallbackId) => {
      const lbl = el.getAttribute('data-seat-label');

      if (lbl && lbl.trim()) return lbl.trim();
      if (labelMap && fallbackId && labelMap[fallbackId]) return labelMap[fallbackId];
      return fallbackId || '';
    };

    return { idFromEl, labelFromEl };
  }

  // ========= HOLD serveur optionnel =========
  async function tryHoldIfConfigured(cfg, seatGuid, targetInput) {
    if (!cfg.hold_url) return true;
    try {
      const cartposId = findCartPosId(targetInput);
      const payload = { seat_guid: seatGuid };
      payload['cartpos_id'] = cartposId ? String(cartposId) : '0';

      const res = await postForm(cfg.hold_url, payload);
      if (!res.ok) {
        const err = res.json?.error || res.json?.detail || 'unknown';
        // Block only if seat is sold or held by someone else
        if (err === 'sold') {
          setLegend(g.container, 'Ce siège est déjà vendu.');
          return false;
        }
        if (err === 'held') {
          setLegend(g.container, 'Ce siège est réservé par un autre utilisateur.');
          return false;
        }
        logW('Hold warning (non-blocking):', res.status, err);
        return true;
      }
      return true;
    } catch (e) {
      logW('Hold network error (non-blocking):', e);
      return true;
    }
  }

  /**
   * Release the hold for a seat_guid via the release endpoint.
   * Fire-and-forget: does not block the UI.
   */
  async function tryReleaseHold(cfg, seatGuid) {
    if (!cfg.release_url || !seatGuid) return;
    try {
      const res = await postForm(cfg.release_url, { seat_guid: seatGuid });
    } catch (e) {
      logW('Release network error (non-blocking):', e);
    }
  }

  // ========= Binding clics sur le plan unique =========

  /**
   * Rafraichit le style "selected" (bleu) sur le plan pour tous les
   * sieges actuellement presents dans les champs du formulaire.
   * Utilise le label (data-seat-label) pour faire la correspondance.
   */
  function refreshSelectedVisuals(svg, cfg) {
    const prefix = cfg.prefix || '';
    const sel = prefix ? `[id^='${prefix}'], [data-seat-id]` : '[id], [data-seat-id]';

    // Collecter les labels de tous les champs seat remplis
    const selectedLabels = new Set();
    for (const inp of g.inputs) {
      const label = (inp.value || '').trim();
      if (label) selectedLabels.add(label);
    }

    if (selectedLabels.size === 0) return;

    // Appliquer le style a chaque siege dont le label correspond
    svg.querySelectorAll(sel).forEach(node => {
      const seatLabel = (node.getAttribute('data-seat-label') || '').trim();
      if (seatLabel && selectedLabels.has(seatLabel)) {
        setSeatVisualState(node, { isSold: false, isHeld: false, isSelected: true });
      }
    });
  }

  function bindPlanClicks(svg, cfg) {
    const { idFromEl, labelFromEl } = makeExtractors(cfg);

    svg.addEventListener('click', async (e) => {
      const sel = cfg.prefix ? `[id^='${cfg.prefix}'], [data-seat-id]` : '[id], [data-seat-id]';
      const el = e.target.closest(sel);
      if (!el) return;

      // Ignore drag-end clicks (mouse was moved during pan)
      const vp = svg.closest('.seat-viewport');
      if (vp && vp._wasDrag && vp._wasDrag()) return;

      // Verifier si le siege est indisponible (sold/held par un autre)
      if (el.style.pointerEvents === 'none') {
        setLegend(g.container, 'Ce siège n\'est pas disponible.');
        return;
      }

      const guid = idFromEl(el);
      if (!guid) return;
      const label = labelFromEl(el, guid);

      // Choisir l'input cible : actif sinon 1er vide
      let target = g.activeInput || firstEmptyInput() || g.inputs[0];
      if (!target) { setLegend(g.container, 'Aucun champ "Siège" trouvé.'); return; }

      // Anti-doublon : si deja pris par un autre input
      const already = g.seatToInput.get(label);
      if (already && already !== target) {
        setLegend(g.container, 'Le siège ' + label + ' est déjà attribué.');
        return;
      }

      // Release old hold for this input (if user is changing seat)
      const prevGuid = g.inputToGuid.get(target);
      if (prevGuid && prevGuid !== guid) {
        tryReleaseHold(cfg, prevGuid); // fire-and-forget
      }

      // Hold new seat on server
      const ok = await tryHoldIfConfigured(cfg, guid, target);
      if (!ok) return;

      // Track which GUID is held for this input
      g.inputToGuid.set(target, guid);

      // Liberer l'ancienne valeur de cet input (si existait)
      const prevLabel = (target.value || '').trim();
      if (prevLabel) g.seatToInput.delete(prevLabel);

      // Affecter valeur lisible
      target.value = label;
      target.dispatchEvent(new Event('change', { bubbles: true }));
      g.seatToInput.set(label, target);


      // Visuel : marquer TOUS les sieges selectionnes dans le formulaire
      refreshSelectedVisuals(svg, cfg);

      setLegend(g.container, 'Siège sélectionné : ' + label);

      // Passer au champ suivant s'il est vide
      const nxt = nextInput(target);
      if (nxt) setActiveInput(nxt);
    });
  }

  // ========= Refresh sold/held (partagé) =========
  function scheduleRefresh(svg, cfg) {
    if (!cfg.status_url) return;
    const prefix = cfg.prefix || '';

    const refresh = async () => {
      try {
        const st = await getJSON(cfg.status_url);
        const sold = new Set(st.sold || []);
        const held = new Set(st.held || []);
        const nodes = prefix
          ? svg.querySelectorAll(`[id^='${prefix}']`)
          : svg.querySelectorAll('[id], [data-seat-id]');

        nodes.forEach((el) => {
          // let id = el.getAttribute('data-seat-id');
          let id = el.getAttribute('id');
          if (!id) {
            const raw = el.getAttribute('id') || '';
            id = prefix ? (raw.startsWith(prefix) ? raw.substring(prefix.length) : null) : raw || null;
          }
          if (!id) return;

          // remove prefix
          id = id.startsWith(prefix) ? id.substring(prefix.length) : id;
          const isSold = sold.has(id);
          const isHeld = held.has(id) && !isSold;
          // el.classList.toggle('sold', isSold);
          // el.classList.toggle('held', isHeld);

          // 4) attributs (toujours mis à jour)
          // el.setAttribute('sold', String(isSold)); // "true"/"false"
          // el.setAttribute('held', String(isHeld));

          // Appliquer directement les couleurs dans le SVG
          setSeatVisualState(el, { isSold, isHeld });

        });
        // Re-apply selected styles on top of sold/held
        if (g.svg) refreshSelectedVisuals(svg, cfg);
      } catch (_) { /* silencieux */ }
    };

    refresh();
    const iv = setInterval(refresh, cfg.status_interval_ms || 1000);
    g.container._cancelRefresh = () => clearInterval(iv);
  }

  // ========= Fit SVG to viewport =========
  function fitSvgToViewport(svg, viewport, panLayer) {
    // Make the SVG fill the viewport at initial zoom
    const vb = svg.getAttribute('viewBox');
    if (vb) {
      // Use viewBox dimensions to size the SVG correctly
      const parts = vb.split(/[\s,]+/).map(Number);
      if (parts.length === 4) {
        const [, , vbW, vbH] = parts;
        const vpW = viewport.clientWidth || 860;
        const vpH = viewport.clientHeight || 480;
        const scale = Math.min(vpW / vbW, vpH / vbH);
        svg.style.width = (vbW * scale) + 'px';
        svg.style.height = (vbH * scale) + 'px';
        svg.removeAttribute('width');
        svg.removeAttribute('height');
      }
    } else {
      // No viewBox: use natural dimensions
      svg.style.width = '100%';
      svg.style.height = 'auto';
    }
  }

  // ========= Zoom & Pan =========
  function initZoomPan(viewport, panLayer, container) {
    let scale = 1, panX = 0, panY = 0;
    const MIN_SCALE = 0.5, MAX_SCALE = 5, ZOOM_STEP = 0.06;
    let dragging = false, startX = 0, startY = 0, startPanX = 0, startPanY = 0;
    let dragMoved = false; // track if mouse actually moved (vs. click)

    function applyTransform() {
      panLayer.style.transform = 'translate(' + panX + 'px, ' + panY + 'px) scale(' + scale + ')';
    }

    function zoomAt(cx, cy, delta) {
      const oldScale = scale;
      scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta));
      // Adjust pan so zoom is centered on pointer
      const ratio = scale / oldScale;
      panX = cx - ratio * (cx - panX);
      panY = cy - ratio * (cy - panY);
      applyTransform();
    }

    // Mouse wheel zoom
    viewport.addEventListener('wheel', (e) => {
      e.preventDefault();
      const rect = viewport.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const delta = e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP;
      zoomAt(cx, cy, delta);
    }, { passive: false });

    // Pan: mouse drag
    viewport.addEventListener('mousedown', (e) => {
      // Only pan with left button, and not on zoom controls
      if (e.button !== 0 || e.target.closest('.seat-controls')) return;
      dragging = true;
      dragMoved = false;
      startX = e.clientX;
      startY = e.clientY;
      startPanX = panX;
      startPanY = panY;
      viewport.classList.add('grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMoved = true;
      panX = startPanX + dx;
      panY = startPanY + dy;
      applyTransform();
    });
    window.addEventListener('mouseup', () => {
      if (dragging) {
        dragging = false;
        viewport.classList.remove('grabbing');
      }
    });

    // Touch: 2 doigts = pan + pinch-zoom, 1 doigt = clic siège uniquement
    let lastTouchDist = 0, lastTouchMid = null;
    viewport.addEventListener('touchstart', (e) => {
      if (e.target.closest('.seat-controls')) return;
      if (e.touches.length === 2) {
        dragging = true;
        dragMoved = false;
        const t = e.touches;
        const mx = (t[0].clientX + t[1].clientX) / 2;
        const my = (t[0].clientY + t[1].clientY) / 2;
        startX = mx;
        startY = my;
        startPanX = panX;
        startPanY = panY;
        lastTouchDist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY);
        const rect = viewport.getBoundingClientRect();
        lastTouchMid = { x: mx - rect.left, y: my - rect.top };
      }
    }, { passive: true });
    viewport.addEventListener('touchmove', (e) => {
      if (e.touches.length === 2) {
        const t = e.touches;
        const mx = (t[0].clientX + t[1].clientX) / 2;
        const my = (t[0].clientY + t[1].clientY) / 2;
        // Pan
        const dx = mx - startX;
        const dy = my - startY;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMoved = true;
        panX = startPanX + dx;
        panY = startPanY + dy;
        // Pinch-zoom
        const dist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY);
        if (lastTouchDist) {
          const delta = (dist - lastTouchDist) * 0.005;
          const rect = viewport.getBoundingClientRect();
          zoomAt(mx - rect.left, my - rect.top, delta);
        }
        lastTouchDist = dist;
        applyTransform();
        e.preventDefault();
      }
    }, { passive: false });
    viewport.addEventListener('touchend', () => {
      dragging = false;
      lastTouchDist = 0;
      lastTouchMid = null;
    });

    // Zoom control buttons
    container.querySelectorAll('.seat-controls button').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const action = btn.getAttribute('data-zoom');
        const rect = viewport.getBoundingClientRect();
        const cx = rect.width / 2, cy = rect.height / 2;
        if (action === 'in')  zoomAt(cx, cy, ZOOM_STEP * 2);
        if (action === 'out') zoomAt(cx, cy, -ZOOM_STEP * 2);
        if (action === 'reset') { scale = 1; panX = 0; panY = 0; applyTransform(); }
      });
    });

    // Expose dragMoved check so click handler can skip drag-clicks
    viewport._wasDrag = () => dragMoved;
  }

  // ========= Boot (idempotent) =========
  async function boot() {
    const cfg = window.SimpleSeatingPlanCfg || {};
    if (!cfg.svg && !cfg.svg_url) {
      logW('Config manquante (cfg.svg | cfg.svg_url).');
      return;
    }

    // 0) Vérifier que la question seat-label est présente sur cette page
    if (cfg.question_label_id && !isQuestionOnPage(cfg.question_label_id)) {
      // Afficher un message d'information si on est sur une page de checkout (présence du formulaire)
      var form = document.querySelector('form.checkout-form, form.form-horizontal, main form');
      if (form && !document.getElementById('seat-checkout-notice')) {
        var notice = document.createElement('div');
        notice.id = 'seat-checkout-notice';
        notice.className = 'seat-instructions';
        notice.innerHTML = '<strong>\ud83d\udcba Plan de salle disponible</strong><br>'
          + 'Le choix des places se fera \u00e0 une \u00e9tape ultérieure du processus de commande.';
        // Insérer en haut du formulaire
        var target = document.getElementById('questions_group') || form;
        target.insertBefore(notice, target.firstChild);
      }
      return;
    }

    // 1) Liste des champs Seat
    g.inputs = findAllSeatInputs();
    if (!g.inputs.length) {
      return;  // silently wait – boot() will be retried via setTimeout & MutationObserver
    }

    // 2) Focus = input actif + protection anti-copie Pretix
    g.inputs.forEach(inp => {
      inp.removeEventListener?.('focus', onFocusSeatInput);
      inp.addEventListener('focus', onFocusSeatInput);
      // Pretix skip les éléments dans .js-do-not-copy-answers lors du "copier les réponses"
      inp.classList.add('js-do-not-copy-answers');
    });
    function onFocusSeatInput(e) { setActiveInput(e.currentTarget); }

    // 3) Conteneur unique
    g.container = ensureSingleContainer();
    if (g.container.dataset.initialized === '1') return; // déjà monté
    g.container.dataset.initialized = '1';

    // 4) Layout & injection du plan (with zoom/pan viewport)
    g.container.innerHTML = ''
      + '<div class="seat-instructions">'
      +   '<strong>Comment choisir vos places\u00a0:</strong>'
      +   '<ol>'
      +     '<li>Cliquez sur le plan pour s\u00e9lectionner une place. R\u00e9p\u00e9tez pour chaque participant.</li>'
      +     '<li>Pour modifier une place, cliquez d\u2019abord sur le champ correspondant ci-dessous, puis s\u00e9lectionnez la nouvelle place sur le plan.</li>'
      +   '</ol>'
      + '</div>'
      + '<div class="seat-viewport">'
      +   '<div class="seat-pan-layer"></div>'
      +   '<div class="seat-controls">'
      +     '<button type="button" data-zoom="in" title="Zoom +">+</button>'
      +     '<button type="button" data-zoom="out" title="Zoom \u2212">\u2212</button>'
      +     '<button type="button" data-zoom="reset" title="Reset">\u21ba</button>'
      +   '</div>'
      + '</div>'
      + '<div class="seat-color-legend">'
      +   '<div class="legend-item"><div class="legend-swatch swatch-free"></div>Libre</div>'
      +   '<div class="legend-item"><div class="legend-swatch swatch-selected"></div>S\u00e9lectionn\u00e9</div>'
      +   '<div class="legend-item"><div class="legend-swatch swatch-held"></div>R\u00e9serv\u00e9</div>'
      +   '<div class="legend-item"><div class="legend-swatch swatch-sold"></div>Vendu</div>'
      + '</div>'
      + '<div class="legend"></div>';
    const viewport = g.container.querySelector('.seat-viewport');
    const panLayer = g.container.querySelector('.seat-pan-layer');

    let svg = null;
    if (cfg.svg) svg = svgFromStringInto(panLayer, cfg.svg);
    else svg = await svgFromUrlInto(panLayer, cfg.svg_url);

    if (!svg) { setLegend(g.container, 'Impossible de charger le plan.'); return; }
    g.svg = svg;

    // Fit SVG to viewport initially
    fitSvgToViewport(svg, viewport, panLayer);

    // 5) Zoom & Pan
    initZoomPan(viewport, panLayer, g.container);

    // 6) Bind clics
    bindPlanClicks(svg, cfg);

    // 7) Refresh sold/held
    scheduleRefresh(svg, cfg);

    // 7) Met le premier input en actif par défaut
    setActiveInput(g.inputs.find(i => !i.disabled) || g.inputs[0]);

    // 8) Validation au submit : doublons + disponibilité
    bindFormValidation(cfg);
  }

  // ========= Validation submit =========
  function bindFormValidation(cfg) {
    const form = document.querySelector('form:has(#questions_group)');
    if (!form || form._seatValidationBound) return;
    form._seatValidationBound = true;

    form.addEventListener('submit', async function (e) {
      // Recalculer les inputs au moment du submit (le DOM peut avoir changé)
      const inputs = findAllSeatInputs();
      if (!inputs.length) return; // pas d'inputs siège, laisser passer

      // 1) Vérifier que chaque champ est rempli
      const empty = inputs.filter(i => !(i.value || '').trim());
      if (empty.length) {
        e.preventDefault();
        e.stopImmediatePropagation();
        clearSeatErrors();
        empty.forEach(i => showSeatError(i, 'Veuillez sélectionner un siège.'));
        empty[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
        return false;
      }

      // 2) Vérifier l'unicité
      const seen = new Map();
      const duplicates = [];
      for (const inp of inputs) {
        const val = (inp.value || '').trim();
        if (seen.has(val)) {
          duplicates.push(inp);
          duplicates.push(seen.get(val));
        } else {
          seen.set(val, inp);
        }
      }
      if (duplicates.length) {
        e.preventDefault();
        e.stopImmediatePropagation();
        clearSeatErrors();
        const unique = [...new Set(duplicates)];
        unique.forEach(i => showSeatError(i, 'Ce siège est déjà attribué à un autre participant.'));
        unique[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
        return false;
      }

      // 3) Vérifier la disponibilité (sold/held) via le status endpoint
      if (cfg.status_url) {
        e.preventDefault();
        e.stopImmediatePropagation();
        clearSeatErrors();

        let st;
        try {
          st = await getJSON(cfg.status_url);
        } catch (_) {
          // Réseau indisponible : laisser le serveur valider
          form.submit();
          return;
        }

        const sold = new Set(st.sold || []);
        const held = new Set(st.held || []);
        const prefix = cfg.prefix || '';

        // Exclure des held les sièges sélectionnés dans le formulaire (nos propres holds)
        const ownGuids = new Set();
        for (const inp of inputs) {
          const label = (inp.value || '').trim();
          if (label) {
            const g = guidFromLabel(label, prefix);
            if (g) ownGuids.add(g);
          }
        }

        const unavailable = [];

        for (const inp of inputs) {
          const label = (inp.value || '').trim();
          const guid = guidFromLabel(label, prefix);
          if (guid && (sold.has(guid) || (held.has(guid) && !ownGuids.has(guid)))) {
            unavailable.push(inp);
          }
        }

        if (unavailable.length) {
          unavailable.forEach(i => showSeatError(i, 'Ce siège n\'est plus disponible. Veuillez en choisir un autre.'));
          unavailable[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
          return;
        }

        // Tout est OK, soumettre le formulaire
        form.submit();
      }
    });
  }

  /**
   * Retrouve le seat_guid à partir du label affiché, en cherchant dans le SVG.
   */
  function guidFromLabel(label, prefix) {
    if (!g.svg || !label) return null;
    const sel = prefix ? `[id^='${prefix}']` : '[id]';
    for (const node of g.svg.querySelectorAll(sel)) {
      const nodeLabel = (node.getAttribute('data-seat-label') || '').trim();
      if (nodeLabel === label) {
        const rawId = node.getAttribute('id') || '';
        return prefix && rawId.startsWith(prefix) ? rawId.substring(prefix.length) : rawId;
      }
    }
    return null;
  }

  function showSeatError(input, msg) {
    input.classList.add('seat-error');
    const group = groupForInput(input);
    let errEl = group.querySelector('.seat-error-msg');
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'seat-error-msg';
      errEl.style.cssText = 'color:#dc2626;font-size:0.85em;margin-top:4px;';
      input.insertAdjacentElement('afterend', errEl);
    }
    errEl.textContent = msg;
  }

  function clearSeatErrors() {
    document.querySelectorAll('.seat-error').forEach(el => el.classList.remove('seat-error'));
    document.querySelectorAll('.seat-error-msg').forEach(el => el.remove());
  }

  // ========= Hooks Pretix / DOM =========
  document.addEventListener('DOMContentLoaded', boot);
  document.addEventListener('pretix:ui:changed', () => boot());

  const mo = new MutationObserver((mut) => {
    const interesting = mut.some(m =>
      Array.from(m.addedNodes || []).some(n =>
        n.nodeType === 1 && (n.querySelector?.('form, .question, .cart, .checkout, [data-seatmap]') || false)
      )
    );
    if (interesting) boot();
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  setTimeout(boot, 100);
  setTimeout(boot, 500);
  setTimeout(boot, 1000);
  setTimeout(boot, 2000);
  setTimeout(boot, 3000);
})();