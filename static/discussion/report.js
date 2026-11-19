'use strict';
document.getElementById('print-report').addEventListener('click', () => window.print());
function revealCitation() {
  const id = location.hash.slice(1);
  const source = id.startsWith('source-E-') ? document.getElementById(id) : null;
  if (source?.tagName === 'DETAILS') { source.open = true; source.scrollIntoView({block:'start'}); }
}
window.addEventListener('hashchange', revealCitation);
revealCitation();
