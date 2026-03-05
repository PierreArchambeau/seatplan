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
      const style = node.getAttribute('style') || '';
      // Optionnel : nettoyer les directives fill/stroke précédentes dans l'attribut style
      // (utile si le style inline précédent restait en conflit)
      if (style) {
        let newStyle = style
          .replace(/(^|;)\s*fill\s*:\s*[^;]+/gi, '')
          .replace(/(^|;)\s*stroke\s*:\s*[^;]+/gi, '');
        newStyle = newStyle.replace(/;;+/g, ';').replace(/^;|;$/g, '');
        if (newStyle !== style) node.setAttribute('style', newStyle);
      }

      // Appliquer fill/stroke
      if (fill != null) {
        if (node.style) node.style.fill = fill;        // prioritaire si style inline
        node.setAttribute('fill', fill);               // attribut de présentation
      }
      if (stroke != null) {
        if (node.style) node.style.stroke = stroke;
        node.setAttribute('stroke', stroke);
      }
    });
  }

  /**
   * Met à jour l'apparence d'un siège selon les états sold/held
   * Couleurs modifiables selon ton design.
   */
  function setSeatVisualState(el, { isSold, isHeld }, palette = {
    sold:   { fill: '#bbbbbb', stroke: '#bbbbbb' },
    held:   { fill: '#f2b705', stroke: '#f2b705' },
    free:   { fill: '#17c8d1', stroke: '#0f5f96' }, // exemple: couleur "libre"
  }) {
    let colors;
    if (isSold) colors = palette.sold;
    else if (isHeld) colors = palette.held;
    else colors = palette.free;

    applyDirectColor(el, colors, { includeSelf: true });

    // Optionnel : bloquer l'interaction quand indisponible
    el.style.pointerEvents = (isSold || isHeld) ? 'none' : '';
    // Optionnel : opacité
    el.style.opacity = isSold ? '0.55' : (isHeld ? '0.75' : '');
  }

  // ========= Logs & garde-fous =========
  const logI = (...a) => console.log('[seatpicker]', ...a);
  const logW = (...a) => console.warn('[seatpicker]', ...a);
  const logE = (...a) => console.error('[seatpicker]', ...a);

  window.addEventListener('error', e => logE('window.onerror', e.message, e.filename, e.lineno));
  window.addEventListener('unhandledrejection', e => logE('unhandledrejection', e.reason));
  logI('JS REACHED TOP OF FILE');

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
  function posIdFromName(name) {
    logI('Tentative d’extraction posId de', name);
    if (!name) return null;
    let m = name.match(/^answers-(\d+)-(\d+)(?:-|$)/); // answers-QID-POS
    if (m) return parseInt(m[2], 10);
    m = name.match(/(?:^|-)pos(?:ition)?-?(\d+)(?:-|$)/i);
    if (m) return parseInt(m[1], 10);
    const nums = name.match(/\d+/g);
    return nums ? parseInt(nums[nums.length - 1], 10) : null;
  }

  function findAllSeatInputs() {
    const langs = /(seat|siège|siege|place|platz|asiento)/i;
    const set = new Set();

    // via <label for="...">
    document.querySelectorAll('label[for]').forEach(lab => {
      const txt = (lab.textContent || '').trim();
      if (langs.test(txt)) {
        const inp = document.getElementById(lab.getAttribute('for'));
        if (inp && /^(INPUT|SELECT|TEXTAREA)$/.test(inp.tagName)) set.add(inp);
      }
    });

    // fallback name/id
    document.querySelectorAll('input,select,textarea').forEach(el => {
      const key = (el.name || '') + ' ' + (el.id || '');
      if (langs.test(key)) set.add(el);
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

    // on essaie de le placer après le 1er champ Seat
    const seats = findAllSeatInputs();
    const anchor = seats.length ? groupForInput(seats[0]) : (document.querySelector('.product-container') || document.body);

    const wrap = document.createElement('div');
    wrap.className = 'my-seatmap-wrapper';
    if (anchor.after) anchor.after(wrap); else anchor.parentElement.appendChild(wrap);

    cont = document.createElement('div');
    cont.className = 'my-seatmap';
    cont.setAttribute('data-seatmap', '');
    wrap.appendChild(cont);

    logI('Conteneur plan unique inséré');
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
    logI('Injection SVG depuis string',svgText);
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
      // logI('Tentative d’extraction GUID de', el, '(data-seat-id:', ds, ')');
      // if (ds && ds.trim()) return ds.trim();
      const raw = el.getAttribute('id') || '';
      logI('Tentative d’extraction seatId de', el, '(id:', raw, ')');
      if (!raw) return null;
      return prefix ? (raw.startsWith(prefix) ? raw.substring(prefix.length) : null) : raw;
    };

    const labelFromEl = (el, fallbackId) => {
      const lbl = el.getAttribute('data-seat-label');
      logI('Tentative d’extraction label de', el, '(data-seat-label:', lbl, ')');
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
      const cartposId = posIdFromName(targetInput?.name) || null;
      const payload = { seat_guid: seatGuid };
      if (cartposId) payload['cartpos_id'] = String(cartposId);

      logI('Envoi de la requête de hold au serveur', cfg.hold_url, payload);
      const res = await postForm(cfg.hold_url, payload);
      logI('Réponse du hold', res);
      if (!res.ok) {
        const err = res.json?.error || res.json?.detail || 'unknown';
        setLegend(g.container, `Réservation impossible (HTTP ${res.status}, ${err}).`);
        return false;
      }
      return true;
    } catch (e) {
      setLegend(g.container, 'Réservation impossible (erreur réseau).');
      return false;
    }
  }

  // ========= Binding clics sur le plan unique =========
  function bindPlanClicks(svg, cfg) {
    logI('Binding clics sur le plan', cfg);
    const { idFromEl, labelFromEl } = makeExtractors(cfg);

    svg.addEventListener('click', async (e) => {
      const sel = cfg.prefix ? `[id^='${cfg.prefix}'], [data-seat-id]` : '[id], [data-seat-id]';
      const el = e.target.closest(sel);
      if (!el) return;

      logI('Class list', el);
      if (el.classList.contains('sold') || el.classList.contains('held')) {
        setLegend(g.container, 'Ce siège n’est pas disponible.');
        return;
      }

      const guid = idFromEl(el);
      logI('Seat clicked, extracted guid:', guid);
      if (!guid) return;
      const label = labelFromEl(el, guid);
      logI('Extracted label:', label);

      // Choisir l'input cible : actif sinon 1er vide
      let target = g.activeInput || firstEmptyInput() || g.inputs[0];
      if (!target) { setLegend(g.container, 'Aucun champ “Seat” trouvé.'); return; }

      // Anti-doublon : si déjà pris par un autre input
      const already = g.seatToInput.get(label);
      if (already && already !== target) {
        setLegend(g.container, `Le siège ${label} est déjà attribué.`);
        return;
      }

      // Optionnel : hold côté serveur
      logI('Tentative de hold côté serveur (si configuré)...');
      const ok = await tryHoldIfConfigured(cfg, guid, target);
      if (!ok) return;

      // Libérer l'ancienne valeur de cet input (si existait)
      const prevLabel = (target.value || '').trim();
      if (prevLabel) g.seatToInput.delete(prevLabel);

      // Affecter valeur lisible
      logI(`Affectation du siège ${label} à l’input`, target);
      target.value = label;
      target.dispatchEvent(new Event('change', { bubbles: true }));
      g.seatToInput.set(label, target);

      //Affecter GUID
      logI(`Affectation du GUID ${guid} à l’input`, target);
      let targetguid = nextInput(target);
      if (!targetguid) { setLegend(g.container, 'Aucun champ “Seat GUID” trouvé.'); return; }
      targetguid.value = guid;
      targetguid.dispatchEvent(new Event('change', { bubbles: true }));

      // Visuel : selected unique
      svg.querySelectorAll(sel).forEach(n => n.classList.remove('selected'));
      el.classList.add('selected');

      setLegend(g.container, 'Siège sélectionné : ' + label);
    });
  }

  // ========= Refresh sold/held (partagé) =========
  function scheduleRefresh(svg, cfg) {

    logI('Configuration du refresh des sièges sold/held', cfg);

    if (!cfg.status_url) return;

    logI('Refresh des sièges sold/held activé (status_url:', cfg.status_url, ')');
    const prefix = cfg.prefix || '';

    const refresh = async () => {
      try {
        logI('Refresh des sièges sold/held en cours...');
        const st = await getJSON(cfg.status_url);
        const sold = new Set(st.sold || []);
        logI('Sièges sold:', sold);
        const held = new Set(st.held || []);
        logI('Sièges held:', held);
        const nodes = prefix
          ? svg.querySelectorAll(`[id^='${prefix}']`)
          : svg.querySelectorAll('[id], [data-seat-id]');
        
        nodes.forEach((el) => {
          // let id = el.getAttribute('data-seat-id');
          let id = el.getAttribute('id');
          //logI(id)
          if (!id) {
            const raw = el.getAttribute('id') || '';
            id = prefix ? (raw.startsWith(prefix) ? raw.substring(prefix.length) : null) : raw || null;
          }
          //logI(id)
          if (!id) return;
          
          // remove prefix
          id = id.startsWith(prefix) ? id.substring(prefix.length) : id;
          //logI(id)
          const isSold = sold.has(id);
          const isHeld = held.has(id) && !isSold;

          if (isSold || isHeld) logI(`Mise à jour du siège ${id}: sold=${isSold}, held=${isHeld}`);
          if (isSold) logI('Siège sold détecté:', id, el);
          if (isHeld) logI('Siège held détecté:', id, el);               
          // el.classList.toggle('sold', isSold);
          // el.classList.toggle('held', isHeld);

          // 4) attributs (toujours mis à jour)
          // el.setAttribute('sold', String(isSold)); // "true"/"false"
          // el.setAttribute('held', String(isHeld));

          // Appliquer directement les couleurs dans le SVG
          setSeatVisualState(el, { isSold, isHeld });

        });
      } catch (_) { /* silencieux */ }
    };

    refresh();
    const iv = setInterval(refresh, cfg.status_interval_ms || 8000);
    g.container._cancelRefresh = () => clearInterval(iv);
  }

  // ========= Boot (idempotent) =========
  async function boot() {
    const cfg = window.SimpleSeatingPlanCfg || {};
    if (!cfg.svg && !cfg.svg_url) {
      logW('Config manquante (cfg.svg | cfg.svg_url).');
      return;
    }

    // 1) Liste des champs Seat
    g.inputs = findAllSeatInputs();
    if (!g.inputs.length) {
      logW('Aucun champ Seat/Siège détecté → on réessaiera.');
      return;
    }

    // 2) Focus = input actif
    g.inputs.forEach(inp => {
      inp.removeEventListener?.('focus', onFocusSeatInput);
      inp.addEventListener('focus', onFocusSeatInput);
    });
    function onFocusSeatInput(e) { setActiveInput(e.currentTarget); }

    // 3) Conteneur unique
    g.container = ensureSingleContainer();
    if (g.container.dataset.initialized === '1') return; // déjà monté
    g.container.dataset.initialized = '1';

    // 4) Layout & injection du plan
    g.container.innerHTML = '<div class="seat-viewport"></div><div class="legend"></div>';
    const viewport = g.container.querySelector('.seat-viewport');

    let svg = null;
    if (cfg.svg) svg = svgFromStringInto(viewport, cfg.svg);
    else svg = await svgFromUrlInto(viewport, cfg.svg_url);

    if (!svg) { setLegend(g.container, 'Impossible de charger le plan.'); return; }
    g.svg = svg;

    // 5) Bind clics
    bindPlanClicks(svg, cfg);

    // 6) Refresh sold/held
    scheduleRefresh(svg, cfg);

    // 7) Met le premier input en actif par défaut
    setActiveInput(g.inputs.find(i => !i.disabled) || g.inputs[0]);

    logI('Plan unique prêt ✔︎');
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

  setTimeout(boot, 500);
  setTimeout(boot, 1000);
})();