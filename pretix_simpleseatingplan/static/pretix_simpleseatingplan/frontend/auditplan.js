/* auditplan.js — Vue en lecture seule et zoomable/pannable du plan de salle
 * pour la page "Seat audit" du panneau de contrôle. Colore chaque siège
 * selon son état le plus pertinent pour le diagnostic (conflit > manquant
 * récupérable > en cours de réservation > vendu > libre). Aucune interaction
 * de sélection (pas de clic sur un siège, pas de hold) : seul le zoom/pan est
 * interactif. Ce n'est pas le picker de checkout — logique volontairement
 * séparée de seatpicker.js pour ne pas mélanger les responsabilités.
 */
(function () {
  'use strict';

  var SHAPES = ['path', 'circle', 'ellipse', 'rect', 'polygon', 'polyline', 'line', 'g', 'use'];

  var COLORS = {
    conflict: '#9b1fe0',
    missing: '#2563eb',
    held: '#f2b705',
    sold: '#ea0d0d',
    free: '#22c55e',
  };

  function paint(el, color) {
    var targets = [el].concat(Array.prototype.slice.call(el.querySelectorAll(SHAPES.join(','))));
    targets.forEach(function (node) {
      var tag = (node.tagName || '').toLowerCase();
      if (SHAPES.indexOf(tag) !== -1) {
        node.setAttribute('fill', color);
      }
    });
  }

  function stripSvgStyles(svgText) {
    return svgText.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
  }

  function paintSeats(svg, cfg) {
    var prefix = cfg.prefix || '';
    var sets = {
      conflict: new Set(cfg.conflict || []),
      missing: new Set(cfg.missing || []),
      held: new Set(cfg.held || []),
      sold: new Set(cfg.sold || []),
    };
    // Ordre de priorité si un siège correspond à plusieurs états à la fois
    // (ex: vendu ET en conflit -> on montre le conflit, plus actionnable).
    var priority = ['conflict', 'missing', 'held', 'sold'];

    var sel = prefix ? "[id^='" + prefix + "']" : '[id]';
    svg.querySelectorAll(sel).forEach(function (node) {
      var raw = node.getAttribute('id') || '';
      var guid = (prefix && raw.indexOf(prefix) === 0) ? raw.substring(prefix.length) : raw;
      var state = 'free';
      for (var i = 0; i < priority.length; i++) {
        if (sets[priority[i]].has(guid)) { state = priority[i]; break; }
      }
      paint(node, COLORS[state]);
      node.setAttribute('data-audit-state', state);
    });
  }

  // ========= Fit SVG to viewport (identique à seatpicker.js) =========
  function fitSvgToViewport(svg, viewport) {
    var vb = svg.getAttribute('viewBox');
    if (vb) {
      var parts = vb.split(/[\s,]+/).map(Number);
      if (parts.length === 4) {
        var vbW = parts[2], vbH = parts[3];
        var vpW = viewport.clientWidth || 860;
        var vpH = viewport.clientHeight || 480;
        var scale = Math.min(vpW / vbW, vpH / vbH);
        svg.style.width = (vbW * scale) + 'px';
        svg.style.height = (vbH * scale) + 'px';
        svg.removeAttribute('width');
        svg.removeAttribute('height');
      }
    } else {
      svg.style.width = '100%';
      svg.style.height = 'auto';
    }
  }

  // ========= Zoom & Pan (identique à seatpicker.js, sans gestion de clic) =========
  function initZoomPan(viewport, panLayer, container) {
    var scale = 1, panX = 0, panY = 0;
    var MIN_SCALE = 0.5, MAX_SCALE = 5, ZOOM_STEP = 0.06;
    var dragging = false, startX = 0, startY = 0, startPanX = 0, startPanY = 0;

    function applyTransform() {
      panLayer.style.transform = 'translate(' + panX + 'px, ' + panY + 'px) scale(' + scale + ')';
    }

    function zoomAt(cx, cy, delta) {
      var oldScale = scale;
      scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta));
      var ratio = scale / oldScale;
      panX = cx - ratio * (cx - panX);
      panY = cy - ratio * (cy - panY);
      applyTransform();
    }

    viewport.addEventListener('wheel', function (e) {
      e.preventDefault();
      var rect = viewport.getBoundingClientRect();
      var cx = e.clientX - rect.left;
      var cy = e.clientY - rect.top;
      zoomAt(cx, cy, e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP);
    }, { passive: false });

    viewport.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || e.target.closest('.audit-plan-controls')) return;
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      startPanX = panX; startPanY = panY;
      viewport.classList.add('grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      panX = startPanX + (e.clientX - startX);
      panY = startPanY + (e.clientY - startY);
      applyTransform();
    });
    window.addEventListener('mouseup', function () {
      if (dragging) { dragging = false; viewport.classList.remove('grabbing'); }
    });

    var lastTouchDist = 0;
    viewport.addEventListener('touchstart', function (e) {
      if (e.target.closest('.audit-plan-controls')) return;
      if (e.touches.length === 2) {
        var t = e.touches;
        var mx = (t[0].clientX + t[1].clientX) / 2;
        var my = (t[0].clientY + t[1].clientY) / 2;
        dragging = true;
        startX = mx; startY = my;
        startPanX = panX; startPanY = panY;
        lastTouchDist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY);
      }
    }, { passive: true });
    viewport.addEventListener('touchmove', function (e) {
      if (e.touches.length === 2) {
        var t = e.touches;
        var mx = (t[0].clientX + t[1].clientX) / 2;
        var my = (t[0].clientY + t[1].clientY) / 2;
        panX = startPanX + (mx - startX);
        panY = startPanY + (my - startY);
        var dist = Math.hypot(t[1].clientX - t[0].clientX, t[1].clientY - t[0].clientY);
        if (lastTouchDist) {
          var rect = viewport.getBoundingClientRect();
          zoomAt(mx - rect.left, my - rect.top, (dist - lastTouchDist) * 0.005);
        }
        lastTouchDist = dist;
        applyTransform();
        e.preventDefault();
      }
    }, { passive: false });
    viewport.addEventListener('touchend', function () {
      dragging = false;
      lastTouchDist = 0;
    });

    container.querySelectorAll('.audit-plan-controls button').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var action = btn.getAttribute('data-zoom');
        var rect = viewport.getBoundingClientRect();
        var cx = rect.width / 2, cy = rect.height / 2;
        if (action === 'in') zoomAt(cx, cy, ZOOM_STEP * 2);
        if (action === 'out') zoomAt(cx, cy, -ZOOM_STEP * 2);
        if (action === 'reset') { scale = 1; panX = 0; panY = 0; applyTransform(); }
      });
    });
  }

  function boot() {
    var dataEl = document.getElementById('audit-plan-data');
    var container = document.getElementById('audit-plan-map');
    if (!dataEl || !container) return;

    var cfg;
    try {
      cfg = JSON.parse(dataEl.textContent);
    } catch (e) {
      console.error('[auditplan] config JSON invalide', e);
      return;
    }
    if (!cfg.svg) return;

    container.innerHTML = ''
      + '<div class="audit-plan-viewport">'
      +   '<div class="audit-plan-pan-layer"></div>'
      +   '<div class="audit-plan-controls">'
      +     '<button type="button" data-zoom="in" title="Zoom +">+</button>'
      +     '<button type="button" data-zoom="out" title="Zoom −">−</button>'
      +     '<button type="button" data-zoom="reset" title="Reset">↺</button>'
      +   '</div>'
      + '</div>';
    var viewport = container.querySelector('.audit-plan-viewport');
    var panLayer = container.querySelector('.audit-plan-pan-layer');

    panLayer.innerHTML = stripSvgStyles(cfg.svg);
    var svg = panLayer.querySelector('svg');
    if (!svg) return;

    fitSvgToViewport(svg, viewport);
    initZoomPan(viewport, panLayer, container);
    paintSeats(svg, cfg);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
