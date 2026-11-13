#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TechSeal · Evaluador de canal
=============================

Introduce el nombre, @handle, URL o ID de un canal de YouTube y el programa:
  1. Descarga sus datos públicos con la YouTube Data API v3.
  2. Calcula las métricas núcleo del baremo TechSeal + estadística descriptiva.
  3. Puntúa el canal (0-100) y le asigna un nivel de sello: ORO / PLATA / BRONCE / NO CERTIFICADO.
  4. Genera un informe HTML con el sello, gráficos y tablas, más un JSON y un CSV
     con las métricas (mismos nombres de columna que el pipeline, para Power BI).

Uso
---
    python techseal_sello.py                          # te pide canal y API key
    python techseal_sello.py "@TopesdeGama"
    python techseal_sello.py "https://www.youtube.com/@clipset" --videos 50
    python techseal_sello.py "Topes de Gama" --referencia channels_metrics.csv
    python techseal_sello.py --demo                   # datos sintéticos, para probar sin API

API key: argumento --api-key, variable de entorno YOUTUBE_API_KEY, o se pide por pantalla.

Referencia (opcional pero recomendada): con --referencia <csv de tu muestra> cada métrica
se puntúa por percentil frente a la muestra (metodología del proyecto). Si el CSV tiene una
columna "tier" también se indica a qué tier de la muestra se parece más el canal.
Sin referencia se usan umbrales por defecto (BENCHMARKS_POR_DEFECTO), ajustables abajo.

