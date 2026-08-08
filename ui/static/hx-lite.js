/*
 * hx-lite -- a ~120 line subset of htmx, vendored so the dashboard has zero
 * external dependencies (a cluster-internal pod usually can't reach a CDN).
 *
 * Supported attributes:
 *   hx-get / hx-post   request URL
 *   hx-target          CSS selector for where the response goes (default: the element itself)
 *   hx-swap            innerHTML (default) | outerHTML
 *   hx-trigger         load | click | change | submit | "every Ns"   (default: click,
 *                      or change/submit on a form)
 *   hx-indicator       CSS selector of an element to give the .active class during the request
 *   hx-swap-oob        on a *returned* element: true|outerHTML | innerHTML -- swapped
 *                      into the live element with the same id, outside the main target
 *
 * Forms serialize their own fields into the query string for GET requests.
 */
(function () {
  "use strict";

  function attr(el, name) {
    return el.getAttribute(name);
  }

  function targetOf(el) {
    var sel = attr(el, "hx-target");
    return sel ? document.querySelector(sel) : el;
  }

  function swapInto(target, html, mode) {
    if (!target) return;
    if (mode === "outerHTML") {
      var tmp = document.createElement("div");
      tmp.innerHTML = html;
      var nodes = Array.prototype.slice.call(tmp.childNodes);
      var parent = target.parentNode;
      nodes.forEach(function (n) { parent.insertBefore(n, target); });
      parent.removeChild(target);
      nodes.forEach(function (n) { if (n.nodeType === 1) process(n); });
    } else {
      target.innerHTML = html;
      process(target);
    }
  }

  /* Pull elements carrying hx-swap-oob out of the response and swap them by id.
     Returns the remaining HTML for the primary target. */
  function extractOob(html) {
    var tmp = document.createElement("div");
    tmp.innerHTML = html;
    tmp.querySelectorAll("[hx-swap-oob]").forEach(function (node) {
      var mode = node.getAttribute("hx-swap-oob");
      var live = node.id ? document.getElementById(node.id) : null;
      if (live) {
        if (mode === "innerHTML") {
          live.innerHTML = node.innerHTML;
          process(live);
        } else {
          node.removeAttribute("hx-swap-oob");
          live.parentNode.replaceChild(node, live);
          process(node);
        }
      }
      node.remove();
    });
    return tmp.innerHTML;
  }

  function serialize(el) {
    var form = el.tagName === "FORM" ? el : el.closest("form");
    if (!form) return "";
    return new URLSearchParams(new FormData(form)).toString();
  }

  function issue(el, method, url) {
    var indicatorSel = attr(el, "hx-indicator");
    var indicator = indicatorSel ? document.querySelector(indicatorSel) : null;
    if (indicator) indicator.classList.add("active");

    var opts = { method: method, headers: { "HX-Request": "true" } };
    var query = el.tagName === "FORM" || el.closest("form") ? serialize(el) : "";
    if (method === "GET" && query) url += (url.indexOf("?") === -1 ? "?" : "&") + query;

    fetch(url, opts)
      .then(function (r) { return r.text(); })
      .then(function (html) {
        swapInto(targetOf(el), extractOob(html), attr(el, "hx-swap") || "innerHTML");
      })
      .catch(function (err) {
        console.error("hx-lite request failed", method, url, err);
      })
      .finally(function () {
        if (indicator) indicator.classList.remove("active");
      });
  }

  function bind(el) {
    if (el.dataset.hxBound) return;
    var url = attr(el, "hx-get") || attr(el, "hx-post");
    if (!url) return;
    el.dataset.hxBound = "1";

    var method = attr(el, "hx-get") ? "GET" : "POST";
    var spec = attr(el, "hx-trigger") || (el.tagName === "FORM" ? "submit" : "click");

    // hx-trigger accepts a comma-separated list, e.g. "load, every 60s".
    spec.split(",").forEach(function (raw) {
      var trigger = raw.trim();
      if (!trigger) return;

      var every = /^every\s+(\d+(?:\.\d+)?)s$/.exec(trigger);
      if (every) {
        setInterval(function () { issue(el, method, url); }, parseFloat(every[1]) * 1000);
        return;
      }
      if (trigger === "load") {
        issue(el, method, url);
        return;
      }
      el.addEventListener(trigger, function (evt) {
        if (trigger === "submit") evt.preventDefault();
        issue(el, method, url);
      });
      // Keyboard access for clickable non-button elements (e.g. table rows).
      if (trigger === "click" && el.tabIndex >= 0 && el.tagName !== "BUTTON") {
        el.addEventListener("keydown", function (evt) {
          if (evt.key === "Enter" || evt.key === " ") { evt.preventDefault(); el.click(); }
        });
      }
    });
  }

  function process(root) {
    (root || document).querySelectorAll("[hx-get],[hx-post]").forEach(bind);
  }

  window.hxLite = { process: process };
  document.addEventListener("DOMContentLoaded", function () { process(document); });
})();

/* ---- dashboard interactions (drawer + copy button) ---- */

function openDrawer() {
  document.getElementById("drawer").classList.add("open");
  document.getElementById("drawer-backdrop").classList.add("open");
}

function closeDrawer() {
  document.getElementById("drawer").classList.remove("open");
  document.getElementById("drawer-backdrop").classList.remove("open");
}

function copyText(btn) {
  var text = btn.getAttribute("data-copy");
  var done = function () {
    var original = btn.textContent;
    btn.textContent = "copied";
    setTimeout(function () { btn.textContent = original; }, 1200);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
  } else {
    // Plain-HTTP port-forward / NodePort access has no clipboard API.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } finally { document.body.removeChild(ta); }
  }
}

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") closeDrawer();
});
