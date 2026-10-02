/* maxads embed snippet -- rendered per ad, served as application/javascript.
   Usage on a foreign page:
     <script src="{{ base }}/embed/{{ slug }}.js" async></script>          */
(function () {
  "use strict";
  var BASE = "{{ base }}";
  var SLUG = "{{ slug }}";
  var W = {{ width }}, H = {{ height }};
  var MAXW = {{ max_width }}, ALIGN = "{{ align }}", RADIUS = {{ radius }};

  // document.currentScript is null inside async callbacks, so capture it now.
  var script = document.currentScript;
  if (!script) {
    var all = document.getElementsByTagName("script");
    script = all[all.length - 1];
  }

  var box = document.createElement("div");
  box.className = "maxads";
  box.setAttribute("data-maxads", SLUG);
  // Reserving the aspect ratio up front is what stops the host page from
  // jumping once the media loads.
  var cap = MAXW > 0 ? Math.min(MAXW, W || MAXW) : (W || 800);
  var margin = ALIGN === "left" ? "0 auto 0 0"
             : ALIGN === "right" ? "0 0 0 auto" : "0 auto";
  box.style.cssText =
    "position:relative;display:block;width:100%;max-width:" + cap + "px;" +
    "margin:" + margin + ";overflow:hidden;line-height:0;" +
    (RADIUS ? "border-radius:" + RADIUS + "px;" : "") +
    (W && H ? "aspect-ratio:" + W + "/" + H + ";" : "");

  var el;
  {% if kind == "image" %}
  el = document.createElement("img");
  el.src = BASE + "/m/" + SLUG;
  el.alt = "{{ title }}";
  el.loading = "lazy";
  el.decoding = "async";
  {% else %}
  el = document.createElement("video");
  el.src = BASE + "/m/" + SLUG;
  el.autoplay = true; el.muted = true; el.loop = true;
  el.playsInline = true; el.setAttribute("playsinline", "");
  el.preload = "metadata";
  {% endif %}
  el.style.cssText =
    "display:block;width:100%;height:100%;object-fit:contain;border:0;";

  {% if has_target %}
  var link = document.createElement("a");
  link.href = BASE + "/c/" + SLUG;
  link.target = "_blank";
  link.rel = "noopener sponsored";
  link.style.cssText = "display:block;width:100%;height:100%;";
  link.appendChild(el);
  box.appendChild(link);
  {% else %}
  box.appendChild(el);
  {% endif %}

  if (script && script.parentNode) {
    script.parentNode.insertBefore(box, script);
  } else {
    document.body.appendChild(box);
  }

  // Count once the banner has actually been on screen, not merely requested.
  var counted = false;
  function count() {
    if (counted) return;
    counted = true;
    var url = BASE + "/api/impression";
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url, SLUG);
    } else {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.send(SLUG);
    }
  }

  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) { count(); io.disconnect(); return; }
      }
    }, { threshold: 0.5 });
    io.observe(box);
  } else {
    count();
  }
})();