Solo usa la librería estándar de Python (no hace falta instalar nada).
Coste de cuota aproximado: 3-5 unidades por canal (100 más si hay que buscar por nombre).
"""

from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import math
import os
import random
import re
import statistics as st
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://www.googleapis.com/youtube/v3/"

# ---------------------------------------------------------------------------
# CONFIGURACIÓN DEL BAREMO  (ajústalo aquí sin tocar el resto del código)
# ---------------------------------------------------------------------------

# Métricas núcleo del score de salud. weight = peso en el score final (suman 100).
# higher_better = True si un valor más alto es mejor.
# Umbrales por defecto (sin CSV de referencia): bad -> 0 puntos, good -> 100 puntos,
# interpolación lineal (o logarítmica si log=True) entre ambos.
METRICAS_NUCLEO = {
    "avg_comment_rate": {
        "label": "Tasa de comentarios", "unit": "% de vistas", "weight": 25,
        "higher_better": True, "bad": 0.03, "good": 0.40, "log": True,
        "why": "Comentarios / vistas. Es la interacción más difícil de inflar.",
    },
    "recent_views_per_subscriber": {
        "label": "Vistas recientes por suscriptor", "unit": "mediana vistas / subs", "weight": 20,
        "higher_better": True, "bad": 0.01, "good": 0.20, "log": True,
        "why": "Mediana de vistas de los últimos vídeos / suscriptores. Mide si la audiencia sigue viva "
               "(más honesta que vistas totales/subs en canales con catálogo antiguo).",
    },
    "views_coefficient_variation": {
        "label": "Consistencia de rendimiento", "unit": "CV de vistas", "weight": 15,
        "higher_better": False, "bad": 2.0, "good": 0.5, "log": False,
        "why": "Desviación típica / media de las vistas. Bajo = rendimiento predecible para una marca.",
    },
    "avg_views_per_day": {
        "label": "Vistas por día (por vídeo)", "unit": "vistas/día", "weight": 15,
        "higher_better": True, "bad": 20, "good": 3000, "log": True,
        "why": "Vistas normalizadas por la antigüedad de cada vídeo. Mide alcance real.",
    },
    "avg_days_between_uploads": {
        "label": "Frecuencia de subida", "unit": "días entre vídeos", "weight": 15,
        "higher_better": False, "bad": 30, "good": 3.5, "log": True,
        "why": "Media de días entre publicaciones.",
    },
    "stdev_days_between_uploads": {
        "label": "Regularidad de subida", "unit": "desv. típ. (días)", "weight": 10,
        "higher_better": False, "bad": 25, "good": 2, "log": True,
        "why": "Desviación típica de los días entre publicaciones. Bajo = calendario estable.",
    },
}

# Nombres alternativos con los que la métrica puede aparecer en tu CSV de referencia.
ALIAS_REFERENCIA = {
    # Primero los nombres de techseal_data_pipeline.py v3 (métricas de vídeos largos, prefijo long_)
    "avg_comment_rate": ["long_avg_comment_rate", "avg_comment_rate", "comment_rate"],
    "recent_views_per_subscriber": ["long_views_per_subscriber", "recent_views_per_subscriber"],
    "views_coefficient_variation": ["long_views_cv", "views_coefficient_variation", "views_cv"],
    "avg_views_per_day": ["long_avg_views_per_day", "avg_views_per_day"],
    "avg_days_between_uploads": ["avg_days_between_uploads"],
    "stdev_days_between_uploads": ["stdev_days_between_uploads"],
}
# La columna "tier" del pipeline es por tamaño (pequeño/micro/mid/macro), no por calidad:
# no sirve para decir "a qué tier se parece" por score, así que se desactiva.
ALIAS_TIER = []

NIVELES_SELLO = [  # (puntuación mínima, nivel)
    (80, "ORO"),
    (65, "PLATA"),
    (50, "BRONCE"),
    (0, "NO CERTIFICADO"),
]

# Requisitos mínimos (filtros eliminatorios, previos al score)
DIAS_MAX_SIN_SUBIR = 90       # más días sin publicar -> canal inactivo, no certificable
MIN_VIDEOS_VALIDOS = 8        # mínimo de vídeos para que la estadística tenga sentido
DIAS_MIN_ANTIGUEDAD_VIDEO = 3  # vídeos más nuevos se excluyen de las métricas de rendimiento
MAX_SEGUNDOS_SHORT = 180      # heurística: vídeo <= 3 min y no directo -> se considera Short

PATRON_AFILIADO = re.compile(
    r"(amzn\.to|amazon\.[a-z.]+/[^\s]*tag=|aliexpress\.[a-z]+/|s\.click\.aliexpress|bit\.ly|geni\.us|"
    r"tidd\.ly|go\.skimresources|awin1\.com|pccomponentes\.com/[^\s]*(ref|aff)|ebay\.[a-z.]+/[^\s]*campid)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# YOUTUBE DATA API
# ---------------------------------------------------------------------------

class YouTubeAPIError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.cuota_usada = 0

    def _get(self, endpoint: str, coste: int = 1, **params) -> dict:
        params["key"] = self.api_key
        url = API_BASE + endpoint + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                self.cuota_usada += coste
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))["error"]
                razon = err.get("errors", [{}])[0].get("reason", "")
                msg = err.get("message", str(e))
            except Exception:
                razon, msg = "", str(e)
            if razon == "quotaExceeded":
                msg = "Cuota diaria de la API agotada. Se reinicia a medianoche (hora del Pacífico)."
            elif razon in ("keyInvalid", "badRequest") and "key" in msg.lower():
                msg = "API key no válida. Revisa la clave en Google Cloud Console."
            raise YouTubeAPIError(f"Error de la API ({e.code} {razon}): {msg}") from None
        except urllib.error.URLError as e:
            raise YouTubeAPIError(f"No se pudo conectar con la API de YouTube: {e.reason}") from None

    # --- resolución del canal -------------------------------------------------
    def resolver_canal(self, entrada: str) -> tuple[str, list[dict]]:
        """Devuelve (channel_id, alternativas_si_se_buscó_por_nombre)."""
        entrada = entrada.strip()
        m = re.search(r"youtube\.com/(channel/(UC[\w-]{22})|@([\w.\-]+)|user/([\w.\-]+)|c/([\w.\-]+))", entrada)
        if m:
            if m.group(2):
                return m.group(2), []
            if m.group(3):
                entrada = "@" + m.group(3)
            elif m.group(4):
                r = self._get("channels", part="id", forUsername=m.group(4))
                if r.get("items"):
                    return r["items"][0]["id"], []
                entrada = m.group(4)
            elif m.group(5):
                entrada = m.group(5)

        if re.fullmatch(r"UC[\w-]{22}", entrada):
            return entrada, []

        handle = entrada if entrada.startswith("@") else "@" + entrada.replace(" ", "")
        r = self._get("channels", part="id", forHandle=handle)
        if r.get("items"):
            return r["items"][0]["id"], []

        # Último recurso: búsqueda por nombre (cuesta 100 unidades de cuota)
        r = self._get("search", coste=100, part="snippet", q=entrada.lstrip("@"), type="channel", maxResults=5)
        items = r.get("items", [])
        if not items:
            raise YouTubeAPIError(f"No se encontró ningún canal para «{entrada}».")
        alternativas = [{"id": it["snippet"]["channelId"], "titulo": it["snippet"]["title"]} for it in items]
        return alternativas[0]["id"], alternativas[1:]

    # --- datos ---------------------------------------------------------------
    def datos_canal(self, channel_id: str) -> dict:
        r = self._get("channels", part="snippet,statistics,contentDetails,brandingSettings", id=channel_id)
        if not r.get("items"):
            raise YouTubeAPIError(f"El canal {channel_id} no existe o no es público.")
        return r["items"][0]

    def ultimos_videos(self, uploads_playlist: str, n: int) -> list[dict]:
        ids, token = [], None
        while len(ids) < n:
            params = dict(part="contentDetails", playlistId=uploads_playlist, maxResults=min(50, n - len(ids)))
            if token:
                params["pageToken"] = token
            r = self._get("playlistItems", **params)
            ids += [it["contentDetails"]["videoId"] for it in r.get("items", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        videos = []
        for i in range(0, len(ids), 50):
            r = self._get("videos", part="snippet,statistics,contentDetails,liveStreamingDetails",
                          id=",".join(ids[i:i + 50]))
            videos += r.get("items", [])
        return videos


# ---------------------------------------------------------------------------
# UTILIDADES ESTADÍSTICAS (sin dependencias)
# ---------------------------------------------------------------------------

def parse_fecha(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_duracion(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def percentil(datos: list[float], p: float) -> float:
    """Percentil con interpolación lineal (equivale a numpy/Excel PERCENTIL.INC)."""
    xs = sorted(datos)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def asimetria(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    m, s = st.mean(xs), st.pstdev(xs)
    if s == 0:
        return 0.0
    g1 = sum((x - m) ** 3 for x in xs) / n / s ** 3
    return g1 * math.sqrt(n * (n - 1)) / (n - 2)  # ajustada (como Excel COEFICIENTE.ASIMETRIA)


def curtosis(xs: list[float]) -> float:
    """Exceso de curtosis muestral (como Excel CURTOSIS)."""
    n = len(xs)
    if n < 4:
        return float("nan")
    m, s = st.mean(xs), st.stdev(xs)
    if s == 0:
        return 0.0
    a = n * (n + 1) / ((n - 1) * (n - 2) * (n - 3)) * sum(((x - m) / s) ** 4 for x in xs)
    return a - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))


def gini(xs: list[float]) -> float:
    xs = sorted(x for x in xs if x >= 0)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return float("nan")
    return sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs)) / (n * total)


def rangos(xs: list[float]) -> list[float]:
    orden = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[orden[j + 1]] == xs[orden[i]]:
            j += 1
        for k in range(i, j + 1):
            r[orden[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(x: list[float], y: list[float]) -> float:
    if len(x) < 3 or st.pstdev(x) == 0 or st.pstdev(y) == 0:
        return float("nan")
    mx, my = st.mean(x), st.mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return num / math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(rangos(x), rangos(y))


def p_valor_correlacion(r: float, n: int) -> float:
    """p-valor bilateral aproximado (t de Student con n-2 g.l., aproximación normal para n>30)."""
    if math.isnan(r) or n < 4 or abs(r) >= 1:
        return float("nan")
    t = r * math.sqrt((n - 2) / (1 - r * r))
    # aproximación de la t por una normal con corrección (Abramowitz-Stegun 26.7.8)
    z = t * (1 - 1 / (4 * (n - 2))) / math.sqrt(1 + t * t / (2 * (n - 2)))
    return math.erfc(abs(z) / math.sqrt(2))


def media_o_nan(xs):
    return st.mean(xs) if xs else float("nan")


def es_num(x) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x)


# ---------------------------------------------------------------------------
# CÁLCULO DE MÉTRICAS
# ---------------------------------------------------------------------------

def construir_dataset_videos(videos_api: list[dict], ahora: datetime) -> list[dict]:
    filas = []
    for v in videos_api:
        sn, stt, cd = v.get("snippet", {}), v.get("statistics", {}), v.get("contentDetails", {})
        if sn.get("liveBroadcastContent") in ("live", "upcoming"):
            continue  # directo en curso o programado: aún sin datos útiles
        publicado = parse_fecha(sn["publishedAt"])
        dur = parse_duracion(cd.get("duration", ""))
        es_directo = "liveStreamingDetails" in v and "actualStartTime" in v.get("liveStreamingDetails", {})
        vistas = int(stt.get("viewCount", 0))
        likes = int(stt["likeCount"]) if "likeCount" in stt else None
        coments = int(stt["commentCount"]) if "commentCount" in stt else None
        dias = max((ahora - publicado).total_seconds() / 86400, 1.0)
        desc = sn.get("description", "") or ""
        filas.append({
            "video_id": v["id"],
            "titulo": sn.get("title", ""),
            "publicado": publicado,
            "duracion_s": dur,
            "es_short": (0 < dur <= MAX_SEGUNDOS_SHORT) and not es_directo,
            "es_directo": es_directo,
            "vistas": vistas,
            "likes": likes,
            "comentarios": coments,
            "dias_publicado": dias,
            "vistas_dia": vistas / dias,
            "like_rate": (likes / vistas * 100) if (likes is not None and vistas) else None,
            "comment_rate": (coments / vistas * 100) if (coments is not None and vistas) else None,
            "n_tags": len(sn.get("tags", []) or []),
            "len_titulo": len(sn.get("title", "")),
            "len_descripcion": len(desc),
            "afiliado": bool(PATRON_AFILIADO.search(desc)),
        })
    filas.sort(key=lambda f: f["publicado"])
    return filas


def calcular_metricas(canal: dict, videos: list[dict], ahora: datetime) -> dict:
    stats = canal.get("statistics", {})
    oculto = stats.get("hiddenSubscriberCount", False)
    subs = None if oculto else int(stats.get("subscriberCount", 0))

    maduros = [v for v in videos if v["dias_publicado"] >= DIAS_MIN_ANTIGUEDAD_VIDEO]
    vistas = [v["vistas"] for v in maduros]
    fechas = [v["publicado"] for v in videos]
    intervalos = [(b - a).total_seconds() / 86400 for a, b in zip(fechas, fechas[1:])]

    m = {
        "channel_id": canal["id"],
        "channel_title": canal["snippet"]["title"],
        "handle": canal["snippet"].get("customUrl", ""),
        "country": canal["snippet"].get("country", ""),
        "channel_created": canal["snippet"]["publishedAt"][:10],
        "subscribers": subs,
        "subscribers_hidden": oculto,
        "total_views": int(stats.get("viewCount", 0)),
        "total_videos": int(stats.get("videoCount", 0)),
        "videos_analizados": len(videos),
        "videos_maduros": len(maduros),
        "dias_desde_ultima_subida": (ahora - fechas[-1]).total_seconds() / 86400 if fechas else float("nan"),
        "periodo_analizado_dias": (fechas[-1] - fechas[0]).total_seconds() / 86400 if len(fechas) > 1 else 0,
    }

    # --- núcleo
    cr = [v["comment_rate"] for v in maduros if v["comment_rate"] is not None]
    lr = [v["like_rate"] for v in maduros if v["like_rate"] is not None]
    m["avg_comment_rate"] = media_o_nan(cr)
    m["avg_like_rate"] = media_o_nan(lr)
    m["engagement_rate"] = media_o_nan([
        ((v["likes"] or 0) + (v["comentarios"] or 0)) / v["vistas"] * 100 for v in maduros if v["vistas"]
    ])
    m["recent_views_per_subscriber"] = (st.median(vistas) / subs) if (vistas and subs) else float("nan")
    m["views_per_subscriber"] = (m["total_views"] / subs) if subs else float("nan")
    m["views_coefficient_variation"] = (st.stdev(vistas) / st.mean(vistas)) if len(vistas) > 1 and st.mean(vistas) else float("nan")
    m["avg_views_per_day"] = media_o_nan([v["vistas_dia"] for v in maduros])
    m["avg_days_between_uploads"] = media_o_nan(intervalos)
    m["stdev_days_between_uploads"] = st.stdev(intervalos) if len(intervalos) > 1 else float("nan")
    m["median_days_between_uploads"] = st.median(intervalos) if intervalos else float("nan")

    # --- descriptiva de vistas
    if vistas:
        m["views_mean"] = st.mean(vistas)
        m["views_median"] = st.median(vistas)
        m["views_p25"] = percentil(vistas, 25)
        m["views_p75"] = percentil(vistas, 75)
        m["views_p90"] = percentil(vistas, 90)
        m["views_iqr"] = m["views_p75"] - m["views_p25"]
        m["views_std"] = st.stdev(vistas) if len(vistas) > 1 else 0.0
        m["views_min"], m["views_max"] = min(vistas), max(vistas)
        m["views_skewness"] = asimetria(vistas)
        m["views_kurtosis"] = curtosis(vistas)
        m["views_gini"] = gini(vistas)
        top = sorted(vistas, reverse=True)[:max(1, round(len(vistas) * 0.10))]
        m["views_top10_share"] = sum(top) / sum(vistas) * 100 if sum(vistas) else float("nan")

    # --- secundarias / descriptivas
    n = len(videos) or 1
    m["shorts_ratio"] = sum(v["es_short"] for v in videos) / n * 100
    m["live_videos_ratio"] = sum(v["es_directo"] for v in videos) / n * 100
    m["avg_tags_count"] = media_o_nan([v["n_tags"] for v in videos])
    m["avg_description_length"] = media_o_nan([v["len_descripcion"] for v in videos])
    m["avg_title_length"] = media_o_nan([v["len_titulo"] for v in videos])
    m["stdev_title_length"] = st.stdev([v["len_titulo"] for v in videos]) if len(videos) > 1 else float("nan")
    m["avg_duration_min"] = media_o_nan([v["duracion_s"] / 60 for v in videos])

    # --- negocio (NO entran en el score de salud)
    m["affiliate_link_ratio"] = sum(v["afiliado"] for v in videos) / n * 100

    # --- correlaciones (Spearman principal, Pearson de referencia)
    corr = {}
    if len(maduros) >= 10:
        y = [v["vistas_dia"] for v in maduros]
        for nombre, x in [
            ("Duración (min) vs vistas/día", [v["duracion_s"] / 60 for v in maduros]),
            ("Longitud del título vs vistas/día", [v["len_titulo"] for v in maduros]),
            ("Nº de tags vs vistas/día", [v["n_tags"] for v in maduros]),
            ("Tasa de comentarios vs vistas/día", [v["comment_rate"] or 0 for v in maduros]),
        ]:
            rs, rp = spearman(x, y), pearson(x, y)
            corr[nombre] = {"spearman": rs, "pearson": rp, "p_spearman": p_valor_correlacion(rs, len(x)), "n": len(x)}
    m["_correlaciones"] = corr
    return m


# ---------------------------------------------------------------------------
# PUNTUACIÓN Y SELLO
# ---------------------------------------------------------------------------

def cargar_referencia(ruta: str) -> tuple[dict, list[dict], str | None]:
    """Lee el CSV de la muestra. Devuelve ({métrica: [valores]}, filas, columna_tier)."""
    with open(ruta, newline="", encoding="utf-8-sig") as f:
        muestra = f.read(4096)
        f.seek(0)
        try:
            dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t")
        except csv.Error:
            dialecto = csv.excel
        filas = list(csv.DictReader(f, dialect=dialecto))
    if not filas:
        raise ValueError("El CSV de referencia está vacío.")
    cols = filas[0].keys()

    def a_float(s):
        try:
            return float(str(s).replace(",", ".")) if s not in (None, "") else float("nan")
        except ValueError:
            return float("nan")

    ref, filas_norm = {}, [dict() for _ in filas]
    for metrica, alias in ALIAS_REFERENCIA.items():
        col = next((a for a in alias if a in cols), None)
        if not col:
            continue
        valores = [a_float(fl[col]) for fl in filas]
        # Unidades de la tasa de comentarios: si viene como fracción (0.002) -> pasar a %
        if metrica == "avg_comment_rate":
            validos = [v for v in valores if es_num(v)]
            if validos and st.median(validos) < 0.02:
                valores = [v * 100 for v in valores]
        for fn, v in zip(filas_norm, valores):
            fn[metrica] = v
        limpios = [v for v in valores if es_num(v)]
        if len(limpios) >= 10:
            ref[metrica] = limpios
    col_tier = next((a for a in ALIAS_TIER if a in cols), None)
    if col_tier:
        for fn, fl in zip(filas_norm, filas):
            fn["_tier"] = (fl[col_tier] or "").strip()
    return ref, filas_norm, col_tier


def puntuar_metrica(metrica: str, valor: float, referencia: dict | None) -> tuple[float, str]:
    """Devuelve (puntos 0-100, método usado)."""
    cfg = METRICAS_NUCLEO[metrica]
    if not es_num(valor):
        return float("nan"), "sin dato"
    if referencia and metrica in referencia:
        xs = referencia[metrica]
        menores = sum(1 for x in xs if x < valor)
        iguales = sum(1 for x in xs if x == valor)
        pr = (menores + 0.5 * iguales) / len(xs) * 100
        return (pr if cfg["higher_better"] else 100 - pr), f"percentil vs muestra (n={len(xs)})"
    bad, good = cfg["bad"], cfg["good"]
    if cfg["log"]:
        valor, bad, good = math.log10(max(valor, 1e-9)), math.log10(bad), math.log10(good)
    pts = (valor - bad) / (good - bad) * 100
    return max(0.0, min(100.0, pts)), "umbral por defecto"


def score_total(metricas: dict, referencia: dict | None) -> tuple[float, dict]:
    detalle, suma, pesos = {}, 0.0, 0.0
    for k, cfg in METRICAS_NUCLEO.items():
        pts, metodo = puntuar_metrica(k, metricas.get(k, float("nan")), referencia)
        detalle[k] = {"valor": metricas.get(k, float("nan")), "puntos": pts, "peso": cfg["weight"], "metodo": metodo}
        if es_num(pts):
            suma += pts * cfg["weight"]
            pesos += cfg["weight"]
    # Si falta alguna métrica (p. ej. suscriptores ocultos) se reparte su peso entre el resto
    total = suma / pesos if pesos else float("nan")
    for d in detalle.values():
        d["peso_efectivo"] = (d["peso"] / pesos * 100) if (pesos and es_num(d["puntos"])) else 0
    return total, detalle


def decidir_sello(metricas: dict, score: float) -> tuple[str, list[str]]:
    motivos = []
    if metricas["videos_maduros"] < MIN_VIDEOS_VALIDOS:
        motivos.append(f"Solo {metricas['videos_maduros']} vídeos con más de {DIAS_MIN_ANTIGUEDAD_VIDEO} días "
                       f"(mínimo {MIN_VIDEOS_VALIDOS}): datos insuficientes.")
    if es_num(metricas["dias_desde_ultima_subida"]) and metricas["dias_desde_ultima_subida"] > DIAS_MAX_SIN_SUBIR:
        motivos.append(f"Lleva {metricas['dias_desde_ultima_subida']:.0f} días sin publicar "
                       f"(máximo {DIAS_MAX_SIN_SUBIR}): canal inactivo.")
    if motivos or not es_num(score):
        return "NO CERTIFICADO", motivos
    for minimo, nivel in NIVELES_SELLO:
        if score >= minimo:
            return nivel, motivos
    return "NO CERTIFICADO", motivos


def alertas(m: dict) -> list[str]:
    a = []
    if es_num(m.get("avg_like_rate")) and es_num(m.get("avg_comment_rate")) and m["avg_comment_rate"] > 0:
        ratio = m["avg_like_rate"] / m["avg_comment_rate"]
        if ratio > 150:
            a.append(f"Muchos likes por cada comentario ({ratio:.0f}:1). Puede ser audiencia pasiva o likes inflados; revisar.")
    if es_num(m.get("recent_views_per_subscriber")) and m["recent_views_per_subscriber"] < 0.005:
        a.append("Los vídeos recientes llegan a menos del 0,5 % de los suscriptores: posible audiencia «fantasma» "
                 "(suscriptores antiguos o comprados).")
    if es_num(m.get("views_top10_share")) and m["views_top10_share"] > 50:
        a.append(f"El 10 % de los vídeos concentra el {m['views_top10_share']:.0f} % de las vistas: "
                 "el canal depende de éxitos puntuales.")
    if m.get("shorts_ratio", 0) > 70:
        a.append(f"El {m['shorts_ratio']:.0f} % de lo analizado son Shorts: las métricas de vistas no son comparables "
                 "con canales de formato largo.")
    if m.get("subscribers_hidden"):
        a.append("El canal oculta sus suscriptores: no se pueden calcular las vistas por suscriptor "
                 "(su peso se reparte entre el resto de métricas).")
    return a


def tier_mas_parecido(filas_ref: list[dict], referencia: dict, score_canal: float) -> dict | None:
    """Puntúa cada canal de la muestra con el mismo baremo y busca el tier con la media más cercana."""
    por_tier: dict[str, list[float]] = {}
    todos = []
    for fl in filas_ref:
        t = fl.get("_tier")
        s, _ = score_total(fl, referencia)
        if es_num(s):
            todos.append(s)
            if t:
                por_tier.setdefault(t, []).append(s)
    por_tier = {t: v for t, v in por_tier.items() if len(v) >= 3}
    if not todos:
        return None
    res = {"percentil_en_muestra": sum(1 for s in todos if s < score_canal) / len(todos) * 100,
           "n_muestra": len(todos), "tiers": {}}
    if por_tier:
        res["tiers"] = {t: {"media": st.mean(v), "mediana": st.median(v), "n": len(v)} for t, v in por_tier.items()}
        res["tier_asignado"] = min(por_tier, key=lambda t: abs(st.median(por_tier[t]) - score_canal))
    return res


# ---------------------------------------------------------------------------
# FORMATO
# ---------------------------------------------------------------------------

def fnum(x, dec=0, sufijo="") -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s + sufijo


def fcompacto(x) -> str:
    if not es_num(x):
        return "—"
    for lim, suf in ((1e9, " mil M"), (1e6, " M"), (1e3, " mil")):
        if abs(x) >= lim:
            return fnum(x / lim, 1 if abs(x) / lim < 100 else 0) + suf
    return fnum(x)


def fmt_valor(metrica: str, v) -> str:
    if metrica == "avg_comment_rate":
        return fnum(v, 3, " %")
    if metrica == "recent_views_per_subscriber":
        return fnum(v, 3)
    if metrica == "views_coefficient_variation":
        return fnum(v, 2)
    if metrica == "avg_views_per_day":
        return fnum(v, 0)
    return fnum(v, 1, " días")


COLOR_SELLO = {"ORO": "#c9a227", "PLATA": "#8e98a3", "BRONCE": "#b06d3b", "NO CERTIFICADO": "#77756f"}


# ---------------------------------------------------------------------------
# INFORME HTML
# ---------------------------------------------------------------------------

def svg_sello(nivel: str, score: float) -> str:
    c = COLOR_SELLO[nivel]
    texto_nivel = "SIN SELLO" if nivel == "NO CERTIFICADO" else nivel
    return f"""
