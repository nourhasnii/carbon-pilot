document.addEventListener('DOMContentLoaded', function () {
  const emissionChartCanvas = document.getElementById('emissionsChart');
  if (emissionChartCanvas) {
    new Chart(emissionChartCanvas, {
      type: 'line',
      data: {
        labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
        datasets: [
          {
            label: 'Actual Emissions',
            data: [82, 78, 76, 72, 69, 64, 61, 58, 54, 50, 47, 42],
            borderColor: '#10B981',
            backgroundColor: 'rgba(16,185,129,0.12)',
            borderWidth: 3,
            tension: 0.4,
            fill: true,
            pointRadius: 0
          },
          {
            label: 'Target',
            data: [86, 82, 79, 75, 71, 68, 64, 60, 57, 52, 49, 45],
            borderColor: '#2563EB',
            borderWidth: 2,
            borderDash: [8, 6],
            tension: 0.35,
            fill: false,
            pointRadius: 0
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'nearest',
          intersect: false
        },
        plugins: {
          legend: {
            display: false
          },
          tooltip: {
            enabled: true,
            backgroundColor: '#0F172A',
            titleColor: '#F8FAFC',
            bodyColor: '#F8FAFC',
            borderColor: 'rgba(148,163,184,0.2)',
            borderWidth: 1,
            padding: 10
          }
        },
        scales: {
          x: {
            grid: {
              display: false
            },
            ticks: {
              color: '#64748B',
              font: {
                family: 'Inter'
              }
            }
          },
          y: {
            grid: {
              color: 'rgba(148,163,184,0.12)'
            },
            ticks: {
              color: '#64748B',
              font: {
                family: 'Inter'
              }
            }
          }
        }
      }
    });
  }

  const notificationToggle = document.getElementById('notificationToggle');
  const notificationPanel = document.getElementById('notificationPanel');
  const profileToggle = document.getElementById('profileToggle');
  const profilePanel = document.getElementById('profilePanel');

  if (notificationToggle && notificationPanel) {
    notificationToggle.addEventListener('click', function () {
      notificationPanel.classList.toggle('hidden');
      if (profilePanel) profilePanel.classList.add('hidden');
    });
  }

  if (profileToggle && profilePanel) {
    profileToggle.addEventListener('click', function () {
      profilePanel.classList.toggle('hidden');
      if (notificationPanel) notificationPanel.classList.add('hidden');
    });
  }

  document.addEventListener('click', function (event) {
    const clickedNotification = event.target.closest('#notificationToggle') || event.target.closest('#notificationPanel');
    const clickedProfile = event.target.closest('#profileToggle') || event.target.closest('#profilePanel');

    if (!clickedNotification && notificationPanel) notificationPanel.classList.add('hidden');
    if (!clickedProfile && profilePanel) profilePanel.classList.add('hidden');
  });

  const darkToggle = document.createElement('button');
  darkToggle.type = 'button';
  darkToggle.className = 'icon-button theme-toggle';
  darkToggle.setAttribute('aria-label', 'Toggle dark mode');
  darkToggle.innerHTML = '<i data-lucide="moon"></i>';

  const topbarTools = document.querySelector('.topbar-tools');
  if (topbarTools && !document.querySelector('.theme-toggle')) {
    topbarTools.insertBefore(darkToggle, topbarTools.lastElementChild);
  }

  darkToggle.addEventListener('click', function () {
    document.body.classList.toggle('dark');
    const isDark = document.body.classList.contains('dark');
    darkToggle.innerHTML = isDark ? '<i data-lucide="sun"></i>' : '<i data-lucide="moon"></i>';
    lucide.createIcons();
  });

  const quickActionModal = document.querySelector('.modal-overlay');
  const quickActionButtons = document.querySelectorAll('.action-button, .primary-btn, .secondary-btn');
  const quickActionClose = document.querySelector('.modal-close');

  function openQuickActionModal() {
    if (!quickActionModal) return;
    quickActionModal.classList.add('is-open');
    quickActionModal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeQuickActionModal() {
    if (!quickActionModal) return;
    quickActionModal.classList.remove('is-open');
    quickActionModal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  quickActionButtons.forEach(function (button) {
    if (button.classList.contains('primary-btn') || button.classList.contains('secondary-btn') || button.classList.contains('action-button')) {
      button.addEventListener('click', function () {
        if (button.textContent.includes('View Full Analytics') || button.textContent.includes('Export Report') || button.textContent.includes('Run Prediction') || button.textContent.includes('Generate ESG Report') || button.textContent.includes('Add Emission') || button.textContent.includes('Create Objective')) {
          openQuickActionModal();
        }
      });
    }
  });

  if (quickActionClose) quickActionClose.addEventListener('click', closeQuickActionModal);

  if (quickActionModal) {
    quickActionModal.addEventListener('click', function (event) {
      if (event.target === quickActionModal) closeQuickActionModal();
    });
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && quickActionModal && quickActionModal.classList.contains('is-open')) {
      closeQuickActionModal();
    }
  });

  const quickActionForm = document.getElementById('quickActionForm');
  if (quickActionForm) {
    quickActionForm.addEventListener('submit', function (event) {
      event.preventDefault();
      closeQuickActionModal();
    });
  }

  if (window.lucide) {
    lucide.createIcons();
  }
});
