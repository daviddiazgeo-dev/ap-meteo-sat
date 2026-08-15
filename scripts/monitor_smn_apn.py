#!/usr/bin/env python3
"""
Sistema de Monitoreo Automático de Alertas SMN para Áreas Protegidas APN
==========================================================================
APN · Coordinación de Gestión Integral del Riesgo

Corre dentro de GitHub Actions (server-side, sin restricción CORS/navegador).
En cada ejecución:

  1. Resuelve la URL vigente del canal principal de alertas (robusto ante
     el cambio de año en el nombre del archivo).
  2. Lee los 3 canales CAP del SMN (Alertas y Advertencias, Temperaturas
     Extremas, Aviso a Muy Corto Plazo).
  3. Descarga y parsea cada XML CAP individual.
  4. Carga las Áreas Protegidas desde el WFS oficial de APN, con
     respaldo automático al shapefile local si el WFS falla.
  5. Cruza cada alerta vigente (Moderate/Severe/Extreme) con las áreas
     protegidas mediante intersección geométrica real.
  6. Genera alertas.geojson, areas_afectadas.json y resumen.json en docs/data/
     (servidos luego por GitHub Pages).
  7. Envía un mail por cada combinación nueva (identifier + área) no
     notificada previamente, evitando duplicados vía historial.json.
  8. Si los 3 canales SMN fallan, publica diagnóstico, avisa por mail
     (una vez por outage) y termina con código 2 para que Actions quede en rojo.

Uso:
    python scripts/monitor_smn_apn.py --config config.json
    python scripts/monitor_smn_apn.py --write-config-from-env --config config.json
    python scripts/monitor_smn_apn.py --self-test
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import geopandas as gpd
    from shapely.geometry import Polygon, mapping
    from shapely.ops import unary_union
    from shapely.validation import make_valid
except ImportError:
    print("Faltan dependencias. Corré: pip install -r requirements.txt")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor_smn_apn")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Referer": "https://www.smn.gob.ar/",
}

CAP_NS_CANDIDATES = [
    "{urn:oasis:names:tc:emergency:cap:1.2}",
    "{urn:oasis:names:tc:emergency:cap:1.1}",
    "",
]

SEVERIDAD_COLOR = {
    "extreme": "rojo",
    "severe": "naranja",
    "moderate": "amarillo",
}
SEVERIDADES_PROCESADAS = set(SEVERIDAD_COLOR.keys())

CANAL_TIPO = {
    "Alertas y Advertencias": "Alerta meteorológica",
    "Temperaturas Extremas": "Temperatura extrema",
    "Aviso a Muy Corto Plazo": "Aviso meteorológico a muy corto plazo",
}

WFS_AREAS_URL = (
    "https://mapas.apn.gob.ar/geoserver/ows?service=WFS&version=1.0.0"
    "&request=GetFeature&typename=geonode%3Aapn_limite&outputFormat=json"
    "&srs=EPSG:4326&srsName=EPSG:4326"
)

FEED_HOSTS = [
    "https://ssl.smn.gob.ar",
    "https://ws2.smn.gob.ar",
]

WATCHDOG_CLAVE = "SISTEMA_CIEGO"
EXIT_SISTEMA_CIEGO = 2


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _build_session()


def _looks_like_challenge(status_code: int, text: str) -> bool:
    if status_code in (403, 429, 503):
        snippet = (text or "")[:4000].lower()
        markers = (
            "just a moment",
            "cf-browser-verification",
            "challenge-platform",
            "enable javascript and cookies",
            "attention required",
            "cloudflare",
            "performing security verification",
        )
        if status_code == 403 or any(m in snippet for m in markers):
            return True
    snippet = (text or "")[:4000].lower()
    return "performing security verification" in snippet or "cf-browser-verification" in snippet


def fetch_url(url: str, timeout: int = 25) -> str:
    resp = SESSION.get(url, timeout=timeout)
    text = resp.text or ""
    if _looks_like_challenge(resp.status_code, text):
        raise requests.HTTPError(
            f"Bloqueo antibot (HTTP {resp.status_code}) al pedir {url}",
            response=resp,
        )
    resp.raise_for_status()
    if "<" not in text[:400]:
        raise ValueError(f"La respuesta de {url} no parece XML/HTML.")
    return text


def fetch_first_ok(urls: list) -> tuple:
    last_exc = None
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            return url, fetch_url(url)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("No se pudo leer %s: %s", url, exc)
    if last_exc is None:
        raise RuntimeError("No hay URLs para intentar.")
    raise last_exc


def _path_on_hosts(path: str) -> list:
    path = path if path.startswith("/") else "/" + path
    return [f"{host}{path}" for host in FEED_HOSTS]


# ---------------------------------------------------------------------------
# Resolución robusta de la URL del canal principal
# ---------------------------------------------------------------------------

def discover_alert_feed_url() -> str:
    """
    Devuelve la URL vigente de 'rss_alertaCAP_nuevo_{AÑO}.xml' sin asumir
    un año fijo. Prueba hosts alternativos y, si hace falta, busca el nombre
    real del archivo en la página índice del SMN.
    """
    current_year = datetime.now(timezone.utc).year
    candidates = []
    for year in (current_year, current_year - 1, current_year + 1):
        candidates.extend(_path_on_hosts(f"/feeds/CAP/rss_alertaCAP_nuevo_{year}.xml"))

    try:
        url, _text = fetch_first_ok(candidates)
        log.info("Feed principal resuelto: %s", url)
        return url
    except Exception as exc:  # noqa: BLE001
        log.warning("No se confirmó el feed por año directo: %s", exc)

    index_urls = _path_on_hosts("/CAP/AR.php") + [
        "https://ws2.smn.gob.ar/rss",
        "https://www.smn.gob.ar/rss",
    ]
    try:
        _url, html = fetch_first_ok(index_urls)
        match = re.search(r"rss_alertaCAP_nuevo_\d{4}\.xml", html)
        if match:
            found = match.group(0)
            url, _text = fetch_first_ok(_path_on_hosts(f"/feeds/CAP/{found}"))
            log.info("Feed principal resuelto por descubrimiento en índice: %s", url)
            return url
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo descubrir el feed en la página índice: %s", exc)

    fallback = _path_on_hosts(f"/feeds/CAP/rss_alertaCAP_nuevo_{current_year}.xml")[0]
    log.warning("No se pudo confirmar el feed principal vigente; se usará: %s", fallback)
    return fallback


FEEDS = [
    {"nombre": "Alertas y Advertencias", "resolver": discover_alert_feed_url},
    {
        "nombre": "Temperaturas Extremas",
        "urls": _path_on_hosts("/feeds/CAP/oladecalor/rss_ola_calor_nuevo.xml"),
    },
    {
        "nombre": "Aviso a Muy Corto Plazo",
        "urls": _path_on_hosts("/feeds/CAP/avisocortoplazo/rss_acpCAP.xml"),
    },
]


# ---------------------------------------------------------------------------
# Utilidades CAP / XML
# ---------------------------------------------------------------------------

def _find(root, tag):
    for ns in CAP_NS_CANDIDATES:
        el = root.find(f".//{ns}{tag}")
        if el is not None:
            return el
    return None


def _findall(root, tag):
    for ns in CAP_NS_CANDIDATES:
        els = root.findall(f".//{ns}{tag}")
        if els:
            return els
    return []


def extract_cap_links(index_xml_text: str) -> list:
    """Extrae URLs a los XML de alertas individuales desde el índice RSS/Atom."""
    links = set()
    try:
        root = ET.fromstring(index_xml_text)
    except ET.ParseError:
        return []

    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "link":
            href = el.get("href") or (el.text or "")
            if href.strip().lower().endswith(".xml"):
                links.add(href.strip())
        if tag == "guid":
            href = (el.text or "").strip()
            if href.lower().endswith(".xml"):
                links.add(href)
    return list(links)


def parse_cap_alert(xml_text: str) -> dict:
    """Parsea un XML CAP individual. Devuelve None si no es una alerta válida."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def text(tag):
        el = _find(root, tag)
        return el.text.strip() if el is not None and el.text else ""

    event = text("event")
    if not event:
        return None

    onset = text("onset") or text("effective")

    alert = {
        "identifier": text("identifier"),
        "sent": text("sent"),
        "event": event,
        "headline": text("headline"),
        "severity": text("severity"),
        "urgency": text("urgency"),
        "certainty": text("certainty"),
        "onset": onset,
        "expires": text("expires"),
        "description": text("description"),
        "instruction": text("instruction"),
        "area_desc": text("areaDesc"),
        "polygons": [],
        "circles": [],
    }

    for poly_el in _findall(root, "polygon"):
        raw = (poly_el.text or "").strip()
        if not raw:
            continue
        coords = []
        for pair in raw.split():
            try:
                lat_s, lon_s = pair.split(",")
                coords.append((float(lon_s), float(lat_s)))
            except ValueError:
                continue
        if len(coords) >= 3:
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            alert["polygons"].append(coords)

    for circ_el in _findall(root, "circle"):
        raw = (circ_el.text or "").strip()
        if not raw:
            continue
        try:
            center, radius_km = raw.split(" ")
            lat_s, lon_s = center.split(",")
            alert["circles"].append(
                {"lat": float(lat_s), "lon": float(lon_s), "radius_km": float(radius_km)}
            )
        except ValueError:
            continue

    return alert