<svg class="seal" viewBox="0 0 200 230" role="img" aria-label="Sello T3chSeal {texto_nivel}, {fnum(score, 0)} puntos">
  <path d="M100 6 L186 38 V112 C186 168 148 205 100 224 C52 205 14 168 14 112 V38 Z" fill="{c}" />
  <path d="M100 20 L173 47 V112 C173 160 141 191 100 208 C59 191 27 160 27 112 V47 Z"
        fill="none" stroke="#ffffff" stroke-opacity=".55" stroke-width="2.5"/>
  <text x="100" y="66" text-anchor="middle" font-size="17" font-weight="700" fill="#fff" letter-spacing="2">T3CHSEAL</text>
  <text x="100" y="128" text-anchor="middle" font-size="56" font-weight="800" fill="#fff">{fnum(score, 0)}</text>
  <text x="100" y="148" text-anchor="middle" font-size="12" fill="#fff" fill-opacity=".85">de 100</text>
  <text x="100" y="180" text-anchor="middle" font-size="{18 if len(texto_nivel) < 8 else 15}" font-weight="700" fill="#fff" letter-spacing="1.5">{texto_nivel}</text>
</svg>"""


def svg_barras_vistas(videos: list[dict], mediana: float) -> str:
    vids = [v for v in videos if v["dias_publicado"] >= DIAS_MIN_ANTIGUEDAD_VIDEO]
    if not vids:
        return "<p class='muted'>Sin vídeos suficientes.</p>"
    W, H, ml, mb, mt = 860, 260, 56, 26, 12
    vmax = max(v["vistas"] for v in vids) or 1
    n = len(vids)
    paso = (W - ml - 8) / n
    ancho = max(2.0, paso - 2)  # 2px de separación entre barras
    y = lambda val: mt + (H - mt - mb) * (1 - val / vmax)
    partes = []
    # rejilla
    for frac in (0, .25, .5, .75, 1):
        yy = y(vmax * frac)
        partes.append(f'<line x1="{ml}" x2="{W - 8}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>'
                      f'<text x="{ml - 6}" y="{yy + 4:.1f}" text-anchor="end" class="tick">{fcompacto(vmax * frac)}</text>')
    for i, v in enumerate(vids):
        x = ml + i * paso + 1
        yy = y(v["vistas"])
        h = max(H - mb - yy, 1)
        r = min(4, ancho / 2, h)
        # barra con extremo superior redondeado anclada a la base
        d = (f"M{x:.1f},{H - mb} V{yy + r:.1f} Q{x:.1f},{yy:.1f} {x + r:.1f},{yy:.1f} "
             f"H{x + ancho - r:.1f} Q{x + ancho:.1f},{yy:.1f} {x + ancho:.1f},{yy + r:.1f} V{H - mb} Z")
        tipo = "Directo" if v["es_directo"] else ("Short" if v["es_short"] else "Vídeo")
        tip = (f"{html.escape(v['titulo'][:80])}|{v['publicado']:%d/%m/%Y} · {tipo}|"
               f"{fnum(v['vistas'])} vistas · {fnum(v['vistas_dia'], 0)}/día|"
               f"Comentarios: {fnum(v['comment_rate'], 3, ' %') if v['comment_rate'] is not None else '—'}")
        clase = "bar short" if v["es_short"] else "bar"
        partes.append(f'<path d="{d}" class="{clase}" data-tip="{html.escape(tip, quote=True)}"/>')
        # zona de hover más grande que la barra
        partes.append(f'<rect x="{ml + i * paso:.1f}" y="{mt}" width="{paso:.1f}" height="{H - mt - mb}" '
                      f'class="hit" data-tip="{html.escape(tip, quote=True)}"/>')
    ym = y(mediana)
    partes.append(f'<line x1="{ml}" x2="{W - 8}" y1="{ym:.1f}" y2="{ym:.1f}" class="median"/>'
                  f'<text x="{W - 10}" y="{ym - 5:.1f}" text-anchor="end" class="label">mediana {fcompacto(mediana)}</text>')
    partes.append(f'<text x="{ml}" y="{H - 6}" class="tick">{vids[0]["publicado"]:%d/%m/%Y}</text>'
                  f'<text x="{W - 8}" y="{H - 6}" text-anchor="end" class="tick">{vids[-1]["publicado"]:%d/%m/%Y}</text>')
    return f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Vistas por vídeo">{"".join(partes)}</svg>'


def svg_intervalos(videos: list[dict]) -> str:
    fechas = [v["publicado"] for v in videos]
    if len(fechas) < 2:
        return ""
    W, H, ml = 860, 70, 56
    t0, t1 = fechas[0], fechas[-1]
    span = max((t1 - t0).total_seconds(), 1)
    x = lambda f: ml + (W - ml - 12) * (f - t0).total_seconds() / span
    puntos = "".join(
        f'<circle cx="{x(v["publicado"]):.1f}" cy="32" r="5" class="dot{" short" if v["es_short"] else ""}" '
        f'data-tip="{html.escape(v["titulo"][:80], quote=True)}|{v["publicado"]:%d/%m/%Y %H:%M}"/>'
        for v in videos)
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Calendario de publicaciones">'
            f'<line x1="{ml}" x2="{W - 12}" y1="32" y2="32" class="axis"/>{puntos}'
            f'<text x="{ml}" y="62" class="tick">{t0:%d/%m/%Y}</text>'
            f'<text x="{W - 12}" y="62" text-anchor="end" class="tick">{t1:%d/%m/%Y}</text></svg>')


def generar_html(m: dict, videos: list[dict], score: float, detalle: dict, nivel: str, motivos: list[str],
                 avisos: list[str], tier_info: dict | None, alternativas: list[dict], demo: bool,
                 fecha: datetime, cuota: int) -> str:
    e = html.escape
    filas_score = ""
    for k, d in detalle.items():
        cfg = METRICAS_NUCLEO[k]
        pts = d["puntos"]
        ancho = pts if es_num(pts) else 0
        filas_score += f"""
        <div class="srow">
          <div class="sname"><strong>{e(cfg['label'])}</strong><span class="muted">{e(cfg['why'])}</span></div>
          <div class="sval">{fmt_valor(k, d['valor'])}<span class="muted">{e(cfg['unit'])}</span></div>
          <div class="sbar" data-tip="{e(cfg['label'])}|{fnum(pts, 0)} / 100 puntos|Peso: {fnum(d['peso_efectivo'], 0)} %|Método: {e(d['metodo'])}">
            <div class="track"><div class="fill" style="width:{ancho:.1f}%"></div></div>
          </div>
          <div class="spts">{fnum(pts, 0)}<span class="muted">peso {fnum(d['peso_efectivo'], 0)} %</span></div>
        </div>"""

    def tabla(filas):
        return "<table>" + "".join(f"<tr><th>{e(a)}</th><td>{b}</td></tr>" for a, b in filas) + "</table>"

    descriptiva = tabla([
        ("Media", fnum(m.get("views_mean"))), ("Mediana", fnum(m.get("views_median"))),
        ("Percentil 25", fnum(m.get("views_p25"))), ("Percentil 75", fnum(m.get("views_p75"))),
        ("Percentil 90", fnum(m.get("views_p90"))), ("Rango intercuartílico (IQR)", fnum(m.get("views_iqr"))),
        ("Desviación típica", fnum(m.get("views_std"))),
        ("Coeficiente de variación", fnum(m.get("views_coefficient_variation"), 2)),
        ("Mínimo / máximo", f"{fnum(m.get('views_min'))} / {fnum(m.get('views_max'))}"),
        ("Asimetría (skewness)", fnum(m.get("views_skewness"), 2)),
        ("Curtosis (exceso)", fnum(m.get("views_kurtosis"), 2)),
        ("Índice de Gini", fnum(m.get("views_gini"), 2)),
        ("Vistas del top 10 % de vídeos", fnum(m.get("views_top10_share"), 1, " %")),
    ])
    actividad = tabla([
        ("Vídeos analizados", f"{m['videos_analizados']} ({m['videos_maduros']} con más de {DIAS_MIN_ANTIGUEDAD_VIDEO} días)"),
        ("Periodo cubierto", fnum(m["periodo_analizado_dias"], 0, " días")),
        ("Días desde la última subida", fnum(m["dias_desde_ultima_subida"], 0)),
        ("Media de días entre vídeos", fnum(m["avg_days_between_uploads"], 1)),
        ("Mediana de días entre vídeos", fnum(m["median_days_between_uploads"], 1)),
        ("Desv. típica de días entre vídeos", fnum(m["stdev_days_between_uploads"], 1)),
        ("Duración media", fnum(m["avg_duration_min"], 1, " min")),
    ])
    engagement = tabla([
        ("Engagement (likes + coment. / vistas)", fnum(m["engagement_rate"], 2, " %")),
        ("Tasa de likes", fnum(m["avg_like_rate"], 2, " %")),
        ("Tasa de comentarios", fnum(m["avg_comment_rate"], 3, " %")),
        ("Vistas recientes / suscriptor (mediana)", fnum(m["recent_views_per_subscriber"], 3)),
        ("Vistas totales / suscriptor (catálogo)", fnum(m["views_per_subscriber"], 1)),
        ("Vistas por día por vídeo (media)", fnum(m["avg_views_per_day"], 0)),
    ])
    secundarias = tabla([
        ("% Shorts (≤ 3 min, heurística)", fnum(m["shorts_ratio"], 1, " %")),
        ("% directos", fnum(m["live_videos_ratio"], 1, " %")),
        ("Tags por vídeo", fnum(m["avg_tags_count"], 1)),
        ("Longitud media del título", fnum(m["avg_title_length"], 0, " caract.")),
        ("Desv. típica longitud título", fnum(m["stdev_title_length"], 1)),
        ("Longitud media de la descripción", fnum(m["avg_description_length"], 0, " caract.")),
    ])
    negocio = tabla([
        ("Vídeos con enlaces de afiliado", fnum(m["affiliate_link_ratio"], 1, " %")),
        ("País declarado", e(m["country"] or "—")),
    ])

    corr_html = ""
    if m["_correlaciones"]:
        filas = ""
        for nombre, c in m["_correlaciones"].items():
            sig = "sí" if es_num(c["p_spearman"]) and c["p_spearman"] < 0.05 else "no"
            filas += (f"<tr><th>{e(nombre)}</th><td>{fnum(c['spearman'], 2)}</td><td>{fnum(c['pearson'], 2)}</td>"
                      f"<td>{fnum(c['p_spearman'], 3)}</td><td>{sig}</td></tr>")
        corr_html = (f"<table class='corr'><tr><th></th><th>Spearman ρ</th><th>Pearson r</th><th>p (Spearman)</th>"
                     f"<th>Significativa (α = 0,05)</th></tr>{filas}</table>"
                     f"<p class='muted'>n = {next(iter(m['_correlaciones'].values()))['n']} vídeos. "
                     "Con tan pocos vídeos de un solo canal, tómalo como indicio, no como conclusión.</p>")
    else:
        corr_html = "<p class='muted'>Hacen falta al menos 10 vídeos para calcular correlaciones.</p>"

    tier_html = ""
    if tier_info:
        tier_html = f"<p>Percentil del canal dentro de tu muestra: <strong>{fnum(tier_info['percentil_en_muestra'], 0)}</strong> (n = {tier_info['n_muestra']}).</p>"
        if tier_info.get("tiers"):
            filas = "".join(
                f"<tr{' class=hl' if t == tier_info.get('tier_asignado') else ''}><th>{e(t)}</th>"
                f"<td>{fnum(v['mediana'], 1)}</td><td>{fnum(v['media'], 1)}</td><td>{v['n']}</td></tr>"
                for t, v in sorted(tier_info["tiers"].items(), key=lambda kv: -kv[1]["mediana"]))
            tier_html += (f"<p>Tier más parecido: <strong>{e(tier_info['tier_asignado'])}</strong> "
                          f"(el de mediana de score más cercana).</p>"
                          f"<table class='corr'><tr><th>Tier</th><th>Mediana score</th><th>Media score</th><th>n</th></tr>{filas}</table>")

    lista = lambda xs, cls: ("<ul class='" + cls + "'>" + "".join(f"<li>{e(x)}</li>" for x in xs) + "</ul>") if xs else ""
    alt_html = ""
    if alternativas:
        alt_html = ("<p class='muted'>Se buscó por nombre y se eligió el primer resultado. Otros canales encontrados: "
                    + ", ".join(f"{e(a['titulo'])} ({a['id']})" for a in alternativas)
                    + ". Si no es el canal correcto, usa su @handle o su URL.</p>")
    hay_ref = any(d["metodo"].startswith("percentil") for d in detalle.values())
    metodo_txt = ("cada métrica se puntúa por su <strong>percentil</strong> frente a tu muestra de referencia"
                  if hay_ref else
                  "cada métrica se puntúa entre un umbral «malo» (0) y uno «bueno» (100) por defecto; "
                  "para puntuar frente a tu muestra usa <code>--referencia channels_metrics.csv</code>")
    banner_demo = ("<div class='demo'>DATOS SINTÉTICOS DE DEMOSTRACIÓN — este canal no existe.</div>" if demo else "")
    subs_txt = "ocultos" if m["subscribers_hidden"] else fcompacto(m["subscribers"])

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>T3chSeal · {e(m['channel_title'])}</title>
<style>
.viz-root{{color-scheme:light;--surface-0:#f4f4f2;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;
 --text-muted:#7a7974;--grid:#e4e3df;--series-1:#2a78d6;--series-1-soft:#9cc1ee;--border:#e4e3df;--hl:#eef4fc;--seal:{COLOR_SELLO[nivel]}}}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme="light"])) .viz-root{{color-scheme:dark;--surface-0:#111110;
 --surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#94938b;--grid:#2e2e2c;--series-1:#3987e5;
 --series-1-soft:#2b5b96;--border:#2e2e2c;--hl:#1c2a3b}}}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--surface-0);color:var(--text-primary)}}
.viz-root{{background:var(--surface-0);min-height:100vh;padding:28px 18px 60px}}
.wrap{{max-width:960px;margin:0 auto}}
.card{{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:22px 24px;margin-bottom:18px}}
h1{{font-size:28px;margin:0 0 4px}} h2{{font-size:17px;margin:0 0 14px}}
.muted{{color:var(--text-muted);font-size:12.5px;display:block;font-weight:400}}
.hero{{display:grid;grid-template-columns:200px 1fr;gap:28px;align-items:center}}
.seal{{width:200px;height:auto}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}
.kpi{{border:1px solid var(--border);border-radius:10px;padding:10px 12px}}
.kpi b{{display:block;font-size:22px}} .kpi span{{color:var(--text-secondary);font-size:12.5px}}
.verdict{{font-size:15px;color:var(--text-secondary);margin:6px 0 0}}
.verdict strong{{color:var(--text-primary)}}
.srow{{display:grid;grid-template-columns:2.2fr 1fr 1.6fr .7fr;gap:14px;align-items:center;padding:10px 0;border-top:1px solid var(--border)}}
.srow:first-child{{border-top:0}}
.sval,.spts{{font-variant-numeric:tabular-nums;font-weight:600;text-align:right}}
.track{{height:10px;background:var(--grid);border-radius:5px;overflow:hidden}}
.fill{{height:100%;background:var(--series-1);border-radius:0 4px 4px 0}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:7px 4px;border-top:1px solid var(--border);text-align:left;font-variant-numeric:tabular-nums}}
td{{text-align:right}} th{{font-weight:500;color:var(--text-secondary)}}
table.corr td,table.corr th{{text-align:right}} table.corr tr th:first-child{{text-align:left}}
tr.hl th,tr.hl td{{background:var(--hl);color:var(--text-primary);font-weight:600}}
.chart{{width:100%;height:auto;display:block}}
.chart .grid{{stroke:var(--grid);stroke-width:1}} .chart .axis{{stroke:var(--text-muted);stroke-width:1}}
.chart .tick{{fill:var(--text-muted);font-size:11px}} .chart .label{{fill:var(--text-secondary);font-size:11.5px}}
.chart .bar{{fill:var(--series-1)}} .chart .bar.short{{fill:var(--series-1-soft)}}
.chart .median{{stroke:var(--text-secondary);stroke-width:1.5;stroke-dasharray:5 4}}
.chart .dot{{fill:var(--series-1);stroke:var(--surface-1);stroke-width:2}} .chart .dot.short{{fill:var(--series-1-soft)}}
.chart .hit{{fill:transparent}} .chart .hit:hover{{fill:var(--text-primary);fill-opacity:.05}}
.legend{{display:flex;gap:16px;font-size:12.5px;color:var(--text-secondary);margin-top:8px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}}
ul.warn li,ul.fail li{{margin:6px 0}} ul.fail li::marker{{content:"✕  "}} ul.warn li::marker{{content:"⚠  "}}
.demo{{background:#e34948;color:#fff;font-weight:700;text-align:center;padding:10px;border-radius:10px;margin-bottom:16px}}
#tip{{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--text-primary);border:1px solid var(--border);
 box-shadow:0 6px 24px rgba(0,0,0,.16);border-radius:8px;padding:8px 10px;font-size:12.5px;max-width:320px;display:none;z-index:9}}
#tip div:first-child{{font-weight:600;margin-bottom:3px}} #tip div{{color:var(--text-secondary)}}
footer{{color:var(--text-muted);font-size:12px;text-align:center;margin-top:24px}}
@media (max-width:720px){{.hero,.grid2{{grid-template-columns:1fr}}.kpis{{grid-template-columns:repeat(2,1fr)}}
 .srow{{grid-template-columns:1fr 1fr}}.seal{{margin:0 auto}}}}
</style></head>
<body><div class="viz-root"><div class="wrap">
{banner_demo}
<div class="card hero">
  {svg_sello(nivel, score)}
  <div>
    <h1>{e(m['channel_title'])}</h1>
    <span class="muted">{e(m['handle'])} · {m['channel_id']} · creado el {m['channel_created']}</span>
    <p class="verdict">Resultado: <strong>{'Sello ' + nivel if nivel != 'NO CERTIFICADO' else 'No certificado'}</strong>
      con <strong>{fnum(score, 1)}</strong> puntos de 100.
      {('Tier más parecido en la muestra: <strong>' + e(tier_info['tier_asignado']) + '</strong>.') if tier_info and tier_info.get('tier_asignado') else ''}</p>
    {lista(motivos, 'fail')}
    <div class="kpis">
      <div class="kpi"><b>{subs_txt}</b><span>suscriptores</span></div>
      <div class="kpi"><b>{fcompacto(m['total_views'])}</b><span>vistas totales</span></div>
      <div class="kpi"><b>{fnum(m['total_videos'])}</b><span>vídeos publicados</span></div>
      <div class="kpi"><b>{fcompacto(m.get('views_median'))}</b><span>mediana de vistas recientes</span></div>
    </div>
    {alt_html}
  </div>
</div>

<div class="card">
  <h2>Desglose del score de salud</h2>
  {filas_score}
  <p class="muted" style="margin-top:12px">Método: {metodo_txt}. Niveles: Oro ≥ 80 · Plata ≥ 65 · Bronce ≥ 50.
  Requisitos previos: haber publicado en los últimos {DIAS_MAX_SIN_SUBIR} días y tener al menos {MIN_VIDEOS_VALIDOS} vídeos analizables.</p>
</div>

{('<div class="card"><h2>Comparación con la muestra de referencia</h2>' + tier_html + '</div>') if tier_html else ''}

{('<div class="card"><h2>Alertas para revisar</h2>' + lista(avisos, 'warn') + '</div>') if avisos else ''}

<div class="card">
  <h2>Vistas de los últimos {m['videos_maduros']} vídeos</h2>
  {svg_barras_vistas(videos, m.get('views_median', 0) or 0)}
  <div class="legend"><span><i style="background:var(--series-1)"></i>Vídeo largo / directo</span>
  <span><i style="background:var(--series-1-soft)"></i>Short (≤ 3 min)</span>
  <span>– – mediana</span></div>
  <h2 style="margin-top:22px">Calendario de publicaciones</h2>
  {svg_intervalos(videos)}
</div>

<div class="grid2">
  <div class="card"><h2>Estadística descriptiva de vistas</h2>{descriptiva}</div>
  <div class="card"><h2>Engagement y alcance</h2>{engagement}<h2 style="margin-top:20px">Actividad</h2>{actividad}</div>
</div>
<div class="grid2">
  <div class="card"><h2>Métricas secundarias (no puntúan)</h2>{secundarias}</div>
  <div class="card"><h2>Variables de negocio (no puntúan)</h2>{negocio}
  <p class="muted">Sirven para decidir a qué marcas les interesa el canal, no para medir su salud.</p></div>
</div>
<div class="card"><h2>Correlaciones a nivel de vídeo</h2>{corr_html}</div>

<footer>T3chSeal · generado el {fecha:%d/%m/%Y %H:%M} · fuente: YouTube Data API v3 (métricas públicas){' · cuota usada: ' + str(cuota) + ' unidades' if not demo else ''}</footer>
</div></div>
<div id="tip"></div>
<script>
(function(){{
  var tip=document.getElementById('tip');
  document.addEventListener('mousemove',function(ev){{
    var t=ev.target.closest('[data-tip]');
    if(!t){{tip.style.display='none';return;}}
    var p=t.getAttribute('data-tip').split('|');
    tip.innerHTML='';
    p.forEach(function(s){{var d=document.createElement('div');d.textContent=s;tip.appendChild(d);}});
    tip.style.display='block';
    var x=ev.clientX+14,y=ev.clientY+14;
    if(x+tip.offsetWidth>innerWidth-8)x=ev.clientX-tip.offsetWidth-14;
    if(y+tip.offsetHeight>innerHeight-8)y=ev.clientY-tip.offsetHeight-14;
    tip.style.left=x+'px';tip.style.top=y+'px';
  }});
}})();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# DEMO (datos sintéticos para probar sin API key)
# ---------------------------------------------------------------------------

def datos_demo(ahora: datetime, n: int) -> tuple[dict, list[dict]]:
    random.seed(7)
    canal = {"id": "UCdemo000000000000000000", "snippet": {"title": "Canal Demo Tech", "customUrl": "@canaldemotech",
             "country": "ES", "publishedAt": "2017-03-01T10:00:00Z"},
             "statistics": {"subscriberCount": "185000", "viewCount": "42000000", "videoCount": "640",
                            "hiddenSubscriberCount": False}}
    videos, t = [], ahora - timedelta(days=2)
    for i in range(n):
        dur = random.choice([45, 58, 600, 840, 720, 1100, 950, 3600]) if i % 6 else 50
        vistas = int(random.lognormvariate(10.2, 0.6) * (0.3 if dur <= 60 else 1))
        desc = "Cómpralo aquí: https://amzn.to/abc123" if random.random() < .6 else "Review completa."
        videos.append({"id": f"demo{i:03d}", "snippet": {"publishedAt": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "title": f"Review del gadget número {i} ¿merece la pena?"[:random.randint(30, 60)],
                       "description": desc, "tags": ["tech"] * random.randint(3, 15), "liveBroadcastContent": "none"},
                       "statistics": {"viewCount": str(vistas), "likeCount": str(int(vistas * random.uniform(.02, .05))),
                                      "commentCount": str(int(vistas * random.uniform(.0008, .004)))},
                       "contentDetails": {"duration": f"PT{dur // 60}M{dur % 60}S"}})
        t -= timedelta(days=random.choice([2, 3, 3, 4, 5, 7]), hours=random.randint(0, 6))
    return canal, videos


# ---------------------------------------------------------------------------
# PROGRAMA PRINCIPAL
# ---------------------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s.lower()).strip("_")[:40] or "canal"


def exportar(m: dict, score: float, nivel: str, carpeta: Path, base: str) -> tuple[Path, Path]:
    plano = {k: (round(v, 6) if isinstance(v, float) and not math.isnan(v) else (None if isinstance(v, float) else v))
             for k, v in m.items() if not k.startswith("_")}
    plano["techseal_score"] = round(score, 2) if es_num(score) else None
    plano["techseal_nivel"] = nivel
    ruta_json = carpeta / f"{base}.json"
    ruta_json.write_text(json.dumps({**plano, "correlaciones": m["_correlaciones"]}, ensure_ascii=False, indent=2,
                                    default=lambda x: None), encoding="utf-8")
    ruta_csv = carpeta / "techseal_evaluaciones.csv"  # se acumula una fila por evaluación
    nuevo = not ruta_csv.exists()
    with open(ruta_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(plano.keys()), extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerow(plano)
    return ruta_json, ruta_csv


def en_notebook() -> bool:
    """True si se ejecuta dentro de Jupyter / Google Colab."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None and "IPKernelApp" in get_ipython().config
    except Exception:
        return False


