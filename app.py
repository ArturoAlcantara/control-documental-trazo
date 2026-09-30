import base64
import os
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components


# =========================================================
# CONFIGURACIÓN
# =========================================================

st.set_page_config(
    page_title="Control Documental",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if "menu_activo" not in st.session_state:
    st.session_state.menu_activo = "home"

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"


# =========================================================
# CARGA DE IMÁGENES
# =========================================================

def obtener_imagen_base64(nombre_archivo):
    ruta = ASSETS_DIR / nombre_archivo

    if not ruta.exists():
        return ""

    extension = ruta.suffix.lower()

    if extension == ".png":
        mime = "image/png"
    elif extension in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    else:
        mime = "application/octet-stream"

    with open(ruta, "rb") as archivo:
        contenido = base64.b64encode(archivo.read()).decode()

    return f"data:{mime};base64,{contenido}"


fondo_planos = obtener_imagen_base64("fondo_planos.jpg")
icono_documento = obtener_imagen_base64("icono_documento.png")
icono_oficios = obtener_imagen_base64("icono_oficios.png")
icono_diseno = obtener_imagen_base64("icono_diseno.png")


# =========================================================
# CSS GENERAL
# =========================================================

st.markdown(
    f"""
<style>

html, body, [class*="css"] {{
    font-family: Arial, Helvetica, sans-serif;
}}

.stApp {{
    background-image: url("{fondo_planos}");
    background-size: cover;
    background-position: center center;
    background-repeat: no-repeat;
    background-attachment: fixed;
}}

header[data-testid="stHeader"] {{
    background: transparent;
}}

footer {{
    visibility: hidden;
}}

#MainMenu {{
    visibility: hidden;
}}

.block-container {{
    max-width: 1200px !important;
    padding-top: 8vh !important;
    padding-left: 40px !important;
    padding-right: 40px !important;
    margin: 0 auto !important;
}}

/* Estilo para los botones invisibles superpuestos ajustados a 145px */
div[data-testid="stButton"] {{
    width: 100%;
    margin-top: -145px;
    position: relative;
    z-index: 10;
}}

div[data-testid="stButton"] > button {{
    width: 100% !important;
    height: 145px !important;
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
    opacity: 0;
}}

div[data-testid="stButton"] > button:hover {{
    background: transparent !important;
    border: none !important;
}}

@media (max-width: 900px) {{
    .block-container {{
        padding-left: 20px !important;
        padding-right: 20px !important;
    }}
}}

</style>
""",
    unsafe_allow_html=True,
)


# =========================================================
# HOME
# =========================================================

if st.session_state.menu_activo == "home":

    # -----------------------------------------------------
    # CABECERA
    # -----------------------------------------------------

    components.html(
        f"""
        <style>
            body {{
                margin: 0;
                padding: 0;
                background: transparent;
                overflow: hidden;
                font-family: Arial, Helvetica, sans-serif;
            }}
            .encabezado {{
                width: 100%;
                text-align: center;
            }}
            .icono-documento {{
                width: 82px;
                height: 82px;
                object-fit: contain;
                margin-bottom: 12px;
            }}
            .titulo-control {{
                color: #102030;
                font-size: 64px;
                font-weight: 900;
                line-height: 0.9;
                letter-spacing: 1px;
            }}
            .titulo-documental {{
                color: #102030;
                font-size: 43px;
                font-weight: 300;
                line-height: 1;
                letter-spacing: 9px;
                margin-top: 13px;
            }}
            .divisor {{
                display: flex;
                justify-content: center;
                align-items: center;
                gap: 18px;
                margin-top: 27px;
            }}
            .linea {{
                width: 150px;
                height: 2px;
                background: #b39258;
            }}
            .rombo {{
                width: 14px;
                height: 14px;
                background: #c99d58;
                transform: rotate(45deg);
            }}
        </style>

        <div class="encabezado">
            <img src="{icono_documento}" class="icono-documento">
            <div class="titulo-control">CONTROL</div>
            <div class="titulo-documental">DOCUMENTAL</div>
            <div class="divisor">
                <div class="linea"></div>
                <div class="rombo"></div>
                <div class="linea"></div>
            </div>
        </div>
        """,
        height=250,
    )

    # -----------------------------------------------------
    # TARJETAS
    # -----------------------------------------------------

    col1, col2 = st.columns(2, gap="large")

    # ==========================================
    # OFICIOS EN TRÁMITE
    # ==========================================

    with col1:
        components.html(
            f"""
            <style>
                body {{
                    margin: 0;
                    padding: 0;
                    background: transparent;
                    overflow: hidden;
                    font-family: Arial, Helvetica, sans-serif;
                }}
                .tarjeta {{
                    width: 100%;
                    height: 145px;
                    display: flex;
                    background: #f6f3ed;
                    border: 1px solid #927449;
                    box-sizing: border-box;
                    overflow: hidden;
                }}
                .contenido {{
                    width: 82%;
                    display: flex;
                    align-items: center;
                    padding: 0 30px;
                    box-sizing: border-box;
                }}
                .icono {{
                    width: 105px;
                    height: 105px;
                    object-fit: contain;
                    flex-shrink: 0;
                }}
                .separador {{
                    width: 1px;
                    height: 65px;
                    background: #9e9586;
                    margin-left: 18px;
                    margin-right: 30px;
                    flex-shrink: 0;
                }}
                .texto {{
                    color: #102030;
                    font-size: 20px;
                    font-weight: 700;
                    line-height: 1.25;
                    letter-spacing: 0.5px;
                    flex: 1;
                    text-align: center;
                }}
                .flecha {{
                    width: 18%;
                    height: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    background: #102030;
                    color: white;
                    font-size: 38px;
                    font-weight: 300;
                }}
            </style>

            <div class="tarjeta">
                <div class="contenido">
                    <img src="{icono_oficios}" class="icono">
                    <div class="separador"></div>
                    <div class="texto">
                        OFICIOS EN
                        <br>
                        TRÁMITE
                    </div>
                </div>
                <div class="flecha">→</div>
            </div>
            """,
            height=145,
        )

        if st.button("Entrar a Oficios en Trámite", key="btn_oficios", use_container_width=True):
            st.session_state.menu_activo = "oficios"
            st.rerun()

    # ==========================================
    # INGENIERÍA DE DETALLE FASE I
    # ==========================================

    with col2:
        components.html(
            f"""
            <style>
                body {{
                    margin: 0;
                    padding: 0;
                    background: transparent;
                    overflow: hidden;
                    font-family: Arial, Helvetica, sans-serif;
                }}
                .tarjeta {{
                    width: 100%;
                    height: 145px;
                    display: flex;
                    background: #f6f3ed;
                    border: 1px solid #927449;
                    box-sizing: border-box;
                    overflow: hidden;
                }}
                .contenido {{
                    width: 82%;
                    display: flex;
                    align-items: center;
                    padding: 0 30px;
                    box-sizing: border-box;
                }}
                .icono {{
                    width: 105px;
                    height: 105px;
                    object-fit: contain;
                    flex-shrink: 0;
                }}
                .icono-diseno {{
                    transform: scale(1.25);
                }}
                .separador {{
                    width: 1px;
                    height: 65px;
                    background: #9e9586;
                    margin-left: 18px;
                    margin-right: 30px;
                    flex-shrink: 0;
                }}
                .texto {{
                    color: #102030;
                    font-size: 16px;
                    font-weight: 700;
                    line-height: 1.25;
                    letter-spacing: 0.3px;
                    flex: 1;
                    text-align: center;
                }}
                .flecha {{
                    width: 18%;
                    height: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    background: #102030;
                    color: white;
                    font-size: 38px;
                    font-weight: 300;
                }}
            </style>

            <div class="tarjeta">
                <div class="contenido">
                    <img src="{icono_diseno}" class="icono icono-diseno">
                    <div class="separador"></div>
                    <div class="texto">
                        INGENIERÍA DE DETALLE
                        <br>
                        FASE I
                    </div>
                </div>
                <div class="flecha">→</div>
            </div>
            """,
            height=145,
        )

        if st.button("Entrar a Ingeniería de Detalle Fase I", key="btn_diseno", use_container_width=True):
            st.session_state.menu_activo = "diseno"
            st.rerun()


# =========================================================
# MÓDULO OFICIOS EN TRÁMITE
# =========================================================

elif st.session_state.menu_activo == "oficios":
    if st.button("← Volver al Menú Principal", key="volver_oficios"):
        st.session_state.menu_activo = "home"
        st.rerun()

    st.markdown("---")
    st.markdown("## Oficios en Trámite")
    st.info("Espacio preparado para conectar la base de datos de oficios y correspondencia.")


# =========================================================
# MÓDULO INGENIERÍA DE DETALLE FASE I
# =========================================================

elif st.session_state.menu_activo == "diseno":
    if st.button("← Volver al Menú Principal", key="volver_diseno"):
        st.session_state.menu_activo = "home"
        st.rerun()

    st.markdown("---")
    st.markdown("## Ingeniería de Detalle Fase I")
    st.info("Espacio preparado para la gestión de proyectos y control de esta mesa.")