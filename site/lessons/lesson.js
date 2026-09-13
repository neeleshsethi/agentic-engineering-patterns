const slides = Array.from(document.querySelectorAll(".slide"));
const progress = document.querySelector("[data-progress]");
const previous = document.querySelector("[data-prev]");
const next = document.querySelector("[data-next]");
let current = 0;

function renderSlide() {
  slides.forEach((slide, index) => {
    slide.classList.toggle("active", index === current);
  });
  if (progress) progress.textContent = `${current + 1} / ${slides.length}`;
  if (previous) previous.disabled = current === 0;
  if (next) next.disabled = current === slides.length - 1;
}

function go(delta) {
  const target = current + delta;
  if (target < 0 || target >= slides.length) return;
  current = target;
  renderSlide();
}

document.addEventListener("keydown", (event) => {
  if (event.key === "ArrowRight" || event.key === "ArrowDown") go(1);
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") go(-1);
});

document.querySelectorAll("[data-prev]").forEach((button) => {
  button.addEventListener("click", () => go(-1));
});

document.querySelectorAll("[data-next]").forEach((button) => {
  button.addEventListener("click", () => go(1));
});

document.querySelectorAll(".quiz").forEach((quiz) => {
  const choices = Array.from(quiz.querySelectorAll(".choice"));
  const feedback = quiz.querySelector(".feedback");

  choices.forEach((choice) => {
    choice.addEventListener("click", () => {
      choices.forEach((item) => item.classList.remove("selected", "correct", "incorrect"));
      choice.classList.add("selected");
      const isCorrect = choice.dataset.correct === "true";
      choice.classList.add(isCorrect ? "correct" : "incorrect");
      const correct = choices.find((item) => item.dataset.correct === "true");
      if (!isCorrect && correct) correct.classList.add("correct");
      if (feedback) {
        feedback.textContent = isCorrect ? quiz.dataset.right : quiz.dataset.wrong;
      }
    });
  });
});

renderSlide();
