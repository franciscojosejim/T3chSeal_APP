# T3chSeal · App web

Escribe un canal de YouTube y obtén su sello T3chSeal (Oro / Plata / Bronce / Sin sello) con el informe estadístico completo.

## Archivos
| Archivo | Para qué |
|---|---|
| `app.py` | La interfaz web (Streamlit) |
| `techseal_sello.py` | El motor: API, métricas, score y sello (el mismo del notebook) |
| `requirements.txt` | Librerías que instala el servidor |
| `channels_metrics.csv` | *(opcional, añádelo tú)* tu muestra de referencia para puntuar por percentiles |

## Publicarla con URL propia (gratis, ~10 min)
1. Crea un repositorio en GitHub (p. ej. `t3chseal`) y sube estos archivos con **Add file → Upload files**.
   No subas nunca tu API key al repositorio.
2. Entra en https://share.streamlit.io con tu cuenta de GitHub → **Create app** → elige el repositorio,
   rama `main` y archivo `app.py`. Puedes elegir la URL (p. ej. `t3chseal.streamlit.app`).
3. En **Advanced settings → Secrets** pega:
   ```
   YOUTUBE_API_KEY = "tu_clave"
   ```
4. **Deploy**. Cuando termine tendrás tu URL.

La clave queda guardada en el servidor de Streamlit: quien use la app no la ve.
Si la app se queda dormida tras días sin uso, se despierta al entrar (tarda unos segundos).

## Usarla en tu ordenador
```
pip install streamlit
copia .streamlit/secrets.toml.example como .streamlit/secrets.toml y pon tu clave
streamlit run app.py
```

## Cuota de la API
Cada canal gasta ~3-5 unidades (≈100 más si se busca por nombre en vez de @handle o URL).
La cuota gratuita es de 10.000 unidades/día. Los resultados se guardan en caché 6 h.
