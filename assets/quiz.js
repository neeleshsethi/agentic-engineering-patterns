/* Shared quiz widget — link from every lesson */

(function () {
  'use strict';

  function initQuiz(blockEl) {
    const options = blockEl.querySelectorAll('.quiz-option');
    const checkBtn = blockEl.querySelector('.quiz-check-btn');
    const feedback = blockEl.querySelector('.quiz-feedback');
    let selected = null;
    let answered = false;

    options.forEach((opt) => {
      opt.addEventListener('click', () => {
        if (answered) return;
        options.forEach((o) => o.classList.remove('selected'));
        opt.classList.add('selected');
        selected = opt;
        if (checkBtn) checkBtn.disabled = false;
      });
    });

    if (checkBtn) {
      checkBtn.disabled = true;
      checkBtn.addEventListener('click', () => {
        if (!selected || answered) return;
        answered = true;
        checkBtn.disabled = true;
        const isCorrect = selected.dataset.correct === 'true';
        selected.classList.add(isCorrect ? 'correct' : 'wrong');
        if (!isCorrect) {
          options.forEach((o) => {
            if (o.dataset.correct === 'true') o.classList.add('correct');
          });
        }
        if (feedback) {
          feedback.classList.add('visible', isCorrect ? 'correct' : 'wrong');
        }
        updateScore(blockEl.closest('.lesson-wrap'), isCorrect);
      });
    }
  }

  function updateScore(wrap, correct) {
    if (!wrap) return;
    const display = wrap.querySelector('#score-display');
    if (!display) return;
    const total = wrap.querySelectorAll('.quiz-block').length;
    const answered = wrap.querySelectorAll('.quiz-block .quiz-check-btn[disabled]').length;
    const corrects = wrap.querySelectorAll('.quiz-block .quiz-option.correct:not(.wrong)').length;
    display.textContent = `${corrects} / ${total}`;
  }

  function initAll() {
    document.querySelectorAll('.quiz-block').forEach(initQuiz);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