def api_key_colab() -> str | None:
    """Lee la API key de los Secretos de Colab (icono de la llave), si existe."""
    try:
        from google.colab import userdata
        return userdata.get("YOUTUBE_API_KEY")
    except Exception:
        return None


def mostrar_en_notebook(ruta_html: Path) -> None:
    try:
        from IPython.display import HTML, display
        doc = ruta_html.read_text(encoding="utf-8")
        display(HTML(f'<iframe srcdoc="{html.escape(doc, quote=True)}" '
                     f'style="width:100%;height:1500px;border:0;border-radius:12px"></iframe>'))
    except Exception as ex:
        print(f"(No se pudo mostrar el informe en la celda: {ex})")


def evaluar(entrada: str, api_key: str | None, n_videos: int = 50, referencia: str | None = None,
            demo: bool = False) -> dict:
    """Evaluación completa sin escribir archivos ni imprimir (la usa la app web).
    Lanza YouTubeAPIError o ValueError si algo falla."""
    ahora = datetime.now(timezone.utc)
    alternativas, cuota = [], 0
    if demo:
        canal, videos_api = datos_demo(ahora, n_videos)
    else:
        if not entrada or not entrada.strip():
            raise ValueError("Escribe el nombre, @handle o URL de un canal.")
        if not api_key:
            raise ValueError("No hay API key configurada.")
        yt = YouTubeClient(api_key)
        cid, alternativas = yt.resolver_canal(entrada)
        canal = yt.datos_canal(cid)
        videos_api = yt.ultimos_videos(canal["contentDetails"]["relatedPlaylists"]["uploads"], n_videos)
        cuota = yt.cuota_usada
    videos = construir_dataset_videos(videos_api, ahora)
    if not videos:
        raise ValueError("El canal no tiene vídeos públicos analizables.")
    m = calcular_metricas(canal, videos, ahora)
    ref, filas_ref, tier_info = None, [], None
    if referencia and os.path.exists(referencia):
        ref, filas_ref, _ = cargar_referencia(referencia)
    score, detalle = score_total(m, ref)
    nivel, motivos = decidir_sello(m, score)
    if ref:
        tier_info = tier_mas_parecido(filas_ref, ref, score)
    avisos = alertas(m)
    informe = generar_html(m, videos, score, detalle, nivel, motivos, avisos, tier_info, alternativas,
                           demo, ahora.astimezone(), cuota)
    plano = {k: (round(v, 6) if es_num(v) else (None if isinstance(v, float) else v))
             for k, v in m.items() if not k.startswith("_")}
    plano.update(techseal_score=round(score, 2) if es_num(score) else None, techseal_nivel=nivel)
    return {"metricas": m, "fila": plano, "score": score, "nivel": nivel, "motivos": motivos,
            "avisos": avisos, "detalle": detalle, "tier": tier_info, "html": informe, "cuota": cuota,
            "alternativas": alternativas, "nombre_archivo": f"techseal_{slug(m['channel_title'])}_{ahora:%Y%m%d}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evalúa un canal de YouTube y le asigna el sello T3chSeal.")
    ap.add_argument("canal", nargs="?", help="Nombre, @handle, URL o ID (UC...) del canal")
    ap.add_argument("--api-key", help="API key de YouTube Data API v3 (o variable YOUTUBE_API_KEY)")
    ap.add_argument("--videos", type=int, default=50, help="Nº de vídeos recientes a analizar (por defecto 50)")
    ap.add_argument("--referencia", help="CSV de tu muestra (p. ej. channels_metrics.csv) para puntuar por percentiles")
    ap.add_argument("--salida", default="techseal_informes", help="Carpeta de salida")
    ap.add_argument("--no-abrir", action="store_true", help="No abrir el informe en el navegador")
    ap.add_argument("--demo", action="store_true", help="Usar datos sintéticos (sin API)")
    # parse_known_args ignora los argumentos internos que añade Jupyter/Colab (p. ej. -f kernel.json)
    a, _ = ap.parse_known_args(argv)
    notebook = en_notebook()

    ahora = datetime.now(timezone.utc)
    alternativas, cuota = [], 0

    if a.demo:
        canal, videos_api = datos_demo(ahora, a.videos)
    else:
        interactivo = notebook or sys.stdin.isatty()
        if not interactivo and not (a.canal and (a.api_key or os.environ.get("YOUTUBE_API_KEY"))):
            # Caso típico: «!python techseal_sello.py» en Colab, donde no se puede escribir en la salida
            print("✕ Este modo no admite escribir datos en la salida. Pasa el canal y la API key así:\n"
                  '    !python techseal_sello.py "@canal" --api-key TU_CLAVE\n'
                  "  o define antes la variable de entorno YOUTUBE_API_KEY, "
                  "o usa:  from techseal_sello import evaluar_canal")
            return 1
        entrada = a.canal or input("Canal de YouTube (nombre, @handle o URL): ").strip()
        if not entrada:
            print("No se indicó ningún canal.")
            return 1
        key = (a.api_key or os.environ.get("YOUTUBE_API_KEY") or api_key_colab()
               or getpass.getpass("API key de YouTube: ").strip())
        yt = YouTubeClient(key)
        try:
            print(f"→ Buscando «{entrada}»...")
            cid, alternativas = yt.resolver_canal(entrada)
            canal = yt.datos_canal(cid)
            print(f"→ Canal: {canal['snippet']['title']} ({cid})")
            uploads = canal["contentDetails"]["relatedPlaylists"]["uploads"]
            print(f"→ Descargando los últimos {a.videos} vídeos...")
            videos_api = yt.ultimos_videos(uploads, a.videos)
        except YouTubeAPIError as ex:
            print(f"✕ {ex}")
            return 1
        cuota = yt.cuota_usada

    videos = construir_dataset_videos(videos_api, ahora)
    if not videos:
        print("✕ El canal no tiene vídeos públicos analizables.")
        return 1
    m = calcular_metricas(canal, videos, ahora)

    referencia, tier_info = None, None
    if a.referencia:
        try:
            referencia, filas_ref, _ = cargar_referencia(a.referencia)
            usadas = ", ".join(referencia) or "ninguna"
            print(f"→ Referencia cargada. Métricas con ≥10 valores válidos: {usadas}")
        except (OSError, ValueError) as ex:
            print(f"⚠ No se pudo leer la referencia ({ex}); se usan umbrales por defecto.")
            referencia, filas_ref = None, []

    score, detalle = score_total(m, referencia)
    nivel, motivos = decidir_sello(m, score)
    if referencia:
        tier_info = tier_mas_parecido(filas_ref, referencia, score)
    avisos = alertas(m)

    carpeta = Path(a.salida)
    carpeta.mkdir(parents=True, exist_ok=True)
    base = f"techseal_{slug(m['channel_title'])}_{ahora:%Y%m%d}"
    ruta_html = carpeta / f"{base}.html"
    ruta_html.write_text(generar_html(m, videos, score, detalle, nivel, motivos, avisos, tier_info, alternativas,
                                      a.demo, ahora.astimezone(), cuota), encoding="utf-8")
    ruta_json, ruta_csv = exportar(m, score, nivel, carpeta, base)

    # --- resumen en consola
    print("\n" + "=" * 62)
    print(f"  T3CHSEAL · {m['channel_title']}")
    print("=" * 62)
    print(f"  Sello: {nivel}   Score: {fnum(score, 1)} / 100")
    for x in motivos:
        print(f"  ✕ {x}")
    print("-" * 62)
    for k, d in detalle.items():
        print(f"  {METRICAS_NUCLEO[k]['label']:<34}{fmt_valor(k, d['valor']):>14}{fnum(d['puntos'], 0):>6} pts")
    if tier_info and tier_info.get("tier_asignado"):
        print(f"  Tier más parecido en la muestra: {tier_info['tier_asignado']}")
    for x in avisos:
        print(f"  ⚠ {x}")
    print("-" * 62)
    print(f"  Informe: {ruta_html.resolve()}")
    print(f"  Datos:   {ruta_json.name} · fila añadida a {ruta_csv.name}")
    if cuota:
        print(f"  Cuota API usada: {cuota} unidades")
    print("=" * 62)
    if notebook:
        mostrar_en_notebook(ruta_html)
    elif not a.no_abrir:
        try:
            webbrowser.open(ruta_html.resolve().as_uri())
        except Exception:
            pass
    return 0


def evaluar_canal(canal: str | None = None, api_key: str | None = None, videos: int = 50,
                  referencia: str | None = None, salida: str = "techseal_informes", demo: bool = False) -> int:
    """Atajo para notebooks:  evaluar_canal("@TopesdeGama", referencia="channels_metrics.csv")"""
    argv = [canal] if canal else []
    argv += ["--videos", str(videos), "--salida", salida]
    if api_key:
        argv += ["--api-key", api_key]
    if referencia:
        argv += ["--referencia", referencia]
    if demo:
        argv.append("--demo")
    return main(argv)


if __name__ == "__main__":
    if en_notebook():
        # En notebooks (VS Code, Colab, Jupyter) no se pide nada por pantalla: se llama a evaluar_canal()
        print("T3chSeal cargado. Ejecuta en otra celda:\n"
              '    evaluar_canal("@canal", api_key=API_KEY)\n'
              '    evaluar_canal("@canal", api_key=API_KEY, referencia="channels_metrics.csv")')
    else:
        sys.exit(main())
