(() => {
  'use strict';
  document.querySelectorAll('[data-chart-card]').forEach(card => {
    const svg = card.querySelector('svg[data-ds-chart]');
    if (!svg) return;
    const initial = svg.getAttribute('viewBox').split(/\s+/).map(Number);
    let view = initial.slice(), drag = null, moved = false, selected = null;
    const detail = card.querySelector('[data-chart-detail]');
    const paint = () => svg.setAttribute('viewBox', view.join(' '));
    const locate = event => {
      const point = svg.createSVGPoint();
      point.x = event.clientX; point.y = event.clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    };
    const zoom = (factor, center) => {
      const next = view[2] * factor;
      if (next < initial[2] / 32 || next > initial[2] * 4) return;
      center = center || {x: view[0] + view[2] / 2, y: view[1] + view[3] / 2};
      view = [center.x + (view[0] - center.x) * factor,
              center.y + (view[1] - center.y) * factor, next, view[3] * factor];
      paint();
    };
    card.querySelector('[data-zoom-in]').addEventListener('click', () => zoom(0.8));
    card.querySelector('[data-zoom-out]').addEventListener('click', () => zoom(1.25));
    const reset = () => {
      view = initial.slice(); paint();
      card.querySelectorAll('.ds-selected').forEach(node => node.classList.remove('ds-selected'));
      card.querySelectorAll('.ds-series-hidden').forEach(node => node.classList.remove('ds-series-hidden'));
      card.querySelectorAll('[data-series-toggle]').forEach(button => button.setAttribute('aria-pressed', 'true'));
      selected = null;
      detail.textContent = '点击图形查看数据；点击系列按钮切换显示。滚轮缩放，拖动平移。';
    };
    card.querySelector('[data-chart-reset]').addEventListener('click', reset);
    const series = new Map();
    svg.querySelectorAll('[data-series]').forEach(node => {
      const name = node.getAttribute('data-series');
      if (!series.has(name)) series.set(name, []);
      series.get(name).push(node);
    });
    series.forEach((nodes, name) => {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = name.replace(/^\d+:/, '');
      button.setAttribute('data-series-toggle', name);
      button.setAttribute('aria-pressed', 'true');
      button.addEventListener('click', () => {
        const visible = button.getAttribute('aria-pressed') !== 'true';
        button.setAttribute('aria-pressed', String(visible));
        nodes.forEach(node => node.classList.toggle('ds-series-hidden', !visible));
        detail.textContent = (visible ? '已显示：' : '已隐藏：') + button.textContent;
      });
      card.querySelector('[data-chart-series]').appendChild(button);
    });
    svg.addEventListener('wheel', event => {
      event.preventDefault(); zoom(event.deltaY < 0 ? 0.8 : 1.25, locate(event));
    }, {passive: false});
    svg.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      drag = {x: event.clientX, y: event.clientY, point: locate(event), view: view.slice()};
      moved = false;
    });
    svg.addEventListener('pointermove', event => {
      if (!drag) return;
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5 && !moved) return;
      moved = true;
      svg.setPointerCapture(event.pointerId);
      const now = locate(event);
      view[0] += drag.point.x - now.x; view[1] += drag.point.y - now.y; paint();
    });
    svg.addEventListener('pointerup', () => { drag = null; });
    svg.addEventListener('pointercancel', () => { drag = null; moved = false; });
    svg.addEventListener('click', event => {
      if (moved) { moved = false; return; }
      let node = event.target.closest('[data-label]');
      if (!node || !svg.contains(node)) return;
      if (selected) selected.classList.remove('ds-selected');
      selected = node; node.classList.add('ds-selected');
      let label = node.getAttribute('data-label');
      if (node.hasAttribute('data-points')) {
        try {
          const point = locate(event), points = JSON.parse(node.getAttribute('data-points'));
          let best = null, distance = Infinity;
          points.forEach(p => {
            const d = Math.hypot(point.x - p[0], point.y - p[1]);
            if (d < distance) { distance = d; best = p[2]; }
          });
          if (best) label += ': ' + best;
        } catch (_) { /* Passive third-party SVG may have no point metadata. */ }
      }
      detail.textContent = label;
    });
    svg.addEventListener('dblclick', reset);
    reset();
    card.setAttribute('data-interactive-ready', 'true');
  });
})();
