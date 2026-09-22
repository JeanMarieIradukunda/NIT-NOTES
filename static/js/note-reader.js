/* Note reader: print button and table-of-contents highlighting.
   Kept in a static file (no inline script) so the reader page can run under a
   strict Content-Security-Policy. */
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-print]').forEach(function (b) {
    b.addEventListener('click', function () { window.print(); });
  });

  var links = {};
  document.querySelectorAll('#toc-list a').forEach(function (a) { links[a.dataset.target] = a; });
  var ids = Object.keys(links);
  if (!ids.length || !('IntersectionObserver' in window)) return;
  var obs = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      ids.forEach(function (id) { links[id].classList.remove('active'); });
      if (links[e.target.id]) links[e.target.id].classList.add('active');
    });
  }, { rootMargin: '-100px 0px -70% 0px' });
  ids.forEach(function (id) { var el = document.getElementById(id); if (el) obs.observe(el); });
});
