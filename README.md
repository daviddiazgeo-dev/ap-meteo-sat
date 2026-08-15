# Monitor de Amenazas Meteorológicas SMN → Áreas Protegidas APN

**Coordinación de Gestión Integral del Riesgo — Administración de Parques Nacionales**

Sistema gratuito y automático que revisa cada hora las alertas del Servicio Meteorológico Nacional (SMN), detecta si afectan a alguna Área Protegida de APN, publica un visor web público con el resultado y envía un mail cuando corresponde. No requiere ninguna computadora encendida ni intervención manual una vez configurado.

**Puesta en marcha (orden real):**

1. Subir este repo a GitHub (público) y activar Actions + Pages (`main` / `docs`).
2. Cargar los 5 Secrets (Gmail + destinatarios).
3. Disparar **Run workflow** una vez.
4. Abrir `docs/data/diagnostico.json` (o `…/data/diagnostico.json` en Pages).
   - Si `sistema_ciego` es `false` y algún canal tiene `rss_accedido: true`: el sistema ya ve al SMN.
   - Si `sistema_ciego` es `true` (típicamente HTTP 403 / Cloudflare): el visor muestra un banner rojo a propósito. No es “día calmo”. En ese caso hay que pedir a SMN/Sistemas APN una vía de lectura (whitelist) o, como plan B, un runner en red APN.

Un 0 en “alertas vigentes” **solo es creíble** si `sistema_ciego` es `false`.

---

## Índice

