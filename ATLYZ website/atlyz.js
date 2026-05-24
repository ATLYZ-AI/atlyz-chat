/* ════════════════════════════════════════════════════════════════════
   ATLYZ — Shared site behavior
   Mobile nav, scroll progress, nav opacity, scroll reveals, year stamp.
   Loaded by every page. Page-specific JS stays in each page.
   ════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── MOBILE NAV ─────────────────────────────────────────────── */
  function toggleNav() {
    var links = document.querySelector('.nav-links');
    if (links) links.classList.toggle('open');
  }
  window.toggleNav = toggleNav; // markup uses inline onclick="toggleNav()"

  document.addEventListener('click', function (e) {
    var link = e.target.closest('.nav-links a');
    if (link) {
      var links = document.querySelector('.nav-links');
      if (links) links.classList.remove('open');
    }
  });

  /* ── SCROLL PROGRESS BAR ────────────────────────────────────── */
  var prog = document.getElementById('scroll-prog');
  if (prog) {
    var updateProg = function () {
      var scrolled = window.scrollY;
      var total = document.documentElement.scrollHeight - window.innerHeight;
      prog.style.width = (total > 0 ? Math.min(scrolled / total * 100, 100) : 0) + '%';
    };
    window.addEventListener('scroll', updateProg, { passive: true });
    updateProg();
  }

  /* ── NAV SCROLL OPACITY ─────────────────────────────────────── */
  var nav = document.querySelector('nav');
  if (nav) {
    window.addEventListener('scroll', function () {
      if (window.scrollY > 80) {
        nav.style.background = 'rgba(0,0,0,0.96)';
        nav.style.boxShadow = '0 8px 32px rgba(0,0,0,0.4)';
      } else {
        nav.style.background = 'rgba(0,0,0,0.88)';
        nav.style.boxShadow = 'inset 0 1px 0 rgba(255,255,255,0.05)';
      }
    }, { passive: true });
  }

  /* ── SCROLL REVEAL ──────────────────────────────────────────── */
  function reveal(el) {
    // Add every revealed-class variant so any page's CSS is satisfied.
    el.classList.add('vis', 'visible', 'is-visible');
  }
  function initReveals() {
    var els = document.querySelectorAll('.reveal');
    if (!('IntersectionObserver' in window)) {
      els.forEach(reveal); // no IO support → just show everything
      return;
    }
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          reveal(e.target);
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.07, rootMargin: '0px 0px -40px 0px' });
    els.forEach(function (el) { obs.observe(el); });
  }

  /* ── FOOTER YEAR ────────────────────────────────────────────── */
  function stampYear() {
    document.querySelectorAll('[data-year]').forEach(function (el) {
      el.textContent = new Date().getFullYear();
    });
  }

  function init() {
    initReveals();
    stampYear();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
