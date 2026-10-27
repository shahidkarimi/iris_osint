// MapLibre integration for the dashboard preview, the full map page and the detail mini-map.
// Popups are built with DOM APIs (textContent), never innerHTML, so scraped titles cannot inject markup.
(function () {
  "use strict";

  var COLORS = { commercial_land_disposal: "#e08a12", big_box_industrial_development: "#3478ea" };
  var GERMANY = [[5.7, 47.2], [15.2, 55.1]];
  var root = document.documentElement;

  function isDark() { return root.classList.contains("dark"); }
  function styleUrl(el) { return isDark() ? el.dataset.styleDark : el.dataset.styleLight; }
  function i18n(el) {
    try { return JSON.parse(el.dataset.i18n || "{}"); } catch (e) { return {}; }
  }

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function fmtArea(v, lang) {
    if (v === null || v === undefined) return null;
    var nf = new Intl.NumberFormat(lang === "en" ? "en-GB" : "de-DE", { maximumFractionDigits: v >= 100000 ? 1 : 0 });
    return v >= 100000 ? nf.format(v / 10000) + " ha" : nf.format(v) + " m²";
  }

  function popupNode(p, t, lang) {
    var wrap = h("div", "w-72 p-4 font-sans");
    var chip = h("span", "chip " + (p.signal_type === "commercial_land_disposal" ? "chip-land" : "chip-box"), t[p.signal_type] || p.signal_type);
    wrap.appendChild(chip);
    var title = h("a", "mt-2 block text-sm leading-snug font-semibold text-slate-900 hover:underline dark:text-white", p.title);
    title.href = "/signale/" + p.signal_id;
    wrap.appendChild(title);
    var meta = h("div", "mt-2 space-y-0.5 text-xs text-slate-500 dark:text-slate-400");
    var place = [p.street, p.district, p.municipality].filter(Boolean).join(", ");
    if (place) meta.appendChild(h("div", null, "📍 " + place));
    if (p.published_at) meta.appendChild(h("div", null, "🗓 " + new Date(p.published_at).toLocaleDateString(lang === "en" ? "en-GB" : "de-DE")));
    var area = fmtArea(p.area_sqm || p.plot_area_sqm, lang);
    if (area) meta.appendChild(h("div", null, "▦ " + area));
    meta.appendChild(h("div", "font-mono text-[11px] text-slate-400", "EPSG:25832 " + Math.round(p.x_25832) + " / " + Math.round(p.y_25832) + " · " + (t["prec." + p.geocode_precision] || p.geocode_precision)));
    wrap.appendChild(meta);
    return wrap;
  }

  function addLayers(map, data, opts) {
    if (map.getSource("signals")) return;
    map.addSource("signals", { type: "geojson", data: data, cluster: !!opts.cluster, clusterRadius: 38, clusterMaxZoom: 9 });
    if (opts.cluster) {
      map.addLayer({
        id: "clusters", type: "circle", source: "signals", filter: ["has", "point_count"],
        paint: {
          "circle-color": isDark() ? "#e2e8f0" : "#0f1420",
          "circle-opacity": 0.9,
          "circle-radius": ["step", ["get", "point_count"], 14, 5, 18, 15, 24],
          "circle-stroke-width": 4,
          "circle-stroke-color": isDark() ? "rgba(226,232,240,0.25)" : "rgba(15,20,32,0.15)"
        }
      });
      map.addLayer({
        id: "cluster-count", type: "symbol", source: "signals", filter: ["has", "point_count"],
        layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 12, "text-font": ["Noto Sans Bold"] },
        paint: { "text-color": isDark() ? "#0a0d14" : "#ffffff" }
      });
    }
    map.addLayer({
      id: "halo", type: "circle", source: "signals", filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-radius": ["match", ["get", "geocode_precision"], "address", 13, "street", 13, "district", 16, 20],
        "circle-color": ["match", ["get", "signal_type"], "commercial_land_disposal", COLORS.commercial_land_disposal, COLORS.big_box_industrial_development],
        "circle-opacity": 0.16, "circle-blur": 0.4
      }
    });
    map.addLayer({
      id: "points", type: "circle", source: "signals", filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-radius": opts.small ? 5 : 6.5,
        "circle-color": ["match", ["get", "signal_type"], "commercial_land_disposal", COLORS.commercial_land_disposal, COLORS.big_box_industrial_development],
        "circle-stroke-width": ["match", ["get", "geocode_precision"], "address", 2.5, "street", 2.5, 1.5],
        "circle-stroke-color": "#ffffff"
      }
    });
  }

  // Basemap labels follow the UI language (OpenMapTiles carries name:de / name:en).
  function localizeLabels(map) {
    var lang = root.lang === "en" ? "en" : "de";
    var expr = ["coalesce", ["get", "name:" + lang], ["get", "name_" + lang], ["get", "name"]];
    (map.getStyle().layers || []).forEach(function (layer) {
      if (layer.type !== "symbol" || layer.id === "cluster-count") return;
      var field = map.getLayoutProperty(layer.id, "text-field");
      if (field && JSON.stringify(field).indexOf("name") !== -1) {
        map.setLayoutProperty(layer.id, "text-field", expr);
      }
    });
  }

  function makeMap(el, opts) {
    var map = new maplibregl.Map({
      container: el,
      style: styleUrl(el),
      bounds: GERMANY,
      fitBoundsOptions: { padding: 24 },
      attributionControl: { compact: true },
      cooperativeGestures: !!opts.cooperative,
      dragRotate: false,
      pitchWithRotate: false
    });
    map.touchZoomRotate.disableRotation();
    map.on("style.load", function () { localizeLabels(map); });
    // the basemap references a few POI icons its sprite does not ship; register blanks instead of warning
    map.on("styleimagemissing", function (e) {
      if (!map.hasImage(e.id)) map.addImage(e.id, { width: 1, height: 1, data: new Uint8Array(4) });
    });
    if (!opts.small) map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    document.addEventListener("iris:theme", function () {
      map.setStyle(styleUrl(el));
    });
    return map;
  }

  // ---------------------------------------------------------------------------------------------
  // Overview & full map
  // ---------------------------------------------------------------------------------------------
  function initSignalsMap(el) {
    var t = i18n(el);
    var lang = root.lang;
    var full = el.dataset.mode === "full";
    var map = makeMap(el, { cooperative: !full, small: !full });
    var state = { data: null, types: { commercial_land_disposal: true, big_box_industrial_development: true }, year: "" };
    var popup = new maplibregl.Popup({ closeButton: true, maxWidth: "320px", offset: 12 });

    function filtered() {
      if (!state.data) return { type: "FeatureCollection", features: [] };
      return {
        type: "FeatureCollection",
        features: state.data.features.filter(function (f) {
          var p = f.properties;
          if (!state.types[p.signal_type]) return false;
          if (state.year && (!p.published_at || p.published_at.slice(0, 4) !== state.year)) return false;
          return true;
        })
      };
    }

    function refresh() {
      var src = map.getSource("signals");
      if (src) src.setData(filtered());
      renderList();
    }

    function renderList() {
      var list = document.querySelector("[data-map-list]");
      var counter = document.querySelector("[data-map-count]");
      if (!list || !state.data) return;
      var bounds = map.getBounds();
      var visible = filtered().features.filter(function (f) { return bounds.contains(f.geometry.coordinates); });
      if (counter) counter.textContent = visible.length;
      list.replaceChildren();
      visible.slice(0, 80).forEach(function (f) {
        var p = f.properties;
        var item = h("button", "group flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition hover:bg-slate-100 dark:hover:bg-white/5");
        item.type = "button";
        var dot = h("span", "mt-1.5 size-2.5 shrink-0 rounded-full");
        dot.style.background = COLORS[p.signal_type];
        item.appendChild(dot);
        var body = h("span", "min-w-0");
        body.appendChild(h("span", "block truncate text-sm font-medium text-slate-900 group-hover:underline dark:text-slate-100", p.title));
        var sub = [p.municipality, p.published_at ? new Date(p.published_at).toLocaleDateString(lang === "en" ? "en-GB" : "de-DE") : null, fmtArea(p.area_sqm || p.plot_area_sqm, lang)].filter(Boolean).join(" · ");
        body.appendChild(h("span", "block truncate text-xs text-slate-500 dark:text-slate-400", sub));
        item.appendChild(body);
        item.addEventListener("click", function () {
          map.flyTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), 11), speed: 1.4 });
          popup.setLngLat(f.geometry.coordinates).setDOMContent(popupNode(p, t, lang)).addTo(map);
        });
        list.appendChild(item);
      });
    }

    function wire() {
      addLayers(map, filtered(), { cluster: true, small: !full });
    }

    map.on("style.load", function () { if (state.data) wire(); });
    map.on("load", function () {
      fetch(el.dataset.src, { headers: { Accept: "application/geo+json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          state.data = data;
          wire();
          renderList();
        })
        .catch(function (err) {
          var note = h("div", "absolute inset-x-4 top-4 rounded-xl bg-rose-50 p-3 text-sm text-rose-700", "Kartendaten konnten nicht geladen werden (" + err.message + ")");
          el.appendChild(note);
        });
    });
    map.on("moveend", renderList);
    map.on("click", "clusters", function (e) {
      var f = map.queryRenderedFeatures(e.point, { layers: ["clusters"] })[0];
      map.getSource("signals").getClusterExpansionZoom(f.properties.cluster_id).then(function (zoom) {
        map.easeTo({ center: f.geometry.coordinates, zoom: zoom + 0.5 });
      });
    });
    map.on("click", "points", function (e) {
      var f = e.features[0];
      popup.setLngLat(f.geometry.coordinates).setDOMContent(popupNode(f.properties, t, lang)).addTo(map);
    });
    ["clusters", "points"].forEach(function (layer) {
      map.on("mouseenter", layer, function () { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layer, function () { map.getCanvas().style.cursor = ""; });
    });

    document.querySelectorAll("[data-map-type]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.dataset.mapType;
        state.types[key] = !state.types[key];
        btn.setAttribute("aria-pressed", state.types[key] ? "true" : "false");
        refresh();
      });
    });
    var year = document.querySelector("[data-map-year]");
    if (year) year.addEventListener("change", function () { state.year = year.value; refresh(); });
    var reset = document.querySelector("[data-map-reset]");
    if (reset) reset.addEventListener("click", function () { map.fitBounds(GERMANY, { padding: 24 }); });
  }

  // ---------------------------------------------------------------------------------------------
  // Detail mini-map
  // ---------------------------------------------------------------------------------------------
  function initPointMap(el) {
    var lon = parseFloat(el.dataset.lon), lat = parseFloat(el.dataset.lat);
    if (!isFinite(lon) || !isFinite(lat)) return;
    var map = makeMap(el, { cooperative: true, small: true });
    var zoom = { address: 15, street: 14, district: 12.5, municipality: 11 }[el.dataset.precision] || 11;
    var feature = {
      type: "FeatureCollection",
      features: [{ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: { signal_type: el.dataset.type, geocode_precision: el.dataset.precision } }]
    };
    function wire() { addLayers(map, feature, { cluster: false }); }
    map.on("load", function () { map.jumpTo({ center: [lon, lat], zoom: zoom }); wire(); });
    map.on("style.load", wire);
  }

  document.querySelectorAll("[data-signals-map]").forEach(initSignalsMap);
  document.querySelectorAll("[data-point-map]").forEach(initPointMap);
})();
