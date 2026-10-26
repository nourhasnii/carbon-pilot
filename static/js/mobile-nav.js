/* CarbonPilot — mobile navigation drawer.
   Progressive enhancement for the workspace sidebar: below the md breakpoint
   the persistent sidebar becomes an off-canvas drawer opened by a hamburger
   button in the top bar.

   Wiring is data-attribute based so every authenticated layout can reuse it:
     [data-mobile-nav]           the drawer element (the sidebar)
     [data-mobile-nav-toggle]    the hamburger button
     [data-mobile-nav-close]     any "close" control (in-drawer button, backdrop)
     [data-mobile-nav-overlay]   the backdrop element
     data-mobile-nav-breakpoint  optional media query (default: below md)

   The script keeps aria-expanded / aria-label in sync, closes on Escape and on
   outside clicks, keeps Tab inside the open drawer and hands focus back to the
   hamburger, then resets itself when the viewport grows back to desktop.

   Without JavaScript the drawer geometry is inert (it is scoped to
   `html.cp-js-nav`), so the sidebar stays in the normal document flow and the
   workspace remains fully usable. */
(function () {
  "use strict";

  var drawer = document.querySelector("[data-mobile-nav]");
  var toggle = document.querySelector("[data-mobile-nav-toggle]");
  if (!drawer || !toggle) { return; }

  var overlay = document.querySelector("[data-mobile-nav-overlay]");
  var closers = document.querySelectorAll("[data-mobile-nav-close]");
  var query =
    toggle.getAttribute("data-mobile-nav-breakpoint") || "(max-width: 767.98px)";
  var OPEN = "is-open";
  var BODY_OPEN = "cp-nav-open";
  var lastFocused = null;

  function isMobile() {
    return typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  }

  function isOpen() { return drawer.classList.contains(OPEN); }

  function focusables() {
    return drawer.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]),' +
        ' select:not([disabled]), textarea:not([disabled]),' +
        ' [tabindex]:not([tabindex="-1"])'
    );
  }

  /* A closed drawer must be unreachable by keyboard and screen readers, but
     only while it really is off-canvas (mobile widths). */
  function hideDrawer(hidden) {
    if (hidden) {
      drawer.setAttribute("aria-hidden", "true");
      drawer.setAttribute("inert", "");
    } else {
      drawer.removeAttribute("aria-hidden");
      drawer.removeAttribute("inert");
    }
  }

  function open() {
    lastFocused = document.activeElement;
    drawer.classList.add(OPEN);
    if (overlay) { overlay.classList.add(OPEN); }
    document.body.classList.add(BODY_OPEN);
    hideDrawer(false);
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "Close menu");
    var target = drawer.querySelector("[data-mobile-nav-close]") || focusables()[0];
    if (target) { target.focus(); }
  }

  function close(returnFocus) {
    drawer.classList.remove(OPEN);
    if (overlay) { overlay.classList.remove(OPEN); }
    document.body.classList.remove(BODY_OPEN);
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Open menu");
    hideDrawer(isMobile());
    if (returnFocus) {
      var target = lastFocused && document.contains(lastFocused) ? lastFocused : toggle;
      target.focus();
    }
  }

  /* Keep the state coherent when the viewport crosses the breakpoint. */
  function sync() {
    if (isOpen() && !isMobile()) {
      close(false);
      return;
    }
    hideDrawer(isMobile() && !isOpen());
    if (!isOpen()) { document.body.classList.remove(BODY_OPEN); }
  }

  toggle.addEventListener("click", function () {
    if (isOpen()) { close(true); } else { open(); }
  });

  Array.prototype.forEach.call(closers, function (element) {
    element.addEventListener("click", function () { close(true); });
  });

  document.addEventListener("keydown", function (event) {
    if (!isOpen()) { return; }
    if (event.key === "Escape" || event.key === "Esc") {
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key !== "Tab") { return; }
    var items = focusables();
    if (!items.length) { return; }
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  document.addEventListener("click", function (event) {
    if (!isOpen()) { return; }
    if (drawer.contains(event.target) || toggle.contains(event.target)) { return; }
    close(false);
  });

  if (typeof window.matchMedia === "function") {
    var mq = window.matchMedia(query);
    if (mq.addEventListener) { mq.addEventListener("change", sync); }
    else if (mq.addListener) { mq.addListener(sync); }
  }
  sync();
})();
