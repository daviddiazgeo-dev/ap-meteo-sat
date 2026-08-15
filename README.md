# Monitor SMN → Áreas Protegidas APN

Visor y cruce espacial de **alertas meteorológicas del Servicio Meteorológico Nacional (SMN)** con las **áreas protegidas de la Administración de Parques Nacionales (APN)**.

Pensado para la Coordinación de Gestión Integral del Riesgo: saber *si* una alerta amarilla, naranja o roja pisa un parque, *cuál* es el fenómeno y *hasta cuándo* rige — y bajar un informe en Word o PDF.

**Visor público:** [daviddiazgeo-dev.github.io/david-sat](https://daviddiazgeo-dev.github.io/david-sat/)  
**Pasos simples (sin programar):** [daviddiazgeo-dev.github.io/david-sat/pasos.html](https://daviddiazgeo-dev.github.io/david-sat/pasos.html)  
**Código:** [github.com/daviddiazgeo-dev/david-sat](https://github.com/daviddiazgeo-dev/david-sat)

---

## Autores

| Autor | Rol |
|---|---|
| **Diego Coria** | Diseño e implementación del software (Python, visor web, GitHub Actions, informe Word/PDF). |
| **David Diaz** | Definición del problema institucional, validación con Áreas Protegidas APN y operación del sistema en Gestión Integral del Riesgo. |

Contacto operativo de alertas: `david.diaz.geo@gmail.com`.

---

## Qué problema resuelve

El SMN publica alertas en formato CAP (polígonos + severidad + vigencia). APN necesita saber si esos polígonos **intersectan** un área protegida, no si el texto menciona una provincia.

Este proyecto:

1. Lee los XML CAP del SMN (alertas y advertencias, temperaturas extremas, avisos a muy corto plazo).
2. Carga los límites de las áreas protegidas (WFS oficial de APN, con shapefile de respaldo).
3. Hace **intersección geométrica real** (no búsqueda por nombre).
4. Muestra en un mapa **solo las coincidencias**, con fenómeno y nivel (amarillo / naranja / rojo).
5. Permite descargar un **informe agrupado por parque** (Word y PDF).
6. Puede enviar mail por Gmail cuando hay un corte del sistema o, si GitHub llega a leer al SMN, cuando aparece una coincidencia nueva.

No reemplaza la matriz institucional Probabilidad × Consecuencia de APN ni el juicio del personal. Es un **índice de apoyo**.

---

## Cómo se usa (para Parques Nacionales)

No hay que instalar nada. Se usa el navegador (Chrome o Edge).

### Una sola vez

1. Abrir el [visor](https://daviddiazgeo-dev.github.io/david-sat/) o la [guía de 4 pasos](https://daviddiazgeo-dev.github.io/david-sat/pasos.html).
2. Si no se ve la barra de favoritos arriba: **Ctrl + Mayús + B**.
3. **Arrastrar** el botón verde **Bajar alertas SMN** hasta esa barra (no hacerle clic en la página: hay que arrastrarlo).

### Cada vez que se quiera revisar (por ejemplo mañana y tarde)

1. Abrir el [RSS de alertas del SMN](https://ssl.smn.gob.ar/feeds/CAP/rss_alertaCAP_nuevo_2026.xml) y esperar a que aparezca la lista.
2. En **esa** pestaña, tocar el favorito **Bajar alertas SMN**. Se descarga `smn-alertas.json` en Descargas. Si el navegador pregunta, **Permitir**.
3. Volver al visor → **Elegir archivo** → `smn-alertas.json` → **Procesar**.
4. El mapa y la lista muestran únicamente parques con alerta. Si hay coincidencias: **Descargar Word** o **Descargar PDF**.

El informe agrupa por área protegida. Si un parque tiene varias alertas, cada una aparece como subítem con fenómeno, nivel y vigencia.

Para cubrir ola de calor y avisos a muy corto plazo, repetir el paso 2 en esos RSS y procesar también esos JSON.

### Qué no hay que hacer

- No interpretar “0 alertas” en el visor **sin haber cargado** el JSON de esa ronda: GitHub no puede bajar el SMN solo (ver más abajo).
- No pegar código en F12. Eso quedó reemplazado por el favorito.

---

## Cómo funciona (para portfolio y para quien mantiene el código)

### Idea de diseño

Un navegador **no puede** pedirle los RSS al SMN desde el visor (CORS / antibot). Un runner de **GitHub Actions tampoco**: `ssl.smn.gob.ar` está detrás de Cloudflare y responde **403**. Eso se comprobó en producción (`docs/data/diagnostico.json`, `sistema_ciego: true`).

Por eso hay **dos capas**:

| Capa | Qué hace | Dónde corre |
|---|---|---|
| **Operativa (hoy)** | La persona abre el RSS en su navegador (Cloudflare la deja pasar). Un favorito baja todos los CAP a un JSON. El visor cruza en el cliente con Turf.js y las geometrías APN. | Navegador |
| **Infraestructura** | Hospeda el visor (GitHub Pages), corre un job horario en Python, intenta el SMN, carga áreas (WFS o shapefile), manda mail de *watchdog* si el SMN está inaccesible, publica `diagnostico.json`. | GitHub Actions + Pages |

Si en el futuro GitHub pudiera leer los feeds, el mismo script Python ya cruza geometrías con GeoPandas/Shapely y puede mandar un mail por cada par `identifier CAP + área` no notificado.

### Stack

- **Python 3.11:** `requests`, `geopandas`, `shapely` — parseo CAP 1.2, polígonos `lat,lon` → GeoJSON, `make_valid`, intersección, SMTP.
- **GitHub Actions:** cron horario + disparo manual; secrets para SMTP; autotest de geometría (Iguazú) sin red; commit de productos; el job termina en rojo a propósito si los 3 canales SMN fallan (no finge un día calmo).
- **GitHub Pages:** visor Leaflet + teselas IGN, carga manual, informe pdfmake / Word HTML.
- **Datos:** WFS `geonode:apn_limite` y respaldo `data/SiFAP_APN.*` (EPSG:4326, encoding ISO-8859-1).

### Flujo de datos

```
RSS CAP SMN (navegador de la persona)
        │  favorito “Bajar alertas SMN”
        ▼
smn-alertas.json  ──►  visor (Turf.js ∩ áreas APN)
                              │
                              ├─ mapa: solo coincidencias
                              ├─ lista agrupada por parque
                              └─ Word / PDF

GitHub Actions (cada hora, sin PC)
        │  intenta los 3 RSS
        ▼
   403 Cloudflare  ──►  diagnostico.json + mail [ALERTA SISTEMA]
                   ──►  visor muestra banner “sistema ciego”
```

### Severidad

Solo se procesan alertas CAP **Moderate / Severe / Extreme** (amarillo / naranja / rojo). El resto se ignora.

### Año del RSS

El feed principal se llama `rss_alertaCAP_nuevo_AAAA.xml`. El script Python prueba año actual, anterior y siguiente, y si hace falta busca el nombre en la página índice. No hay un año hardcodeado.

---

## Estructura del repositorio

```
.
├── .github/workflows/monitor_apn.yml   Reloj horario + secrets → config.json
├── scripts/monitor_smn_apn.py          Monitor Python (CAP, GIS, mail, autotest)
├── data/SiFAP_APN.*                    Respaldo de áreas protegidas
├── docs/                               GitHub Pages
│   ├── index.html                      Visor
│   ├── pasos.html                      Manual de 4 pasos
│   ├── bajar-caps-smn.js               Script del favorito
│   └── data/                           GeoJSON / JSON publicados
├── config.example.json                 Plantilla (nunca commitear config.json)
├── requirements.txt
└── historial.json                      Deduplicación de mails
```

---

## Despliegue (ya hecho en este repo)

Repo: `daviddiazgeo-dev/david-sat`  
Pages: branch `main`, carpeta `/docs`.

### Secrets de Actions

| Nombre | Ejemplo |
|---|---|
| `APN_SMTP_HOST` | `smtp.gmail.com` |
| `APN_SMTP_PORT` | `587` |
| `APN_SMTP_USER` | cuenta Gmail que **envía** |
| `APN_SMTP_PASS` | contraseña de **aplicación** (16 letras), no la de Gmail |
| `APN_RECIPIENTS_JSON` | `["david.diaz.geo@gmail.com"]` |

Gmail exige verificación en 2 pasos para crear la contraseña de aplicación: [app passwords](https://myaccount.google.com/apppasswords).

Más destinatarios: editar `APN_RECIPIENTS_JSON` con la lista completa, por ejemplo `["david.diaz.geo@gmail.com","otra@apn.gob.ar"]`.

### Qué hace el workflow cada hora

1. Autotest: polígono sintético sobre Iguazú ∩ shapefile.
2. Arma `config.json` desde secrets (sin heredoc bash).
3. Intenta WFS APN y los 3 RSS SMN.
4. Publica `docs/data/*` e `historial.json`.
5. Si ningún RSS respondió: mail `[ALERTA SISTEMA]…` (una vez por corte) y el job queda en rojo.

Un workflow rojo con `sistema_ciego: true` **no es un crash**: es el diagnóstico correcto. Ver [diagnostico.json](https://daviddiazgeo-dev.github.io/david-sat/data/diagnostico.json).

---

## Limitación conocida (importante)

GitHub Actions corre en IPs de datacenter. Cloudflare del SMN las bloquea. **No hay modo 24×7 gratuito, sin PC prendida y sin pedir whitelist**, que lea los CAP desde Actions.

Por eso la operación real es el **favorito + visor**. El job horario sigue siendo útil: hospeda el sitio, prueba las fuentes y avisa por mail si el corte continúa o si algún día el SMN deja de bloquear.

---

## Desarrollo local

```bash
pip install -r requirements.txt
python scripts/monitor_smn_apn.py --self-test
```

El autotest no necesita red ni SMN. Para una corrida completa hace falta `config.json` (copiar `config.example.json`; está en `.gitignore`).

---

## Licencia de uso institucional

Código y visor pensados para uso interno de apoyo a la decisión en APN. Las alertas son del SMN; los límites de áreas, de APN. Este repositorio no publica contraseñas: solo GitHub Secrets.

---

## Autores (cierre)

**Diego Coria** y **David Diaz**, 2026.
