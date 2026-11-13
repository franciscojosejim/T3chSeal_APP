# -*- coding: utf-8 -*-
"""
T3chSeal · App web (Streamlit)
Escribe un canal de YouTube y obtén su sello T3chSeal.

Local:   streamlit run app.py
Online:  Streamlit Community Cloud (ver LEEME.md). La API key va en los "Secrets" de la app.
"""
import csv
import io
import json
import os

import streamlit as st
import streamlit.components.v1 as components

import techseal_sello as ts

st.set_page_config(page_title="T3chSeal · Sello de calidad para canales tech", page_icon="🛡️", layout="centered")

REFERENCIA = "channels_metrics.csv"  # si este CSV está junto a app.py, se puntúa contra tu muestra


def leer_api_key() -> str:
    try:
        clave = st.secrets.get("YOUTUBE_API_KEY", "")
    except Exception:  # no hay archivo de secretos (ejecución local sin configurar)
        clave = ""
    return clave or os.environ.get("YOUTUBE_API_KEY", "")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def evaluar_cacheado(canal: str, videos: int, demo: bool, _api_key: str) -> dict:
    # Caché de 6 h: repetir el mismo canal no gasta cuota de la API
    return ts.evaluar(canal, _api_key, n_videos=videos, referencia=REFERENCIA, demo=demo)


API_KEY = leer_api_key()
hay_referencia = os.path.exists(REFERENCIA)

# --- cabecera ---------------------------------------------------------------
st.markdown("<h1 style='margin-bottom:0'>🛡️ T3chSeal</h1>", unsafe_allow_html=True)
st.caption("Sello de calidad para canales de YouTube de tecnología de consumo · datos públicos de la YouTube Data API")

with st.form("buscador"):
    canal = st.text_input("Canal de YouTube", placeholder="@TopesdeGama  ·  https://www.youtube.com/@clipset  ·  Topes de Gama")
    with st.expander("Opciones"):
        videos = st.slider("Vídeos recientes a analizar", 15, 100, 50, step=5)
    enviado = st.form_submit_button("Obtener sello", type="primary", use_container_width=True)

demo = False
if not API_KEY:
    st.warning("La app no tiene API key configurada. Añade `YOUTUBE_API_KEY` en los Secrets (ver LEEME.md).")
    demo = st.button("Probar con datos de demostración")

if enviado or demo:
    if enviado and not demo and not canal.strip():
        st.info("Escribe el nombre, @handle o URL de un canal.")
        st.stop()
    with st.spinner("Analizando el canal…"):
        try:
            r = evaluar_cacheado(canal.strip(), videos, demo, API_KEY)
        except ts.YouTubeAPIError as ex:
            st.error(str(ex))
            st.stop()
        except ValueError as ex:
            st.error(str(ex))
            st.stop()

    m = r["metricas"]
    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sello", r["nivel"].title() if r["nivel"] != "NO CERTIFICADO" else "Sin sello")
    c2.metric("Score", ts.fnum(r["score"], 1) + " / 100")
    c3.metric("Suscriptores", "ocultos" if m["subscribers_hidden"] else ts.fcompacto(m["subscribers"]))
    c4.metric("Mediana de vistas", ts.fcompacto(m.get("views_median")))
    if r["tier"] and r["tier"].get("tier_asignado"):
        st.caption(f"Tier más parecido en la muestra de referencia: **{r['tier']['tier_asignado']}**")
    elif not hay_referencia:
        st.caption("Puntuado con umbrales por defecto (sube `channels_metrics.csv` junto a la app para comparar con tu muestra).")

    # Informe completo (el mismo HTML que genera el notebook)
    components.html(r["html"], height=2700, scrolling=True)

    # Descargas
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(r["fila"].keys()))
    w.writeheader()
    w.writerow(r["fila"])
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇ Informe HTML", r["html"], file_name=r["nombre_archivo"] + ".html", mime="text/html",
                       use_container_width=True)
    d2.download_button("⬇ Métricas CSV", buf.getvalue(), file_name=r["nombre_archivo"] + ".csv", mime="text/csv",
                       use_container_width=True)
    d3.download_button("⬇ Métricas JSON", json.dumps(r["fila"], ensure_ascii=False, indent=2),
                       file_name=r["nombre_archivo"] + ".json", mime="application/json", use_container_width=True)

st.markdown(
    "<div style='text-align:center;color:#888;font-size:12px;margin-top:40px'>"
    "T3chSeal · Niveles: Oro ≥ 80 · Plata ≥ 65 · Bronce ≥ 50 · Requiere actividad en los últimos 90 días</div>",
    unsafe_allow_html=True,
)
