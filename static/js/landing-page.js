document.addEventListener('DOMContentLoaded', function () {
  if (window.lucide) window.lucide.createIcons();

  const menuToggle = document.querySelector('.menu-toggle');
  const navigation = document.querySelector('.main-nav');

  function closeNavigation() {
    if (!menuToggle || !navigation) return;
    menuToggle.setAttribute('aria-expanded', 'false');
    menuToggle.setAttribute('aria-label', 'Open navigation');
    menuToggle.innerHTML = '<i data-lucide="menu"></i>';
    navigation.classList.remove('is-open');
    document.body.classList.remove('menu-open');
    if (window.lucide) window.lucide.createIcons();
  }

  if (menuToggle && navigation) {
    menuToggle.addEventListener('click', function () {
      const isOpen = menuToggle.getAttribute('aria-expanded') === 'true';
      menuToggle.setAttribute('aria-expanded', String(!isOpen));
      menuToggle.setAttribute('aria-label', isOpen ? 'Open navigation' : 'Close navigation');
      menuToggle.innerHTML = isOpen ? '<i data-lucide="menu"></i>' : '<i data-lucide="x"></i>';
      navigation.classList.toggle('is-open', !isOpen);
      document.body.classList.toggle('menu-open', !isOpen);
      if (window.lucide) window.lucide.createIcons();
    });

    navigation.querySelectorAll('a').forEach(function (link) {
      link.addEventListener('click', closeNavigation);
    });

    window.addEventListener('resize', function () {
      if (window.innerWidth > 820) closeNavigation();
    });
  }

  const modal = document.getElementById('demoModal');
  let lastFocusedElement = null;

  function openModal() {
    if (!modal) return;
    lastFocusedElement = document.activeElement;
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    const firstInput = modal.querySelector('input');
    if (firstInput) firstInput.focus();
  }

  function closeModal() {
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    if (lastFocusedElement && typeof lastFocusedElement.focus === 'function') lastFocusedElement.focus();
  }

  document.querySelectorAll('[data-open-demo]').forEach(function (button) {
    button.addEventListener('click', openModal);
  });
  document.querySelectorAll('[data-close-demo]').forEach(function (button) {
    button.addEventListener('click', closeModal);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeModal();
      closeNavigation();
    }
  });

  const demoForm = document.querySelector('.demo-form');
  if (demoForm) {
    demoForm.addEventListener('submit', function (event) {
      event.preventDefault();
      const note = demoForm.querySelector('.form-note');
      if (note) note.textContent = 'Thanks. Your demo request is ready to send.';
    });
  }

  if (window.Chart) {
    const heroCanvas = document.getElementById('heroEmissionsChart');
    if (heroCanvas) {
      new Chart(heroCanvas, {
        type: 'line',
        data: {
          labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
          datasets: [
            { label: 'Actual Emissions', data: [82, 77, 74, 67, 62, 56], borderColor: '#10B981', backgroundColor: 'rgba(16,185,129,.10)', borderWidth: 2.5, pointRadius: 0, tension: .38, fill: true },
            { label: 'Target', data: [85, 81, 76, 72, 67, 63], borderColor: '#2563EB', borderWidth: 2, borderDash: [5, 5], pointRadius: 0, tension: .32, fill: false }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 700 },
          plugins: { legend: { display: false }, tooltip: { enabled: true, displayColors: false, backgroundColor: '#0f172a', padding: 9 } },
          scales: {
            x: { grid: { display: false }, border: { display: false }, ticks: { color: '#94a3b8', font: { family: 'DM Sans', size: 9 }, maxTicksLimit: 6 } },
            y: { display: false, min: 45, max: 95, border: { display: false }, grid: { display: false } }
          }
        }
      });
    }

    const forecastCanvas = document.getElementById('forecastChart');
    if (forecastCanvas) {
      const context = forecastCanvas.getContext('2d');
      const forecastGradient = context.createLinearGradient(0, 0, 0, 260);
      forecastGradient.addColorStop(0, 'rgba(37,99,235,.15)');
      forecastGradient.addColorStop(1, 'rgba(37,99,235,0)');
      new Chart(forecastCanvas, {
        type: 'line',
        data: {
          labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
          datasets: [
            { label: 'Actual emissions', data: [83, 79, 76, 74, 69, 65, null, null, null, null, null, null], borderColor: '#10B981', backgroundColor: 'rgba(16,185,129,.08)', borderWidth: 2.5, pointRadius: 2, pointBackgroundColor: '#10B981', tension: .38, fill: true },
            { label: 'Forecast', data: [null, null, null, null, null, 65, 62, 59, 55, 52, 48, 45], borderColor: '#2563EB', backgroundColor: forecastGradient, borderWidth: 2.5, pointRadius: 0, borderDash: [5, 4], tension: .38, fill: true, spanGaps: true },
            { label: 'Target', data: [86, 82, 78, 74, 70, 66, 63, 60, 57, 54, 51, 48], borderColor: '#8B83EF', borderWidth: 2, pointRadius: 0, borderDash: [3, 5], tension: .3, fill: false }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: { legend: { display: false }, tooltip: { backgroundColor: '#0f172a', padding: 11, titleFont: { family: 'DM Sans' }, bodyFont: { family: 'DM Sans' } } },
          scales: {
            x: { grid: { display: false }, border: { display: false }, ticks: { color: '#8492a3', font: { family: 'DM Sans', size: 10 }, maxRotation: 0 } },
            y: { min: 35, max: 95, border: { display: false }, grid: { color: 'rgba(148,163,184,.16)' }, ticks: { color: '#8492a3', font: { family: 'DM Sans', size: 10 }, padding: 8, callback: function (value) { return value + 'k'; } } }
          }
        }
      });
    }
  }

  const carousel = document.querySelector('[data-carousel]');
  if (carousel) {
    const track = carousel.querySelector('[data-carousel-track]');
    const slides = Array.from(track.children);
    const dots = Array.from(document.querySelectorAll('.carousel-dot'));
    const previous = document.querySelector('[data-carousel-prev]');
    const next = document.querySelector('[data-carousel-next]');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let activeIndex = 0;
    let autoplayId;

    function showSlide(index) {
      activeIndex = (index + slides.length) % slides.length;
      track.style.transform = 'translateX(-' + (activeIndex * 100) + '%)';
      slides.forEach(function (slide, slideIndex) {
        slide.setAttribute('aria-hidden', String(slideIndex !== activeIndex));
        slide.inert = slideIndex !== activeIndex;
      });
      dots.forEach(function (dot, dotIndex) {
        const isActive = dotIndex === activeIndex;
        dot.classList.toggle('is-active', isActive);
        if (isActive) dot.setAttribute('aria-current', 'true');
        else dot.removeAttribute('aria-current');
      });
    }

    function stopAutoplay() {
      window.clearInterval(autoplayId);
    }

    function startAutoplay() {
      stopAutoplay();
      if (!reducedMotion) autoplayId = window.setInterval(function () { showSlide(activeIndex + 1); }, 6000);
    }

    if (previous) previous.addEventListener('click', function () { showSlide(activeIndex - 1); startAutoplay(); });
    if (next) next.addEventListener('click', function () { showSlide(activeIndex + 1); startAutoplay(); });
    dots.forEach(function (dot, index) { dot.addEventListener('click', function () { showSlide(index); startAutoplay(); }); });
    carousel.addEventListener('mouseenter', stopAutoplay);
    carousel.addEventListener('mouseleave', startAutoplay);
    carousel.addEventListener('focusin', stopAutoplay);
    carousel.addEventListener('focusout', function (event) {
      if (!carousel.contains(event.relatedTarget)) startAutoplay();
    });
    showSlide(0);
    startAutoplay();
  }

  const revealItems = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const revealObserver = new IntersectionObserver(function (entries, observer) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealItems.forEach(function (item) { revealObserver.observe(item); });
  } else {
    revealItems.forEach(function (item) { item.classList.add('is-visible'); });
  }
});