def circle_to_polygon(lat: float, lon: float, radius_km: float, points: int = 32) -> Polygon:
    import math

    coords = []
    earth_radius = 6371.0
    for i in range(points + 1):
        angle = 2 * math.pi * i / points
        dx = (radius_km / earth_radius) * math.cos(angle)
        dy = (radius_km / earth_radius) * math.sin(angle)
        new_lat = lat + (dy * 180 / math.pi)
        new_lon = lon + (dx * 180 / math.pi) / math.cos(math.radians(lat))
        coords.append((new_lon, new_lat))
    return Polygon(coords)


def fetch_alerts_from_feed(feed_def: dict) -> tuple:
    """
    Descarga y parsea todas las alertas de un canal. No lanza excepción
    hacia afuera: si el canal falla, devuelve lista vacía y lo loguea.
    """
    nombre = feed_def["nombre"]
    stats = {
        "canal": nombre,
        "url_usada": None,
        "rss_accedido": False,
        "links_cap_encontrados": 0,
        "xml_cap_descargados_ok": 0,
        "xml_cap_descargados_fallidos": 0,
        "error": None,
    }

    try:
        if "resolver" in feed_def:
            url = feed_def["resolver"]()
            index_xml = fetch_url(url)
        else:
            url, index_xml = fetch_first_ok(feed_def["urls"])
        stats["url_usada"] = url
        stats["rss_accedido"] = True
    except Exception as exc:  # noqa: BLE001
        stats["error"] = str(exc)
        log.error("Canal '%s' caído: %s", nombre, exc)
        return [], stats

    links = extract_cap_links(index_xml)
    stats["links_cap_encontrados"] = len(links)
    alerts = []

    if not links:
        a = parse_cap_alert(index_xml)
        if a:
            a["canal"] = nombre
            alerts.append(a)
            stats["xml_cap_descargados_ok"] = 1
        return alerts, stats

    for link in links:
        try:
            xml_text = fetch_url(link)
            a = parse_cap_alert(xml_text)
            if a:
                a["canal"] = nombre
                a["source_url"] = link
                alerts.append(a)
                stats["xml_cap_descargados_ok"] += 1
            else:
                stats["xml_cap_descargados_fallidos"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["xml_cap_descargados_fallidos"] += 1
            log.warning("No se pudo procesar %s: %s", link, exc)

    log.info(
        "Canal '%s': %d/%d XML CAP descargados correctamente (%d link(s) encontrados en el RSS)",
        nombre, stats["xml_cap_descargados_ok"], len(links), len(links),
    )
    return alerts, stats


def fetch_all_alerts() -> tuple:
    all_alerts = []
    canales_stats = []
    for feed_def in FEEDS:
        alerts, stats = fetch_alerts_from_feed(feed_def)
        all_alerts.extend(alerts)
        canales_stats.append(stats)
    return all_alerts, canales_stats


# ---------------------------------------------------------------------------
# Áreas protegidas
# ---------------------------------------------------------------------------

def _read_shapefile(path: str):
    try:
        return gpd.read_file(path, encoding="ISO-8859-1")
    except TypeError:
        return gpd.read_file(path)


def load_apn_areas(fallback_shapefile: str) -> tuple:
    """
    Intenta el WFS oficial de APN. Si falla, usa el shapefile local
    (ISO-8859-1) para que una caída puntual del GeoServer no deje al
    sistema sin geometrías.
    """
    try:
        resp = SESSION.get(WFS_AREAS_URL, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("features"):
            raise ValueError("El WFS respondió sin features.")
        gdf = gpd.GeoDataFrame.from_features(data["features"], crs="EPSG:4326")
        log.info("Áreas protegidas cargadas desde el WFS oficial de APN (%d áreas).", len(gdf))
        return gdf, "wfs"
    except Exception as exc:  # noqa: BLE001
        log.warning("El WFS de APN falló (%s). Usando shapefile local de respaldo.", exc)

    gdf = _read_shapefile(fallback_shapefile)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    log.info("Áreas protegidas cargadas desde el respaldo local (%d áreas).", len(gdf))
    return gdf, "shapefile_local"


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if value != value:
            return True
    except Exception:  # noqa: BLE001
        pass
    text = str(value).strip()
    return text == "" or text.lower() == "nan" or text.lower() == "none"


def area_field(row, *candidates, default="s/d"):
    for c in candidates:
        if c in row and not _is_blank(row[c]):
            return str(row[c]).strip()
    return default


def _as_valid(geom):
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        try:
            geom = make_valid(geom)
        except Exception:  # noqa: BLE001
            return None
    if geom is None or geom.is_empty:
        return None
    return geom


# ---------------------------------------------------------------------------
# Geometría e intersección
# ---------------------------------------------------------------------------

def alert_geometry(alert: dict):
    geoms = []
    for coords in alert["polygons"]:
        try:
            geom = _as_valid(Polygon(coords))
            if geom is not None:
                geoms.append(geom)
        except Exception:  # noqa: BLE001
            continue
    for circ in alert["circles"]:
        try:
            geom = _as_valid(circle_to_polygon(circ["lat"], circ["lon"], circ["radius_km"]))
            if geom is not None:
                geoms.append(geom)
        except Exception:  # noqa: BLE001
            continue
    if not geoms:
        return None
    return _as_valid(unary_union(geoms))


def is_vigente(alert: dict, now: datetime) -> bool:
    if not alert.get("expires"):
        return True
    try:
        exp = datetime.fromisoformat(alert["expires"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > now
    except ValueError:
        return True


def find_affected_areas(geom, areas_gdf) -> list:
    if geom is None:
        return []
    affected = []
    for _, row in areas_gdf.iterrows():
        try:
            other = _as_valid(row.geometry)
            if other is None:
                continue
            if geom.intersects(other):
                affected.append(
                    {
                        "nombre": area_field(row, "nombre", "NOMBRE", "name"),
                        "categoria": area_field(row, "cat_gral", "categoria", "CATEGORIA"),
                        "provincia": area_field(row, "provincia", "PROVINCIA"),
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    return affected


# ---------------------------------------------------------------------------
# Historial y correo
# ---------------------------------------------------------------------------

def load_historial(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"enviados": []}
    return {"enviados": []}


def save_historial(path: Path, historial: dict) -> None:
    path.write_text(json.dumps(historial, ensure_ascii=False, indent=2), encoding="utf-8")


def clave_envio(identifier: str, area_nombre: str) -> str:
    return f"{identifier}::{area_nombre}"


def smtp_configured(config: dict) -> bool:
    smtp = config.get("smtp") or {}
    return bool(
        config.get("recipients")
        and smtp.get("host")
        and smtp.get("username")
        and smtp.get("password")
    )


def send_email(config: dict, subject: str, body: str) -> None:
    smtp_cfg = config["smtp"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from_address"]
    msg["To"] = ", ".join(config["recipients"])
    port = int(smtp_cfg.get("port") or 587)

    with smtplib.SMTP(smtp_cfg["host"], port, timeout=30) as server:
        server.starttls()
        server.login(smtp_cfg["username"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["from_address"], config["recipients"], msg.as_string())


def build_email(alert: dict, area: dict, visor_url: str) -> tuple:
    color = SEVERIDAD_COLOR.get((alert.get("severity") or "").lower(), "s/d")
    tipo = CANAL_TIPO.get(alert.get("canal", ""), alert.get("canal", "s/d"))

    subject = f"[ALERTA SMN] Área Protegida afectada — {area['nombre']}"
    body = "\n".join([
        "Área Protegida:",
        f"{area['nombre']} ({area['categoria']}, {area['provincia']})",
        "",
        "Origen:",
        f"{tipo}",
        "",
        "Fenómeno:",
        f"{alert['event']}",
        "",
        "Nivel de alerta:",
        f"{color.capitalize()} ({alert['severity']})",
        "",
        "Inicio:",
        f"{alert.get('onset') or 's/d'}",
        "",
        "Fin:",
        f"{alert.get('expires') or 's/d'}",
        "",
        "Descripción:",
        f"{alert.get('description') or '(sin descripción)'}",
        "",
        "Visor web:",
        f"{visor_url}",
        "",
        "Fuente: https://www.smn.gob.ar",
        "",
        "---",
        "Este es un índice de apoyo automatizado. No reemplaza la matriz institucional",
        "Probabilidad x Consecuencia de APN ni el juicio del personal de Gestión Integral de Riesgo.",
    ])
    return subject, body


def build_watchdog_email(canales_stats: list, visor_url: str) -> tuple:
    lineas = [
        "El monitor SMN-APN no pudo leer NINGUNO de los 3 canales del SMN.",
        "Hasta que esto se recupere, el visor puede mostrar 0 alertas aunque haya amenazas reales.",
        "",
        "Detalle por canal:",
    ]
    for stats in canales_stats:
        lineas.append(
            f"- {stats.get('canal')}: rss_accedido={stats.get('rss_accedido')} "
            f"url={stats.get('url_usada') or 's/d'} error={stats.get('error') or 's/d'}"
        )
    lineas.extend([
        "",
        "Causa más frecuente: Cloudflare/antibot de ssl.smn.gob.ar bloqueando GitHub Actions.",
        f"Diagnóstico publicado: {visor_url.rstrip('/')}/data/diagnostico.json" if visor_url else "",
        "",
        "Este aviso se envía una vez por cada período de corte, no cada hora.",
    ])
    return "[ALERTA SISTEMA] Monitor SMN-APN sin acceso a los feeds", "\n".join(lineas)


# ---------------------------------------------------------------------------
# Productos de salida
# ---------------------------------------------------------------------------

def _geom_to_geojson(geom):
    return mapping(geom)


def build_outputs(alertas_procesadas: list, areas_source: str, output_dir: Path) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()

    features = []
    areas_afectadas_rows = []
    contador_color = {"rojo": 0, "naranja": 0, "amarillo": 0}
    areas_unicas = set()

    for item in alertas_procesadas:
        alert = item["alert"]
        geom = item["geom"]
        affected = item["affected"]
        vigente = item["vigente"]
        color = SEVERIDAD_COLOR.get((alert.get("severity") or "").lower(), "s/d")

        if vigente and color in contador_color:
            contador_color[color] += 1

        if geom is not None:
            features.append({
                "type": "Feature",
                "geometry": _geom_to_geojson(geom),
                "properties": {
                    "identifier": alert["identifier"],
                    "origen": alert.get("canal", ""),
                    "tipo": CANAL_TIPO.get(alert.get("canal", ""), ""),
                    "fenomeno": alert["event"],
                    "headline": alert.get("headline", ""),
                    "severidad": alert["severity"],
                    "color": color,
                    "urgency": alert.get("urgency", ""),
                    "certainty": alert.get("certainty", ""),
                    "onset": alert.get("onset", ""),
                    "expires": alert.get("expires", ""),
                    "description": alert.get("description", ""),
                    "instruction": alert.get("instruction", ""),
                    "vigente": vigente,
                    "areas_afectadas": [a["nombre"] for a in affected],
                },
            })

        if not vigente:
            continue
        for area in affected:
            areas_unicas.add(area["nombre"])
            areas_afectadas_rows.append({
                "area_protegida": area["nombre"],
                "categoria": area["categoria"],
                "provincia": area["provincia"],
                "fenomeno": alert["event"],
                "severidad": alert["severity"],
                "color": color,
                "inicio": alert.get("onset", ""),
                "fin": alert.get("expires", ""),
                "identifier": alert["identifier"],
                "origen": alert.get("canal", ""),
            })

    alertas_geojson = {
        "type": "FeatureCollection",
        "generated_at": now_iso,
        "areas_source": areas_source,
        "features": features,
    }

    total_vigentes = sum(1 for it in alertas_procesadas if it["vigente"])

    resumen = {
        "generated_at": now_iso,
        "total_alertas_vigentes": total_vigentes,
        "total_areas_afectadas": len(areas_unicas),
        "total_amarillas": contador_color["amarillo"],
        "total_naranjas": contador_color["naranja"],
        "total_rojas": contador_color["rojo"],
        "areas_source": areas_source,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "alertas.geojson").write_text(
        json.dumps(alertas_geojson, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "areas_afectadas.json").write_text(
        json.dumps(areas_afectadas_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "resumen.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log.info(
        "Productos generados: %d alerta(s) vigente(s), %d área(s) afectada(s) "
        "(amarillo=%d naranja=%d rojo=%d)",
        total_vigentes, len(areas_unicas),
        contador_color["amarillo"], contador_color["naranja"], contador_color["rojo"],
    )
    return resumen


def write_areas_geojson(areas_gdf, areas_source: str, output_dir: Path) -> None:
    features = []
    for _, row in areas_gdf.iterrows():
        geom = _as_valid(row.geometry)
        if geom is None:
            continue
        try:
            geom_simpl = _as_valid(geom.simplify(0.0015, preserve_topology=True)) or geom
        except Exception:  # noqa: BLE001
            geom_simpl = geom
        features.append({
            "type": "Feature",
            "properties": {
                "nombre": area_field(row, "nombre", "NOMBRE", "name"),
                "categoria": area_field(row, "cat_gral", "categoria", "CATEGORIA"),
                "provincia": area_field(row, "provincia", "PROVINCIA"),
            },
            "geometry": _geom_to_geojson(geom_simpl),
        })

    fc = {
        "type": "FeatureCollection",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "areas_source": areas_source,
        "features": features,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "areas.geojson").write_text(
        json.dumps(fc, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Publicado areas.geojson con %d área(s) (fuente: %s)", len(features), areas_source)


# ---------------------------------------------------------------------------
# Config desde variables de entorno (GitHub Secrets, sin heredoc bash)
# ---------------------------------------------------------------------------

def write_config_from_env(path: str) -> None:
    raw = os.environ.get("APN_RECIPIENTS_JSON", "").strip()
    if not raw:
        recipients = []
    else:
        recipients = json.loads(raw)
        if not isinstance(recipients, list):
            raise ValueError("APN_RECIPIENTS_JSON debe ser un array JSON, p.ej. [\"mail@dominio\"]")

    port_raw = os.environ.get("APN_SMTP_PORT", "587").strip() or "587"
    user = os.environ.get("APN_SMTP_USER", "").strip()
    # Gmail muestra la contraseña de aplicación en grupos de 4; los espacios sobran.
    password = os.environ.get("APN_SMTP_PASS", "").replace(" ", "").strip()
    config = {
        "fallback_shapefile_path": os.environ.get("APN_FALLBACK_SHAPEFILE", "data/SiFAP_APN.shp"),
        "output_dir": os.environ.get("APN_OUTPUT_DIR", "docs/data"),
        "historial_path": os.environ.get("APN_HISTORIAL_PATH", "historial.json"),
        "visor_url": os.environ.get("APN_VISOR_URL", ""),
        "recipients": recipients,
        "smtp": {
            "host": os.environ.get("APN_SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
            "port": int(port_raw),
            "username": user,
            "password": password,
            "from_address": (os.environ.get("APN_SMTP_FROM") or user).strip(),
        },
    }
    Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("config.json escrito en %s (%d destinatario(s)).", path, len(recipients))


# ---------------------------------------------------------------------------
# Autotest de geometría (no necesita red ni SMN)
# ---------------------------------------------------------------------------

SAMPLE_CAP = """<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>SELFTEST-IGUAZU</identifier>
  <sent>2026-08-14T12:00:00-03:00</sent>
  <info>
    <event>Tormenta</event>
    <headline>Autotest Iguazú</headline>
    <severity>Severe</severity>
    <urgency>Expected</urgency>
    <certainty>Likely</certainty>
    <onset>2026-08-14T12:00:00-03:00</onset>
    <expires>2099-01-01T00:00:00+00:00</expires>
    <description>Polígono sintético sobre Iguazú para verificar la intersección.</description>
    <instruction>Ninguna — es un autotest.</instruction>
    <area>
      <areaDesc>Iguazú</areaDesc>
      <polygon>-25.80,-54.60 -25.80,-54.30 -25.50,-54.30 -25.50,-54.60 -25.80,-54.60</polygon>
    </area>
  </info>
</alert>
"""


def run_self_test(shapefile: str = "data/SiFAP_APN.shp") -> int:
    log.info("Autotest: parseo CAP de muestra.")
    alert = parse_cap_alert(SAMPLE_CAP)
    if not alert or alert["identifier"] != "SELFTEST-IGUAZU":
        log.error("Autotest: falló el parseo CAP.")
        return 1
    if not alert["polygons"]:
        log.error("Autotest: no se leyó el polígono.")
        return 1

    geom = alert_geometry(alert)
    if geom is None:
        log.error("Autotest: no se construyó la geometría.")
        return 1

    if not Path(shapefile).exists():
        log.error("Autotest: no está el shapefile %s", shapefile)
        return 1

    gdf = _read_shapefile(shapefile)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    affected = find_affected_areas(geom, gdf)
    nombres = {a["nombre"].lower() for a in affected}
    log.info("Autotest: áreas intersectadas = %s", sorted(nombres))
    if not any("iguaz" in n for n in nombres):
        log.error("Autotest: el polígono de Iguazú no cruzó el área esperada. Encoding o shapefile rotos.")
        return 1

    now = datetime.now(timezone.utc)
    if not is_vigente(alert, now):
        log.error("Autotest: la alerta de muestra debería estar vigente.")
        return 1

    log.info("Autotest OK: parseo CAP + intersección con Iguazú.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(config_path: str) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    historial_path = Path(config.get("historial_path", "historial.json"))
    historial = load_historial(historial_path)
    ya_notificados = {e["clave"] for e in historial.get("enviados", [])}

    fallback_shapefile = config.get("fallback_shapefile_path", "data/SiFAP_APN.shp")
    areas_gdf, areas_source = load_apn_areas(fallback_shapefile)

    now = datetime.now(timezone.utc)
    all_alerts, canales_stats = fetch_all_alerts()
    log.info("Total de alertas descargadas (los 3 canales): %d", len(all_alerts))

    alertas_procesadas = []
    ignoradas_por_severidad = 0
    sin_geometria = 0

    for alert in all_alerts:
        severidad_lower = (alert.get("severity") or "").lower()
        if severidad_lower not in SEVERIDADES_PROCESADAS:
            ignoradas_por_severidad += 1
            continue

        geom = alert_geometry(alert)
        if geom is None:
            sin_geometria += 1
        vigente = is_vigente(alert, now)
        affected = find_affected_areas(geom, areas_gdf)

        alertas_procesadas.append({
            "alert": alert, "geom": geom, "vigente": vigente, "affected": affected,
        })

    if ignoradas_por_severidad:
        log.info(
            "%d alerta(s) ignorada(s) por severidad fuera de Moderate/Severe/Extreme.",
            ignoradas_por_severidad,
        )
    if sin_geometria:
        log.warning(
            "%d alerta(s) sin polígono/círculo válido: se publican sin cruce espacial.",
            sin_geometria,
        )

    output_dir = Path(config.get("output_dir", "docs/data"))
    write_areas_geojson(areas_gdf, areas_source, output_dir)
    build_outputs(alertas_procesadas, areas_source, output_dir)

    sistema_ciego = bool(canales_stats) and all(not s.get("rss_accedido") for s in canales_stats)
    diagnostico = {
        "generated_at": now.isoformat(),
        "sistema_ciego": sistema_ciego,
        "smn_canales": canales_stats,
        "areas_apn": {
            "fuente_usada": areas_source,
            "wfs_url": WFS_AREAS_URL,
            "cantidad_areas_cargadas": int(len(areas_gdf)),
        },
        "alertas_severidad_relevante": len(alertas_procesadas),
        "alertas_ignoradas_por_severidad": ignoradas_por_severidad,
        "alertas_sin_geometria": sin_geometria,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diagnostico.json").write_text(
        json.dumps(diagnostico, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Publicado diagnostico.json (sistema_ciego=%s).", sistema_ciego)

    visor_url = config.get("visor_url", "")
    nuevos_envios = 0
    mail_ok = smtp_configured(config)
    if not mail_ok:
        log.warning("SMTP o destinatarios incompletos: no se enviarán correos en esta corrida.")

    if mail_ok:
        for item in alertas_procesadas:
            if not item["vigente"]:
                continue
            alert = item["alert"]
            for area in item["affected"]:
                key = clave_envio(alert["identifier"], area["nombre"])
                if key in ya_notificados:
                    continue
                subject, body = build_email(alert, area, visor_url or "(visor no configurado)")
                try:
                    send_email(config, subject, body)
                    historial.setdefault("enviados", []).append({
                        "clave": key,
                        "identifier": alert["identifier"],
                        "area": area["nombre"],
                        "fecha_envio": now.isoformat(),
                    })
                    ya_notificados.add(key)
                    nuevos_envios += 1
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "Falló el envío de mail para %s / %s: %s",
                        alert["identifier"], area["nombre"], exc,
                    )

        if sistema_ciego and WATCHDOG_CLAVE not in ya_notificados:
            subject, body = build_watchdog_email(canales_stats, visor_url)
            try:
                send_email(config, subject, body)
                historial.setdefault("enviados", []).append({
                    "clave": WATCHDOG_CLAVE,
                    "identifier": WATCHDOG_CLAVE,
                    "area": "",
                    "fecha_envio": now.isoformat(),
                })
                ya_notificados.add(WATCHDOG_CLAVE)
                log.info("Mail de watchdog enviado: sistema ciego.")
            except Exception as exc:  # noqa: BLE001
                log.error("Falló el mail de watchdog: %s", exc)
        elif not sistema_ciego:
            historial["enviados"] = [
                e for e in historial.get("enviados", []) if e.get("clave") != WATCHDOG_CLAVE
            ]

    save_historial(historial_path, historial)
    log.info("Correos nuevos enviados en esta corrida: %d", nuevos_envios)

    if sistema_ciego:
        log.error(
            "SISTEMA CIEGO: ninguno de los 3 canales SMN respondió. "
            "El visor puede mostrar 0 alertas aunque haya amenazas reales. "
            "Revisá docs/data/diagnostico.json."
        )
        return EXIT_SISTEMA_CIEGO
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor de alertas SMN vs Áreas Protegidas APN")
    parser.add_argument("--config", default="config.json", help="Ruta al archivo config.json")
    parser.add_argument(
        "--write-config-from-env",
        action="store_true",
        help="Escribe config.json desde APN_* (GitHub Secrets) y sale.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Verifica parseo CAP e intersección con el shapefile local, sin red.",
    )
    args = parser.parse_args()

    if args.write_config_from_env:
        write_config_from_env(args.config)
        sys.exit(0)

    if args.self_test:
        shapefile = "data/SiFAP_APN.shp"
        if os.path.exists(args.config):
            try:
                shapefile = json.loads(Path(args.config).read_text(encoding="utf-8")).get(
                    "fallback_shapefile_path", shapefile
                )
            except (OSError, json.JSONDecodeError):
                pass
        sys.exit(run_self_test(shapefile))

    if not os.path.exists(args.config):
        log.error("No se encontró el archivo de configuración: %s", args.config)
        sys.exit(1)

    sys.exit(run(args.config))
