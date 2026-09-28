/* pywinhtml page runtime. Injected automatically, so a page never needs to
   include a <script> of its own.

   What it provides:
     winCall("fn", ...args)   -> call a Python callback, returns a Promise
     win.minimize() / maximize() / restore() / toggle() / close()
     win.on("channel", cb)    -> receives app.event.emit(...) from Python
     data-win="drag|minimize|maximize|restore|toggle|close"
     data-win-call="fn"       -> calls a Python callback on click
   plus the eight invisible resize edges, added here so the page has no
   window-chrome markup at all.
*/
(function () {
  "use strict";
  if (window.win && window.win.__installed) return;

  var EDGE = 6;    // thickness of the resize edges, px
  var CORNER = 12; // size of the resize corners, px
  var root = document.documentElement;
  var channels = {};
  var dragging = false;

  // --- bridge to Python ---------------------------------------------------
  function api() {
    return window.pywebview && window.pywebview.api;
  }

  function send(method, args) {
    var bridge = api();
    if (!bridge || typeof bridge[method] !== "function") {
      return Promise.reject(new Error("pywinhtml: no bridge method " + method));
    }
    return bridge[method].apply(bridge, args || []);
  }

  // --- styles -------------------------------------------------------------
  function injectCss() {
    var css = [
      '[data-win]{-webkit-user-select:none;user-select:none}',
      '.win-rz{position:fixed;z-index:2147483000}',
      'html.win-max .win-rz{display:none}',
      '.win-rz-t{top:0;left:' + CORNER + 'px;right:' + CORNER + 'px;height:' + EDGE + 'px;cursor:ns-resize}',
      '.win-rz-b{bottom:0;left:' + CORNER + 'px;right:' + CORNER + 'px;height:' + EDGE + 'px;cursor:ns-resize}',
      '.win-rz-l{left:0;top:' + CORNER + 'px;bottom:' + CORNER + 'px;width:' + EDGE + 'px;cursor:ew-resize}',
      '.win-rz-r{right:0;top:' + CORNER + 'px;bottom:' + CORNER + 'px;width:' + EDGE + 'px;cursor:ew-resize}',
      '.win-rz-tl{top:0;left:0;width:' + CORNER + 'px;height:' + CORNER + 'px;cursor:nwse-resize}',
      '.win-rz-tr{top:0;right:0;width:' + CORNER + 'px;height:' + CORNER + 'px;cursor:nesw-resize}',
      '.win-rz-bl{bottom:0;left:0;width:' + CORNER + 'px;height:' + CORNER + 'px;cursor:nesw-resize}',
      '.win-rz-br{bottom:0;right:0;width:' + CORNER + 'px;height:' + CORNER + 'px;cursor:nwse-resize}'
    ].join("\n");
    var tag = document.createElement("style");
    tag.id = "win-runtime-css";
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  // --- invisible edges used for resizing ----------------------------------
  function injectEdges() {
    var box = document.createDocumentFragment();
    ["t", "b", "l", "r", "tl", "tr", "bl", "br"].forEach(function (edge) {
      var el = document.createElement("div");
      el.className = "win-rz win-rz-" + edge;
      el.setAttribute("data-win-edge", edge);
      box.appendChild(el);
    });
    document.body.appendChild(box);
  }

  // --- declarative attributes ---------------------------------------------
  var ACTIONS = {
    minimize: function () { return send("minimize"); },
    maximize: function () { return send("maximize"); },
    restore: function () { return send("restore"); },
    toggle: function () { return send("toggle_max"); },
    close: function () { return send("close"); }
  };

  // Inside a drag area these never start a drag.
  var NO_DRAG = 'button,a,input,select,textarea,label,[data-win-call],[data-win-nodrag]';

  function valueOf(el) {
    if (!el) return null;
    if (el.type === "checkbox" || el.type === "radio") return el.checked;
    if ("value" in el) return el.value;
    return el.textContent;
  }

  function argsOf(el) {
    var from = el.getAttribute("data-win-from");
    if (from) {
      return from.split(",").map(function (selector) {
        return valueOf(document.querySelector(selector.trim()));
      });
    }

    var raw = el.getAttribute("data-win-args");
    if (raw !== null && raw !== "") {
      try {
        var parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [parsed];
      } catch (err) {
        return [raw];
      }
    }
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return [valueOf(el)];
    return [];
  }

  function runCall(el) {
    var name = el.getAttribute("data-win-call");
    var target = el.getAttribute("data-win-target");
    var promise = win.call.apply(null, [name].concat(argsOf(el)));
    promise.then(function (value) {
      if (!target) return;
      var box = document.querySelector(target);
      if (!box) return;
      if (value === null || value === undefined) box.textContent = "";
      else if (typeof value === "object") box.textContent = JSON.stringify(value);
      else box.textContent = String(value);
    }, function (err) {
      console.error("pywinhtml: " + name + "()", err);
    });
    return promise;
  }

  function delegate(type) {
    document.addEventListener(type, function (event) {
      if (!event.target || !event.target.closest) return;

      var caller = event.target.closest("[data-win-call]");
      if (caller && (caller.getAttribute("data-win-on") || "click") === type) {
        if (type === "submit") event.preventDefault();
        runCall(caller);
      }

      if (type !== "click") return;
      var actor = event.target.closest("[data-win]");
      if (!actor) return;
      var action = ACTIONS[actor.getAttribute("data-win")];
      if (action) action();
    }, true);
  }

  // --- moving and resizing the window -------------------------------------
  // Only the start of a gesture is reported; Python then follows the cursor
  // itself until the mouse button is released. WebView2 keeps the mouse
  // capture in its own process, so Win32's own SC_MOVE / SC_SIZE never work.
  function wireGestures() {
    document.addEventListener("mousedown", function (event) {
      if (event.button !== 0 || !event.target || !event.target.closest) return;

      var zone = event.target.closest(".win-rz");
      if (zone) {
        event.preventDefault();
        dragging = true;
        send("begin_resize", [zone.getAttribute("data-win-edge")]);
        return;
      }

      var handle = event.target.closest('[data-win="drag"]');
      if (!handle) return;
      if (event.target.closest(NO_DRAG)) return;
      event.preventDefault();
      dragging = true;
      send("begin_drag");
    }, true);

    // Safety net: the Python loop also stops on its own when the button is up.
    function stop() {
      if (!dragging) return;
      dragging = false;
      send("end_gesture");
    }
    document.addEventListener("mouseup", stop, true);
    window.addEventListener("blur", stop);

    document.addEventListener("dblclick", function (event) {
      if (!event.target || !event.target.closest) return;
      if (event.target.closest('[data-win="drag"]')) send("toggle_max");
    }, true);
  }

  // --- public API ---------------------------------------------------------
  var win = {
    __installed: true,
    maximized: false,

    /** Call a Python callback by name. Returns a Promise with its result. */
    call: function (name) {
      var args = Array.prototype.slice.call(arguments, 1);
      return send("invoke", [name, args]);
    },

    minimize: ACTIONS.minimize,
    maximize: ACTIONS.maximize,
    restore: ACTIONS.restore,
    toggle: ACTIONS.toggle,
    close: ACTIONS.close,
    beginDrag: function () { dragging = true; return send("begin_drag"); },
    beginResize: function (edge) { dragging = true; return send("begin_resize", [edge]); },

    /** Listen for app.event.emit("channel", ...) coming from Python. */
    on: function (channel, handler) {
      (channels[channel] = channels[channel] || []).push(handler);
      return function () { win.off(channel, handler); };
    },

    off: function (channel, handler) {
      var list = channels[channel];
      if (!list) return;
      if (!handler) { delete channels[channel]; return; }
      var index = list.indexOf(handler);
      if (index > -1) list.splice(index, 1);
    },

    /** Runs fn now; kept so page code can read like other runtimes. */
    ready: function (fn) { fn(win); return win; },

    /** Internal: deliver a message emitted from Python. */
    _emit: function (channel, args) {
      if (channel === "__state") {
        win.maximized = !!(args[0] && args[0].maximized);
        root.classList.toggle("win-max", win.maximized);
        return;
      }
      (channels[channel] || []).forEach(function (fn) {
        try { fn.apply(null, args || []); } catch (err) { console.error(err); }
      });
      window.dispatchEvent(new CustomEvent("win:" + channel, { detail: args }));
    }
  };

  window.win = win;
  window.winCall = function () { return win.call.apply(win, arguments); };

  injectCss();
  injectEdges();
  wireGestures();
  // Event types data-win-on can pick from.
  ["click", "dblclick", "change", "input", "submit", "keyup", "mousedown"].forEach(delegate);
  root.classList.add("win-desktop");

  send("state").then(function (state) { win._emit("__state", [state]); }, function () {});
  window.dispatchEvent(new CustomEvent("winready", { detail: win }));
})();
