(async function bajarCapsSMN() {
  if (!/smn\.gob\.ar/i.test(location.hostname)) {
    alert("Primero tenés que estar en la página del SMN (la lista de alertas). Abrí ese link y ahí sí tocá el favorito.");
    return;
  }
  var INDEX = /rss_alertaCAP_nuevo|rss_ola_calor_nuevo|rss_acpCAP/i;
  function add(set, raw) {
    if (!raw) return;
    try {
      var url = new URL(raw, location.href).href;
      if (!/\.xml(\?|$)/i.test(url)) return;
      if (!/smn\.gob\.ar/i.test(url)) return;
      set.add(url.split("#")[0]);
    } catch (e) {}
  }
  var found = new Set();
  document.querySelectorAll("[href]").forEach(function (n) {
    add(found, n.href || n.getAttribute("href"));
  });
  var serialized = "";
  try { serialized = new XMLSerializer().serializeToString(document); }
  catch (e) { serialized = (document.documentElement && document.documentElement.innerHTML) || ""; }
  var matches = serialized.match(/https?:\/\/[^\s"'<>]+?\.xml/gi) || [];
  matches.forEach(function (m) { add(found, m); });
  var here = location.href.split("#")[0];
  var urls = [];
  found.forEach(function (u) {
    if (u !== here && !INDEX.test(u)) urls.push(u);
  });
  if (!urls.length) {
    alert("No encontré las alertas en esta pantalla. Asegurate de haber abierto el link del SMN que te pasaron.");
    return;
  }
  var alerts = [];
  var failed = 0;
  for (var i = 0; i < urls.length; i++) {
    try { document.title = "Bajando alertas " + (i + 1) + " de " + urls.length; } catch (e) {}
    try {
      var res = await fetch(urls[i], { credentials: "include" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      var xml = await res.text();
      if (xml.indexOf("<") < 0) throw new Error("vacio");
      alerts.push({ url: urls[i], xml: xml });
    } catch (err) { failed += 1; }
  }
  var blob = new Blob([JSON.stringify({
    generated_at: new Date().toISOString(),
    source: here,
    ok: alerts.length,
    failed: failed,
    alerts: alerts
  })], { type: "application/json" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "smn-alertas.json";
  (document.body || document.documentElement).appendChild(a);
  a.click();
  a.remove();
  alert("Listo. Se descargó el archivo smn-alertas.json (carpeta Descargas).\nAlertas: " + alerts.length + (failed ? (". No se pudieron bajar: " + failed) : ".") + "\nAhora volvé al visor, elegí ese archivo y tocá Procesar.");
})();
