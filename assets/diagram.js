/* Step-through diagram sequencer — link from every lesson that uses diagrams */

(function () {
  'use strict';

  function initDiagram(wrap) {
    const steps = JSON.parse(wrap.dataset.steps || '[]');
    if (!steps.length) return;

    const stage = wrap.querySelector('.diagram-stage');
    const prevBtn = wrap.querySelector('.diagram-prev');
    const nextBtn = wrap.querySelector('.diagram-next');
    const progressEl = wrap.querySelector('.diagram-progress');
    const captionEl = wrap.querySelector('.diagram-caption');

    let current = 0;

    function render(idx) {
      const step = steps[idx];

      // Show/hide SVG nodes
      wrap.querySelectorAll('[data-step-show]').forEach((el) => {
        const showAt = parseInt(el.dataset.stepShow, 10);
        el.classList.toggle('hidden', idx < showAt);
      });

      // Highlight active nodes
      wrap.querySelectorAll('[data-step-active]').forEach((el) => {
        const activeAt = el.dataset.stepActive.split(',').map(Number);
        el.classList.toggle('step-active', activeAt.includes(idx));
      });

      if (captionEl) captionEl.textContent = step.caption || '';
      if (progressEl) progressEl.textContent = `Step ${idx + 1} of ${steps.length}`;

      if (prevBtn) prevBtn.disabled = idx === 0;
      if (nextBtn) nextBtn.disabled = idx === steps.length - 1;
    }

    if (prevBtn) {
      prevBtn.addEventListener('click', () => {
        if (current > 0) { current--; render(current); }
      });
    }

    if (nextBtn) {
      nextBtn.addEventListener('click', () => {
        if (current < steps.length - 1) { current++; render(current); }
      });
    }

    render(0);
  }

  function initAll() {
    document.querySelectorAll('.diagram-wrap[data-steps]').forEach(initDiagram);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
