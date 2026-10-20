document.addEventListener('DOMContentLoaded', function () {
  if (window.lucide) window.lucide.createIcons();

  const emissionsCanvas = document.getElementById('authEmissionsChart');
  if (window.Chart && emissionsCanvas) {
    new Chart(emissionsCanvas, {
      type: 'line',
      data: {
        labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
        datasets: [
          { label: 'Actual emissions', data: [82, 77, 73, 68, 62, 57], borderColor: '#10B981', backgroundColor: 'rgba(16,185,129,.10)', borderWidth: 2, pointRadius: 0, tension: .38, fill: true },
          { label: 'Target', data: [85, 81, 77, 73, 69, 64], borderColor: '#60A5FA', borderWidth: 1.5, borderDash: [5, 5], pointRadius: 0, tension: .3, fill: false }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true, backgroundColor: '#0F172A', padding: 8 } },
        scales: {
          x: { grid: { display: false }, border: { display: false }, ticks: { color: '#91A2B5', font: { family: 'Inter', size: 8 }, maxTicksLimit: 6 } },
          y: { display: false, min: 45, max: 95, grid: { display: false }, border: { display: false } }
        }
      }
    });
  }

  document.querySelectorAll('.password-toggle').forEach(function (button) {
    button.addEventListener('click', function () {
      const input = button.closest('.input-shell')?.querySelector('.password-input');
      if (!input) return;
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(visible));
      button.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
      button.innerHTML = visible ? '<i data-lucide="eye-off"></i>' : '<i data-lucide="eye"></i>';
      if (window.lucide) window.lucide.createIcons();
      input.focus();
    });
  });

  const signupForm = document.querySelector('.signup-form');
  if (!signupForm) return;
  signupForm.querySelectorAll('.role-card input[type="radio"]').forEach(function (input) {
    input.addEventListener('change', function () {
      signupForm.querySelectorAll('.role-card').forEach(function (card) {
        card.classList.toggle('is-selected', card.contains(input) && input.checked);
      });
    });
  });
});