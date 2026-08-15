/* Pegá TODO este archivo en la consola (F12 → Consola) con el RSS del SMN abierto.
   Baja todos los XML CAP y descarga un solo archivo smn-alertas.json. */
(async function bajarCapsSMN() {
  const INDEX = /rss_alertaCAP_nuevo|rss_ola_calor_nuevo|rss_acpCAP/i;
  const add = (set, raw) => {
    if (!raw) return;
    try {
      const url = new URL(raw, location.href).href;
      if (!/\.xml(\?|$)/i.test(url)) return;
      if (!/smn\.gob\.ar/i.test(url)) return;
      set.add(url.split("#")[0]);
    } catch (e) { /* ignore */ }
  };

  const found = new Set();
  document.querySelectorAll("a[href]").forEach((a) => add(found, a.href));
  document.querySelectorAll("[href]").forEach((a) => add(found, a.getAttribute("href")));
  let serialized = "";
  try { serialized = new XMLSerializer().serializeToString(document); }
  catch (e) { serialized = document.documentElement.innerHTML || ""; }
  for (const m of serialized.matchAll(/https?:\/\/[^\s"'<>]+?\.xml/gi)) add(found, m[0]);

  const here = location.href.split("#")[0];
  const urls = [...found].filter((u) => u !== here && !INDEX.test(u));
  if (!urls.length) {
    alert("No encontré links CAP en esta página. Abrí el RSS del SMN (ssl.smn.gob.ar/feeds/CAP/...) y volvé a pegar el script.");
    return;
  }

  const alerts = [];
  const failed = [];
  const total = urls.length;
  for (let i = 0; i < urls.length; i++) {
    const url = urls[i];
    document.title = "SMN " + (i + 1) + "/" + total;
    try {
      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const xml = await res.text();
      if (!xml.includes("<")) throw new Error("respuesta vacía");
      alerts.push({ url, xml });
    } catch (err) {
      failed.push({ url, error: String(err) });
    }
  }

  const payload = {
    generated_at: new Date().toISOString(),
    source: here,
    ok: alerts.length,
    failed: failed.length,
    alerts,
    errors: failed,
  };
  const blob = new Blob([JSON.stringify(payload)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "smn-alertas.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  document.title = "SMN listo " + alerts.length + "/" + total;
  alert("Listo: " + alerts.length + " CAP descargados, " + failed.length + " fallidos.\nSe bajó smn-alertas.json. Cargalo en el visor APN.");
})();
