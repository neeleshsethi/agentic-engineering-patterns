/* Interactive step-through ("scrubber") component.
 *
 * Markup contract (authored as raw HTML inside a markdown article):
 *
 *   <div class="scrubber" data-scrubber>
 *     <div class="scrubber-stage">
 *       <div class="scrubber-step">
 *         <span class="scrubber-time">t=0</span>
 *         <div class="scrubber-caption">Worker A acquires the claim…</div>
 *         <pre class="scrubber-item">lock#abc { owner: "aaa" }  <span class="ok">A holds it</span></pre>
 *       </div>
 *       <div class="scrubber-step"> … </div>
 *     </div>
 *   </div>
 *
 * Controls (prev / next / play / dots) are generated automatically.
 * Keyboard: ← / → step when the scrubber is focused. Autoplay is disabled
 * when the user prefers reduced motion.
 */
(function () {
  "use strict";

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function build(root) {
    if (root.dataset.scrubberReady === "1") return;
    root.dataset.scrubberReady = "1";

    var steps = Array.prototype.slice.call(root.querySelectorAll(".scrubber-step"));
    if (steps.length === 0) return;

    var index = 0;
    var timer = null;

    // Controls
    var controls = document.createElement("div");
    controls.className = "scrubber-controls";

    var prev = document.createElement("button");
    prev.type = "button";
    prev.textContent = "‹ Back";
    prev.setAttribute("aria-label", "Previous step");

    var dots = document.createElement("div");
    dots.className = "scrubber-dots";

    var next = document.createElement("button");
    next.type = "button";
    next.textContent = "Next ›";
    next.setAttribute("aria-label", "Next step");

    var play = document.createElement("button");
    play.type = "button";
    play.textContent = "▶ Play";
    play.setAttribute("aria-label", "Play the sequence");

    controls.appendChild(prev);
    controls.appendChild(dots);
    controls.appendChild(next);
    if (!REDUCED) controls.appendChild(play);
    root.appendChild(controls);

    var dotEls = steps.map(function (_, i) {
      var d = document.createElement("button");
      d.type = "button";
      d.className = "scrubber-dot";
      d.setAttribute("aria-label", "Go to step " + (i + 1));
      d.addEventListener("click", function () { stop(); go(i); });
      dots.appendChild(d);
      return d;
    });

    root.setAttribute("tabindex", "0");
    root.setAttribute("role", "group");
    root.setAttribute("aria-roledescription", "step-through animation");

    function render() {
      steps.forEach(function (s, i) { s.classList.toggle("is-active", i === index); });
      dotEls.forEach(function (d, i) { d.classList.toggle("is-active", i === index); });
      prev.disabled = index === 0;
      next.disabled = index === steps.length - 1;
    }

    function go(i) {
      index = Math.max(0, Math.min(steps.length - 1, i));
      render();
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; play.textContent = "▶ Play"; }
    }

    function start() {
      if (timer) { stop(); return; }
      if (index === steps.length - 1) go(0);
      play.textContent = "❚❚ Pause";
      timer = setInterval(function () {
        if (index >= steps.length - 1) { stop(); return; }
        go(index + 1);
      }, 1900);
    }

    prev.addEventListener("click", function () { stop(); go(index - 1); });
    next.addEventListener("click", function () { stop(); go(index + 1); });
    play.addEventListener("click", start);

    root.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { stop(); go(index - 1); e.preventDefault(); }
      else if (e.key === "ArrowRight") { stop(); go(index + 1); e.preventDefault(); }
    });

    go(0);
  }

  function init() {
    document.querySelectorAll("[data-scrubber]").forEach(build);
  }

  // Run on first load, and again after Material for MkDocs instant navigation swaps the page.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(init);
  } else if (document.readyState !== "loading") {
    init();
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
