// Reviewer name is typed once and attached to every form marked needs-actor.
const who = document.getElementById('reviewer');
if (who) {
  try { who.value = localStorage.getItem('reviewer') || ''; } catch (e) {}
  who.addEventListener('input', () => { try { localStorage.setItem('reviewer', who.value); } catch (e) {} });
  document.querySelectorAll('form.needs-actor').forEach((f) => f.addEventListener('submit', (ev) => {
    if (!who.value.trim()) { ev.preventDefault(); who.focus(); alert('Enter your reviewer name first.'); return; }
    f.querySelector('input[name=actor]').value = who.value.trim();
  }));
}
