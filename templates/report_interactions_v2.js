(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  let activeModal = null;
  document.querySelectorAll('[data-chart-card]').forEach(card => {
    const svg = card.querySelector('svg[data-ds-chart]');
    if (!svg) return;
    const initial = svg.getAttribute('viewBox').trim().split(/\s+/).map(Number);
    if (initial.length !== 4 || initial.some(n => !Number.isFinite(n))) return;
    const canvas = card.querySelector('.chart-canvas');
    const detail = card.querySelector('[data-chart-detail]');
    const inspector = card.querySelector('.chart-inspector');
    const status = card.querySelector('[data-selection-status]');
    const tooltip = card.querySelector('[data-chart-tooltip]');
    const tipTitle = tooltip.querySelector('strong'), tipValue = tooltip.querySelector('span');
    const zoomLabel = card.querySelector('[data-chart-zoom]');
    const title = svg.getAttribute('data-chart-title');
    if (title) card.querySelector('[data-chart-title-text]').textContent = title;
    const hint = '悬浮查看数据，点击固定详情。选择下方系列可切换显示。';
    const series = new Map(), pointCache = new WeakMap();
    let view = initial.slice(), drag = null, moved = false, selected = null;
    let hovered = null, frame = 0, pending = null;
    const marker = document.createElementNS(NS, 'circle');
    marker.classList.add('ds-point-marker');
    marker.setAttribute('data-interaction-layer', 'true');
    marker.style.display = 'none'; svg.appendChild(marker);
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', (title || '交互图表') + '。加减键缩放，方向键平移，零键重置，Escape 清除选中。');
    svg.querySelectorAll('[data-label] > title').forEach(node => node.remove());
    svg.querySelectorAll('[data-series]').forEach(node => {
      const name = node.getAttribute('data-series');
      if (!series.has(name)) series.set(name, {nodes: [], visible: true, button: null});
      series.get(name).nodes.push(node);
    });
    // A wider invisible target makes thin line charts easier to inspect.
    svg.querySelectorAll('[data-points]').forEach(node => {
      const path = node.querySelector('path');
      if (!path) return;
      const hit = path.cloneNode(true);
      hit.removeAttribute('id'); hit.setAttribute('data-interaction-layer', 'true');
      hit.style.fill = 'none'; hit.style.stroke = 'transparent';
      hit.style.strokeWidth = '14px'; hit.style.pointerEvents = 'stroke';
      node.appendChild(hit);
    });
    const paint = () => {
      svg.setAttribute('viewBox', view.join(' '));
      const percent = initial[2] / view[2] * 100;
      zoomLabel.textContent = Math.round(percent) + '%';
      card.querySelector('[data-zoom-in]').disabled = percent >= 1599;
      card.querySelector('[data-zoom-out]').disabled = percent <= 25.01;
    };
    const locate = event => {
      const point = svg.createSVGPoint();
      point.x = event.clientX; point.y = event.clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    };
    const hideTip = () => {
      tooltip.hidden = true; marker.style.display = 'none';
      if (hovered) hovered.classList.remove('ds-hovered');
      hovered = null;
    };
    const clearSelection = () => {
      if (selected) selected.classList.remove('ds-selected');
      selected = null; detail.textContent = hint;
      status.textContent = '数据详情'; inspector.classList.remove('is-pinned');
      card.querySelector('[data-chart-clear]').hidden = true;
    };
    const inspect = (node, event) => {
      let text = node.getAttribute('data-label') || '', nearest = null;
      if (node.hasAttribute('data-points')) {
        let points = pointCache.get(node);
        if (!points) {
          try { points = JSON.parse(node.getAttribute('data-points')); }
          catch (_) { points = []; }
          if (!Array.isArray(points)) points = [];
          pointCache.set(node, points);
        }
        const point = locate(event);
        let distance = Infinity;
        points.forEach(p => {
          if (!Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) return;
          const d = Math.hypot(point.x - p[0], point.y - p[1]);
          if (d < distance) { distance = d; nearest = p; }
        });
        if (nearest) text += ': ' + nearest[2];
      }
      return {text, nearest};
    };
    const showTip = event => {
      const node = event.target.closest('[data-label]');
      if (!node || !svg.contains(node) || node.closest('.ds-series-hidden')) { hideTip(); return; }
      if (hovered !== node) {
        if (hovered) hovered.classList.remove('ds-hovered');
        hovered = node; node.classList.add('ds-hovered');
      }
      const result = inspect(node, event), group = node.closest('[data-series]');
      tipTitle.textContent = group ? group.getAttribute('data-series').replace(/^\d+:/, '') : '数据';
      tipValue.textContent = result.text.replace(/;\s*/g, '\n').replace(/, (?=[^,=]+?=)/g, '\n');
      tooltip.hidden = false;
      const rect = canvas.getBoundingClientRect(), size = tooltip.getBoundingClientRect();
      const x = Math.min(Math.max(8, event.clientX - rect.left + 16), Math.max(8, rect.width - size.width - 8));
      let y = event.clientY - rect.top - size.height - 12;
      if (y < 8) y = event.clientY - rect.top + 18;
      y = Math.min(y, Math.max(8, rect.height - size.height - 8));
      tooltip.style.left = x + 'px'; tooltip.style.top = y + 'px';
      if (result.nearest) {
        marker.setAttribute('cx', result.nearest[0]); marker.setAttribute('cy', result.nearest[1]);
        marker.setAttribute('r', 4 * view[2] / svg.getBoundingClientRect().width);
        marker.style.display = '';
      } else marker.style.display = 'none';
    };
    const zoom = (factor, center) => {
      const ratio = Math.min(4, Math.max(1 / 16, view[2] * factor / initial[2]));
      factor = initial[2] * ratio / view[2];
      center = center || {x: view[0] + view[2] / 2, y: view[1] + view[3] / 2};
      view = [center.x + (view[0] - center.x) * factor,
              center.y + (view[1] - center.y) * factor, view[2] * factor, view[3] * factor];
      hideTip(); paint();
    };
    const setVisible = (entry, visible) => {
      entry.visible = visible; entry.button.setAttribute('aria-pressed', String(visible));
      entry.nodes.forEach(node => node.classList.toggle('ds-series-hidden', !visible));
      if (!visible && selected && entry.nodes.some(node => node === selected || node.contains(selected))) clearSelection();
    };
    const clearEmphasis = () => series.forEach(entry => entry.nodes.forEach(node => node.classList.remove('ds-muted')));
    series.forEach((entry, name) => {
      const button = document.createElement('button'), label = document.createElement('span');
      entry.button = button; button.type = 'button'; label.textContent = name.replace(/^\d+:/, '');
      button.appendChild(label); button.setAttribute('data-series-toggle', name);
      button.setAttribute('aria-pressed', 'true'); button.title = '单击显示或隐藏；双击只看此系列';
      let color = '#138b84';
      for (const shape of entry.nodes[0].querySelectorAll('path,use,rect,circle,polygon')) {
        if (shape.closest('defs,clipPath') || shape.hasAttribute('data-interaction-layer')) continue;
        const style = getComputedStyle(shape);
        color = style.fill !== 'none' && style.fill !== 'transparent' ? style.fill : style.stroke;
        if (color && color !== 'none') break;
      }
      button.style.setProperty('--series-color', color);
      button.addEventListener('click', event => {
        if (event.detail > 1) return;
        setVisible(entry, !entry.visible); hideTip();
        if (!selected) detail.textContent = (entry.visible ? '已显示：' : '已隐藏：') + label.textContent;
      });
      button.addEventListener('dblclick', () => {
        series.forEach(other => setVisible(other, other === entry)); hideTip(); clearEmphasis();
        detail.textContent = '当前聚焦：' + label.textContent + '。点击“全部显示”恢复其他系列。';
      });
      button.addEventListener('pointerenter', () => {
        series.forEach(other => other.nodes.forEach(node => node.classList.toggle('ds-muted', other !== entry)));
      });
      button.addEventListener('pointerleave', clearEmphasis);
      card.querySelector('[data-chart-series]').appendChild(button);
    });
    const reset = () => {
      view = initial.slice(); paint(); hideTip(); clearSelection(); clearEmphasis();
      series.forEach(entry => setVisible(entry, true));
    };
    card.querySelector('[data-zoom-in]').addEventListener('click', () => zoom(0.8));
    card.querySelector('[data-zoom-out]').addEventListener('click', () => zoom(1.25));
    card.querySelector('[data-chart-reset]').addEventListener('click', reset);
    card.querySelector('[data-chart-clear]').addEventListener('click', clearSelection);
    card.querySelector('[data-chart-show-all]').addEventListener('click', () => {
      series.forEach(entry => setVisible(entry, true)); clearEmphasis(); hideTip();
      if (!selected) detail.textContent = hint;
    });
    svg.addEventListener('wheel', event => {
      if (!event.ctrlKey && !event.metaKey) { hideTip(); return; }
      event.preventDefault(); zoom(event.deltaY < 0 ? 0.8 : 1.25, locate(event));
    }, {passive: false});
    svg.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      drag = {x: event.clientX, y: event.clientY, point: locate(event)}; moved = false;
    });
    svg.addEventListener('pointermove', event => {
      if (drag && (moved || Math.hypot(event.clientX - drag.x, event.clientY - drag.y) >= 5)) {
        moved = true; hideTip(); svg.classList.add('is-dragging');
        if (!svg.hasPointerCapture(event.pointerId)) svg.setPointerCapture(event.pointerId);
        const now = locate(event);
        view[0] += drag.point.x - now.x; view[1] += drag.point.y - now.y; paint(); return;
      }
      pending = event;
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; if (pending && !moved) showTip(pending); });
    });
    const endDrag = event => {
      drag = null; svg.classList.remove('is-dragging');
      if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    };
    svg.addEventListener('pointerup', endDrag);
    svg.addEventListener('pointercancel', event => { endDrag(event); moved = false; hideTip(); });
    svg.addEventListener('lostpointercapture', () => { drag = null; svg.classList.remove('is-dragging'); });
    svg.addEventListener('pointerleave', () => { pending = null; hideTip(); if (!moved) drag = null; });
    svg.addEventListener('click', event => {
      if (moved) { moved = false; return; }
      const node = event.target.closest('[data-label]');
      if (!node || !svg.contains(node)) { clearSelection(); return; }
      if (selected) selected.classList.remove('ds-selected');
      selected = node; node.classList.add('ds-selected');
      detail.textContent = inspect(node, event).text; status.textContent = '已固定';
      inspector.classList.add('is-pinned'); card.querySelector('[data-chart-clear]').hidden = false;
    });
    svg.addEventListener('dblclick', reset);
    svg.addEventListener('keydown', event => {
      if (event.key === '+' || event.key === '=') zoom(0.8);
      else if (event.key === '-') zoom(1.25);
      else if (event.key === '0' || event.key === 'Home') reset();
      else if (event.key === 'Escape') { clearSelection(); hideTip(); return; }
      else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        view[0] += (event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0) * view[2] * 0.05;
        view[1] += (event.key === 'ArrowUp' ? -1 : event.key === 'ArrowDown' ? 1 : 0) * view[3] * 0.05;
        hideTip(); paint();
      } else return;
      event.preventDefault();
    });
    const expand = card.querySelector('[data-chart-expand]');
    expand.addEventListener('click', () => {
      if (activeModal && activeModal.card === card) { activeModal.dialog.close(); return; }
      if (activeModal) activeModal.dialog.close();
      const placeholder = document.createElement('span'), dialog = document.createElement('dialog');
      placeholder.hidden = true; card.before(placeholder);
      dialog.className = 'chart-modal'; dialog.setAttribute('aria-label', title || '图表专注查看');
      document.body.appendChild(dialog); dialog.appendChild(card); activeModal = {dialog, card};
      expand.textContent = '收起视图'; expand.setAttribute('aria-expanded', 'true');
      dialog.addEventListener('close', () => {
        hideTip(); placeholder.replaceWith(card); dialog.remove(); activeModal = null;
        expand.textContent = '专注查看'; expand.setAttribute('aria-expanded', 'false'); expand.focus();
      }, {once: true});
      dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
      dialog.showModal(); hideTip();
    });
    card.querySelector('[data-chart-export]').addEventListener('click', () => {
      const clone = svg.cloneNode(true);
      clone.querySelectorAll('[data-interaction-layer],.ds-series-hidden').forEach(node => node.remove());
      clone.querySelectorAll('.ds-selected,.ds-hovered,.ds-muted').forEach(node => node.classList.remove('ds-selected', 'ds-hovered', 'ds-muted'));
      clone.removeAttribute('tabindex'); clone.classList.remove('is-dragging');
      clone.querySelectorAll('[data-label]').forEach(node => {
        if (!node.querySelector('title')) {
          const label = document.createElementNS(NS, 'title'); label.textContent = node.getAttribute('data-label'); node.appendChild(label);
        }
      });
      const blob = new Blob([new XMLSerializer().serializeToString(clone)], {type: 'image/svg+xml;charset=utf-8'});
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url;
      link.download = (card.getAttribute('data-chart-file') || 'chart.svg').replace(/[^\w\u4e00-\u9fff.-]+/g, '_').replace(/\.(png|svg)$/i, '') + '.svg';
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      if (!selected) detail.textContent = '已导出当前可见系列与视图，可直接用于文档或演示。';
    });
    let printView = null;
    window.addEventListener('beforeprint', () => { printView = view.slice(); view = initial.slice(); paint(); hideTip(); });
    window.addEventListener('afterprint', () => { if (printView) { view = printView; printView = null; paint(); } });
    reset(); card.setAttribute('data-interactive-ready', 'true');
  });
})();