1. [Arquitectura](#1-arquitectura)
2. [Estructura de carpetas](#2-estructura-de-carpetas)
3. [Cómo funciona la continuidad ante el cambio de año del RSS](#3-continuidad-ante-el-cambio-de-año)
4. [Despliegue desde cero](#4-despliegue-desde-cero)
5. [Configuración de GitHub Pages](#5-configuración-de-github-pages)
6. [Configuración del envío de correo](#6-configuración-del-envío-de-correo)
7. [Agregar más destinatarios](#7-agregar-más-destinatarios)
8. [Procedimiento de pruebas](#8-procedimiento-de-pruebas)
9. [Procedimiento de mantenimiento](#9-procedimiento-de-mantenimiento)
10. [Verificar continuidad en años futuros](#10-verificar-continuidad-en-años-futuros)
11. [Preguntas frecuentes](#11-preguntas-frecuentes)

---

## 1. Arquitectura

```
GitHub Actions (corre solo, cada hora, sin depender de ninguna PC encendida)
   │
   ├─ 1. Resuelve la URL vigente del canal principal de alertas
   ├─ 2. Lee los 3 canales CAP del SMN
   ├─ 3. Descarga y procesa cada XML CAP individual
   ├─ 4. Carga las Áreas Protegidas (WFS oficial de APN, con
   │      respaldo automático al shapefile local si el WFS falla)
   ├─ 5. Cruza cada alerta vigente con las Áreas Protegidas
   │      (intersección geométrica real, no por texto)
   ├─ 6. Genera areas.geojson, alertas.geojson,
   │      areas_afectadas.json y resumen.json
   ├─ 7. Envía un mail por cada combinación nueva
   │      (identifier + área) no notificada antes
   └─ 8. Publica todo en GitHub Pages (commit automático)
              │
              ▼
     Visor web público (docs/index.html)
     Lee los archivos generados — nunca habla directamente
     con el SMN, por eso no sufre el bloqueo CORS del navegador.
```

**Por qué esta arquitectura y no otra:** ya comprobamos en este proyecto que un navegador no puede pedirle datos directamente al SMN (lo bloquea). La solución es que el trabajo pesado (leer el SMN, procesar XML, cruzar geometrías) lo haga un programa que **no es un navegador** — un script Python corriendo en un servidor de GitHub — y que el visor web sólo lea el resultado ya procesado, publicado como archivos comunes. Ese es el rol de cada pieza:

| Pieza | Rol |
|---|---|
| GitHub Actions | Ejecuta el script Python cada hora, gratis, sin servidor propio. |
| GitHub Pages | Publica el visor y los datos generados en una URL pública. |
| Python (`scripts/monitor_smn_apn.py`) | Todo el trabajo de descarga, parseo, geometría, cruce y mail. |
| HTML/JS (`docs/index.html`) | Sólo lectura y visualización — no hace ningún cálculo geoespacial ni le habla al SMN. |

---

## 2. Estructura de carpetas

```
.
├── .github/workflows/monitor_apn.yml   ← el "reloj" que dispara todo cada hora
├── scripts/monitor_smn_apn.py           ← todo el procesamiento (único archivo de lógica)
├── data/SiFAP_APN.{shp,shx,dbf,prj,cst} ← respaldo local de Áreas Protegidas
├── docs/                                ← esto es lo que publica GitHub Pages
│   ├── index.html                       ← el visor web público
│   └── data/
│       ├── areas.geojson                ← generado automáticamente
│       ├── alertas.geojson              ← generado automáticamente
│       ├── areas_afectadas.json         ← generado automáticamente
│       ├── resumen.json                 ← generado automáticamente
│       └── diagnostico.json             ← evidencia independiente de cada fuente (ver sección 8)
├── config.example.json                  ← plantilla de configuración
├── requirements.txt                     ← dependencias de Python
└── historial.json                       ← registro técnico para no duplicar mails
```

No hay que tocar nada dentro de `docs/data/` a mano — esos 4 archivos los reescribe el workflow en cada corrida.

---

## 3. Continuidad ante el cambio de año

El nombre del canal principal del SMN incluye el año (`rss_alertaCAP_nuevo_2026.xml`). El script **no tiene el año escrito fijo en ningún lado** — lo calcula solo en cada ejecución:

1. Toma el año actual de la fecha del sistema (en 2027 será 2027, sin tocar código).
2. Prueba la URL con ese año. Si responde, la usa.
3. Si no responde, prueba con el año anterior (por si el SMN todavía no renovó el archivo) y con el año siguiente (por si ya lo renovó antes de tiempo).
4. Si ninguna de esas tres funciona, como último recurso busca el nombre real del archivo dentro de la página índice del SMN mediante un patrón de texto (`rss_alertaCAP_nuevo_\d{4}\.xml`), por si el SMN cambia el patrón de nombre por completo.

Esto está implementado en la función `discover_alert_feed_url()` de `scripts/monitor_smn_apn.py`. No requiere ninguna acción humana en el cambio de año — ver también la sección [10](#10-verificar-continuidad-en-años-futuros) para cómo confirmarlo cuando llegue el momento.

---

## 4. Despliegue desde cero

No hace falta saber programar para seguir estos pasos — son configuración, no código.

### Paso 1 — Crear la cuenta y el repositorio

1. Si APN no tiene ya una cuenta de GitHub institucional, crear una en [github.com](https://github.com) (gratis).
2. Crear un repositorio nuevo (botón verde "New"). Puede ser público — no va a contener ninguna contraseña ni dato sensible, sólo código y alertas meteorológicas públicas.
3. Subir todos los archivos de este paquete al repositorio (arrastrando la carpeta completa en la interfaz web de GitHub, o mediante GitHub Desktop si prefieren una app gráfica en vez de la terminal).

### Paso 2 — Cargar las credenciales como Secrets

Ir a **Settings → Secrets and variables → Actions → New repository secret** y crear estos 5 secretos:

| Nombre del secreto | Valor |
|---|---|
| `APN_SMTP_HOST` | `smtp.gmail.com` (o el servidor SMTP institucional de APN) |
| `APN_SMTP_PORT` | `587` |
| `APN_SMTP_USER` | la cuenta de mail que va a enviar los avisos |
| `APN_SMTP_PASS` | la contraseña de aplicación (ver sección 6) |
| `APN_RECIPIENTS_JSON` | `["david.diaz.geo@gmail.com"]` (ver sección 7 para agregar más) |

**Los secrets no se pueden volver a ver una vez guardados** (por seguridad) — sólo reemplazar. Guardalos también en un lugar seguro propio (gestor de contraseñas institucional).

### Paso 3 — Activar GitHub Pages

Ver sección 5 completa.

### Paso 4 — Primera ejecución

1. Ir a la pestaña **Actions** del repositorio.
2. Seleccionar el workflow "Monitor SMN - Áreas Protegidas APN".
3. Tocar **Run workflow** (botón a la derecha) para dispararlo manualmente, sin esperar a que pase una hora.
4. Esperar 1-2 minutos y refrescar.
5. Abrir el log del paso "Ejecutar monitoreo" **y** el archivo `docs/data/diagnostico.json` del commit que acaba de salir.
6. Un tilde verde ✓ con `rss_accedido: true` es el resultado bueno.
7. Una cruz roja ✗ con `sistema_ciego: true` **no es un bug de Python**: significa que GitHub no pudo leer al SMN (Cloudflare/403). El visor y el diagnóstico igual se publican, y se manda un mail de watchdog si SMTP está bien. Ver sección 8.

A partir de acá, el workflow se dispara solo cada hora, para siempre, sin que nadie tenga que hacer nada.

---

## 5. Configuración de GitHub Pages

1. Ir a **Settings → Pages**.
2. En "Source", elegir **Deploy from a branch**.
3. En "Branch", elegir **main** y la carpeta **/docs**.
4. Guardar.
5. GitHub va a mostrar la URL pública (algo como `https://TU_USUARIO.github.io/TU_REPO/`) — puede tardar 1-2 minutos en estar activa la primera vez.

Esa URL es la que se comparte con el equipo: funciona desde cualquier computadora, celular o tablet, sin instalar nada, y no depende de que nadie tenga el archivo local (a diferencia de abrir un `.html` con doble clic).

---

## 6. Configuración del envío de correo

**Con una cuenta de Gmail** (la opción más simple):

1. Activar la verificación en dos pasos en la cuenta de Gmail que va a enviar los avisos (Google lo exige para este paso): [myaccount.google.com/security](https://myaccount.google.com/security).
2. Generar una "contraseña de aplicación" en [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Usar esa clave de 16 caracteres (no la contraseña normal de la cuenta) como `APN_SMTP_PASS`.

**Con el correo institucional de APN** (`@apn.gob.ar`): pedirle al área de Sistemas los datos del servidor SMTP saliente institucional (host y puerto) y si permite autenticación por aplicación — puede requerir un paso de habilitación por parte de ellos.

---

## 7. Agregar más destinatarios

Editar el secreto `APN_RECIPIENTS_JSON` (**Settings → Secrets and variables → Actions**, tocar el secreto existente y "Update") con la lista completa entre corchetes, separada por comas:

```json
["david.diaz.geo@gmail.com", "persona2@apn.gob.ar", "persona3@apn.gob.ar"]
```

No hace falta editar ningún archivo de código ni volver a desplegar nada — el próximo disparo automático (o uno manual desde la pestaña Actions) ya va a usar la lista nueva. Soporta cómodamente los ~5 destinatarios adicionales previstos, y más si hiciera falta.

---

## 8. Procedimiento de pruebas

### Probar sin esperar a que pase una hora
Pestaña **Actions** → seleccionar el workflow → **Run workflow**.

### Ver qué pasó en una corrida

**La forma más directa: abrir `docs/data/diagnostico.json`.** Es un archivo de texto plano (JSON) que el script reescribe en cada corrida con evidencia independiente de cada una de las 3 fuentes — no hace falta interpretar ningún log. Por ejemplo:

```json
{
  "generated_at": "2026-08-15T01:00:51+00:00",
  "smn_canales": [
    {
      "canal": "Alertas y Advertencias",
      "url_usada": "https://ssl.smn.gob.ar/feeds/CAP/rss_alertaCAP_nuevo_2026.xml",
      "rss_accedido": true,
      "links_cap_encontrados": 4,
      "xml_cap_descargados_ok": 4,
      "xml_cap_descargados_fallidos": 0,
      "error": null
    },
    { "canal": "Temperaturas Extremas", "...": "..." },
    { "canal": "Aviso a Muy Corto Plazo", "...": "..." }
  ],
  "areas_apn": {
    "fuente_usada": "wfs",
    "cantidad_areas_cargadas": 52
  },
  "alertas_severidad_relevante": 2,
  "alertas_ignoradas_por_severidad": 1
}
```

Con esto se puede confirmar, fuente por fuente:

- **Los 3 RSS**: el campo `rss_accedido` de cada canal (`true`/`false`) y, si falló, el mensaje exacto en `error`.
- **Los XML CAP individuales**: `links_cap_encontrados` (cuántos encontró el RSS) contra `xml_cap_descargados_ok` / `xml_cap_descargados_fallidos` (cuántos realmente se pudieron bajar y parsear).
- **El WFS de APN**: el campo `areas_apn.fuente_usada`, que vale `"wfs"` si pudo acceder al servicio oficial, o `"shapefile_local"` si tuvo que usar el respaldo.

Se puede ver directamente en el navegador entrando a `https://TU_USUARIO.github.io/TU_REPO/data/diagnostico.json` una vez que Pages esté activo, sin necesidad de entrar a GitHub ni a la pestaña Actions.

**Para más detalle (mensajes en español, más legibles):** entrar a esa ejecución en la pestaña Actions y abrir el paso "Ejecutar monitoreo" — el log narra lo mismo en prosa.

### Errores más comunes y cómo identificarlos en el log

| Mensaje en el log | Qué significa | Qué hacer |
|---|---|---|
| `SISTEMA CIEGO` / `Bloqueo antibot` / HTTP 403 | GitHub no pudo leer ningún canal del SMN (Cloudflare). El visor muestra banner rojo; **no** interpretar 0 alertas como “no hay amenaza”. | Confirmar en `diagnostico.json`. Pedir a SMN/Sistemas una vía de lectura, o un runner en red APN. |
| `El WFS de APN falló (...). Usando shapefile local de respaldo.` | El WFS de APN no respondió; el sistema siguió funcionando igual con la copia local. | Normal de forma ocasional. Si se repite siempre, avisar a quien administra `mapas.apn.gob.ar`. |
| `Canal '...' caído (...)` | Ese canal del SMN no respondió en esta corrida puntual. | Normal de forma ocasional. Si los 3 fallan siempre, es el caso SISTEMA CIEGO. |
| `Falló el envío de mail para ...` | Se detectó una alerta que afecta un área, pero el mail no se pudo mandar. | Revisar `APN_SMTP_USER`/`APN_SMTP_PASS` — es casi siempre una contraseña de aplicación vencida o mal copiada. |
| `SMTP o destinatarios incompletos` | El workflow corrió pero no hay secrets de mail. | Cargar los 5 secrets y volver a disparar. |
| El workflow termina en rojo ✗ y `sistema_ciego` es true | Comportamiento esperado cuando el SMN está bloqueado: se publica el diagnóstico y recién después se marca el job en rojo. | No reintentar en loop. Resolver el acceso al SMN. |

### Confirmar que el visor muestra datos reales
Abrir la URL pública de GitHub Pages después de una corrida exitosa y verificar que el panel de la izquierda muestre una fecha de actualización reciente.

---

## 9. Procedimiento de mantenimiento

Este sistema fue diseñado para no requerir mantenimiento habitual. Igualmente:

- **Revisar la pestaña Actions una vez cada tanto** (por ejemplo, mensualmente) para confirmar que las corridas siguen en verde.
- **La contraseña de aplicación de Gmail puede vencer o revocarse** si se desactiva la verificación en dos pasos de esa cuenta — si el mail deja de salir mientras el resto funciona, es el primer lugar para revisar.
- **No es necesario renovar nada relacionado con GitHub Actions o Pages** — son gratuitos de forma indefinida para este volumen de uso (una corrida por hora, en un repositorio pequeño).
- **Si cambia la persona responsable**, sólo hay que darle acceso de colaborador al repositorio de GitHub — no depende de ninguna PC ni instalación personal.

---

## 10. Verificar continuidad en años futuros

Cuando termine 2026 (o cualquier año siguiente), no hace falta hacer nada de forma preventiva — el sistema está diseñado para resolver el año solo (ver sección 3). Aun así, como buena práctica, alrededor de enero de cada año:

1. Disparar una ejecución manual (Actions → Run workflow).
2. Revisar en el log la línea que dice `Feed principal resuelto por...` — va a mostrar qué URL terminó usando y por qué método la encontró.
3. Si en algún momento aparece la advertencia `No se pudo confirmar el feed principal vigente`, es la señal de que el SMN cambió el patrón de nombres de una forma que el mecanismo automático no anticipó, y ahí sí requeriría un ajuste puntual de una línea de código (el patrón de texto en `discover_alert_feed_url()`).

---

## 11. Preguntas frecuentes

**¿Cuesta algo?** No. GitHub Actions es gratuito sin límite de minutos en repositorios públicos, y GitHub Pages es gratuito siempre. No hay ningún paso de este sistema que requiera una tarjeta de crédito.

**¿Necesito tener la computadora prendida?** No, en ningún momento. Todo corre en los servidores de GitHub.

**¿Qué pasa si nadie abre el visor durante semanas?** El workflow sigue corriendo cada hora igual, y los mails se siguen mandando igual — el visor es sólo una ventana opcional a los mismos datos.

**¿El repositorio público expone algo sensible?** No — el código, las alertas del SMN y los límites de las áreas protegidas son todos datos públicos. Las únicas credenciales (usuario y contraseña de mail) viven exclusivamente como GitHub Secrets, nunca en el código ni en el historial del repositorio.

**¿Un workflow en rojo significa que está todo roto?** No necesariamente. Si `diagnostico.json` dice `sistema_ciego: true`, el código corrió bien y GitHub no pudo leer al SMN. El visor igual se actualiza, con un aviso visible.
