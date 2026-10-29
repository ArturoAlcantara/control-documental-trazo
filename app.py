import base64
import html
import json
import os
import time
import random
import hashlib
import hmac
import secrets
import requests
from pathlib import Path
import textwrap
import unicodedata
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import gspread
from google.oauth2.service_account import Credentials

from io import BytesIO
import re
from urllib.parse import quote
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from datetime import datetime
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    KeepTogether,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.lib.enums import TA_CENTER
from xml.sax.saxutils import escape


# =========================================================
# CONFIGURACIÓN
# =========================================================

st.set_page_config(
    page_title="Control Documental",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# Estilo específico de envío a revisión: grafito cálido + acento dorado.
# Se limita a estos dos botones para no modificar otras acciones del sistema.
st.markdown("""
<style>
[class*="st-key-doc_reenviar_"] button,
[class*="st-key-doc_tte_"] button {
    background: #4A4844 !important;
    border: 1px solid #C6A05D !important;
    border-left: 4px solid #D0AA68 !important;
    color: #FFFFFF !important;
    border-radius: 8px !important;
    font-weight: 650 !important;
    box-shadow: 0 3px 10px rgba(35, 33, 29, 0.14) !important;
    transition: background-color .18s ease, border-color .18s ease !important;
}
[class*="st-key-doc_reenviar_"] button:hover,
[class*="st-key-doc_tte_"] button:hover {
    background: #625B50 !important;
    border-color: #E0BC7A !important;
    color: #FFFFFF !important;
}
[class*="st-key-doc_reenviar_"] button:focus-visible,
[class*="st-key-doc_tte_"] button:focus-visible {
    outline: 2px solid #D0AA68 !important;
    outline-offset: 2px !important;
}
</style>
""", unsafe_allow_html=True)


def aplicar_fondo():
    ruta = Path(__file__).parent / "assets" / "fondo_ferroviario.png"

    if not ruta.exists():
        st.warning("No se encontró el fondo ferroviario.")
        return

    imagen_base64 = base64.b64encode(
        ruta.read_bytes()
    ).decode("utf-8")

    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{
            background-image: url("data:image/png;base64,{imagen_base64}") !important;
            background-size: cover !important;
            background-position: center center !important;
            background-repeat: no-repeat !important;
            background-attachment: fixed !important;
        }}

        [data-testid="stHeader"] {{
            background: transparent !important;
        }}
        </style>
        """,
        unsafe_allow_html=True
    )


# =========================================================
# NAVEGACIÓN Y ESTADO
# =========================================================

if "menu_activo" not in st.session_state:
    st.session_state.menu_activo = "home"

# Estado de autenticación
if "autenticado" not in st.session_state:
    st.session_state.autenticado = False
if "usuario_actual" not in st.session_state:
    st.session_state.usuario_actual = None
if "usuario_id" not in st.session_state:
    st.session_state.usuario_id = None
if "rol" not in st.session_state:
    st.session_state.rol = "auxiliar"
if "cargo_institucional" not in st.session_state:
    st.session_state.cargo_institucional = ""
if "responsable_oficios" not in st.session_state:
    st.session_state.responsable_oficios = ""


# Los roles se definen en .streamlit/secrets.toml, NO por similitud de nombres.
ROLES_VALIDOS = ("propietario", "administrador", "auxiliar")


def obtener_rol():
    """Rol de la sesión autenticada; sin asignación explícita = auxiliar."""
    rol = str(st.session_state.get("rol", "auxiliar")).strip().lower()
    return rol if rol in ROLES_VALIDOS else "auxiliar"


def es_administrador():
    return obtener_rol() in ("propietario", "administrador")


def es_propietario():
    return obtener_rol() == "propietario"


# Permisos funcionales: el rol general no equivale al puesto institucional.
# Los identificadores corresponden a las secciones [usuarios.*] de secrets.toml.
REVISORES_BORRADORES = frozenset({"jhon", "john", "alejandra"})
USUARIOS_REVISION_FINAL = frozenset({"alondra"})


def identificador_sesion():
    return str(st.session_state.get("usuario_id") or "").strip().lower()


def puede_gestionar_borradores():
    """John y Alejandra reciben/corrigen borradores; propietario da soporte."""
    return bool(st.session_state.get("autenticado")) and (
        es_propietario() or (
            obtener_rol() == "administrador"
            and identificador_sesion() in REVISORES_BORRADORES
        )
    )


def puede_realizar_revision_final():
    """La aprobación institucional corresponde solo a la Tte. Alondra."""
    return bool(st.session_state.get("autenticado")) and (
        obtener_rol() == "administrador"
        and identificador_sesion() in USUARIOS_REVISION_FINAL
    )


def exigir_permiso_borradores():
    if not puede_gestionar_borradores():
        raise PermissionError("Solo John y Alejandra pueden gestionar la revisión de borradores.")


def exigir_permiso_revision_final():
    if not puede_realizar_revision_final():
        raise PermissionError("La revisión final está reservada a la Tte. Alondra.")


def descripcion_acceso():
    if es_propietario():
        return "Propietario técnico · administración del sistema"
    if puede_realizar_revision_final():
        return "Administradora · revisión final institucional"
    if puede_gestionar_borradores():
        return "Administrador/a · recepción y corrección de borradores"
    if es_administrador():
        return "Administrador/a · gestión documental"
    return "Auxiliar · seguimiento de oficios asignados"


def responsable_de_sesion():
    return str(st.session_state.get("responsable_oficios", "")).strip()


def puede_editar_responsable(responsable):
    if not st.session_state.get("autenticado", False):
        return False
    if puede_administrar_oficios_actual():
        return True
    asignado = responsable_de_sesion()
    return bool(asignado) and normalizar_texto(asignado) == normalizar_texto(responsable)


# SETF: la administración es explícita por cuenta, NO por tener rol DG.
# Si los IDs de Secrets fueran distintos, declarar los IDs exactos en
# [setf] administradores_documentales = ["arturo", "francely", ...].
ADMINISTRADORES_SETf_PREDETERMINADOS = frozenset({"arturo", "francely", "alondra", "adan", "daniel"})


def es_administrador_documental_setf():
    if not st.session_state.get("autenticado", False):
        return False
    if _area_activa_validada() != "setf":
        return False
    configurados = st.secrets.get("setf", {}).get("administradores_documentales", None)
    autorizados = ({str(x).strip().lower() for x in configurados}
                   if isinstance(configurados, (list, tuple)) else ADMINISTRADORES_SETf_PREDETERMINADOS)
    return identificador_sesion() in autorizados


def puede_administrar_oficios_actual():
    if _area_activa_validada() == "setf":
        return es_administrador_documental_setf()
    return es_administrador()


def exigir_permiso_edicion(responsable):
    if not puede_editar_responsable(responsable):
        raise PermissionError("Solo puedes modificar oficios asignados a tu cuenta.")


def exigir_sesion():
    if not st.session_state.get("autenticado", False):
        raise PermissionError("Debes iniciar sesión para realizar esta operación.")


def navegar_a(destino):
    """Cambia de módulo y actualiza la interfaz sin perder la sesión."""
    destinos = {"home", "oficios", "diseno", "pendientes", "urgentes", "firmados", "minutas", "reuniones"}
    if destino not in destinos:
        raise ValueError("Módulo de navegación no reconocido.")
    if destino == "reuniones" and _area_activa_validada() != "setf":
        raise PermissionError("Gestión de reuniones está disponible únicamente para SETF.")
    st.session_state.menu_activo = destino
    st.rerun()


# =========================================================
# SESIÓN PERSISTENTE (12 HORAS, COOKIE CIFRADA)
# =========================================================
# Requiere streamlit-cookies-manager y una clave privada estable en Secrets:
# [auth]
# cookie_secret = "CLAVE_ALEATORIA_DE_32_CARACTERES_O_MAS"
# Nunca incluir esta clave en GitHub.
if not hasattr(st, "cache"):
    st.cache = st.cache_resource
from streamlit_cookies_manager import EncryptedCookieManager


def _clave_sesion():
    try:
        clave = str(st.secrets["auth"]["cookie_secret"])
    except (KeyError, FileNotFoundError, TypeError):
        raise RuntimeError(
            'Configura [auth] cookie_secret en .streamlit/secrets.toml y '
            'en los Secrets de Streamlit Cloud para habilitar la sesión persistente.'
        )
    if len(clave) < 32:
        raise RuntimeError('La clave [auth].cookie_secret debe tener al menos 32 caracteres.')
    return clave


_clave_privada_sesion = _clave_sesion()
_cookies_sesion = EncryptedCookieManager(
    prefix="control_trazo_v1_", password=_clave_privada_sesion
)
if not _cookies_sesion.ready():
    st.stop()


def _firma_token(usuario, vence, clave):
    datos = f"{usuario}|{vence}".encode("utf-8")
    return hmac.new(clave.encode("utf-8"), datos, hashlib.sha256).hexdigest()


def _crear_token(usuario):
    # Se invalida al cambiar la contraseña del usuario en Secrets.
    datos = st.secrets["usuarios"][usuario]
    clave = _clave_privada_sesion + str(datos["password"])
    vence = int(time.time()) + 12 * 60 * 60
    return f"{usuario}|{vence}|{_firma_token(usuario, vence, clave)}"


def _recuperar_sesion():
    if st.session_state.get("autenticado"):
        return
    token = str(_cookies_sesion.get("sesion", ""))
    try:
        usuario, vence_str, firma = token.split("|", 2)
        vence = int(vence_str)
        if not usuario or not (int(time.time()) < vence <= int(time.time()) + 12 * 3600):
            return
        datos = st.secrets["usuarios"][usuario]
        clave = _clave_privada_sesion + str(datos["password"])
        if not hmac.compare_digest(firma, _firma_token(usuario, vence, clave)):
            return
        rol = str(datos.get("rol", "auxiliar")).strip().lower()
        if rol not in ROLES_VALIDOS:
            return
        st.session_state.autenticado = True
        st.session_state.usuario_id = usuario
        st.session_state.usuario_actual = datos["nombre"]
        st.session_state.rol = rol
        st.session_state.cargo_institucional = str(datos.get("cargo", ""))
        st.session_state.responsable_oficios = str(datos.get("responsable", "")).strip()
    except (ValueError, KeyError, TypeError):
        return


_recuperar_sesion()

# =========================================================
# AUTENTICACIÓN
# =========================================================

def mostrar_login():
    st.markdown(
        """
        <h4 style="text-align:center; color:#B08A4A; letter-spacing:3px;">
            INFRAESTRUCTURA FERROVIARIA
        </h4>
        <h1 style="text-align:center; color:#10283B;">
            CONTROL DOCUMENTAL
        </h1>
        <p style="text-align:center; color:#667784;">
            Acceso al sistema
        </p>
        """,
        unsafe_allow_html=True
    )

    col_izquierda, col_centro, col_derecha = st.columns(
        [1.2, 1, 1.2]
    )

    with col_centro:

        usuario = st.text_input(
            "Usuario",
            key="login_usuario"
        )

        contrasena = st.text_input(
            "Contraseña",
            type="password",
            key="login_contrasena"
        )

        if st.button(
            "Iniciar sesión",
            type="primary",
            use_container_width=True,
            key="login_boton"
        ):
            try:
                identificador = usuario.strip().lower()
                datos_usuario = st.secrets["usuarios"][identificador]
                if contrasena == datos_usuario["password"]:
                    rol_configurado = str(datos_usuario.get("rol", "auxiliar")).strip().lower()
                    if rol_configurado not in ROLES_VALIDOS:
                        st.error("El rol de esta cuenta no es válido. Contacta al responsable del sistema.")
                        return
                    st.session_state.autenticado = True
                    st.session_state.usuario_id = identificador
                    st.session_state.usuario_actual = datos_usuario["nombre"]
                    st.session_state.rol = rol_configurado
                    st.session_state.cargo_institucional = str(datos_usuario.get("cargo", ""))
                    st.session_state.responsable_oficios = str(datos_usuario.get("responsable", "")).strip()
                    st.session_state.menu_activo = "home"
                    st.session_state.pop("_ultima_area_datos", None)
                    st.session_state.pop("pend_bandeja", None)
                    _cookies_sesion["sesion"] = _crear_token(identificador)
                    _cookies_sesion.save()
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")
            except KeyError:
                st.error("Usuario o contraseña incorrectos.")


def mostrar_sesion():
    if st.session_state.get("menu_activo") in ("oficios", "pendientes", "urgentes", "firmados", "minutas", "reuniones"):
        return  # Sesión y cierre están en la barra institucional del módulo.
    col_sesion1, col_sesion2 = st.columns([6, 1])
    with col_sesion2:
        rol = st.session_state.get("rol", "auxiliar")
        st.caption(
           f"Sesión: {st.session_state.usuario_actual} | "
           f"{rol.capitalize()}"
        )
        if st.button(
            "Cerrar sesión",
            key=f"cerrar_sesion_{st.session_state.menu_activo}",
            use_container_width=True
        ):
            # Invalidar primero la cookie para impedir reingreso automático.
            _cookies_sesion["sesion"] = ""
            _cookies_sesion.save()
            st.session_state.autenticado = False
            st.session_state.usuario_actual = None
            st.session_state.usuario_id = None
            st.session_state.rol = "auxiliar"
            st.session_state.cargo_institucional = ""
            st.session_state.responsable_oficios = ""
            st.session_state.menu_activo = "home"
            st.session_state.pop("area_actual", None)
            st.session_state.pop("pend_bandeja", None)
            st.rerun()


# =========================================================
# BLOQUEO DE ACCESO
# =========================================================

if not st.session_state.autenticado:
    mostrar_login()
    st.stop()


# =========================================================
# SEPARACIÓN DE ÁREAS — ANTES DE CARGAR DISEÑO GEOMÉTRICO
# =========================================================
# Los IDs son las claves [usuarios.<id>] en secrets.toml, NO nombres mostrados.
# Solo estas dos cuentas pueden cambiar de área.
USUARIOS_DOS_AREAS = frozenset({"alondra", "arturo"})
AREAS_DISPONIBLES = {"diseno": "Diseño Geométrico", "setf": "Sistemas Eléctricos de Tracción Ferroviaria"}


def areas_autorizadas():
    exigir_sesion()
    usuario = identificador_sesion()
    if usuario in USUARIOS_DOS_AREAS:
        return ("diseno", "setf")
    # La adscripción solo la controla secrets.toml, nunca un parámetro de URL
    # ni una selección hecha por el navegador.
    try:
        area = str(st.secrets["usuarios"][usuario].get("area", "diseno")).strip().lower()
    except (KeyError, TypeError):
        area = "diseno"
    if area not in AREAS_DISPONIBLES:
        raise ValueError("Área no válida para esta cuenta. Revisa [usuarios.*].area en Secrets.")
    return (area,)


autorizadas = areas_autorizadas()
if st.session_state.get("area_actual") not in autorizadas:
    st.session_state.area_actual = None if len(autorizadas) == 2 else autorizadas[0]


def cerrar_sesion_selector():
    _cookies_sesion["sesion"] = ""
    _cookies_sesion.save()
    for clave, valor in {
        "autenticado": False, "usuario_actual": None, "usuario_id": None,
        "rol": "auxiliar", "cargo_institucional": "", "responsable_oficios": "",
        "area_actual": None, "menu_activo": "home",
    }.items():
        st.session_state[clave] = valor
    st.session_state.pop("pend_bandeja", None)
    st.rerun()


def _imagen_selector(nombre):
    """Codifica imágenes locales; si faltan, mantiene el selector utilizable."""
    ruta = Path(__file__).resolve().parent / "assets" / nombre
    if not ruta.is_file():
        return ""
    mime = "image/png" if ruta.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(ruta.read_bytes()).decode('ascii')}"


def mostrar_selector_areas():
    fondo = _imagen_selector("fondo_selector.png")
    foto_diseno = _imagen_selector("tarjeta_diseno.png")
    foto_setf = _imagen_selector("tarjeta_setf.png")
    logo = _imagen_selector("logo_selector.png")
    # Identificación de versión y comprobación de recursos SOLO en el selector.
    # Si aparece este código, Streamlit está ejecutando el app.py correcto.
    recursos = {
        "fondo_selector.png": fondo,
        "tarjeta_diseno.png": foto_diseno,
        "tarjeta_setf.png": foto_setf,
        "logo_selector.png": logo,
    }
    faltantes = [nombre for nombre, contenido in recursos.items() if not contenido]
    if faltantes:
        st.warning(
            "Faltan imágenes en la carpeta assets junto a este app.py: "
            + ", ".join(faltantes)
            + ". Carpeta esperada: "
            + str(Path(__file__).resolve().parent / "assets")
        )

    # CSS exclusivo del selector; el resto del sistema no recibe estos estilos.
    fondo_css = (f'linear-gradient(90deg,rgba(247,249,250,.74),rgba(250,251,251,.84)),url("{fondo}")'
                 if fondo else 'linear-gradient(130deg,#eef3f6,#fafafa)')
    st.markdown(f"""
    <style>
    [data-testid="stAppViewContainer"] {{
       background-image:{fondo_css} !important;
       background-position:center center !important;
       background-size:cover !important;
       background-attachment:fixed !important;
    }}
    [data-testid="stHeader"] {{background:transparent!important;}}
    #MainMenu, footer {{visibility:hidden;}}
    .block-container {{padding-top:.35rem!important;padding-bottom:0!important;max-width:1480px!important;}}
    [data-testid="stAppViewContainer"] {{overflow-y:auto;}}
    @media(min-width:1000px) and (min-height:700px) {{
       [data-testid="stAppViewContainer"] {{overflow-y:hidden!important;}}
    }}
    [data-testid="stHeader"] {{height:0!important;min-height:0!important;}}
    [data-testid="stToolbar"] {{display:none!important;}}
    [data-testid="stMainBlockContainer"] > div {{gap:0!important;}}
    /* Compactamos únicamente la portada sin afectar los módulos de oficios. */
    /* La franja se extiende a toda la pantalla, pero solo en este selector. */
    [data-testid="stAppViewContainer"]::before {{
       content:"";position:fixed;top:0;left:0;right:0;height:100px;
       background:linear-gradient(110deg,#102235,#20374a);z-index:0;
       border-bottom:1px solid rgba(200,164,96,.3);
    }}
    .selector-cabecera {{height:72px;display:flex;align-items:center;gap:18px;color:white;position:relative;z-index:1;transform:translateY(-22px);}}
    .selector-logo {{width:76px;height:62px;object-fit:contain;}}
    .selector-marca {{border-left:1px solid #caa45e;padding-left:20px;line-height:1.1}}
    .selector-marca strong {{font-size:23px;letter-spacing:1.4px;display:block;color:#fff}}
    .selector-marca span {{font-size:11px;letter-spacing:3.5px;display:block;color:#d5dce3;margin-top:8px}}
    .selector-sesion {{color:#e7edf3;text-align:right;padding-top:0;font-size:12px;transform:translateY(-22px);}}
    .selector-sesion strong {{font-size:15px;color:white;display:block;margin-top:4px}}
    .selector-hero {{text-align:center;margin:28px auto 42px;}}
    .selector-eyebrow {{color:#a87b34;letter-spacing:3.2px;font-size:12px;font-weight:800}}
    .selector-rule {{width:46px;height:2px;background:#b8893e;margin:10px auto}}
    .selector-titulo {{color:#10283b;font-size:clamp(27px,3.2vw,42px);font-weight:850;letter-spacing:-1px;line-height:1.13}}
    .selector-subtitulo {{color:#657688;font-size:15px;margin-top:9px}}
    .selector-card {{overflow:hidden;background:rgba(255,255,255,.96);
       border:1px solid #d7dce0;border-radius:20px;
       box-shadow:0 10px 28px rgba(13,33,49,.15);height:316px;
       transition:transform .18s ease,box-shadow .18s ease;}}
    .selector-card:hover {{transform:translateY(-4px);box-shadow:0 20px 42px rgba(13,33,49,.19)}}
    .selector-card.diseno {{border-color:#c89b51;}}
    .selector-foto {{height:126px;position:relative;background-color:#294359;background-size:cover;background-position:center;}}
    .selector-foto::after {{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(13,35,54,.55),rgba(18,38,57,.12));}}
    .selector-etiqueta {{position:absolute;top:17px;left:18px;z-index:1;color:#fff;
       background:rgba(21,36,51,.7);padding:8px 13px;border-radius:7px;
       font-size:12px;letter-spacing:1px;font-weight:750;}}
    .diseno .selector-etiqueta {{background:#d3a75d;color:#112a3d;}}
    .selector-cuerpo {{padding:24px 24px 12px;min-height:166px;position:relative;}}
    .selector-icono {{position:absolute;top:-25px;left:24px;width:52px;height:52px;
       display:flex;align-items:center;justify-content:center;
       background:#172f46;border-radius:13px;color:#d4a656;border:2px solid #d1a358;
       font-size:27px;box-shadow:0 5px 15px rgba(9,28,44,.25);}}
    .selector-nombre {{font-size:21px;font-weight:820;color:#11283e;line-height:1.18;margin-top:10px;min-height:49px}}
     .selector-descripcion {{font-size:13px;color:#637485;line-height:1.55;margin-top:5px}}
    .selector-pie {{font-size:11px;color:#637585;text-align:center;margin:10px 0 3px;}}
    /* Botones auténticos de Streamlit: conservan toda la interacción Python. */
    [class*="st-key-seleccionar_diseno"] button,
    [class*="st-key-seleccionar_setf"] button {{
       min-height:48px!important;border-radius:9px!important;font-size:14px!important;
       font-weight:750!important;transition:all .2s ease!important;margin-top:2px!important;
    }}
    [class*="st-key-seleccionar_diseno"] button {{background:#c79a4d!important;color:white!important;border:1px solid #bd9043!important}}
    [class*="st-key-seleccionar_diseno"] button:hover {{background:#b78b41!important;}}
    [class*="st-key-seleccionar_setf"] button {{background:rgba(255,255,255,.94)!important;color:#142c40!important;border:1px solid #9caebc!important}}
    [class*="st-key-seleccionar_setf"] button:hover {{background:#edf2f5!important;}}
    [class*="st-key-salir_selector"] {{display:flex;align-items:center;min-height:104px;transform:translateY(-22px);}}
    [class*="st-key-salir_selector"] button {{background:transparent!important;color:#fff!important;
       border:1px solid #cca75f!important;border-radius:9px!important;min-height:43px!important;}}
    [class*="st-key-salir_selector"] button:hover {{background:#334b60!important;color:#fff!important;}}
    @media(max-width:900px) {{
       .selector-marca strong {{font-size:17px;}}.selector-marca span {{font-size:9px;}}
       .selector-logo {{width:55px;}}.selector-hero {{margin-top:22px;}}
       .selector-card {{height:290px;}}.selector-foto {{height:115px;}}
       .selector-nombre {{font-size:19px;}}.selector-cuerpo {{padding-left:17px;padding-right:17px;}}
    }}
    @media(min-width:901px) and (max-height:760px) {{
       .selector-hero {{margin:16px auto 27px;}}
       .selector-titulo {{font-size:34px;}}
       .selector-card {{height:258px;}}
       .selector-foto {{height:106px;}}
       .selector-cuerpo {{padding-top:21px;}}
       .selector-pie {{margin-top:5px;}}
    }}
    @media(max-width:640px) {{
       .selector-marca strong {{font-size:13px;}}
       .selector-marca span {{font-size:8px;letter-spacing:1px;}}
       .selector-hero {{margin:15px auto 22px;}}
       .selector-card {{height:auto;min-height:235px;}}
       .selector-foto {{height:110px;}}
       .selector-cuerpo {{min-height:125px;}}
    }}
    </style>
    """, unsafe_allow_html=True)

    nav_izq, nav_usuario, nav_salir = st.columns([4.3, 1.55, 1.2], gap="medium", vertical_alignment="center")
    with nav_izq:
        marca_logo = f'<img class="selector-logo" src="{logo}" alt="Logotipo">' if logo else '<span style="font-size:34px;color:#d3a75d">▰</span>'
        st.markdown(f"""<div class="selector-cabecera">{marca_logo}<div class="selector-marca">
            <strong>CONTROL DOCUMENTAL</strong><span>INFRAESTRUCTURA FERROVIARIA</span>
            </div></div>""", unsafe_allow_html=True)
    with nav_usuario:
        # Nombre institucional del selector, independiente del tratamiento en Secrets.
        usuario_actual_id = identificador_sesion()
        if usuario_actual_id == "alondra" or any(
            palabra in str(st.session_state.get("usuario_actual") or "").casefold()
            for palabra in ("alondra", "barajas")
        ):
            nombre = "Tte. Alondra Barajas"
        else:
            nombre = html.escape(str(st.session_state.get("usuario_actual") or "Usuario"))
        # Chrome puede traducir "Tte." como "Señora" incluso cuando la UI ya está en español.
        # Restringir la traducción en el propio elemento del nombre, no en toda la página.
        st.markdown(
            f'<div class="selector-sesion">Sesión:'
            f'<strong class="notranslate" translate="no" lang="es-MX">{nombre}</strong>'
            f'</div>',
            unsafe_allow_html=True,
        )
    with nav_salir:
        if st.button("↪ Cerrar sesión", key="salir_selector", use_container_width=True):
            cerrar_sesion_selector()

    st.markdown("""<div class="selector-hero">
      <div class="selector-eyebrow">CONTROL DOCUMENTAL · ACCESO INSTITUCIONAL</div>
      <div class="selector-rule"></div>
      <div class="selector-titulo">Selecciona tu área de trabajo</div>
      <div class="selector-subtitulo">Elige la mesa de trabajo que deseas consultar.</div>
      </div>""", unsafe_allow_html=True)

    espacio_izq, columna_diseno, columna_setf, espacio_der = st.columns([.4, 4.8, 4.8, .4], gap="large")
    with columna_diseno:
        foto = f'background-image:url(\'{foto_diseno}\')' if foto_diseno else 'background:linear-gradient(115deg,#1a374f,#a77d49)'
        st.markdown(f"""<div class="selector-card diseno">
          <div class="selector-foto" style="{foto}"><span class="selector-etiqueta">✓ &nbsp; ÁREA ACTIVA</span></div>
          <div class="selector-cuerpo"><div class="selector-icono">◩</div>
          <div class="selector-nombre">Diseño Geométrico</div>
          <div class="selector-descripcion">Registro y seguimiento de oficios, revisión y firmas.</div>
          </div></div>""", unsafe_allow_html=True)
        if st.button("Ingresar a Diseño Geométrico  →", key="seleccionar_diseno", type="primary", use_container_width=True):
            st.session_state.area_actual = "diseno"
            st.session_state.menu_activo = "home"
            st.rerun()
    with columna_setf:
        foto = f'background-image:url(\'{foto_setf}\')' if foto_setf else 'background:linear-gradient(115deg,#314b60,#92a2aa)'
        st.markdown(f"""<div class="selector-card setf">
          <div class="selector-foto" style="{foto}"><span class="selector-etiqueta">✓ &nbsp; ÁREA ACTIVA</span></div>
          <div class="selector-cuerpo"><div class="selector-icono" style="color:white;border-color:#dce5ed">ϟ</div>
          <div class="selector-nombre">Sistemas Eléctricos de<br>Tracción Ferroviaria</div>
          <div class="selector-descripcion">Registro y seguimiento documental de Sistemas Eléctricos.</div>
          </div></div>""", unsafe_allow_html=True)
        if st.button("Ingresar a Sistemas Eléctricos  →", key="seleccionar_setf", use_container_width=True):
            st.session_state.area_actual = "setf"
            st.session_state.menu_activo = "home"
            st.rerun()
    st.markdown('<div class="selector-pie">ⓘ &nbsp; La selección de áreas está disponible únicamente para Alondra y Arturo.<br><span style="font-size:10px;opacity:.65;letter-spacing:.8px">INTERFAZ INSTITUCIONAL · VERSIÓN VISUAL 2.4</span></div>', unsafe_allow_html=True)


if len(autorizadas) == 2 and st.session_state.area_actual is None:
    mostrar_selector_areas()
    st.stop()

if len(autorizadas) == 2 and st.session_state.menu_activo not in ("home", "oficios", "pendientes", "urgentes", "firmados", "minutas", "reuniones"):
    espacio, cambiar = st.columns([5,1])
    with cambiar:
        if st.button("⇄ Cambiar de área", key="cambiar_area_institucional", use_container_width=True):
            st.session_state.area_actual = None
            st.session_state.menu_activo = "home"
            st.session_state.pop("pend_bandeja", None)
            st.rerun()

# SETF utiliza los mismos módulos reales que Diseño Geométrico.
# La separación se aplica antes de toda operación sobre Sheets/Drive.
# Nunca usar automáticamente el libro ni la carpeta de DG como alternativa.
if st.session_state.area_actual == "setf":
    try:
        configuracion_setf = dict(st.secrets["setf"])
        id_setf = str(configuracion_setf.get("spreadsheet_id", "")).strip()
        carpeta_setf = str(configuracion_setf.get("drive_borradores_folder_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", id_setf):
            raise ValueError("Configura [setf].spreadsheet_id con el ID del libro de SETF.")
        if id_setf == "1jweDu5kKkF2onZArr8-QV_CgwO1tPzfMBtdkEeHRQgY":
            raise ValueError("El libro de SETF no puede ser el mismo que el de Diseño Geométrico.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", carpeta_setf):
            raise ValueError("Configura [setf].drive_borradores_folder_id con una carpeta de SETF independiente.")
        carpeta_dg = str(st.secrets.get("drive_borradores_folder_id", "")).strip()
        if carpeta_dg and carpeta_setf == carpeta_dg:
            raise ValueError("La carpeta de Drive de SETF debe ser diferente de la de DG.")
    except (KeyError, TypeError, FileNotFoundError) as error:
        st.error("La configuración de SETF aún no está disponible. No se accederá a los datos de DG.")
        st.code('[setf]\nspreadsheet_id = "ID_DEL_GOOGLE_SHEETS_SETf"\ndrive_borradores_folder_id = "ID_CARPETA_DRIVE_SETF"', language="toml")
        st.stop()
    except ValueError as error:
        st.error(str(error))
        st.stop()

# Evitar que una selección, un PDF o un documento preparado en una mesa se
# conserve visible al cambiar a la otra en la misma sesión del navegador.
_area_previa = st.session_state.get("_ultima_area_datos")
if _area_previa != st.session_state.area_actual:
    claves_sesion_documental = {
        "oficios_marcados", "pdf_reporte_generado", "exp_identidad",
        "exp_editando", "exp_guardado", "eliminacion_pendiente", "eliminacion_lote_pendiente",
        "pend_bandeja", "pend_expediente_abierto", "pend_guardado",
        "firmados_kpi_filtro", "buscar_firmados", "buscar_urgentes",
        "oficio_guardado", "mostrar_registro", "firma_editor_actual",
        "registro_guardado", "version_formulario", "epoch_editor",
        "firm_orden", "urgentes_estado",
    }
    claves_prefijo = ("doc_", "ver_pdf_", "preview_", "pdf_", "word_", "tte_dictamen_", "docx_", "reun_")
    for _clave in list(st.session_state.keys()):
        if _clave in claves_sesion_documental or _clave.startswith(claves_prefijo):
            st.session_state.pop(_clave, None)
    st.session_state._ultima_area_datos = st.session_state.area_actual


def _area_activa_validada():
    """Frontera obligatoria para operaciones con datos."""
    area = str(st.session_state.get("area_actual") or "")
    if not st.session_state.get("autenticado") or area not in areas_autorizadas():
        raise PermissionError("La sesión no tiene permisos para consultar esta área.")
    return area


def _rotulos_area(contenido):
    """Personaliza solo los rótulos visibles; no toca las reglas de negocio DG."""
    if _area_activa_validada() != "setf":
        return contenido
    if not isinstance(contenido, str):
        return contenido
    return (contenido
        .replace("MESA DE DISEÑO GEOMÉTRICO DE VÍA FÉRREA", "MESA DE SISTEMAS ELÉCTRICOS DE TRACCIÓN FERROVIARIA")
        .replace("MESA DE DISEÑO GEOMÉTRICO", "MESA DE SISTEMAS ELÉCTRICOS")
        .replace("Mesa de Diseño Geométrico", "Mesa de Sistemas Eléctricos")
        .replace("DISEÑO GEOMÉTRICO ·", "SISTEMAS ELÉCTRICOS ·")
        .replace("DISEÑO GEOMÉTRICO /", "SISTEMAS ELÉCTRICOS /")
        .replace("<strong>DISEÑO GEOMÉTRICO</strong>", "<strong>SISTEMAS ELÉCTRICOS</strong>")
        .replace("TRAZO · ALINEAMIENTO · RASANTE", "ENERGÍA · CATENARIA · TRACCIÓN")
        .replace("TRAZO &nbsp; / &nbsp; ALINEAMIENTO &nbsp; / &nbsp; RASANTE", "ENERGÍA &nbsp; / &nbsp; CATENARIA &nbsp; / &nbsp; TRACCIÓN")
        .replace("TRAZO / ALINEAMIENTO / RASANTE", "ENERGÍA / CATENARIA / TRACCIÓN"))

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"


# =========================================================
# CONEXIÓN A GOOGLE SHEETS (HÍBRIDA: LOCAL + SECRETS)
# =========================================================

def conectar_google_sheets():
    return conectar_google_sheets_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def conectar_google_sheets_por_area(area):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    ruta = Path(__file__).parent / "credentials.json"

    if ruta.exists():
        with open(ruta, "r", encoding="utf-8") as archivo:
            credenciales_json = json.load(archivo)
    else:
        try:
            credenciales_json = dict(st.secrets["gcp_service_account"])
        except (KeyError, FileNotFoundError) as error:
            raise RuntimeError(
                "No se encontraron las credenciales de Google Sheets."
            ) from error

    credenciales = Credentials.from_service_account_info(
        credenciales_json,
        scopes=scopes
    )
    cliente = gspread.authorize(credenciales)
    # La cuota se comparte entre las sesiones que usan esta cuenta de servicio.
    # Reintentos acotados para respuestas 429 y errores temporales del servidor.
    # Compatibilidad con gspread antiguo (Client.request) y reciente
    # (Client.http_client.request). No acceder a http_client si no existe.
    transporte = getattr(cliente, "http_client", None)
    if transporte is None:
        transporte = cliente
    solicitud_original = getattr(transporte, "request", None)
    if solicitud_original is None:
        raise RuntimeError(
            "La versión instalada de gspread no ofrece un método request compatible."
        )

    def solicitud_con_reintentos(*args, **kwargs):
        """Reintentos limitados solo para lecturas seguras.

        Nunca se reintentan escrituras ante fallos de red: el servidor pudo
        haberlas aplicado aunque el cliente no recibiera la respuesta.
        """
        # No pasar timeout a gspread.Client.request: algunas versiones no lo admiten.
        # Los reintentos de lectura siguen funcionando ante errores de red.
        metodo = str(kwargs.get("method", args[0] if args else "GET")).upper()
        es_lectura = metodo == "GET"
        max_intentos = 3 if es_lectura else 1
        for intento in range(max_intentos):
            try:
                return solicitud_original(*args, **kwargs)
            except gspread.exceptions.APIError as error:
                codigo = getattr(getattr(error, "response", None), "status_code", None)
                recuperable = codigo in (429, 500, 502, 503, 504)
                if not recuperable or intento == max_intentos - 1:
                    raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if intento == max_intentos - 1:
                    raise
            time.sleep(min(1.5 * (2 ** intento) + random.uniform(0, 0.5), 5))

    transporte.request = solicitud_con_reintentos

    try:
        spreadsheet_id = (
            "1jweDu5kKkF2onZArr8-QV_CgwO1tPzfMBtdkEeHRQgY"
            if area == "diseno" else str(st.secrets["setf"]["spreadsheet_id"]).strip()
        )
        documento = cliente.open_by_key(spreadsheet_id)
        hoja = documento.worksheet("OFICIOS")
        
    except gspread.exceptions.APIError as e:
        raise RuntimeError(
            f"Error al abrir el archivo: {e.response.text}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Error en la conexión: {e}") from e

    return hoja



# =========================================================
# BLOQUE 1: BORRADORES WORD EN GOOGLE DRIVE + SHEETS
# =========================================================
COLUMNAS_BORRADORES = [
    "Folio", "No. de oficio", "Responsable", "Version", "Nombre",
    "Drive ID", "URL", "Etapa", "Usuario", "Fecha registro",
]
ETAPA_BORRADOR = "Borrador cargado"
ETAPA_ENVIADO = "Pendiente de revisión inicial"
ETAPA_TTE = "Pendiente de revisión de la Tte."


def _descripcion_segura_error_oauth(error):
    """Clasifica errores de Google sin exponer tokens, IDs o secretos."""
    detalle = str(error).lower()
    if "invalid_grant" in detalle:
        return (
            "invalid_grant: Google indica que el refresh_token caducó, fue revocado "
            "o no corresponde a una autorización vigente. Comprueba además si la "
            "pantalla de consentimiento OAuth continúa en modo Testing."
        )
    if "invalid_client" in detalle or "unauthorized_client" in detalle:
        return (
            "invalid_client: el client_id y el client_secret no corresponden "
            "al mismo cliente OAuth que emitió el refresh_token, o ese cliente "
            "ya no está habilitado."
        )
    if "invalid_scope" in detalle:
        return (
            "invalid_scope: la autorización creada no incluye el alcance de "
            "Google Drive requerido por la aplicación."
        )
    if "invalid_rapt" in detalle:
        return "invalid_rapt: Google exige una nueva autenticación de la cuenta."
    if "access_denied" in detalle or "admin_policy_enforced" in detalle:
        return "acceso denegado: revisa las políticas de la cuenta de Google."
    return (
        "sin código OAuth identificable. Revisa los parámetros de autorización, "
        "la conexión a Google y la configuración del proyecto en Google Cloud "
        f"(tipo de error: {type(error).__name__})."
    )


def _credenciales_drive():
    """Obtiene credenciales OAuth de Drive sin exponer datos sensibles.

    Prioriza siempre los Secrets de Streamlit frente al JSON local, que sirve
    solo como alternativa en entornos de desarrollo. Así se evita que un JSON
    antiguo incluido accidentalmente en el servidor reemplace los Secrets.
    """
    alcance = ["https://www.googleapis.com/auth/drive"]
    token_local = BASE_DIR / "drive_oauth_token.json"

    seccion_secreta = None
    try:
        seccion_secreta = st.secrets.get("drive_oauth")
    except (KeyError, FileNotFoundError):
        pass

    if seccion_secreta:
        datos = dict(seccion_secreta)
        origen = "Secrets de Streamlit Cloud"
    elif token_local.is_file():
        datos = json.loads(token_local.read_text(encoding="utf-8"))
        origen = "drive_oauth_token.json local"
    else:
        raise RuntimeError(
            "No se encontraron credenciales de Google Drive. Configura "
            "[drive_oauth] en los Secrets privados de Streamlit Cloud con "
            "refresh_token, client_id, client_secret y token_uri."
        )

    faltantes = [
        clave for clave in ("refresh_token", "client_id", "client_secret", "token_uri")
        if not str(datos.get(clave, "")).strip()
    ]
    if faltantes:
        raise RuntimeError(
            f"Configuración OAuth incompleta en {origen}; faltan los campos: "
            + ", ".join(faltantes)
            + ". El Word no se ha guardado."
        )

    try:
        credenciales = OAuthCredentials.from_authorized_user_info(
            datos, scopes=alcance
        )
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError(
            f"No se pudo interpretar la configuración OAuth de {origen}. "
            "Revisa el formato del JSON y los Secrets, sin publicar sus valores. "
            "El Word no se ha guardado."
        ) from error

    if not credenciales.valid:
        try:
            credenciales.refresh(GoogleAuthRequest())
        except Exception as error:
            explicacion = _descripcion_segura_error_oauth(error)
            raise RuntimeError(
                f"Google Drive rechazó la renovación OAuth usando {origen}. "
                f"Diagnóstico: {explicacion} "
                "El Word no se ha guardado ni se ha registrado una nueva versión. "
                "No publiques tokens ni contraseñas en GitHub ni en capturas."
            ) from error

        # Solo se reescribe un token local si realmente se obtuvo desde él.
        if origen == "drive_oauth_token.json local":
            token_local.write_text(credenciales.to_json(), encoding="utf-8")
    return credenciales

def _carpeta_raiz_borradores():
    """Obtiene el ID de carpeta desde Streamlit o secrets.toml local.

    La lectura directa del archivo permite detectar cambios locales aun si
    Streamlit todavía conserva una versión anterior de sus secretos.
    """
    if _area_activa_validada() == "setf":
        carpeta_setf = str(st.secrets["setf"].get("drive_borradores_folder_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", carpeta_setf):
            raise ValueError("La carpeta de SETF no está configurada correctamente.")
        if carpeta_setf == str(st.secrets.get("drive_borradores_folder_id", "")).strip():
            raise PermissionError("SETF no puede almacenar borradores dentro de la carpeta de DG.")
        return carpeta_setf
    carpeta = ""
    ruta_secrets = BASE_DIR / ".streamlit" / "secrets.toml"

    # En localhost priorizamos el archivo del proyecto, que el usuario edita.
    if ruta_secrets.is_file():
        try:
            import tomllib
            with ruta_secrets.open("rb") as archivo:
                configuracion = tomllib.load(archivo)
            carpeta = str(configuracion.get("drive_borradores_folder_id") or "").strip()
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "No se pudo leer .streamlit/secrets.toml. Comprueba que el "
                "archivo esté guardado y tenga sintaxis TOML válida."
            ) from error

    # En despliegues remotos, los secretos pueden existir solo en st.secrets.
    if not carpeta:
        try:
            carpeta = str(st.secrets.get("drive_borradores_folder_id", "") or "").strip()
        except (KeyError, FileNotFoundError):
            pass

    if not carpeta:
        raise RuntimeError(
            "No se encontró drive_borradores_folder_id. En localhost agrega "
            "drive_borradores_folder_id = \"ID_DE_LA_CARPETA\" al inicio de "
            "C:/Control_Trazo/.streamlit/secrets.toml, guarda el archivo y "
            "reinicia Streamlit."
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", carpeta):
        raise ValueError(
            "El ID de la carpeta de borradores no es válido. "
            "Pega únicamente el identificador, no la URL completa."
        )
    return carpeta


def _drive_sesion():
    return AuthorizedSession(_credenciales_drive())


def _drive_json(respuesta, operacion):
    if not respuesta.ok:
        raise RuntimeError(
            f"Google Drive rechazó {operacion} (HTTP {respuesta.status_code}): "
            + respuesta.text[:350]
        )
    return respuesta.json()


def _drive_carpeta_expediente(folio, numero):
    """Busca o crea una carpeta por expediente bajo la raíz configurada."""
    raiz = _carpeta_raiz_borradores()
    etiqueta = re.sub(r"[^A-Za-z0-9._ -]", "_", f"{folio}_{numero}")[:100]
    sesion = _drive_sesion()
    # Diagnóstico exclusivo de SETF: comprobar la carpeta con la identidad OAuth
    # real antes de intentar crear nada. No altera el comportamiento de DG.
    if _area_activa_validada() == "setf":
        identidad = ""
        try:
            respuesta_cuenta = sesion.get(
                "https://www.googleapis.com/drive/v3/about",
                params={"fields": "user(emailAddress)"}, timeout=20,
            )
            if respuesta_cuenta.ok:
                correo = str(respuesta_cuenta.json().get("user", {}).get("emailAddress", ""))
                if "@" in correo:
                    usuario, dominio = correo.rsplit("@", 1)
                    identidad = f" (cuenta OAuth: {usuario[:2]}***@{dominio})"
        except (requests.RequestException, ValueError, KeyError, TypeError):
            pass  # La comprobación de carpeta no depende del endpoint about.

        ver_raiz = sesion.get(
            f"https://www.googleapis.com/drive/v3/files/{raiz}",
            params={"fields": "id,name,mimeType,trashed,capabilities(canAddChildren)",
                    "supportsAllDrives": "true"}, timeout=25,
        )
        if ver_raiz.status_code in (403, 404):
            raise RuntimeError(
                "Google Drive no permite consultar la carpeta raíz de SETF "
                f"con las credenciales OAuth actuales{identidad}. "
                f"Respuesta HTTP {ver_raiz.status_code}. Aunque la carpeta se abra "
                "en el navegador, el token de Streamlit puede pertenecer a otra cuenta "
                "o tener permisos diferentes. Comprueba que autorizaste "
                "configurar_drive_oauth.py con el Gmail propietario de "
                "CONTROL DOCUMENTAL - SETF y que [drive_oauth] contiene el "
                "refresh_token generado para esa cuenta. No cambies el ID de la carpeta "
                "ni compartas tus tokens. No se guardó ninguna versión."
            )
        detalles_raiz = _drive_json(ver_raiz, "comprobar carpeta raíz de SETF")
        if detalles_raiz.get("trashed") or detalles_raiz.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(
                "La carpeta raíz configurada para SETF no corresponde a una carpeta "
                "de Google Drive activa. Revisa su selección en [setf] de Secrets."
            )
        if detalles_raiz.get("capabilities", {}).get("canAddChildren") is False:
            raise RuntimeError(
                "La cuenta OAuth de Streamlit tiene acceso a la carpeta SETF, "
                "pero no puede crear subcarpetas allí" + identidad + ". "
                "Revisa que esa cuenta tenga permiso de edición. No se guardó ninguna versión."
            )

    consulta = (
        f"'{raiz}' in parents and name = '{etiqueta.replace(chr(39), '')}' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    r = sesion.get("https://www.googleapis.com/drive/v3/files",
        params={"q": consulta, "fields": "files(id,name),nextPageToken",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                "pageSize": 10}, timeout=25)
    carpetas = _drive_json(r, "consultar carpetas").get("files", [])
    if carpetas:
        return carpetas[0]["id"]
    r = sesion.post("https://www.googleapis.com/drive/v3/files",
        params={"fields": "id", "supportsAllDrives": "true"},
        json={"name": etiqueta, "mimeType": "application/vnd.google-apps.folder",
              "parents": [raiz]}, timeout=25)
    return _drive_json(r, "crear carpeta del expediente")["id"]


def _hoja_borradores():
    return _hoja_borradores_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def _hoja_borradores_por_area(area):
    hoja_oficios = conectar_google_sheets_por_area(area)
    libro = hoja_oficios.spreadsheet
    try:
        hoja = libro.worksheet("BORRADORES")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="BORRADORES", rows=1000, cols=12)
        hoja.append_row(COLUMNAS_BORRADORES, value_input_option="RAW")
    encabezados = hoja.row_values(1)
    if encabezados != COLUMNAS_BORRADORES:
        raise ValueError(
            "La hoja BORRADORES tiene columnas distintas de las esperadas. "
            "No se modificó su estructura para proteger los datos existentes."
        )
    return hoja


def _leer_registros_borradores():
    return _leer_registros_borradores_por_area(_area_activa_validada())


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_borradores_por_area(area):
    return _hoja_borradores_por_area(area).get_all_records()


def _leer_filas_borradores():
    return _leer_filas_borradores_por_area(_area_activa_validada())


@st.cache_data(ttl=25, show_spinner=False)
def _leer_filas_borradores_por_area(area):
    return _hoja_borradores_por_area(area).get_all_values()


def _leer_registros_eventos():
    return _leer_registros_eventos_por_area(_area_activa_validada())


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_eventos_por_area(area):
    return _hoja_eventos_borradores_por_area(area).get_all_records()


def _leer_registros_oficios():
    return _leer_registros_oficios_por_area(_area_activa_validada())


@st.cache_data(ttl=25, show_spinner=False)
def _leer_registros_oficios_por_area(area):
    return conectar_google_sheets_por_area(area).get_all_records()


def _invalidar_cache_documental(borradores=False, eventos=False, oficios=False):
    if borradores:
        _leer_registros_borradores_por_area.clear()
        _leer_filas_borradores_por_area.clear()
    if eventos:
        _leer_registros_eventos_por_area.clear()
    if oficios:
        _leer_registros_oficios_por_area.clear()
        obtener_oficios_por_area.clear()


def _borradores_expediente(folio, numero):
    registros = _leer_registros_borradores()
    encontrados = [
        r for r in registros
        if str(r.get("Folio", "")).strip() == str(folio).strip()
        and str(r.get("No. de oficio", "")).strip() == str(numero).strip()
    ]
    return sorted(encontrados, key=lambda r: int(r.get("Version") or 0))


def _verificar_oficio_para_borrador(folio, numero):
    """Valida acceso contra la hoja actual, no contra valores del navegador."""
    hoja = conectar_google_sheets()
    encontrados = [
        r for r in _leer_registros_oficios()
        if str(r.get("Folio", "")).strip() == str(folio).strip()
        and str(r.get("No. de oficio", "")).strip() == str(numero).strip()
    ]
    if len(encontrados) != 1:
        raise ValueError("El oficio no existe o su identificación no es única.")
    exigir_permiso_edicion(encontrados[0].get("Responsable", ""))
    return encontrados[0]


def _subir_borrador_drive(archivo, folio, numero, version):
    """Carga DOCX sin sobrescribir versiones; la carpeta debe permitir escritura."""
    contenido = archivo.getvalue()
    if len(contenido) > 15 * 1024 * 1024:
        raise ValueError("El borrador no puede superar los 15 MB.")
    if not archivo.name.lower().endswith(".docx") or not contenido.startswith(b"PK"):
        raise ValueError("Selecciona un documento Word .docx válido.")
    carpeta = _drive_carpeta_expediente(folio, numero)
    nombre_seguro = re.sub(r"[^A-Za-z0-9._-]", "_", Path(archivo.name).name)
    nombre = f"{folio}_v{version:03d}_{nombre_seguro}"[:180]
    # Carga reanudable en dos pasos: evita construir multipart manualmente.
    # Paso 1: crear la sesión de subida enviando únicamente los metadatos.
    sesion = _drive_sesion()
    tipo_word = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    r = sesion.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "resumable", "fields": "id,name,webViewLink",
                "supportsAllDrives": "true"},
        json={"name": nombre, "parents": [carpeta], "mimeType": tipo_word},
        headers={"X-Upload-Content-Type": tipo_word,
                 "X-Upload-Content-Length": str(len(contenido))},
        timeout=30,
    )
    if not r.ok:
        _drive_json(r, "iniciar subida de borrador")
    url_subida = r.headers.get("Location")
    if not url_subida or not url_subida.startswith("https://www.googleapis.com/"):
        raise RuntimeError("Google Drive no devolvió una URL válida para subir el Word.")

    # Paso 2: enviar los bytes exactos del DOCX, sin codificación multipart.
    r = sesion.put(
        url_subida, data=contenido,
        headers={"Content-Type": tipo_word,
                 "Content-Length": str(len(contenido))},
        timeout=120,
    )
    respuesta = _drive_json(r, "subir borrador")
    return {"id": respuesta["id"], "nombre": nombre,
            "url": respuesta.get("webViewLink") or
                   f"https://drive.google.com/file/d/{respuesta['id']}/view"}


def _hoja_eventos_borradores():
    return _hoja_eventos_borradores_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def _hoja_eventos_borradores_por_area(area):
    """Bitácora independiente; nunca altera las columnas de BORRADORES."""
    libro = conectar_google_sheets_por_area(area).spreadsheet
    columnas = ["Folio", "No. de oficio", "Version", "Accion", "Usuario", "Fecha", "Detalle"]
    try:
        hoja = libro.worksheet("EVENTOS_BORRADORES")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="EVENTOS_BORRADORES", rows=1000, cols=7)
        hoja.append_row(columnas, value_input_option="RAW")
    if hoja.row_values(1) != columnas:
        raise ValueError("La bitácora EVENTOS_BORRADORES tiene columnas inesperadas.")
    return hoja


def _registrar_evento_borrador(folio, numero, version, accion, detalle=""):
    _hoja_eventos_borradores().append_row([
        str(folio), str(numero), int(version), accion, identificador_sesion(),
        datetime.now().astimezone().isoformat(timespec="seconds"), str(detalle)
    ], value_input_option="RAW")
    _invalidar_cache_documental(eventos=True)


def _eventos_expediente(folio, numero):
    return [r for r in _leer_registros_eventos()
            if str(r.get("Folio", "")).strip() == str(folio).strip()
            and str(r.get("No. de oficio", "")).strip() == str(numero).strip()]


ETAPA_CORRECCIONES = "Correcciones solicitadas por la Tte."
ETAPA_FIRMA = "En proceso de firma"
ETAPA_ARCHIVADO = "Firmado y archivado"
ETAPA_RETORNO_FIRMA = "Devuelto de firma para correcciones"


def _permiso_documental(responsable):
    if _area_activa_validada() == "setf":
        return bool(st.session_state.get("autenticado"))
    return puede_gestionar_borradores() or puede_realizar_revision_final() or puede_editar_responsable(responsable)


def _verificar_expediente_documental(folio, numero):
    hoja = conectar_google_sheets()
    coincidencias = [r for r in _leer_registros_oficios()
                     if str(r.get("Folio", "")).strip() == str(folio).strip()
                     and str(r.get("No. de oficio", "")).strip() == str(numero).strip()]
    if len(coincidencias) != 1:
        raise ValueError("No se encontró un oficio único con esos datos.")
    if not _permiso_documental(coincidencias[0].get("Responsable", "")):
        raise PermissionError("No tienes acceso a este expediente.")
    return coincidencias[0]


def _registrar_borrador(folio, numero, responsable, archivo, procedencia="No especificada", origen=""):
    oficio = _verificar_expediente_documental(folio, numero)
    anteriores = _borradores_expediente(folio, numero)
    etapa = str(anteriores[-1].get("Etapa", "")) if anteriores else ""
    if puede_realizar_revision_final() and not puede_gestionar_borradores():
        raise PermissionError("La revisión final no permite subir versiones Word.")
    if anteriores:
        if not (puede_gestionar_borradores() or
                (_area_activa_validada() == "setf" and
                 puede_editar_responsable(oficio.get("Responsable", "")))):
            raise PermissionError("Solo el responsable vigente o un revisor autorizado puede subir correcciones.")
        if etapa not in (ETAPA_BORRADOR, ETAPA_ENVIADO, ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA):
            raise PermissionError("El expediente está en revisión final, firma o archivado. No se puede sustituir la versión enviada.")
    version = max([int(x.get("Version") or 0) for x in anteriores], default=0) + 1
    if version > 15:
        raise ValueError("Este expediente alcanzó el máximo de 15 versiones. Contacta al administrador.")
    subido = _subir_borrador_drive(archivo, folio, numero, version)
    try:
        _hoja_borradores().append_row([
            str(folio), str(numero), str(oficio.get("Responsable", responsable)),
            version, subido["nombre"], subido["id"], subido["url"],
            ETAPA_BORRADOR, identificador_sesion(),
            datetime.now().astimezone().isoformat(timespec="seconds"),
        ], value_input_option="RAW")
    except Exception as error:
        raise RuntimeError("El Word ya está en Drive, pero falló el registro en Sheets. "
                           f"Recupéralo aquí: {subido['url']}. {error}") from error
    _invalidar_cache_documental(borradores=True)
    try:
        _registrar_evento_borrador(folio, numero, version, "Versión cargada",
                                  f"Procedencia: {procedencia}. Entregado/elaborado por: {origen or 'No indicado'}")
    except Exception as error:
        st.warning(f"La versión quedó guardada, pero falló el registro de procedencia: {error}")
    # Se guarda también una copia PDF privada e inmutable para el historial.
    # El Word original permanece porque las correcciones requieren DOCX editable.
    try:
        _archivar_pdf_borrador(
            folio, numero,
            {"Folio": str(folio), "No. de oficio": str(numero),
             "Version": version, "Nombre": subido["nombre"],
             "Drive ID": subido["id"]},
            docx_bytes=archivo.getvalue(),
        )
    except Exception as error:
        st.warning(
            "La versión Word quedó registrada, pero su copia histórica PDF está "
            "pendiente de generar. Se podrá preparar después desde el historial. "
            f"Detalle: {_mensaje_error_sheets(error)}"
        )
    return subido




# Registro independiente de oficios NUEVOS enviados a firma. Cada mesa usa su libro.
COLUMNAS_ENVIOS_FIRMA = ["Folio", "No. de oficio origen", "No. de oficio nuevo", "Asunto nuevo", "Fecha envío", "Usuario"]

@st.cache_resource(show_spinner=False)
def _hoja_envios_firma_por_area(area):
    libro = conectar_google_sheets_por_area(area).spreadsheet
    try:
        hoja = libro.worksheet("ENVIOS_FIRMA")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="ENVIOS_FIRMA", rows=1000, cols=6)
        hoja.append_row(COLUMNAS_ENVIOS_FIRMA, value_input_option="RAW")
    if hoja.row_values(1) != COLUMNAS_ENVIOS_FIRMA:
        raise ValueError("La hoja ENVIOS_FIRMA tiene una estructura distinta; no se modificaron los datos.")
    return hoja


def _leer_envios_firma():
    # Lectura fresca: los movimientos deben verse entre cuentas sin reiniciar sesión.
    return _hoja_envios_firma_por_area(_area_activa_validada()).get_all_records()


def _registrar_envio_firma(folio, numero, nuevo_numero, nuevo_asunto):
    """Registra la identidad del documento enviado antes de cambiar su etapa."""
    from zoneinfo import ZoneInfo
    nuevo_numero = str(nuevo_numero or "").strip()
    nuevo_asunto = str(nuevo_asunto or "").strip()
    if not nuevo_numero or not nuevo_asunto:
        raise ValueError("El número y el asunto del nuevo oficio son obligatorios para enviarlo a firma.")
    if len(nuevo_numero) > 180 or len(nuevo_asunto) > 1000:
        raise ValueError("El número o el asunto excede la longitud permitida.")
    hoja = _hoja_envios_firma_por_area(_area_activa_validada())
    registros = hoja.get_all_records()
    identidad = (str(folio).strip(), str(numero).strip())
    for registro in registros:
        anterior = (str(registro.get("Folio", "")).strip(),
                    str(registro.get("No. de oficio origen", "")).strip())
        if normalizar_texto(registro.get("No. de oficio nuevo", "")) == normalizar_texto(nuevo_numero):
            if anterior != identidad:
                raise ValueError("Este número de oficio nuevo ya se registró en otro expediente de esta mesa.")
            if (str(registro.get("Asunto nuevo", "")).strip() == nuevo_asunto):
                # Evita duplicar un registro idéntico si una operación posterior falló.
                return
            raise ValueError("Este número ya está asociado al expediente con otro asunto. Revisa el historial.")
    fecha = datetime.now(ZoneInfo("America/Mexico_City")).isoformat(timespec="seconds")
    hoja.append_row([str(folio), str(numero), nuevo_numero, nuevo_asunto,
                     fecha, identificador_sesion()], value_input_option="RAW")


def _cambiar_etapa_borrador(folio, numero, etapa_nueva, detalle=""):
    oficio = _verificar_expediente_documental(folio, numero)
    hoja = _hoja_borradores()
    # Lectura fresca para validar transiciones entre sesiones y evitar envíos duplicados.
    valores = hoja.get_all_values()
    coincidencias = [(i, fila) for i, fila in enumerate(valores[1:], start=2)
                     if len(fila) >= 8 and fila[0].strip() == str(folio).strip()
                     and fila[1].strip() == str(numero).strip()]
    if not coincidencias:
        raise ValueError("Primero debes cargar un Word.")
    indice, ultima = max(coincidencias, key=lambda par: int(par[1][3]))
    actual = ultima[7]
    if actual == etapa_nueva:
        raise ValueError("Esta versión ya se encuentra en esa etapa. Actualiza la pantalla para ver el estado vigente.")
    admin = puede_gestionar_borradores()
    tte = puede_realizar_revision_final()
    auxiliar = puede_editar_responsable(oficio.get("Responsable", "")) and not (admin or tte)
    permitido = (
        (actual == ETAPA_BORRADOR and etapa_nueva == ETAPA_ENVIADO and (admin or auxiliar)) or
        (actual in (ETAPA_BORRADOR, ETAPA_ENVIADO, ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA)
         and etapa_nueva == ETAPA_TTE
         and (admin or (_area_activa_validada() == "setf" and auxiliar))) or
        (actual == ETAPA_TTE and etapa_nueva in (ETAPA_CORRECCIONES, ETAPA_FIRMA) and tte) or
        (actual == ETAPA_FIRMA and etapa_nueva in (ETAPA_RETORNO_FIRMA, ETAPA_ARCHIVADO) and admin)
    )
    if not permitido:
        raise PermissionError("La transición no está permitida para tu usuario o la etapa actual.")
    # Las observaciones son opcionales en la devolución de la Tte.;
    # el resto de transiciones conserva su comportamiento previo.
    if etapa_nueva == ETAPA_RETORNO_FIRMA and not detalle.strip():
        raise ValueError("Describe las correcciones solicitadas.")
    hoja.update_cell(indice, 8, etapa_nueva)
    _invalidar_cache_documental(borradores=True)
    try:
        _registrar_evento_borrador(folio, numero, int(ultima[3]), etapa_nueva, detalle)
    except Exception as error:
        st.warning(f"Se actualizó la etapa, pero no se pudo escribir en la bitácora: {error}")


def _actualizar_oficio_flujo(folio, numero, cambios, *, autorizacion_tte=False):
    """Actualización restringida del flujo; no permite modificar otros campos.

    Reutiliza la validación de fila y el batch existente, pero habilita a la Tte.
    exclusivamente para cambiar el estatus según su decisión documental.
    """
    permitidas = {"Estatus", "Fecha de entrega", "Firmado"}
    if not cambios or not set(cambios).issubset(permitidas):
        raise ValueError("Se intentó modificar un campo ajeno al proceso de firma.")
    if autorizacion_tte:
        exigir_permiso_revision_final()
        if set(cambios) != {"Estatus"} or cambios["Estatus"] not in ("En firma", "En correcciones"):
            raise PermissionError("La Tte. solo puede autorizar la firma o solicitar correcciones.")
    else:
        # En SETF el responsable asignado puede mandar su propio Word
        # directamente a revisión de la Tte.; DG conserva sus revisores.
        if _area_activa_validada() == "setf" and cambios == {"Estatus": "En revisión"}:
            oficio_autorizado = _verificar_expediente_documental(folio, numero)
            if not (puede_gestionar_borradores() or
                    puede_editar_responsable(oficio_autorizado.get("Responsable", ""))):
                raise PermissionError("No tienes permiso para enviar este oficio a revisión.")
        else:
            exigir_permiso_borradores()
    # La comprobación de acceso ocurre en el servidor, no en el formulario.
    _verificar_expediente_documental(folio, numero)
    hoja = conectar_google_sheets()
    valores = hoja.get_all_values()
    if not valores:
        raise ValueError("La hoja OFICIOS está vacía.")
    encabezados = [str(c).strip() for c in valores[0]]
    obligatorias = {"Folio", "No. de oficio", "Responsable"} | set(cambios)
    faltantes = obligatorias - set(encabezados)
    if faltantes:
        raise ValueError("Faltan columnas en OFICIOS: " + ", ".join(sorted(faltantes)))
    def campo(fila, nombre):
        i = encabezados.index(nombre)
        return str(fila[i]).strip() if i < len(fila) else ""
    coincidencias = [(i, fila) for i, fila in enumerate(valores[1:], start=2)
                    if campo(fila, "Folio") == str(folio).strip()
                    and campo(fila, "No. de oficio") == str(numero).strip()]
    if len(coincidencias) != 1:
        raise ValueError("El oficio no se encontró de forma única en OFICIOS.")
    fila_num, actual = coincidencias[0]
    if not autorizacion_tte:
        # SETF: el responsable asignado o administrador documental autorizado
        # puede enviar su documento a revisión; el flujo DG no se altera.
        if _area_activa_validada() == "setf" and cambios == {"Estatus": "En revisión"}:
            if not (puede_editar_responsable(campo(actual, "Responsable"))):
                raise PermissionError("No tienes permiso para mandar este oficio a revisión de la Tte. Alondra.")
        elif not puede_gestionar_borradores():
            raise PermissionError("Solo John o Alejandra pueden registrar la firma.")
    from gspread.utils import rowcol_to_a1
    actualizaciones = []
    for nombre, valor in cambios.items():
        if campo(actual, nombre) != str(valor).strip():
            actualizaciones.append({"range": rowcol_to_a1(fila_num, encabezados.index(nombre) + 1),
                                    "values": [[str(valor)]]})
    if actualizaciones:
        hoja.batch_update(actualizaciones, value_input_option="RAW")
    _invalidar_cache_documental(oficios=True)
    # Verificación con lectura fresca: no mostrar éxito si Sheets no persistió.
    verificado = hoja.row_values(fila_num)
    if any((verificado[encabezados.index(k)].strip()
            if len(verificado) > encabezados.index(k) else "") != str(v).strip()
           for k, v in cambios.items()):
        raise RuntimeError("Google Sheets no confirmó la actualización; verifica el oficio y reintenta.")


def _registrar_resultado_firma(folio, numero, resultado, nombre="", url="", fecha=None, observaciones=""):
    """Guarda el resultado en OFICIOS antes de cerrar la etapa en BORRADORES."""
    from urllib.parse import urlparse
    exigir_permiso_borradores()
    _verificar_expediente_documental(folio, numero)
    versiones = _borradores_expediente(folio, numero)
    if not versiones or str(versiones[-1].get("Etapa", "")) != ETAPA_FIRMA:
        raise ValueError("El expediente ya no está en proceso de firma. Actualiza la pantalla.")
    if resultado == "Regresó firmado":
        nombre, url = str(nombre).strip(), str(url).strip()
        partes = urlparse(url)
        if not nombre:
            raise ValueError("Es obligatorio indicar el nombre del documento firmado.")
        if partes.scheme != "https" or (partes.hostname or "").lower() not in (
                "drive.google.com", "docs.google.com"):
            raise ValueError("Introduce una liga https válida de Google Drive.")
        if fecha is None:
            raise ValueError("Selecciona la fecha de recepción del documento firmado.")
        documentos = json.dumps([{"nombre": nombre, "url": url}], ensure_ascii=False)
        _actualizar_oficio_flujo(folio, numero, {
            "Estatus": "Firmado", "Fecha de entrega": fecha.isoformat(),
            "Firmado": documentos,
        })
        detalle = f"Firmado recibido: {nombre} | Drive: {url} | Fecha: {fecha.isoformat()}"
        if str(observaciones).strip():
            detalle += " | " + str(observaciones).strip()
        _cambiar_etapa_borrador(folio, numero, ETAPA_ARCHIVADO, detalle)
    elif resultado == "Regresó con correcciones":
        if not str(observaciones).strip():
            raise ValueError("Describe las correcciones solicitadas.")
        _actualizar_oficio_flujo(folio, numero, {"Estatus": "En correcciones"})
        _cambiar_etapa_borrador(folio, numero, ETAPA_RETORNO_FIRMA, observaciones.strip())
    else:
        raise ValueError("Resultado de firma no reconocido.")


def _descargar_docx_drive(drive_id):
    """Descarga privada con OAuth; no necesita hacer público el archivo."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", str(drive_id)):
        raise ValueError("ID de archivo no válido.")
    respuesta = _drive_sesion().get(
        f"https://www.googleapis.com/drive/v3/files/{drive_id}",
        params={"alt": "media", "supportsAllDrives": "true"}, timeout=90)
    if not respuesta.ok:
        raise RuntimeError(f"No se pudo descargar el Word (HTTP {respuesta.status_code}).")
    return respuesta.content



def _convertir_word_pdf_privado(documento_word):
    """Convierte el Word original a PDF localmente sin publicar ni modificar Drive.

    Prioridad: LibreOffice. En Windows, también admite Word por COM (PowerShell
    sin dependencias Python adicionales, o pywin32 como alternativa).
    Nunca sustituye el documento por un PDF reconstruido de manera incompleta.
    """
    import shutil
    import subprocess
    import sys
    import tempfile
    import zipfile

    if not documento_word or not zipfile.is_zipfile(BytesIO(documento_word)):
        raise ValueError("El archivo de origen no es un documento Word .docx válido.")

    with tempfile.TemporaryDirectory(prefix="revision_word_pdf_") as tmp:
        carpeta = Path(tmp)
        archivo_word = carpeta / "documento_original.docx"
        archivo_pdf = carpeta / "documento_original.pdf"
        archivo_word.write_bytes(documento_word)
        intentos = []

        # 1. LibreOffice, preferido por no requerir Microsoft Word ni pywin32.
        candidatos = [shutil.which("soffice"), shutil.which("libreoffice")]
        if sys.platform.startswith("win"):
            candidatos.extend(
                str(Path(directorio) / "LibreOffice" / "program" / "soffice.exe")
                for directorio in (
                    os.getenv("PROGRAMFILES", r"C:\Program Files"),
                    os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                    os.getenv("LOCALAPPDATA", ""),
                ) if directorio
            )
        ejecutable = next((c for c in candidatos if c and Path(c).is_file()), None)
        if ejecutable:
            try:
                perfil = (carpeta / "perfil_libreoffice").as_uri()
                proceso = subprocess.run(
                    [ejecutable, f"-env:UserInstallation={perfil}", "--headless",
                     "--convert-to", "pdf:writer_pdf_Export", "--outdir",
                     str(carpeta), str(archivo_word)],
                    capture_output=True, text=True, timeout=120, check=False,
                )
                if proceso.returncode != 0 or not archivo_pdf.is_file():
                    intentos.append("LibreOffice: " +
                                   (proceso.stderr or proceso.stdout or "No generó un PDF.").strip()[:200])
            except (OSError, subprocess.TimeoutExpired) as error:
                intentos.append("LibreOffice: " + str(error)[:200])
        else:
            intentos.append("LibreOffice no está instalado o no se encontró su ejecutable.")

        # 2. Word instalado en el escritorio Windows: PowerShell crea el objeto COM
        #    sin exigir que el entorno Python tenga instalado 'pythoncom'.
        if not archivo_pdf.is_file() and sys.platform.startswith("win"):
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if powershell:
                script = r"""
$ErrorActionPreference = 'Stop'
$word = $null
$documento = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    try { $word.AutomationSecurity = 3 } catch {}
    $documento = $word.Documents.Open($env:PDF_WORD_ORIGEN, $false, $true)
    $documento.ExportAsFixedFormat($env:PDF_WORD_DESTINO, 17)
} finally {
    if ($null -ne $documento) {
        try { $documento.Close(0) } catch {}
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($documento)
    }
    if ($null -ne $word) {
        try { $word.Quit() } catch {}
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }
}
"""
                entorno = os.environ.copy()
                entorno["PDF_WORD_ORIGEN"] = str(archivo_word)
                entorno["PDF_WORD_DESTINO"] = str(archivo_pdf)
                try:
                    proceso = subprocess.run(
                        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                        env=entorno, capture_output=True, text=True, timeout=120,
                        check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if proceso.returncode != 0 or not archivo_pdf.is_file():
                        intentos.append("Microsoft Word: " +
                                       (proceso.stderr or proceso.stdout or "No generó el PDF.").strip()[:200])
                except (OSError, subprocess.TimeoutExpired) as error:
                    intentos.append("Microsoft Word: " + str(error)[:200])
            else:
                intentos.append("No se encontró PowerShell para ejecutar Microsoft Word.")

        # 3. Alternativa adicional para instalaciones donde Word COM + pywin32
        #    ya esté configurado. No se requiere que pythoncom exista para probar
        #    la vía anterior.
        if not archivo_pdf.is_file() and sys.platform.startswith("win"):
            word_app = None
            documento = None
            pythoncom_mod = None
            try:
                import pythoncom as pythoncom_mod
                import win32com.client
                pythoncom_mod.CoInitialize()
                word_app = win32com.client.DispatchEx("Word.Application")
                word_app.Visible = False
                word_app.DisplayAlerts = 0
                documento = word_app.Documents.Open(
                    str(archivo_word), ReadOnly=True, AddToRecentFiles=False
                )
                documento.ExportAsFixedFormat(str(archivo_pdf), 17)
            except ModuleNotFoundError:
                intentos.append("pywin32 no está instalado (opcional si funciona Word o LibreOffice).")
            except Exception as error:
                intentos.append("Word/pywin32: " + str(error)[:200])
            finally:
                if documento is not None:
                    try:
                        documento.Close(False)
                    except Exception:
                        pass
                if word_app is not None:
                    try:
                        word_app.Quit()
                    except Exception:
                        pass
                if pythoncom_mod is not None:
                    try:
                        pythoncom_mod.CoUninitialize()
                    except Exception:
                        pass

        if not archivo_pdf.is_file():
            diagnostico = " | ".join(intentos[-2:])
            if sys.platform.startswith("linux"):
                mensaje = (
                    "No se pudo preparar el PDF en el servidor. "
                    "Para Streamlit Community Cloud, agrega a la raíz de tu "
                    "repositorio de GitHub un archivo packages.txt que contenga "
                    "libreoffice-writer, y vuelve a desplegar la aplicación. "
                    "La versión Word original permanece intacta."
                )
            else:
                mensaje = (
                    "No se pudo generar el PDF. Instala LibreOffice o "
                    "utiliza Microsoft Word instalado en Windows. "
                    "La versión Word original permanece intacta."
                )
            raise RuntimeError(
                mensaje + (" Detalles: " + diagnostico if diagnostico else "")
            )
        pdf = archivo_pdf.read_bytes()
        if not pdf.startswith(b"%PDF-") or len(pdf) < 200:
            raise RuntimeError("La conversión devolvió un archivo que no es un PDF válido.")
        return pdf


# ================================================================
# HISTORIAL DOCUMENTAL EN PDF + VISTA PREVIA PRIVADA
# ================================================================
# Nunca se borran ni sobreescriben los Word originales. Son los archivos
# editables del flujo de correcciones. Los PDF quedan archivados en Drive
# dentro de la carpeta privada del mismo expediente, con nombre estable.


def _nombre_pdf_archivado(version):
    import hashlib
    version_num = int(version.get("Version") or 0)
    original = Path(str(version.get("Nombre") or "documento.docx")).stem
    original = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._-")[:85] or "documento"
    origen_id = str(version.get("Drive ID") or "")
    huella = hashlib.sha256(origen_id.encode("utf-8")).hexdigest()[:12]
    return f"V{version_num:02d}_{original}_{huella}.pdf"


def _buscar_pdf_archivado(folio, numero, version):
    """Consulta el PDF privado si ya fue archivado para este DOCX concreto."""
    nombre = _nombre_pdf_archivado(version)
    carpeta = _drive_carpeta_expediente(folio, numero)
    # Nombre generado solo con [A-Za-z0-9._-], seguro para la consulta de Drive.
    consulta = (
        f"'{carpeta}' in parents and name = '{nombre}' "
        "and mimeType = 'application/pdf' and trashed = false"
    )
    respuesta = _drive_sesion().get(
        "https://www.googleapis.com/drive/v3/files",
        params={"q": consulta, "fields": "files(id,name,mimeType,webViewLink),nextPageToken",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                "pageSize": 20},
        timeout=30,
    )
    archivos = _drive_json(respuesta, "consultar archivo PDF del historial").get("files", [])
    return archivos[0] if archivos else None


def _subir_pdf_archivado(folio, numero, version, pdf_bytes):
    """Almacena el PDF en la misma carpeta privada que el Word sin crear duplicados."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("La conversión no generó un PDF válido.")
    if len(pdf_bytes) > 40 * 1024 * 1024:
        raise ValueError("El PDF supera los 40 MB permitidos para esta copia histórica.")
    existente = _buscar_pdf_archivado(folio, numero, version)
    if existente:
        return existente["id"], False

    carpeta = _drive_carpeta_expediente(folio, numero)
    nombre = _nombre_pdf_archivado(version)
    sesion = _drive_sesion()
    inicio = sesion.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "resumable", "fields": "id,name",
                "supportsAllDrives": "true"},
        json={"name": nombre, "parents": [carpeta], "mimeType": "application/pdf"},
        headers={"X-Upload-Content-Type": "application/pdf",
                 "X-Upload-Content-Length": str(len(pdf_bytes))},
        timeout=30,
    )
    if not inicio.ok:
        _drive_json(inicio, "iniciar archivo PDF del historial")
    url_subida = inicio.headers.get("Location", "")
    if not url_subida.startswith("https://www.googleapis.com/"):
        raise RuntimeError("Drive no devolvió una URL segura para archivar el PDF.")
    fin = sesion.put(
        url_subida, data=pdf_bytes,
        headers={"Content-Type": "application/pdf",
                 "Content-Length": str(len(pdf_bytes))}, timeout=120,
    )
    archivo = _drive_json(fin, "guardar PDF histórico")
    return archivo["id"], True


def _descargar_pdf_privado(drive_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", str(drive_id)):
        raise ValueError("ID de archivo PDF no válido.")
    respuesta = _drive_sesion().get(
        f"https://www.googleapis.com/drive/v3/files/{drive_id}",
        params={"alt": "media", "supportsAllDrives": "true"}, timeout=90,
    )
    if not respuesta.ok:
        raise RuntimeError(f"No se pudo consultar el PDF privado (HTTP {respuesta.status_code}).")
    data = respuesta.content
    if not data.startswith(b"%PDF-"):
        raise ValueError("La copia en Drive no es un PDF válido.")
    return data


def _archivar_pdf_borrador(folio, numero, version, docx_bytes=None):
    """Obtiene la copia PDF; las versiones antiguas se convierten una sola vez."""
    _verificar_expediente_documental(folio, numero)
    documento_id = str(version.get("Drive ID") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", documento_id):
        raise ValueError("Esta versión no tiene un Word de origen válido en Drive.")
    previo = _buscar_pdf_archivado(folio, numero, version)
    if previo:
        return _descargar_pdf_privado(previo["id"])

    origen = docx_bytes if docx_bytes is not None else _descargar_docx_drive(documento_id)
    convertido = _convertir_word_pdf_privado(origen)
    pdf_id, creado = _subir_pdf_archivado(folio, numero, version, convertido)
    if creado:
        # La bitácora es complementaria: el PDF seguirá localizable por su
        # nombre estable aunque falle el registro de este evento.
        try:
            _registrar_evento_borrador(
                folio, numero, int(version.get("Version") or 0),
                "PDF histórico archivado",
                "Copia de lectura: " + _nombre_pdf_archivado(version),
            )
        except Exception as error:
            st.warning("El PDF se archivó en Drive, pero la bitácora no pudo actualizarse: "
                       + _mensaje_error_sheets(error))
    return convertido


def _mostrar_pdf_privado(pdf_bytes, clave):
    """Vista previa embebida con visor local; nunca se publica el PDF en una URL."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        st.error("No se puede mostrar un PDF inválido.")
        return
    try:
        from streamlit_pdf_viewer import pdf_viewer
        pdf_viewer(pdf_bytes, width="100%", height=690, key=clave)
        return
    except ImportError:
        pass
    except Exception as error:
        st.warning("El visor adicional no pudo abrir el documento: " + str(error)[:170])
    if hasattr(st, "pdf"):
        try:
            st.pdf(pdf_bytes, height=690)
            return
        except Exception as error:
            st.warning("El visor integrado no está disponible: " + str(error)[:170])
    st.info("Para visualizar el PDF aquí mismo, instala en el entorno de Streamlit: "
            "python -m pip install streamlit-pdf-viewer. "
            "La descarga PDF seguirá funcionando sin el visor.")


def _enlace_pdf_drive(archivo):
    """Enlace de visualización de Drive para un PDF privado ya archivado.

    El link no modifica permisos. Google Drive exige que la persona tenga
    acceso al archivo o a la carpeta que lo contiene.
    """
    from urllib.parse import urlparse

    drive_id = str((archivo or {}).get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", drive_id):
        raise ValueError("El PDF archivado no tiene un ID de Drive válido.")
    url_oficial = str((archivo or {}).get("webViewLink") or "").strip()
    if url_oficial:
        parsed = urlparse(url_oficial)
        if parsed.scheme == "https" and parsed.hostname in ("drive.google.com", "docs.google.com"):
            return url_oficial
    return f"https://drive.google.com/file/d/{drive_id}/view"


def _fila_version_pdf(folio, numero, version, version_vigente=False, *, compacto=False):
    """Histórico PDF en Drive: enlace directo, sin descargas ni visor duplicado.

    Las versiones DOCX de origen se conservan para recuperación y auditoría.
    Una versión antigua sin copia PDF ofrece archivarla una única vez.
    """
    import hashlib

    n = int(version.get("Version") or 0)
    doc_id = str(version.get("Drive ID") or "")
    clave = "hist_pdf_" + hashlib.sha256(
        f"{folio}|{numero}|{n}|{doc_id}".encode("utf-8")
    ).hexdigest()[:18]
    titulo = f"V{n:02d} · {Path(str(version.get('Nombre') or 'documento')).stem}.pdf"

    try:
        archivo = _buscar_pdf_archivado(folio, numero, version)
    except Exception as error:
        st.warning("No fue posible consultar el archivo histórico en Drive: " +
                   _mensaje_error_sheets(error))
        return

    if archivo:
        # Abre el PDF real en Drive; los permisos siguen controlados por Google.
        url = _enlace_pdf_drive(archivo)
        if compacto:
            st.markdown(
                '<div class="hist-pdf-link-row">'
                '<span class="hist-pdf-mark">PDF</span>'
                f'<span class="hist-pdf-filename" title="{html.escape(titulo, quote=True)}">'
                f'{html.escape(titulo)}</span>'
                f'<a class="hist-pdf-open" href="{html.escape(url, quote=True)}" '
                'target="_blank" rel="noopener noreferrer" '
                'aria-label="Abrir PDF histórico en Google Drive">'
                'Abrir en Drive ↗</a></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="padding:9px 12px;border-left:2px solid #B9936C;'
                'border-bottom:1px solid #E0D5CB;background:rgba(255,254,251,.65);'
                'border-radius:4px 7px 7px 4px">'
                f'<a href="{html.escape(url, quote=True)}" target="_blank" '
                'rel="noopener noreferrer" '
                'style="color:#604551;font-weight:700;text-decoration:none">'
                f'📑 {html.escape(titulo)} &nbsp; ↗ Abrir en Google Drive'
                '</a></div>',
                unsafe_allow_html=True,
            )
            st.caption("Archivo PDF histórico · " + str(version.get("Fecha registro") or "Sin fecha") +
                       " · Requiere acceso a Google Drive")
    else:
        if compacto:
            st.markdown(
                f'<div class="hist-pdf-pending">PDF de V{n:02d} pendiente de archivar en Drive</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(f"**📑 {html.escape(titulo)}**", unsafe_allow_html=True)
            st.caption("Esta versión aún no tiene PDF archivado en Google Drive.")
        if st.button("↻ Archivar PDF histórico en Drive", key=f"{clave}_archivar",
                     use_container_width=True):
            try:
                with st.spinner("Convirtiendo y guardando el PDF histórico en Drive..."):
                    _archivar_pdf_borrador(folio, numero, version)
                st.rerun()
            except Exception as error:
                st.error("No se pudo archivar el PDF histórico: " +
                         _mensaje_error_sheets(error))




def _eventos_agrupados_por_version(movimientos):
    """Organiza el histórico según la versión REAL registrada en Sheets.

    No se infieren versiones a partir de la fecha ni se trasladan comentarios
    de una versión a la siguiente. Cada decisión queda junto al Word revisado.
    """
    por_version = {}
    no_asignados = []
    for evento in movimientos:
        try:
            numero_version = int(str(evento.get("Version") or "").strip())
            if numero_version < 1:
                raise ValueError("Número inválido")
        except (ValueError, TypeError):
            no_asignados.append(evento)
            continue
        por_version.setdefault(numero_version, []).append(evento)
    # Las fechas se guardan como ISO 8601; garantiza el orden histórico.
    for relacionados in por_version.values():
        relacionados.sort(key=lambda e: str(e.get("Fecha") or ""))
    return por_version, no_asignados


def _texto_detalle_historial(evento):
    """Evita mostrar JSON técnico o URLs extensas como si fueran observaciones."""
    detalle = str(evento.get("Detalle") or "").strip()
    if not detalle:
        return ""
    if detalle.startswith("{"):
        try:
            metadatos = json.loads(detalle)
            if isinstance(metadatos, dict):
                nombre = str(metadatos.get("nombre") or "").strip()
                if str(evento.get("Accion") or "").strip() in (
                    "Word corregido por la Tte.", "PDF corregido por la Tte.",
                ):
                    formato = ("PDF" if str(evento.get("Accion") or "").strip().startswith("PDF")
                               else "Word")
                    return (f"Documento {formato} con modificaciones adjuntado por la Tte." +
                            (f" Archivo: {nombre}." if nombre else ""))
                if nombre:
                    return f"Documento adjunto: {nombre}."
                return "Información técnica de archivo registrada en la bitácora."
        except (ValueError, TypeError):
            pass
    return detalle


def _historial_version_estilos():
    """Estilo localizado: no cambia botones, PDFs, autorizaciones ni otros módulos."""
    st.markdown("""
    <style>
    [class*="st-key-hist_pdf_version_"] {
        background:linear-gradient(145deg,#FFFEFC,#F8F4EF)!important;
        border:1px solid #D7C6B7!important;
        border-left:3px solid #B9936C!important;
        border-radius:10px!important;
        padding:12px 15px 10px!important;
        margin:9px 0!important;
        box-shadow:0 3px 11px rgba(65,48,46,.045)!important;
    }
    .hist-version-title {
        display:flex;justify-content:space-between;align-items:baseline;
        gap:10px;margin-bottom:5px;color:#493842;font-weight:780;font-size:13px;
    }
    .hist-version-date {color:#867A79;font-size:11px;font-weight:500;}
    .hist-feedback {
        border-left:2px solid #C69B67;
        background:linear-gradient(90deg,rgba(191,147,107,.11),rgba(191,147,107,.025));
        padding:9px 12px;margin:9px 0 6px;border-radius:2px 7px 7px 2px;
    }
    .hist-feedback.aprobado {border-left-color:#688C78;background:rgba(104,140,120,.08);}
    .hist-feedback-title {font-size:11px;font-weight:800;color:#5B4146;margin-bottom:4px;}
    .hist-feedback-body {font-size:12px;color:#493B42;line-height:1.56;overflow-wrap:anywhere;}
    .hist-feedback-meta {font-size:10px;color:#897A76;margin-top:4px;}
    .hist-version-empty {font-size:11px;color:#827874;margin:8px 0 2px;}
    [class*="st-key-hist_pdf_version_"] [data-testid="stExpander"] {
        border:1px solid #D8CDC6!important;border-radius:7px!important;
        background:rgba(250,248,245,.88)!important;
    }
    [class*="st-key-hist_pdf_version_"] [data-testid="stExpander"] summary {
        padding:5px 8px!important;min-height:30px!important;
    }
    [class*="st-key-hist_pdf_version_"] [data-testid="stExpander"] summary p {
        font-size:11px!important;color:#5C4E52!important;
    }
    /* Encabezado compacto: Vxx, fecha y enlace en una fila legible. */
    .hist-version-number {display:flex;align-items:center;gap:9px;font-size:13px;font-weight:800;}
    .hist-version-number small {font-size:9px;letter-spacing:1.1px;font-weight:800;color:#906C58;}
    .hist-pdf-link-row {display:flex;align-items:center;gap:9px;min-width:0;
        padding:5px 8px;background:rgba(255,255,255,.55);
        border:1px solid rgba(182,145,122,.45);border-radius:7px;}
    .hist-pdf-mark {font-size:9px;color:#9A4E40;background:#F0DDDA;
        border:1px solid #D8B2AB;border-radius:4px;padding:4px 5px;font-weight:850;}
    .hist-pdf-filename {min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap;font-size:11px;color:#534943;}
    .hist-pdf-open {flex-shrink:0;color:#7C4B32!important;text-decoration:none!important;
        border:1px solid #BF9981;background:#FFFBF7;border-radius:6px;
        padding:6px 10px;font-size:11px;font-weight:800;white-space:nowrap;}
    .hist-pdf-open:hover {background:#EDDDCF;color:#50311F!important;}
    .hist-pdf-pending {color:#85543C;font-size:11px;margin:2px 0;}
    .hist-quick-summary {display:flex;justify-content:space-between;gap:8px;
        color:#75665D;font-size:10px;padding:3px 2px 0;}
    .hist-movement-label {font-size:9px;font-weight:800;letter-spacing:1.3px;
        margin-top:9px;color:#8B624B;}
    [class*="st-key-hist_pdf_version_"] [data-testid="stVerticalBlock"] {gap:4px!important;}
    [class*="st-key-hist_pdf_version_"] [data-testid="stToggle"] label p {font-size:11px!important;}
    [class*="st-key-hist_pdf_version_"] [data-testid="stToggle"] {margin-top:0!important;}
    .hist-movement {border-top:1px solid #E8DFD8;padding:7px 0;}
    .hist-movement strong {display:block;font-size:11px;color:#4B3F44;}
    .hist-movement span {display:block;font-size:10px;color:#776D6D;}
    .hist-movement p {font-size:11px;line-height:1.5;color:#574D4F;margin:3px 0 0;overflow-wrap:anywhere;}
    </style>
    """, unsafe_allow_html=True)


def _mostrar_movimientos_version(movimientos, numero_version, resumen_actual=False, *, dentro_de_detalle=False):
    """Comentarios de revisión visibles; demás hechos bajo un desplegable.

    Las observaciones se consultan en EVENTOS_BORRADORES, sin duplicar nada
    ni editar celdas, y se asignan usando exclusivamente el campo Version.
    """
    acciones_decision = {
        ETAPA_CORRECCIONES: ("↩ Correcciones solicitadas por la Tte.", False),
        ETAPA_RETORNO_FIRMA: ("↩ Correcciones posteriores a firma", False),
        ETAPA_FIRMA: ("✓ Autorizado para firma por la Tte.", True),
    }
    decisiones = [e for e in movimientos if str(e.get("Accion") or "").strip() in acciones_decision]
    secundarios = [e for e in movimientos if str(e.get("Accion") or "").strip() not in acciones_decision]
    for evento in decisiones:
        accion = str(evento.get("Accion") or "").strip()
        titulo, aprobado = acciones_decision[accion]
        detalle = _texto_detalle_historial(evento)
        detalle_visible = detalle or ("Sin observaciones adicionales." if aprobado
                                      else "No se registró texto de observaciones.")
        css = " aprobado" if aprobado else ""
        st.markdown(
            f'<div class="hist-feedback{css}">'
            f'<div class="hist-feedback-title">{html.escape(titulo)}</div>'
            f'<div class="hist-feedback-body">{html.escape(detalle_visible).replace(chr(10), "<br>")}</div>'
            f'<div class="hist-feedback-meta">{html.escape(str(evento.get("Usuario") or "Usuario no indicado"))}'
            f' · {html.escape(str(evento.get("Fecha") or "Sin fecha"))}</div>'
            '</div>', unsafe_allow_html=True)
    if not decisiones and not resumen_actual:
        st.markdown('<div class="hist-version-empty">Sin devolución ni decisión de la Tte. registrada para esta versión.</div>',
                    unsafe_allow_html=True)
    if secundarios:
        def _mostrar_entradas():
            st.markdown('<div class="hist-movement-label">REGISTRO TÉCNICO</div>',
                        unsafe_allow_html=True)
            for evento in secundarios:
                accion = html.escape(str(evento.get("Accion") or "Movimiento"))
                fecha = html.escape(str(evento.get("Fecha") or "Sin fecha"))
                usuario = html.escape(str(evento.get("Usuario") or "Usuario no indicado"))
                detalle = html.escape(_texto_detalle_historial(evento)).replace("\n", "<br>")
                st.markdown(
                    f'<div class="hist-movement"><strong>{accion}</strong>'
                    f'<span>{fecha} · {usuario}</span>'
                    + (f'<p>{detalle}</p>' if detalle else "")
                    + '</div>', unsafe_allow_html=True)
        if dentro_de_detalle:
            _mostrar_entradas()
        else:
            # Sin expanders anidados, ya que historial puede vivir dentro
            # de otro st.expander en la pantalla de revisión.
            if st.toggle(f"Otros movimientos de V{numero_version:02d} · {len(secundarios)}",
                         key=f"hist_mov_{numero_version}_{len(secundarios)}"):
                _mostrar_entradas()


def _mostrar_historial_pdf_por_version(folio, numero, versiones_anteriores, movimientos, *, actual=None):
    """Una fila compacta por PDF, comentarios y bitácora desplegables por versión.

    El archivo vigente no se confunde con los históricos; cada comentario
    conserva la asociación exacta a la columna Version de Sheets.
    """
    from hashlib import sha256
    _historial_version_estilos()
    por_version, sin_version = _eventos_agrupados_por_version(movimientos)
    acciones_decision = {ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA, ETAPA_FIRMA}
    if not versiones_anteriores:
        st.caption("Todavía no hay versiones anteriores.")
    for v in sorted(versiones_anteriores, key=lambda x: int(x.get("Version") or 0), reverse=True):
        n = int(v.get("Version") or 0)
        clave = sha256(f"{folio}|{numero}|{n}".encode("utf-8")).hexdigest()[:14]
        relacionados = por_version.get(n, [])
        decisiones = sum(str(e.get("Accion") or "").strip() in acciones_decision for e in relacionados)
        devoluciones = sum(str(e.get("Accion") or "").strip() in
                           (ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA) for e in relacionados)
        aprobaciones = decisiones - devoluciones
        secundarios = len(relacionados) - decisiones
        fecha = html.escape(str(v.get("Fecha registro") or "")[:10])
        if devoluciones:
            texto_resumen = f"{devoluciones} devolución{'es' if devoluciones != 1 else ''}"
        elif aprobaciones:
            texto_resumen = f"{aprobaciones} autorización{'es' if aprobaciones != 1 else ''}"
        else:
            texto_resumen = "Sin observaciones de revisión"
        with st.container(key=f"hist_pdf_version_{clave}"):
            st.markdown(
                '<div class="hist-version-title">'
                f'<span class="hist-version-number">V{n:02d} <small>HISTÓRICO</small></span>'
                f'<span class="hist-version-date">{fecha}</span>'
                '</div>', unsafe_allow_html=True,
            )
            _fila_version_pdf(folio, numero, v, compacto=True)
            st.markdown(
                '<div class="hist-quick-summary">'
                f'<span>{html.escape(texto_resumen)}</span>'
                f'<span>{secundarios} movimiento{'s' if secundarios != 1 else ''}</span>'
                '</div>', unsafe_allow_html=True,
            )
            if relacionados:
                if st.toggle(f"Ver detalles de V{n:02d}", key=f"hist_detalle_{clave}"):
                    _mostrar_movimientos_version(relacionados, n, dentro_de_detalle=True)
    if actual is not None:
        n_actual = int(actual.get("Version") or 0)
        if por_version.get(n_actual):
            clave_actual = sha256(f"{folio}|{numero}|{n_actual}".encode("utf-8")).hexdigest()[:14]
            if st.toggle(
                f"Movimientos de la versión vigente V{n_actual:02d} · {len(por_version[n_actual])}",
                key=f"hist_actual_{clave_actual}",
            ):
                _mostrar_movimientos_version(por_version[n_actual], n_actual,
                                             resumen_actual=True, dentro_de_detalle=True)
    if sin_version:
        # Los registros sin Version NO se adjudican a ninguna versión.
        if st.toggle(f"Movimientos sin versión identificada · {len(sin_version)}",
                     key=f"hist_sin_version_{sha256(f'{folio}|{numero}'.encode()).hexdigest()[:12]}"):
            for evento in sin_version:
                st.caption(f"{evento.get('Fecha', '')} · {evento.get('Accion', '')} · "
                           f"{_texto_detalle_historial(evento)}")


def _botones_word_y_pdf(drive_id, nombre, clave):
    """Accesos elegantes para Word/PDF, conservando Drive privado y el flujo actual.

    Preparar Word guarda el DOCX en la sesión y Preparar PDF convierte esa misma
    versión vigente. Las descargas aparecen únicamente cuando están listas.
    """
    import hashlib

    clave_segura = hashlib.sha256(str(clave).encode("utf-8")).hexdigest()[:18]
    base = f"revision_formatos_{clave_segura}"
    id_registrado = f"{base}_id"
    id_actual = str(drive_id or "").strip()
    nombre_word = Path(str(nombre or "documento.docx")).name
    if not nombre_word.lower().endswith(".docx"):
        nombre_word += ".docx"
    nombre_pdf = str(Path(nombre_word).with_suffix(".pdf"))

    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", id_actual):
        st.warning("Este expediente no tiene un documento Word descargable registrado.")
        return

    # Evita entregar una versión anterior si cambia el documento de Drive.
    if st.session_state.get(id_registrado) != id_actual:
        st.session_state[id_registrado] = id_actual
        st.session_state.pop(f"{base}_word", None)
        st.session_state.pop(f"{base}_pdf", None)

    # Se limita a los elementos con claves revision_formatos_* para no alterar
    # los botones de aprobación, correcciones, navegación ni otros módulos.
    st.markdown("""
    <style>
    /* DOSSIER DOCUMENTAL · ACCIONES DE ARCHIVO */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_word_panel"],
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"] {
        position:relative!important;
        background:linear-gradient(150deg,#FFFEFB 0%,#F4F0EA 100%)!important;
        border:1px solid #D6C5B7!important;
        border-radius:13px!important;
        padding:15px 17px 15px!important;
        box-shadow:0 5px 15px rgba(62,47,44,.075),inset 0 1px rgba(255,255,255,.75)!important;
        min-width:0!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_word_panel"]:before,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"]:before {
        content:"";position:absolute;left:17px;top:0;width:59px;height:2px;
        background:linear-gradient(90deg,#B78C61,transparent);
    }
    .revision-file-heading {display:flex;align-items:center;gap:11px;margin-bottom:9px;min-height:42px;}
    .revision-file-symbol {display:flex;align-items:center;justify-content:center;width:38px;height:38px;
        flex:0 0 38px;border-radius:11px;background:#E7EBF0;border:1px solid #C6D1DB;color:#3E5D79;
        font-size:12px;letter-spacing:.5px;font-weight:850;}
    .revision-file-heading.pdf .revision-file-symbol {background:#F2E9E6;border-color:#DDBEB7;color:#985D52;}
    .revision-file-name {font-size:14px;color:#453843;font-weight:800;line-height:1.22;}
    .revision-file-detail {font-size:11px;line-height:1.4;color:#81777A;margin-top:3px;}
    .revision-file-flag {display:inline-block;margin:0 0 9px;color:#5E776D;font-size:10px;
        font-weight:750;letter-spacing:.25px;}
    .revision-file-pending {display:inline-block;margin:0 0 9px;color:#98826B;font-size:10px;
        font-weight:650;letter-spacing:.25px;}
    /* Botón discreto de preparación */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button {
        background:#F7F3EE!important;color:#58434C!important;
        border:1px solid #CBB7A7!important;border-radius:9px!important;
        min-height:39px!important;height:auto!important;padding:8px 14px!important;
        font-size:12px!important;font-weight:680!important;
        box-shadow:none!important;transition:transform .18s ease,background .18s ease,box-shadow .18s ease!important;
    }
    /* Descarga, acción principal, sin cambiar la lógica de st.download_button */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button {
        background:#483A45!important;color:#FFF9F3!important;
        border:1px solid #B58B68!important;border-radius:9px!important;
        min-height:39px!important;height:auto!important;padding:8px 14px!important;
        font-size:12px!important;font-weight:750!important;
        box-shadow:0 3px 9px rgba(43,33,37,.14)!important;
        transition:transform .18s ease,background .18s ease,box-shadow .18s ease!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button *,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button * {
        color:#FFF9F3!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button:hover,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button:hover {
        background:#EAE1D9!important;border-color:#B88D66!important;
        transform:translateY(-1px);box-shadow:0 3px 9px rgba(67,48,42,.08)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button:hover,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button:hover {
        background:#5B4753!important;border-color:#C39A76!important;
        transform:translateY(-1px);box-shadow:0 6px 14px rgba(43,33,37,.16)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"] button:focus-visible {
        outline:2px solid #B99272!important;outline-offset:2px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"] [data-testid="stExpander"] {
        border:1px solid #D9CAC0!important;border-radius:8px!important;
        background:transparent!important;margin-top:8px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"] [data-testid="stExpander"] summary {
        min-height:35px!important;padding:6px 11px!important;
    }
    @media(max-width:700px) {
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_word_panel"],
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"] {
            padding:12px!important;
        }
    }
    </style>
    """, unsafe_allow_html=True)

    # Los botones nativos de Streamlit no permiten translate="no" directamente.
    # Las etiquetas visuales se pintan con CSS (Chrome no las reescribe al traducir).
    # El texto original del widget sigue presente para accesibilidad y como respaldo.
    st.markdown("""
    <style>
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button {
        position:relative!important;min-height:39px!important;overflow:hidden!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button p,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button p,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button p,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button p {
        font-size:0!important;color:transparent!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button::after,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button::after,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button::after,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button::after {
        position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        padding:3px 9px;font-size:12px;font-weight:720;line-height:1.2;
        white-space:nowrap;pointer-events:none;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_word"] button::after {content:"Preparar Word";color:#58434C;}
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_prep_pdf"] button::after {content:"Preparar PDF";color:#58434C;}
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_word"] button::after {content:"Descargar Word";color:#FFF9F3;}
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_desc_pdf"] button::after {content:"Descargar PDF";color:#FFF9F3;}
    .revision-file-heading {min-height:36px!important;margin-bottom:5px!important;}
    .revision-ext {font-size:11px;font-weight:600;color:#857A72;margin-left:5px;}
    .revision-file-flag {font-size:10px!important;margin-bottom:5px!important;}
    div[class*="st-key-tte_consulta_panel_"] .tte-module-heading {margin-bottom:7px!important;padding-bottom:7px!important;}
    div[class*="st-key-tte_consulta_panel_"] .tte-module-icon {width:32px;height:32px;font-size:17px;}
    div[class*="st-key-tte_consulta_panel_"] .tte-document-line small {
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;
    }
    @media (max-width:700px) {
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"] button::after {font-size:11px;}
    }
    </style>
    """, unsafe_allow_html=True)

    columna_word, columna_pdf = st.columns(2, gap="small")
    with columna_word:
        with st.container(key=f"{base}_word_panel"):
            st.markdown(
                '<div class="revision-file-heading word notranslate" translate="no" lang="es-MX">'
                '<div class="revision-file-symbol">W</div>'
                '<div class="revision-file-name">Word <span class="revision-ext">.docx</span></div>'
                '</div>', unsafe_allow_html=True,
            )
            listo_word = f"{base}_word" in st.session_state
            if listo_word:
                st.markdown('<span class="revision-file-flag notranslate" translate="no">✓ Listo</span>',
                            unsafe_allow_html=True)
            if listo_word:
                preparar, descargar = st.columns([1, 1.2], gap="small")
            else:
                preparar, descargar = st.container(), None
            with preparar:
                if st.button("Preparar archivo .docx",
                             key=f"{base}_prep_word", use_container_width=True):
                    try:
                        with st.spinner("Recuperando Word del expediente..."):
                            st.session_state[f"{base}_word"] = _descargar_docx_drive(id_actual)
                        st.rerun()  # Muestra la descarga en cuanto termina la preparación.
                    except Exception as error:
                        st.error(f"No se pudo preparar Word: {_mensaje_error_sheets(error)}")
            if listo_word:
                with descargar:
                    st.download_button(
                        "Descargar archivo .docx",
                        data=st.session_state[f"{base}_word"],
                        file_name=nombre_word,
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"{base}_desc_word", use_container_width=True,
                    )

    with columna_pdf:
        with st.container(key=f"{base}_pdf_panel"):
            st.markdown(
                '<div class="revision-file-heading pdf notranslate" translate="no" lang="es-MX">'
                '<div class="revision-file-symbol">PDF</div>'
                '<div class="revision-file-name">PDF <span class="revision-ext">.pdf</span></div>'
                '</div>', unsafe_allow_html=True,
            )
            listo_pdf = f"{base}_pdf" in st.session_state
            if listo_pdf:
                st.markdown('<span class="revision-file-flag notranslate" translate="no">✓ Listo</span>',
                            unsafe_allow_html=True)
            if listo_pdf:
                preparar, descargar = st.columns([1, 1.2], gap="small")
            else:
                preparar, descargar = st.container(), None
            with preparar:
                if st.button("Preparar archivo .pdf",
                             key=f"{base}_prep_pdf", use_container_width=True):
                    try:
                        with st.spinner("Convirtiendo el Word vigente a PDF..."):
                            word = st.session_state.get(f"{base}_word")
                            if word is None:
                                word = _descargar_docx_drive(id_actual)
                                st.session_state[f"{base}_word"] = word
                            st.session_state[f"{base}_pdf"] = _convertir_word_pdf_privado(word)
                        st.rerun()  # Habilita descarga y vista previa inmediatamente.
                    except Exception as error:
                        st.error(f"No se pudo preparar PDF: {_mensaje_error_sheets(error)}")
            if listo_pdf:
                with descargar:
                    st.download_button(
                        "Descargar archivo .pdf", data=st.session_state[f"{base}_pdf"],
                        file_name=nombre_pdf, mime="application/pdf",
                        key=f"{base}_desc_pdf", use_container_width=True,
                    )
                with st.expander("◉ Vista previa del PDF", expanded=False):
                    _mostrar_pdf_privado(st.session_state[f"{base}_pdf"], f"{base}_visor")


def _adjuntar_documento_corregido_tte(folio, numero, archivo):
    """Adjunto opcional DOCX o PDF de la Tte., guardado en Drive y en bitácora.

    La versión principal en BORRADORES sigue intacta. Solo se registra el
    adjunto de devolución si la Tte. adjuntó un archivo efectivamente.
    """
    if archivo is None:
        return None
    exigir_permiso_revision_final()
    _verificar_expediente_documental(folio, numero)
    versiones = _borradores_expediente(folio, numero)
    if not versiones or str(versiones[-1].get("Etapa", "")) != ETAPA_TTE:
        raise ValueError("El oficio ya no está en revisión de la Tte.")

    nombre_original = Path(str(archivo.name)).name
    extension = Path(nombre_original).suffix.lower()
    if extension == ".docx":
        # Reutilizar el comportamiento existente para Word y su bitácora.
        return _adjuntar_word_corregido_tte(folio, numero, archivo)
    if extension != ".pdf":
        raise ValueError("La Tte. solo puede adjuntar Word (.docx) o PDF (.pdf).")

    contenido = archivo.getvalue()
    if not contenido.startswith(b"%PDF-"):
        raise ValueError("El archivo seleccionado no parece ser un PDF válido.")
    if len(contenido) > 40 * 1024 * 1024:
        raise ValueError("El PDF adjunto no puede superar 40 MB.")

    version = int(versiones[-1].get("Version") or 0)
    carpeta = _drive_carpeta_expediente(folio, numero)
    nombre_seguro = re.sub(r"[^A-Za-z0-9._-]", "_", nombre_original)
    nombre_drive = f"tte_correcciones_v{version:03d}_{nombre_seguro}"[:180]
    sesion = _drive_sesion()
    inicio = sesion.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "resumable", "fields": "id,name,webViewLink", "supportsAllDrives": "true"},
        json={"name": nombre_drive, "parents": [carpeta], "mimeType": "application/pdf"},
        headers={"X-Upload-Content-Type": "application/pdf",
                 "X-Upload-Content-Length": str(len(contenido))},
        timeout=30,
    )
    if not inicio.ok:
        _drive_json(inicio, "iniciar subida del PDF corregido por la Tte.")
    url_subida = inicio.headers.get("Location", "")
    if not url_subida.startswith("https://www.googleapis.com/"):
        raise RuntimeError("Drive no devolvió una dirección válida para subir el PDF.")
    fin = sesion.put(
        url_subida, data=contenido,
        headers={"Content-Type": "application/pdf", "Content-Length": str(len(contenido))},
        timeout=120,
    )
    subido_drive = _drive_json(fin, "guardar PDF corregido por la Tte.")
    drive_id = str(subido_drive["id"])
    url = str(subido_drive.get("webViewLink") or f"https://drive.google.com/file/d/{drive_id}/view")
    subido = {"id": drive_id, "nombre": nombre_drive, "url": url}
    detalle = json.dumps({"nombre": nombre_drive, "url": url,
                          "drive_id": drive_id, "formato": "pdf"}, ensure_ascii=False)
    try:
        _registrar_evento_borrador(folio, numero, version,
                                   "PDF corregido por la Tte.", detalle)
    except Exception as error:
        raise RuntimeError(
            "El PDF se guardó en Drive, pero falló el registro en la bitácora. "
            f"Se encuentra aquí: {url}. {error}"
        ) from error
    return subido


def _adjuntar_word_corregido_tte(folio, numero, archivo):
    """Guarda el Word opcional de la Tte. sin reemplazar la versión del equipo."""
    exigir_permiso_revision_final()
    _verificar_expediente_documental(folio, numero)
    versiones = _borradores_expediente(folio, numero)
    if not versiones or str(versiones[-1].get("Etapa", "")) != ETAPA_TTE:
        raise ValueError("El oficio ya no está en revisión de la Tte.")
    version = int(versiones[-1].get("Version") or 0)
    # El cargador existente genera un archivo nuevo en Drive, sin sobrescribir.
    subido = _subir_borrador_drive(archivo, folio, numero, version)
    detalle = json.dumps({"nombre": subido["nombre"], "url": subido["url"],
                          "drive_id": subido["id"]}, ensure_ascii=False)
    try:
        _registrar_evento_borrador(folio, numero, version,
                                   "Word corregido por la Tte.", detalle)
    except Exception as error:
        raise RuntimeError(
            "El Word se guardó en Drive, pero falló su registro en la bitácora. "
            f"Recupéralo aquí: {subido['url']}. {error}"
        ) from error
    return subido


def _adjuntos_tte_expediente(folio, numero):
    """Adjuntos de devolución, agrupados por versión y sin depender del orden de filas.

    Los archivos se conservan en Drive y su trazabilidad en EVENTOS_BORRADORES.
    No se modifica ni reemplaza ninguna versión de BORRADORES.
    """
    acciones = ("Word corregido por la Tte.", "PDF corregido por la Tte.")
    eventos = _eventos_expediente(folio, numero)
    grupos = {}
    for evento in eventos:
        if str(evento.get("Accion", "")).strip() not in acciones:
            continue
        try:
            dato = json.loads(str(evento.get("Detalle") or ""))
            if not isinstance(dato, dict):
                continue
            version = int(str(evento.get("Version") or "0").strip())
        except (ValueError, TypeError):
            continue
        if version < 1:
            continue
        id_drive = str(dato.get("drive_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", id_drive):
            continue
        es_pdf = str(evento.get("Accion", "")).strip() == acciones[1]
        grupos.setdefault(version, []).append({
            "id": id_drive,
            "nombre": str(dato.get("nombre") or ("Correcciones_Tte.pdf" if es_pdf else "Correcciones_Tte.docx")),
            "formato": "PDF" if es_pdf else "Word",
            "es_pdf": es_pdf,
            "url": str(dato.get("url") or ""),
            "fecha": str(evento.get("Fecha") or ""),
        })
    for archivos in grupos.values():
        archivos.sort(key=lambda x: x["fecha"])
    return grupos


def _mostrar_adjunto_tte(archivo, folio, numero, *, historico=False, instancia=""):
    """Permite descargar el adjunto privado a quien consulta el expediente."""
    drive_id = archivo["id"]
    formato = archivo["formato"]
    nombre = archivo["nombre"]
    version_texto = " (histórico)" if historico else ""
    st.markdown(f"**Documento de correcciones de la Tte.: {html.escape(nombre)}{version_texto}**")
    identificador = hashlib.sha256(f"{folio}|{numero}|{drive_id}".encode()).hexdigest()[:18]
    # El mismo adjunto puede mostrarse en la sección vigente y en el historial.
    # Cada instancia necesita claves distintas, conservando los bytes en
    # session_state por archivo para evitar descargas innecesarias.
    clave = f"tte_archivo_{identificador}_{'historial' if historico else 'actual'}_{instancia}"
    izquierda, derecha = st.columns(2)
    with izquierda:
        url = archivo["url"]
        if url.startswith("https://drive.google.com/file/d/"):
            st.link_button(f"Ver {formato} en Drive", url, use_container_width=True)
    with derecha:
        if st.button(f"Preparar {formato} de la Tte.", key=f"{clave}_preparar", use_container_width=True):
            try:
                data = (_descargar_pdf_privado(drive_id) if archivo["es_pdf"]
                        else _descargar_docx_drive(drive_id))
                st.session_state[f"{clave}_bytes"] = data
            except Exception as error:
                st.error(f"No se pudo descargar el documento: {_mensaje_error_sheets(error)}")
        if f"{clave}_bytes" in st.session_state:
            st.download_button(
                f"Descargar {formato} de la Tte.",
                data=st.session_state[f"{clave}_bytes"], file_name=nombre,
                mime=("application/pdf" if archivo["es_pdf"] else
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                key=f"{clave}_descargar", use_container_width=True,
            )
    if archivo["es_pdf"] and f"{clave}_bytes" in st.session_state:
        with st.expander("Vista previa del PDF devuelto"):
            _mostrar_pdf_privado(st.session_state[f"{clave}_bytes"], f"{clave}_visor")


def _word_tte_devolucion(folio, numero, etapa):
    """Presenta el último adjunto de la ronda de correcciones actual.

    Se localiza por versión y fecha; no depende del orden de aparición en la hoja.
    Los adjuntos históricos se muestran por separado en el historial.
    """
    if etapa != ETAPA_CORRECCIONES:
        return
    try:
        versiones = _borradores_expediente(folio, numero)
        if not versiones:
            return
        version_actual = int(versiones[-1].get("Version") or 0)
        adjuntos = _adjuntos_tte_expediente(folio, numero).get(version_actual, [])
        if not adjuntos:
            return
        st.info("La Tte. adjuntó un documento con correcciones. El Word original del expediente se conserva sin cambios.")
        _mostrar_adjunto_tte(adjuntos[-1], folio, numero)
    except Exception as error:
        st.warning("No fue posible consultar el adjunto de la Tte.: " + _mensaje_error_sheets(error))


def _mensaje_error_sheets(error):
    mensaje = str(error)
    if ("Read timed out" in mensaje or "ConnectTimeout" in mensaje
            or "ConnectionError" in mensaje or "timed out" in mensaje.lower()):
        return ("La consulta a Google Sheets tardó demasiado. No se han borrado "
                "los registros. Comprueba tu conexión y vuelve a intentar en unos instantes.")
    if "429" in mensaje or "RATE_LIMIT_EXCEEDED" in mensaje or "RESOURCE_EXHAUSTED" in mensaje:
        return ("Google Sheets alcanzó temporalmente su límite de consultas. "
                "Espera aproximadamente un minuto y vuelve a abrir el expediente "
                "para comprobar la etapa antes de intentar enviarlo de nuevo.")
    return mensaje


def _mostrar_observaciones_revisor(folio, numero, etapa):
    """Muestra las indicaciones conservadas en EVENTOS_BORRADORES."""
    import html
    if etapa not in (ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA):
        return
    # En SETF el responsable asignado también debe ver la devolución de la Tte.
    # DG conserva su validación original de revisores.
    if _area_activa_validada() != "setf" and not puede_gestionar_borradores():
        return
    try:
        eventos = _eventos_expediente(folio, numero)
        evento = next((e for e in reversed(eventos)
                       if str(e.get("Accion", "")).strip() == etapa), None)
    except Exception as error:
        st.warning(f"No se pudieron consultar las observaciones: {_mensaje_error_sheets(error)}")
        return
    if evento is None:
        st.warning("El expediente fue devuelto, pero no se encontraron observaciones en la bitácora.")
        return
    titulo = ("Correcciones solicitadas por la Tte. Alondra"
              if etapa == ETAPA_CORRECCIONES else "Correcciones solicitadas tras el trámite de firma")
    detalle = html.escape(str(evento.get("Detalle", "")).strip()).replace("\\n", "<br>")
    autor = html.escape(str(evento.get("Usuario", "")) or "Revisor")
    fecha = html.escape(str(evento.get("Fecha", "")))
    st.markdown(
        '<div class="revision-feedback">'
        '<div class="revision-feedback-eyebrow">✎ ACCIÓN REQUERIDA · DEVOLUCIÓN</div>'
        f'<div class="revision-feedback-title">{html.escape(titulo)}</div>'
        f'<div class="revision-feedback-text">{detalle or "Sin observaciones registradas."}</div>'
        f'<div class="revision-feedback-footer">Registrado por {autor} · {fecha}</div>'
        '<div class="revision-feedback-hint">Descarga el Word, realiza los ajustes y carga una nueva versión. '
        'Después podrás enviarla nuevamente a revisión.</div></div>',
        unsafe_allow_html=True
    )


def mostrar_borradores_pendiente(folio, numero, responsable, compacto=False):
    """Centro documental: versión vigente, procedencia, acciones y trazabilidad."""
    import html
    try:
        versiones = _borradores_expediente(folio, numero)
    except Exception as error:
        st.error(f"No fue posible consultar los borradores: {error}")
        return
    ultimo = versiones[-1] if versiones else None
    etapa = str(ultimo.get("Etapa", "")) if ultimo else "Sin borrador"
    if not compacto:
        st.markdown("""<div class="doc-center-head"><span class="doc-center-kicker">EXPEDIENTE DIGITAL · WORD</span>
          <div class="doc-center-title">Centro de revisión documental</div>
          <div class="doc-center-sub">Versión vigente · Historial · Acciones</div></div>""",
          unsafe_allow_html=True)
        # Indicadores compactos: las etapas largas no deben convertirse en
        # métricas enormes ni truncarse en pantallas pequeñas.
        estados_cortos = {
            ETAPA_BORRADOR: "Borrador listo",
            ETAPA_ENVIADO: "Revisión inicial",
            ETAPA_TTE: "En revisión",
            ETAPA_CORRECCIONES: "Requiere ajustes",
            ETAPA_FIRMA: "En firma",
            ETAPA_RETORNO_FIRMA: "Ajustes de firma",
            ETAPA_ARCHIVADO: "Archivado",
        }
        etapa_resumida = estados_cortos.get(etapa, etapa)
        # Cerrar la cuadrícula mediante un único fragmento HTML evita que
        # Streamlit divida su estructura entre bloques independientes.
        st.markdown(
            '<div class="doc-summary-grid">'
            f'<div class="doc-summary-item"><span>VERSIÓN ACTUAL</span><strong>{"V" + format(int(ultimo["Version"]), "02d") if ultimo else "—"}</strong></div>'
            f'<div class="doc-summary-item"><span>VERSIONES</span><strong>{len(versiones)}</strong></div>'
            f'<div class="doc-summary-item doc-summary-status"><span>ESTADO ACTUAL</span><strong title="{html.escape(etapa, quote=True)}">{html.escape(etapa_resumida)}</strong></div>'
            '</div>', unsafe_allow_html=True,
        )
    else:
        if ultimo:
            st.caption(f"Versión V{int(ultimo['Version']):02d} · {len(versiones)} archivo(s) conservado(s)")
    _mostrar_observaciones_revisor(folio, numero, etapa)
    _word_tte_devolucion(folio, numero, etapa)
    if ultimo:
        st.markdown(f"**📄 {html.escape(str(ultimo.get('Nombre', 'Documento Word')))}**")
        st.caption(f"Registrado por: {ultimo.get('Usuario', '')} · {ultimo.get('Fecha registro', '')}")
        _botones_word_y_pdf(
            ultimo.get("Drive ID", ""), ultimo.get("Nombre", "Documento Word"),
            f"borrador_{folio}_{numero}_{ultimo.get('Version', '')}",
        )
        with st.expander(f"Historial PDF y observaciones · {max(len(versiones)-1,0)} anteriores",
                         expanded=False):
            try:
                movimientos = _eventos_expediente(folio, numero)
            except Exception as error:
                movimientos = []
                st.warning(f"No se pudo consultar la bitácora: {_mensaje_error_sheets(error)}")
            anteriores = [v for v in versiones
                          if int(v.get("Version") or 0) < int(ultimo.get("Version") or 0)]
            _mostrar_historial_pdf_por_version(folio, numero, anteriores, movimientos, actual=ultimo)
            # Descargas del Word/PDF de devolución incluso después de registrar
            # una versión corregida. Permanecen asociados a la versión revisada.
            if _area_activa_validada() == "setf":
                adjuntos_por_version = _adjuntos_tte_expediente(folio, numero)
                if adjuntos_por_version:
                    st.markdown("**Documentos devueltos por la Tte. · Historial**")
                    for numero_version in sorted(adjuntos_por_version, reverse=True):
                        for indice_adjunto, adjunto in enumerate(adjuntos_por_version[numero_version]):
                            st.caption(f"Versión revisada V{numero_version:02d} · {adjunto['fecha']}")
                            _mostrar_adjunto_tte(adjunto, folio, numero, historico=True, instancia=f"v{numero_version}_{indice_adjunto}")
    else:
        if not compacto:
            st.info("Aún no hay un Word registrado. Puedes comenzar aquí, incluso si el archivo llegó por USB o correo.")


    admin = puede_gestionar_borradores()
    tte = puede_realizar_revision_final()
    auxiliar = puede_editar_responsable(responsable) and not (admin or tte)
    puede_subir = ((not ultimo and (admin or auxiliar)) or
                   (ultimo and (admin or (_area_activa_validada() == "setf" and auxiliar))
                    and etapa in (ETAPA_BORRADOR, ETAPA_ENVIADO,
                                  ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA)))
    if puede_subir:
        titulo_carga = ("☁ Cargar documento Word (.docx)" if compacto and not ultimo else
                        "☁ Cargar nueva versión (.docx)" if compacto else
                        "＋ Cargar primera versión" if not ultimo else "＋ Cargar nueva versión corregida")
        with st.expander(titulo_carga, expanded=(not ultimo and not compacto)):
            with st.form(key=f"doc_carga_{folio}_{numero}"):
                archivo = st.file_uploader("Documento Word editable (.docx)", type=["docx"],
                                           key=f"doc_archivo_{folio}_{numero}")
                procedencia = st.selectbox("¿Cómo se recibió o elaboró el documento?",
                                           ["Elaboración propia", "Memoria USB", "Correo electrónico",
                                            "Entregado por auxiliar", "Otro medio"],
                                           key=f"doc_procedencia_{folio}_{numero}")
                # En SETF se ofrecen EXCLUSIVAMENTE los integrantes de SETF,
                # incluidos los registrados en Secrets; en DG se conserva la lista anterior.
                if _area_activa_validada() == "setf":
                    # Los autores no deben heredarse de los responsables DG.
                    # Usar cuentas adscritas a SETF y autores opcionales explícitos.
                    nombres_setf = []
                    for info in st.secrets["usuarios"].values():
                        if (hasattr(info, "get") and
                            str(info.get("area", "diseno")).strip().lower() == "setf"):
                            nombre = str(info.get("nombre") or info.get("responsable") or "").strip()
                            if nombre and nombre not in nombres_setf:
                                nombres_setf.append(nombre)
                    # Arturo y Alondra pueden alternar las dos áreas sin
                    # tener necesariamente area='setf' en su perfil.
                    for usuario_id in ("arturo", "alondra"):
                        info = st.secrets["usuarios"].get(usuario_id, {})
                        nombre = str(info.get("nombre") or info.get("responsable") or "").strip()
                        if nombre and nombre not in nombres_setf:
                            nombres_setf.append(nombre)
                    for nombre in st.secrets["setf"].get("autores_documentos", []):
                        nombre = str(nombre).strip()
                        if nombre and nombre not in nombres_setf:
                            nombres_setf.append(nombre)
                    personas = ["No especificado"] + sorted(nombres_setf) + ["Otro"]
                else:
                    personas = ["No especificado", "Magali Rivera", "Ricardo Godínez", "Cecilia Santos",
                                "Jesús Rodriguez", "Alejandra Cardoso", "John Bautista", "Otro"]
                persona_elegida = st.selectbox("¿Quién lo elaboró o entregó?", personas,
                                              key=f"doc_origen_lista_{_area_activa_validada()}_{folio}_{numero}")
                origen = (st.text_input("Especifica el nombre", key=f"doc_origen_otro_{folio}_{numero}")
                          if persona_elegida == "Otro" else persona_elegida)
                guardar = st.form_submit_button("Guardar Word y conservar versión", use_container_width=True)
            if guardar:
                if archivo is None:
                    st.warning("Selecciona un archivo .docx.")
                else:
                    try:
                        with st.spinner("Guardando nueva versión en Drive y Sheets..."):
                            _registrar_borrador(folio, numero, responsable, archivo, procedencia, origen)
                        st.success("Versión registrada sin sobrescribir archivos anteriores.")
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))

    if not ultimo:
        return
    # CSS contextual: el estilo se monta junto al botón, después de las reglas
    # globales del tablero; se aplica solo a las dos acciones de envío.
    st.markdown("""
    <style>
    div[class*="st-key-doc_reenviar_"] [data-testid="stBaseButton-primary"],
    div[class*="st-key-doc_tte_"] [data-testid="stBaseButton-primary"],
    div[class*="st-key-doc_reenviar_"] button[kind="primary"],
    div[class*="st-key-doc_tte_"] button[kind="primary"] {
        background:linear-gradient(110deg,#444648,#535350) !important;
        background-color:#484A49 !important;
        color:#fff !important;
        border:1px solid #C49B59 !important;
        border-left:4px solid #D5AF70 !important;
        border-radius:8px !important;
        box-shadow:0 3px 10px rgba(35,34,32,.16) !important;
        font-weight:650 !important;
    }
    div[class*="st-key-doc_reenviar_"] button:hover,
    div[class*="st-key-doc_tte_"] button:hover {
        background:#5B5A55 !important;
        color:#fff !important;
        border-color:#E3BC79 !important;
    }
    </style>
    """, unsafe_allow_html=True)
    st.markdown("**Siguiente acción**")
    if etapa == ETAPA_BORRADOR:
        if _area_activa_validada() == "setf" and (admin or auxiliar):
            if st.button("Mandar versión a la Tte. Alondra →", key=f"doc_tte_{folio}_{numero}",
                         type="primary", use_container_width=True):
                try:
                    _cambiar_etapa_borrador(folio, numero, ETAPA_TTE)
                    _actualizar_oficio_flujo(folio, numero, {"Estatus": "En revisión"})
                    st.rerun()
                except Exception as error:
                    st.error(_mensaje_error_sheets(error))
        elif admin:
            if st.button("Enviar versión actual a la Tte. Alondra →", key=f"doc_tte_{folio}_{numero}",
                         type="primary", use_container_width=True):
                try:
                    _cambiar_etapa_borrador(folio, numero, ETAPA_TTE)
                    _actualizar_oficio_flujo(folio, numero, {"Estatus": "En revisión"})
                    st.rerun()
                except Exception as error:
                    st.error(str(error))
        elif auxiliar:
            if st.button("Entregar borrador a John y Alejandra →", key=f"doc_entregar_{folio}_{numero}",
                         type="primary", use_container_width=True):
                try:
                    _cambiar_etapa_borrador(folio, numero, ETAPA_ENVIADO)
                    st.rerun()
                except Exception as error:
                    st.error(_mensaje_error_sheets(error))
    elif etapa in (ETAPA_ENVIADO, ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA) and (
            admin or (_area_activa_validada() == "setf" and auxiliar)):
        st.info("Puedes enviar la versión actual a la Tte. Alondra." if _area_activa_validada() == "setf"
                else "John y Alejandra pueden cargar una versión corregida o enviar la actual a la Tte.")
        if st.button("Mandar versión a la Tte. Alondra →" if _area_activa_validada() == "setf"
                     else "Enviar versión actual a la Tte. Alondra →", key=f"doc_reenviar_{folio}_{numero}",
                     type="primary", use_container_width=True):
            try:
                _cambiar_etapa_borrador(folio, numero, ETAPA_TTE)
                # Sincronizar OFICIOS con BORRADORES para todas las rutas de envío.
                _actualizar_oficio_flujo(folio, numero, {"Estatus": "En revisión"})
                st.rerun()
            except Exception as error:
                st.error(_mensaje_error_sheets(error))
    elif etapa == ETAPA_TTE and tte:
        with st.form(key=f"doc_revision_tte_{folio}_{numero}"):
            decision = st.radio("Resultado de la revisión", ["Autorizar para firma", "Solicitar correcciones"],
                                horizontal=True)
            if decision == "Autorizar para firma":
                nuevo_numero_firma = st.text_input("Número del nuevo oficio enviado a firma *", key=f"firma_num_doc_{folio}_{numero}")
                nuevo_asunto_firma = st.text_area("Asunto del nuevo oficio *", key=f"firma_asunto_doc_{folio}_{numero}")
            else:
                nuevo_numero_firma, nuevo_asunto_firma = "", ""
            comentario = st.text_area("Observaciones de revisión (opcional)",
                                      placeholder="Puedes dejar indicaciones o continuar sin comentarios")
            archivo_tte = (st.file_uploader("Word o PDF con modificaciones de la Tte. (opcional)",
                                          type=["docx", "pdf"],
                                          key=f"tte_archivo_opcional_{folio}_{numero}")
                           if decision == "Solicitar correcciones" else None)
            enviar = st.form_submit_button("Registrar decisión", type="primary", use_container_width=True)
        if enviar:
            try:
                nueva = ETAPA_FIRMA if decision == "Autorizar para firma" else ETAPA_CORRECCIONES
                # Registrar primero el adjunto para evitar que quede inaccesible
                # si el expediente cambia de etapa durante la operación.
                if nueva == ETAPA_CORRECCIONES and archivo_tte is not None:
                    with st.spinner("Guardando documento corregido en Drive..."):
                        _adjuntar_documento_corregido_tte(folio, numero, archivo_tte)
                if nueva == ETAPA_FIRMA:
                    _registrar_envio_firma(folio, numero, nuevo_numero_firma, nuevo_asunto_firma)
                _actualizar_oficio_flujo(folio, numero, {
                    "Estatus": "En firma" if nueva == ETAPA_FIRMA else "En correcciones"
                }, autorizacion_tte=True)
                _cambiar_etapa_borrador(folio, numero, nueva, comentario)
                st.rerun()
            except Exception as error:
                st.error(str(error))
    elif etapa == ETAPA_FIRMA and admin:
        st.caption("Registra el resultado de la firma externa; se actualizará OFICIOS y el historial.")
        # El selector fuera del formulario permite mostrar los campos necesarios inmediatamente.
        resultado = st.radio("Resultado de firma externa",
                            ["Regresó firmado", "Regresó con correcciones"],
                            key=f"doc_resultado_firma_{folio}_{numero}")
        with st.form(key=f"doc_firma_{folio}_{numero}"):
            if resultado == "Regresó firmado":
                st.markdown("**Documento firmado recibido**")
                nombre_firmado = st.text_input("Nombre del documento firmado *",
                                              placeholder="Ej. Oficio 21845 firmado")
                enlace_firmado = st.text_input("Liga de Google Drive *",
                                              placeholder="https://drive.google.com/file/d/...")
                fecha_firmado = st.date_input("Fecha de recepción *", value=datetime.now().date(),
                                              format="DD/MM/YYYY")
            else:
                nombre_firmado, enlace_firmado, fecha_firmado = "", "", None
            comentario = st.text_area("Observaciones" +
                                      (" *" if resultado == "Regresó con correcciones" else " (opcional)"))
            enviar = st.form_submit_button("Guardar y actualizar tabla principal",
                                           type="primary", use_container_width=True)
        if enviar:
            try:
                with st.spinner("Guardando firma y actualizando Google Sheets..."):
                    _registrar_resultado_firma(folio, numero, resultado, nombre_firmado,
                                               enlace_firmado, fecha_firmado, comentario)
                st.success("Resultado registrado en OFICIOS y en el expediente documental.")
                st.rerun()
            except Exception as error:
                st.error(_mensaje_error_sheets(error))
                st.info("Si el registro quedó parcialmente guardado, vuelve a abrir el expediente "
                        "y comprueba el estado antes de reintentar.")
    elif etapa == ETAPA_ARCHIVADO:
        st.success("Expediente documental cerrado. Todas las versiones Word permanecen resguardadas.")
        try:
            oficio_firmado = _verificar_expediente_documental(folio, numero)
            for documento in leer_documentos(oficio_firmado.get("Firmado", "")):
                st.link_button("Ver documento firmado: " + documento["nombre"], documento["url"],
                               use_container_width=True)
        except Exception as error:
            st.warning("No se pudo consultar el enlace del documento firmado: " + str(error))
    else:
        st.info("Esperando la siguiente revisión. El historial sigue disponible para consulta.")


# =========================================================
# FUNCIONES DE BÚSQUEDA Y NORMALIZACIÓN
# =========================================================

def normalizar_texto(texto):
    texto = str(texto or "").strip().lower()
    return "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caracter) != "Mn"
    )


def buscar_oficios(consulta):
    registros = _leer_registros_oficios()

    consulta = normalizar_texto(consulta)

    if not consulta:
        return []

    resultados = []

    for registro in registros:
        registro_limpio = {
            normalizar_texto(clave): valor
            for clave, valor in registro.items()
        }

        campos_busqueda = [
            "folio",
            "no. de oficio",
            "asunto",
        ]

        if any(
            consulta in normalizar_texto(
                registro_limpio.get(campo, "")
            )
            for campo in campos_busqueda
        ):
            resultados.append(registro_limpio)

    return resultados


def obtener_oficios():
    return obtener_oficios_por_area(_area_activa_validada())


@st.cache_data(ttl=60, show_spinner=False)
def obtener_oficios_por_area(area):
    try:
        registros = _leer_registros_oficios_por_area(area)
        return pd.DataFrame(registros)
    except Exception as error:
        st.warning(f"No se pudo consultar Google Sheets: {_mensaje_error_sheets(error)}")
        return pd.DataFrame()


def guardar_oficio(datos):
    exigir_sesion()
    hoja = conectar_google_sheets()
    hoja.append_row(
        datos,
        value_input_option="USER_ENTERED"
    )
    _invalidar_cache_documental(oficios=True)


def _eliminar_oficio_confirmado(folio, numero):
    """Elimina un expediente de las tres hojas tras dejar constancia de auditoría.

    Los archivos en Drive NO se borran; así se evita perder documentos originales.
    """
    exigir_sesion()
    if not puede_administrar_oficios_actual():
        raise PermissionError("No tienes permisos administrativos para eliminar oficios en esta área.")
    hojas = {
        "OFICIOS": conectar_google_sheets(),
        "BORRADORES": _hoja_borradores(),
        "EVENTOS_BORRADORES": _hoja_eventos_borradores(),
        "MODALIDADES_TRAMITE": _hoja_modalidades(),
    }
    filas_borrar = {}
    for nombre, hoja in hojas.items():
        datos = hoja.get_all_values()
        if not datos or "Folio" not in datos[0] or "No. de oficio" not in datos[0]:
            raise ValueError(f"Estructura inesperada en {nombre}; no se eliminó nada.")
        i_f, i_n = datos[0].index("Folio"), datos[0].index("No. de oficio")
        filas_borrar[nombre] = [i for i, fila in enumerate(datos[1:], 2)
            if i_f < len(fila) and i_n < len(fila)
            and fila[i_f].strip() == str(folio).strip()
            and fila[i_n].strip() == str(numero).strip()]
    if len(filas_borrar["OFICIOS"]) != 1:
        raise ValueError("El oficio no existe o no tiene una identidad única. No se eliminó nada.")
    libro = hojas["OFICIOS"].spreadsheet
    columnas = ["Folio", "No. de oficio", "Usuario", "Fecha", "Versiones", "Eventos", "Nota"]
    try:
        auditoria = libro.worksheet("OFICIOS_ELIMINADOS")
    except gspread.WorksheetNotFound:
        auditoria = libro.add_worksheet(title="OFICIOS_ELIMINADOS", rows=1000, cols=7)
        auditoria.append_row(columnas, value_input_option="RAW")
    if auditoria.row_values(1) != columnas:
        raise ValueError("La hoja de auditoría tiene encabezados distintos. No se eliminó nada.")
    auditoria.append_row([str(folio), str(numero), identificador_sesion(),
        datetime.now().astimezone().isoformat(timespec="seconds"),
        len(filas_borrar["BORRADORES"]), len(filas_borrar["EVENTOS_BORRADORES"]),
        "Eliminado de Sheets (incluida modalidad); archivos de Google Drive conservados"], value_input_option="RAW")
    # Eliminar de abajo arriba; si hay fallo, la auditoría permite reconstruir el incidente.
    for nombre in ("EVENTOS_BORRADORES", "BORRADORES", "MODALIDADES_TRAMITE", "OFICIOS"):
        for indice in sorted(filas_borrar[nombre], reverse=True):
            hojas[nombre].delete_rows(indice)
    _invalidar_cache_documental(borradores=True, eventos=True, oficios=True)
    _leer_modalidades_por_area.clear()
    # Comprobación de consistencia: ningún panel debe recuperar el expediente.
    for nombre, hoja in hojas.items():
        datos_restantes = hoja.get_all_values()
        if not datos_restantes:
            continue
        cabecera = datos_restantes[0]
        i_f, i_n = cabecera.index("Folio"), cabecera.index("No. de oficio")
        if any(len(fila) > max(i_f, i_n) and
               fila[i_f].strip() == str(folio).strip() and
               fila[i_n].strip() == str(numero).strip()
               for fila in datos_restantes[1:]):
            raise RuntimeError(f"La eliminación quedó incompleta en {nombre}. "
                               "Revisa las hojas antes de reintentar.")




def _eliminar_oficios_lote_confirmado(identidades):
    """Elimina varios oficios usando la eliminación auditada individual.

    No se garantiza atomicidad entre llamadas a Sheets. En caso de error se
    entrega el listado de los que ya fueron eliminados para evitar reintentos ciegos.
    """
    exigir_sesion()
    if not puede_administrar_oficios_actual():
        raise PermissionError("No tienes permisos para eliminar oficios en esta área.")
    identidades = [(str(f).strip(), str(n).strip()) for f, n in identidades]
    if len(identidades) < 2 or len(identidades) != len(set(identidades)):
        raise ValueError("Selecciona al menos dos oficios diferentes.")
    registros_actuales = _leer_registros_oficios()
    for folio, numero in identidades:
        if sum(str(r.get("Folio", "")).strip() == folio and
               str(r.get("No. de oficio", "")).strip() == numero
               for r in registros_actuales) != 1:
            raise ValueError(f"El expediente {folio} / {numero} ya cambió o no es único. No se inició el lote.")
    completados = []
    for folio, numero in identidades:
        try:
            _eliminar_oficio_confirmado(folio, numero)
            completados.append((folio, numero))
        except Exception as error:
            detalle = ", ".join(f"{f} / {n}" for f, n in completados) or "ninguno"
            raise RuntimeError(
                f"El lote se detuvo al procesar {folio} / {numero}. "
                f"Eliminados antes del fallo: {detalle}. "
                f"Revisa Google Sheets antes de repetir. Motivo: {error}"
            ) from error
    return completados


def _panel_eliminar_oficio(folio, numero, *, origen):
    """Confirmación doble reutilizable en tabla y ficha de búsqueda."""
    if not puede_administrar_oficios_actual():
        return
    folio, numero = str(folio).strip(), str(numero).strip()
    clave = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{origen}_{folio}_{numero}")
    with st.container(key=f"zona_eliminar_{clave}"):
        st.warning("⚠️ **Eliminar oficio**: esta acción quitará el registro de OFICIOS, "
                   "sus versiones y su bitácora de Google Sheets. Los documentos en "
                   "Google Drive se conservarán. **No se puede deshacer desde la aplicación.**")
        st.markdown(f"**Oficio seleccionado:** {numero} · **Folio:** {folio}")
        confirmacion = st.checkbox(
            "Confirmo que deseo eliminar definitivamente este oficio y sus registros.",
            key=f"confirmar_eliminacion_{clave}")
        texto_confirmacion = st.text_input(
            "Para confirmar, escribe exactamente el número de oficio",
            key=f"texto_eliminacion_{clave}", placeholder=numero)
        confirmacion_valida = confirmacion and texto_confirmacion.strip() == numero
        if st.button("Eliminar oficio definitivamente", key=f"ejecutar_eliminacion_{clave}",
                     type="secondary", disabled=not confirmacion_valida, use_container_width=True):
            try:
                with st.spinner("Registrando auditoría y eliminando el oficio..."):
                    _eliminar_oficio_confirmado(folio, numero)
                st.session_state.oficios_marcados = set()
                st.session_state.pdf_reporte_generado = None
                st.session_state.epoch_editor = st.session_state.get("epoch_editor", 0) + 1
                st.session_state.exp_identidad = None
                st.success("Oficio eliminado. Los documentos de Drive se conservaron.")
                st.rerun()
            except Exception as error:
                st.error("No se pudo completar la eliminación: " + _mensaje_error_sheets(error))


def _reasignar_oficio_setf_emergencia(folio, numero, responsable_original, nuevo_responsable, motivo):
    """Reasignación excepcional auditable, restringida por cuenta y por área."""
    if not es_administrador_documental_setf():
        raise PermissionError("Reasignación reservada a los cinco administradores SETF.")
    permitidos = _responsables_setf()
    if nuevo_responsable not in permitidos or not str(motivo).strip():
        raise ValueError("Selecciona un usuario SETF y registra el motivo.")
    hoja = conectar_google_sheets()
    datos = hoja.get_all_records()
    coincidencias = [r for r in datos if str(r.get("Folio", "")).strip() == str(folio).strip()
                     and str(r.get("No. de oficio", "")).strip() == str(numero).strip()]
    if len(coincidencias) != 1:
        raise ValueError("El expediente cambió o no es único; vuelve a consultar.")
    actual = str(coincidencias[0].get("Responsable", "")).strip()
    if normalizar_texto(actual) != normalizar_texto(responsable_original):
        raise ValueError("Otro usuario cambió el responsable. Actualiza antes de reintentar.")
    libro = hoja.spreadsheet
    encabezados = ["Fecha", "Usuario", "Folio", "No. de oficio", "Responsable anterior",
                   "Nuevo responsable", "Motivo", "Estado"]
    try:
        bitacora = libro.worksheet("REASIGNACIONES_SETF")
    except gspread.WorksheetNotFound:
        bitacora = libro.add_worksheet(title="REASIGNACIONES_SETF", rows=1000, cols=len(encabezados))
        bitacora.append_row(encabezados, value_input_option="RAW")
    if bitacora.row_values(1) != encabezados:
        raise ValueError("La bitácora de reasignaciones tiene columnas inesperadas.")
    registro = [datetime.now().astimezone().isoformat(timespec="seconds"),
                identificador_sesion(), str(folio), str(numero), actual, nuevo_responsable,
                motivo.strip(), "PENDIENTE"]
    bitacora.append_row(registro, value_input_option="RAW")
    num_bitacora = len(bitacora.get_all_values())
    actualizar_campo_oficio(folio, numero, "Responsable", nuevo_responsable)
    # Si esta marca falla, la entrada PENDIENTE conserva evidencia para verificar.
    bitacora.update_cell(num_bitacora, 8, "COMPLETADO")


def actualizar_campo_oficio(folio_original, numero_original, columna, nuevo_valor):
    """Actualiza una sola celda del oficio, verificando la identidad antes de escribir[cite: 4]."""
    hoja = conectar_google_sheets()
    valores = hoja.get_all_values()
    if not valores:
        raise ValueError("La hoja de oficios está vacía.")
    cabeceras = [c.strip() for c in valores[0]]
    for nombre in ("Folio", "No. de oficio", columna):
        if nombre not in cabeceras:
            raise ValueError(f"No se encontró la columna {nombre} en Google Sheets.")
    col_folio = cabeceras.index("Folio")
    col_numero = cabeceras.index("No. de oficio")
    col_destino = cabeceras.index(columna)
    coincidencias = []
    for indice, fila in enumerate(valores[1:], start=2):
        folio = fila[col_folio].strip() if col_folio < len(fila) else ""
        numero = fila[col_numero].strip() if col_numero < len(fila) else ""
        if folio == str(folio_original).strip() and numero == str(numero_original).strip():
            coincidencias.append(indice)
    if len(coincidencias) != 1:
        raise ValueError("No se pudo identificar de forma única el oficio. No se guardaron cambios.")
    fila_objetivo = coincidencias[0]
    if "Responsable" not in cabeceras:
        raise ValueError("No existe la columna Responsable en Google Sheets.")
    fila_actual = valores[fila_objetivo - 1]
    col_responsable = cabeceras.index("Responsable")
    responsable_actual = fila_actual[col_responsable] if col_responsable < len(fila_actual) else ""
    exigir_permiso_edicion(responsable_actual)
    if _area_activa_validada() == "setf" and not puede_administrar_oficios_actual():
        raise PermissionError("La modificación de datos generales en SETF es administrativa.")
    if columna == "Responsable" and not puede_administrar_oficios_actual():
        if normalizar_texto(nuevo_valor) != normalizar_texto(responsable_actual):
            raise PermissionError("Solo los administradores pueden reasignar oficios.")
    if columna in ("Folio", "No. de oficio"):
        for indice, fila in enumerate(valores[1:], start=2):
            if indice == fila_objetivo:
                continue
            valor = fila[col_destino].strip() if col_destino < len(fila) else ""
            if valor == str(nuevo_valor).strip():
                raise ValueError(f"Ya existe otro oficio con ese valor en {columna}.")
    hoja.update_cell(fila_objetivo, col_destino + 1, nuevo_valor)
    _invalidar_cache_documental(oficios=True)


def actualizar_estatus_y_entrega(folio_original, numero_original, nuevo_estatus, fecha_entrega):
    """Guarda juntos el estatus y la fecha; conserva documentos ya archivados."""
    if nuevo_estatus not in ESTATUS_OFICIOS:
        raise ValueError("Selecciona uno de los seis estatus permitidos.")
    if nuevo_estatus == "Firmado" and not fecha_entrega:
        raise ValueError("Debes seleccionar una fecha de entrega para un oficio firmado.")

    hoja = conectar_google_sheets()
    valores = hoja.get_all_values()
    if not valores:
        raise ValueError("La hoja de oficios está vacía.")
    cabeceras = [nombre.strip() for nombre in valores[0]]
    requeridas = ("Folio", "No. de oficio", "Estatus", "Fecha de entrega")
    for nombre in requeridas:
        if nombre not in cabeceras:
            raise ValueError(f"No existe la columna {nombre} en Google Sheets.")

    col_folio = cabeceras.index("Folio")
    col_numero = cabeceras.index("No. de oficio")
    coincidencias = []
    for indice, fila in enumerate(valores[1:], start=2):
        folio = fila[col_folio].strip() if col_folio < len(fila) else ""
        numero = fila[col_numero].strip() if col_numero < len(fila) else ""
        if folio == str(folio_original).strip() and numero == str(numero_original).strip():
            coincidencias.append(indice)
    if len(coincidencias) != 1:
        raise ValueError("No se pudo identificar de forma única el oficio.")

    if "Responsable" not in cabeceras:
        raise ValueError("No existe la columna Responsable en Google Sheets.")
    fila_actual = valores[coincidencias[0] - 1]
    col_responsable = cabeceras.index("Responsable")
    responsable_actual = fila_actual[col_responsable] if col_responsable < len(fila_actual) else ""
    exigir_permiso_edicion(responsable_actual)
    if _area_activa_validada() == "setf" and not puede_administrar_oficios_actual():
        raise PermissionError("En SETF los cambios de estatus y fecha son administrativos.")

    from gspread.utils import rowcol_to_a1
    fila = coincidencias[0]
    celda_estatus = rowcol_to_a1(fila, cabeceras.index("Estatus") + 1)
    celda_entrega = rowcol_to_a1(fila, cabeceras.index("Fecha de entrega") + 1)
    hoja.batch_update([
        {"range": celda_estatus, "values": [[nuevo_estatus]]},
        {"range": celda_entrega, "values": [[fecha_entrega if nuevo_estatus == "Firmado" else ""]]},
    ], value_input_option="USER_ENTERED")
    _invalidar_cache_documental(oficios=True)


def guardar_edicion_pendiente(folio, numero, cambios):
    """Actualiza los campos de un oficio en OFICIOS con una única solicitud batch."""
    hoja = conectar_google_sheets()
    valores = hoja.get_all_values()
    if not valores:
        raise ValueError("La hoja OFICIOS está vacía.")
    cabeceras = [str(c).strip() for c in valores[0]]
    requeridas = {"Folio", "No. de oficio", "Responsable"} | set(cambios)
    faltantes = requeridas.difference(cabeceras)
    if faltantes:
        raise ValueError("Faltan columnas en OFICIOS: " + ", ".join(sorted(faltantes)))
    encontrados = []
    for i, fila in enumerate(valores[1:], start=2):
        f = fila[cabeceras.index("Folio")].strip() if len(fila) > cabeceras.index("Folio") else ""
        n = fila[cabeceras.index("No. de oficio")].strip() if len(fila) > cabeceras.index("No. de oficio") else ""
        if f == str(folio).strip() and n == str(numero).strip():
            encontrados.append(i)
    if len(encontrados) != 1:
        raise ValueError("No se pudo identificar un único oficio para actualizar.")
    fila_num = encontrados[0]
    fila_actual = valores[fila_num - 1]
    idx_responsable = cabeceras.index("Responsable")
    responsable = fila_actual[idx_responsable] if idx_responsable < len(fila_actual) else ""
    exigir_permiso_edicion(responsable)
    if _area_activa_validada() == "setf" and not puede_administrar_oficios_actual():
        raise PermissionError("Solo los administradores SETF pueden editar datos generales.")
    from gspread.utils import rowcol_to_a1
    actualizaciones = []
    for columna, nuevo in cambios.items():
        indice = cabeceras.index(columna)
        actual = fila_actual[indice] if indice < len(fila_actual) else ""
        if str(actual).strip() != str(nuevo).strip():
            actualizaciones.append({
                "range": rowcol_to_a1(fila_num, indice + 1),
                "values": [[str(nuevo)]],
            })
    if actualizaciones:
        hoja.batch_update(actualizaciones, value_input_option="USER_ENTERED")
        _invalidar_cache_documental(oficios=True)
    return len(actualizaciones)


ESTATUS_OFICIOS = [
    "En elaboración",
    "En correcciones",
    "En revisión",
    "En firma",
    "Firmado",
    "Recaído",
]

# =========================================================
# GENERACIÓN DE REPORTE INSTITUCIONAL EN PDF
# =========================================================

def generar_pdf_oficios(registros):
    """Reporte imprimible: fechas, observaciones y firmas según los responsables."""
    buffer = BytesIO()
    ruta_logo = Path(__file__).parent / "assets" / ("logo_setf.png" if _area_activa_validada() == "setf" else "logo_diseno_geometrico.png")
    ancho, alto = landscape(letter)
    azul = colors.HexColor("#273B49")
    acero = colors.HexColor("#647E8D")
    dorado = colors.HexColor("#B99A65")
    gris = colors.HexColor("#D9E2E7")

    def dibujar_canvas(canvas_obj, doc):
        canvas_obj.saveState()
        if ruta_logo.exists():
            try:
                img_reader = ImageReader(str(ruta_logo))
                canvas_obj.saveState()
                canvas_obj.setFillAlpha(0.10)
                canvas_obj.drawImage(
                    img_reader, ancho / 2 - 190, alto / 2 - 190,
                    width=380, height=380, mask="auto"
                )
                canvas_obj.restoreState()
            except Exception:
                pass
        canvas_obj.setFillColor(azul)
        canvas_obj.rect(35, alto - 49, ancho - 70, 29, fill=1, stroke=0)
        canvas_obj.setFillColor(dorado)
        canvas_obj.rect(35, alto - 52, ancho - 70, 3, fill=1, stroke=0)
        canvas_obj.setFillColor(colors.white)
        canvas_obj.setFont("Helvetica-Bold", 9)
        canvas_obj.drawString(45, alto - 38, "CONTROL DOCUMENTAL - " + ("MESA SETF" if _area_activa_validada() == "setf" else "MESA DE DISEÑO GEOMÉTRICO"))
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.drawRightString(
            ancho - 45, alto - 38,
            f"Fecha de emisión: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.setFillColor(acero)
        canvas_obj.drawString(35, 30, "Sistema de Control Documental - Ferrocarriles")
        canvas_obj.drawRightString(ancho - 35, 30, f"Página {doc.page}")
        canvas_obj.restoreState()

    documento = SimpleDocTemplate(
        buffer, pagesize=(ancho, alto), leftMargin=35, rightMargin=35,
        topMargin=59, bottomMargin=44
    )
    estilo_encabezado = ParagraphStyle(
        "PDFEncabezadoCompacto", fontName="Helvetica-Bold", fontSize=6.4,
        leading=7.4, textColor=colors.white, alignment=TA_CENTER,
        splitLongWords=True
    )
    estilo_celda = ParagraphStyle(
        "PDFCeldaCentrada", fontName="Helvetica", fontSize=6.4,
        leading=7.6, textColor=colors.HexColor("#203849"),
        alignment=TA_CENTER, splitLongWords=True
    )
    estilo_firma = ParagraphStyle(
        "PDFFirma", fontName="Helvetica", fontSize=8,
        leading=11, textColor=azul, alignment=TA_CENTER
    )
    estilo_titulo = ParagraphStyle(
        "PDFTituloCompacto", fontName="Helvetica-Bold", fontSize=10,
        leading=11.5, textColor=azul, alignment=TA_CENTER
    )

    def texto(valor):
        if valor is None or pd.isna(valor):
            return ""
        return str(valor).strip()

    def celda(valor, estilo=estilo_celda):
        return Paragraph(escape(texto(valor)).replace("\n", "<br/>"), estilo)

    def fecha_legible(valor):
        cadena = texto(valor)
        if not cadena:
            return ""
        for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(cadena[:10], formato).strftime("%d/%m/%Y")
            except ValueError:
                continue
        return cadena

    def nombre_pdf_responsable(valor):
        original = texto(valor)
        nombres = {"karina": "Karina Vazquez", "alex": "Alexis Santiago"}
        return nombres.get(normalizar_texto(original), original)

    elementos = [
        Paragraph("RELACIÓN INSTITUCIONAL DE OFICIOS EN TRÁMITE", estilo_titulo),
        Spacer(1, 5),
    ]
    cabeceras = [
        "Folio", "No. de oficio", "Asunto", "Proyecto", "Responsable",
        "Estatus", "Recepción", "Entrega", "Observaciones"
    ]
    datos_tabla = [[celda(c, estilo_encabezado) for c in cabeceras]]
    for _, fila in registros.iterrows():
        estatus = texto(fila.get("Estatus", ""))
        fecha_entrega = (
            fecha_legible(fila.get("Fecha de entrega", "")) or "Pendiente"
            if normalizar_texto(estatus) == normalizar_texto("Firmado")
            else "Pendiente"
        )
        datos_tabla.append([
            celda(fila.get("Folio", "")),
            celda(fila.get("No. de oficio", "")),
            celda(fila.get("Asunto", "")),
            celda(fila.get("Proyecto", "")),
            celda(nombre_pdf_responsable(fila.get("Responsable", ""))),
            celda(estatus),
            celda(fecha_legible(fila.get("Fecha de recepción", ""))),
            celda(fecha_entrega),
            celda(fila.get("Observaciones", "")),
        ])

    anchos_columnas = [43, 65, 145, 90, 65, 58, 70, 70, 116]
    tabla = Table(datos_tabla, colWidths=anchos_columnas, repeatRows=1, hAlign="CENTER")
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), azul),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.45, gris),
    ]))
    elementos.extend([tabla, Spacer(1, 12)])

    auxiliares = {
        "ricardo": "Ricardo Godínez",
        "cecilia": "Cecilia Santos",
        "jesus": "Jesús Rodriguez",
        "alejandra": "Alejandra Cardoso",
        "john": "John Bautista",
        "magali": "Magali Rivera",
        "karina": "Karina Vazquez",
        "alex": "Alexis Santiago",
    }
    responsables = {
        normalizar_texto(v)
        for v in registros.get("Responsable", pd.Series(dtype=str)).fillna("").astype(str)
    }
    nombres_presentes = [
        nombre for clave, nombre in auxiliares.items()
        if clave in responsables or normalizar_texto(nombre) in responsables
    ]

    def bloque_firma_individual(nombre, cargo, ancho_linea=145, matricula=None):
        if matricula:
            texto_firma = (
                f"<b>{escape(nombre)}</b><br/>"
                f"{escape(cargo)}<br/>{escape(matricula)}"
            )
        else:
            texto_firma = f"<b>{escape(nombre)}</b><br/>{escape(cargo)}"
        contenido = Paragraph(texto_firma, estilo_firma)
        bloque = Table(
            [[contenido]], colWidths=[ancho_linea], hAlign="CENTER"
        )
        bloque.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (0, 0), 0.8, azul),
            ("ALIGN", (0, 0), (0, 0), "CENTER"),
            ("TOPPADDING", (0, 0), (0, 0), 7),
            ("LEFTPADDING", (0, 0), (0, 0), 2),
            ("RIGHTPADDING", (0, 0), (0, 0), 2),
        ]))
        return bloque

    if _area_activa_validada() == "diseno":
        elementos.append(KeepTogether([
            Paragraph("VALIDACIÓN Y FIRMAS", estilo_titulo),
            Spacer(1, 18),
            bloque_firma_individual(
                "Jefa de la Mesa de Diseño Geométrico de Vía Férrea",
                "Tte. Alondra Leticia Barajas Ramirez",
                ancho_linea=220,
                matricula="(A-10057999)",
            ),
        ]))

    else:
        elementos.append(Paragraph(
            "REPORTE DE CONSULTA SETF · SIN FIRMAS INSTITUCIONALES",
            estilo_firma,
        ))
        nombres_presentes = []

    if nombres_presentes:
        for inicio in range(0, len(nombres_presentes), 3):
            grupo = nombres_presentes[inicio:inicio + 3]
            celdas = [
                bloque_firma_individual(nombre, "Auxiliar Técnico")
                for nombre in grupo
            ]
            while len(celdas) < 3:
                celdas.append("")
            fila_firmas = Table(
                [celdas], colWidths=[240, 240, 242], hAlign="CENTER"
            )
            fila_firmas.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
            elementos.append(Spacer(1, 20))
            elementos.append(KeepTogether([fila_firmas]))

    documento.build(elementos, onFirstPage=dibujar_canvas, onLaterPages=dibujar_canvas)
    buffer.seek(0)
    return buffer.getvalue()


# =========================================================
# DOCUMENTOS ADJUNTOS
# =========================================================

def agregar_documento(tipo):
    if tipo not in st.session_state:
        st.session_state[tipo] = []
    st.session_state[tipo].append({
        "nombre": "",
        "url": ""
    })


def documentos_formulario(tipo):
    if tipo not in st.session_state:
        st.session_state[tipo] = []

    documentos = st.session_state[tipo]

    for indice, documento in enumerate(documentos):
        st.markdown(f"**Documento {indice + 1}**")
        documento["nombre"] = st.text_input(
            "Nombre del documento",
            value=documento["nombre"],
            key=f"{tipo}_nombre_{indice}"
        )
        documento["url"] = st.text_input(
            "Enlace de Google Drive",
            value=documento["url"],
            key=f"{tipo}_url_{indice}"
        )

    if st.button(
        "+ Agregar documento",
        key=f"agregar_{tipo}"
    ):
        agregar_documento(tipo)
        st.rerun()

    return documentos


# =========================================================
# MODALIDADES DE TRÁMITE (hoja auxiliar, OFICIOS sin cambios)
# =========================================================
COLUMNAS_MODALIDADES = [
    "Folio", "No. de oficio", "Tipo de trámite", "Tiene antecedentes",
    "Nombre principal", "URL principal", "Estado directo", "Registrado por",
    "Fecha registro", "Fecha envío firma", "Autorizado por", "Observaciones",
]


def _hoja_modalidades():
    return _hoja_modalidades_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def _hoja_modalidades_por_area(area):
    libro = conectar_google_sheets_por_area(area).spreadsheet
    try:
        hoja = libro.worksheet("MODALIDADES_TRAMITE")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="MODALIDADES_TRAMITE", rows=1000,
                                  cols=len(COLUMNAS_MODALIDADES))
        hoja.append_row(COLUMNAS_MODALIDADES, value_input_option="RAW")
    if hoja.row_values(1) != COLUMNAS_MODALIDADES:
        raise ValueError("La hoja MODALIDADES_TRAMITE tiene columnas inesperadas.")
    return hoja


def _leer_modalidades():
    return _leer_modalidades_por_area(_area_activa_validada())


@st.cache_data(ttl=25, show_spinner=False)
def _leer_modalidades_por_area(area):
    return _hoja_modalidades_por_area(area).get_all_records()


def _modalidad_expediente(folio, numero):
    """Devuelve el registro auxiliar; ausencia equivale a trámite legado/ordinario."""
    return next((r for r in _leer_modalidades()
                 if str(r.get("Folio", "")).strip() == str(folio).strip()
                 and str(r.get("No. de oficio", "")).strip() == str(numero).strip()), None)


def _registrar_modalidad(folio, numero, tipo, tiene_antecedentes,
                         nombre_principal="", url_principal=""):
    """Registrar modalidad sin alterar ni añadir columnas a OFICIOS."""
    exigir_sesion()
    hoja = _hoja_modalidades()
    if _modalidad_expediente(folio, numero) is not None:
        raise ValueError("Ya hay una modalidad registrada para este expediente.")
    from zoneinfo import ZoneInfo
    fecha = datetime.now(ZoneInfo("America/Mexico_City")).isoformat(timespec="seconds")
    hoja.append_row([
        str(folio), str(numero), str(tipo), "Sí" if tiene_antecedentes else "No",
        str(nombre_principal), str(url_principal),
        "Pendiente de autorización" if tipo == "Directo" else "Registro ordinario",
        identificador_sesion(), fecha, "", "", "",
    ], value_input_option="RAW")
    _leer_modalidades_por_area.clear()


def _actualizar_modalidad_directa(folio, numero, estado, observaciones=""):
    """La etapa se actualiza sobre la fila única, con comprobación de concurrencia."""
    exigir_permiso_revision_final()
    hoja = _hoja_modalidades()
    valores = hoja.get_all_values()
    if not valores or valores[0] != COLUMNAS_MODALIDADES:
        raise ValueError("Estructura inesperada en la hoja de modalidades.")
    matches = [(i, r) for i, r in enumerate(valores[1:], start=2)
               if len(r) >= 2 and r[0].strip() == str(folio).strip()
               and r[1].strip() == str(numero).strip()]
    if len(matches) != 1:
        raise ValueError("No se encontró una modalidad única para este oficio.")
    fila, actual = matches[0]
    if len(actual) < 7 or actual[2] != "Directo":
        raise PermissionError("El trámite no está registrado como directo.")
    if actual[6] != "Pendiente de autorización":
        raise ValueError("Este trámite ya recibió una decisión; recarga la página.")
    from zoneinfo import ZoneInfo
    fecha = datetime.now(ZoneInfo("America/Mexico_City")).isoformat(timespec="seconds")
    cambios = {7: estado, 11: identificador_sesion(), 12: str(observaciones).strip()}
    if estado == "En firma":
        cambios[10] = fecha
    from gspread.utils import rowcol_to_a1
    hoja.batch_update([{"range": rowcol_to_a1(fila, col), "values": [[valor]]}
                       for col, valor in cambios.items()], value_input_option="RAW")
    _leer_modalidades_por_area.clear()


def _autorizar_tramite_directo(folio, numero, autorizar, observaciones="", nuevo_numero="", nuevo_asunto=""):
    """Solo Tte.: confirma un envío; no cuenta un simple registro como salida."""
    exigir_permiso_revision_final()
    registro = _modalidad_expediente(folio, numero)
    if not registro or registro.get("Tipo de trámite") != "Directo":
        raise ValueError("El expediente no está dado de alta como trámite directo.")
    if registro.get("Estado directo") != "Pendiente de autorización":
        raise ValueError("La decisión ya fue registrada.")
    # Validación fresca antes de autorizar, independiente de Session State.
    coincidencias = [r for r in _leer_registros_oficios()
                     if str(r.get("Folio", "")).strip() == str(folio).strip()
                     and str(r.get("No. de oficio", "")).strip() == str(numero).strip()]
    if len(coincidencias) != 1 or str(coincidencias[0].get("Estatus", "")).strip() != "En revisión":
        raise ValueError("El oficio ya no espera revisión, o no es único en OFICIOS.")
    nuevo = "En firma" if autorizar else "En correcciones"
    if autorizar:
        _registrar_envio_firma(folio, numero, nuevo_numero, nuevo_asunto)
    # El origen/versión 0 distingue al trámite directo de los borradores.
    _actualizar_oficio_flujo(folio, numero, {"Estatus": nuevo}, autorizacion_tte=True)
    try:
        _registrar_evento_borrador(
            folio, numero, 0, ETAPA_FIRMA if autorizar else ETAPA_CORRECCIONES,
            "Trámite directo: " + (observaciones.strip() or "Sin observaciones")
        )
    except Exception as error:
        # No fingir que un envío quedó contado cuando falló la bitácora.
        raise RuntimeError("Se actualizó el estatus en OFICIOS, pero falló la bitácora. "
                           "Contacta al administrador para conciliar el envío sin duplicarlo. "
                           f"Detalle: {error}") from error
    try:
        _actualizar_modalidad_directa(folio, numero, nuevo, observaciones)
    except Exception as error:
        st.warning("El cambio y su bitácora ya están guardados, pero no se pudo "
                   "actualizar la ficha auxiliar: " + str(error))


def _actualizar_datos_directo_revisor(folio, numero, estado_previo, estado_nuevo,
                                   nombre_principal=None, url_principal=None):
    """Actualización auxiliar para John/Alejandra sin modificar los permisos de la Tte."""
    exigir_permiso_borradores()
    hoja = _hoja_modalidades()
    valores = hoja.get_all_values()
    filas = [(i, r) for i, r in enumerate(valores[1:], start=2)
             if len(r) >= 7 and r[0].strip() == str(folio).strip()
             and r[1].strip() == str(numero).strip()]
    if len(filas) != 1:
        raise ValueError("No se encontró el expediente directo de forma única.")
    fila, anterior = filas[0]
    if anterior[2] != "Directo" or anterior[6] != estado_previo:
        raise ValueError("El trámite directo cambió de etapa. Actualiza la bandeja.")
    cambios = {7: estado_nuevo}
    if nombre_principal is not None:
        cambios[5] = nombre_principal
    if url_principal is not None:
        cambios[6] = url_principal
    from gspread.utils import rowcol_to_a1
    hoja.batch_update([{"range": rowcol_to_a1(fila, columna), "values": [[dato]]}
                       for columna, dato in cambios.items()], value_input_option="RAW")
    _leer_modalidades_por_area.clear()


def _guardar_resultado_directo(folio, numero, resultado, nombre="", url="", fecha=None, comentario=""):
    """Recepción real del oficio firmado o retorno con correcciones (sin borradores)."""
    exigir_permiso_borradores()
    datos = _modalidad_expediente(folio, numero)
    if not datos or datos.get("Tipo de trámite") != "Directo" or datos.get("Estado directo") != "En firma":
        raise ValueError("No existe un trámite directo activo en firma.")
    oficio = _verificar_expediente_documental(folio, numero)
    if str(oficio.get("Estatus", "")).strip() != "En firma":
        raise ValueError("El oficio no está en firma externa.")
    if resultado == "Regresó firmado":
        from urllib.parse import urlparse
        link = urlparse(str(url).strip())
        if not nombre.strip() or link.scheme != "https" or (link.hostname or "").lower() not in (
                "drive.google.com", "docs.google.com"):
            raise ValueError("Indica el nombre y un enlace válido de Drive del oficio firmado.")
        if fecha is None:
            raise ValueError("Indica la fecha real de recepción de la firma.")
        archivo = json.dumps([{"nombre": nombre.strip(), "url": url.strip()}], ensure_ascii=False)
        _actualizar_oficio_flujo(folio, numero, {
            "Estatus": "Firmado", "Firmado": archivo, "Fecha de entrega": fecha.isoformat()
        })
        try:
            _registrar_evento_borrador(folio, numero, 0, ETAPA_ARCHIVADO,
                                      f"Trámite directo · recepción de firma: {nombre} · {fecha.isoformat()}")
        finally:
            # El estado real de OFICIOS sigue siendo fuente de verdad.
            _actualizar_datos_directo_revisor(folio, numero, "En firma", "Firmado")
    else:
        if not comentario.strip():
            raise ValueError("Describe las correcciones de firma externa.")
        _actualizar_oficio_flujo(folio, numero, {"Estatus": "En correcciones"})
        try:
            _registrar_evento_borrador(folio, numero, 0, ETAPA_RETORNO_FIRMA,
                                      "Trámite directo · " + comentario.strip())
        finally:
            _actualizar_datos_directo_revisor(folio, numero, "En firma", "En correcciones")


def _reenviar_tramite_directo(folio, numero, nombre, url):
    exigir_permiso_borradores()
    from urllib.parse import urlparse
    link = urlparse(str(url).strip())
    if not nombre.strip() or link.scheme != "https" or (link.hostname or "").lower() not in (
            "drive.google.com", "docs.google.com"):
        raise ValueError("Indica un documento actualizado de Google Drive.")
    datos = _modalidad_expediente(folio, numero)
    oficio = _verificar_expediente_documental(folio, numero)
    if not datos or datos.get("Estado directo") != "En correcciones" or oficio.get("Estatus") != "En correcciones":
        raise ValueError("El oficio ya no está en correcciones.")
    _actualizar_oficio_flujo(folio, numero, {"Estatus": "En revisión"})
    try:
        _registrar_evento_borrador(folio, numero, 0, ETAPA_TTE,
                                  "Trámite directo · nueva entrega para revisión")
    finally:
        _actualizar_datos_directo_revisor(folio, numero, "En correcciones",
                                         "Pendiente de autorización", nombre.strip(), url.strip())


def _panel_directos_para_revisores():
    """Recibir firmas y reentregar directos, sin modificar el flujo Word ordinario."""
    if not puede_gestionar_borradores():
        return
    try:
        todos = _leer_modalidades()
        directos = [m for m in todos if m.get("Tipo de trámite") == "Directo"
                    and m.get("Estado directo") in ("En firma", "En correcciones")]
        estados = {(str(x.get("Folio", "")).strip(), str(x.get("No. de oficio", "")).strip()): x
                   for x in _leer_registros_oficios()}
        directos = [m for m in directos
                    if str(estados.get((str(m.get("Folio", "")).strip(),
                                        str(m.get("No. de oficio", "")).strip()), {}).get("Estatus", "")).strip() == m.get("Estado directo")]
    except Exception as error:
        st.warning("Trámites directos: no se pudo leer la información auxiliar: " + str(error))
        return
    if not directos:
        return
    st.markdown("#### Seguimiento · trámites directos")
    st.caption("Aquí se registra el regreso firmado o el reenvío corregido, sin crear borradores Word.")
    for m in directos:
        folio = str(m.get("Folio", "")).strip()
        numero = str(m.get("No. de oficio", "")).strip()
        estado = str(m.get("Estado directo", ""))
        clave = hashlib.sha256(f"direct-review|{folio}|{numero}".encode()).hexdigest()[:12]
        with st.expander(f"{numero} · Folio {folio} · {estado}", expanded=False):
            enlace = str(m.get("URL principal", "")).strip()
            if enlace.startswith("https://"):
                st.link_button("Oficio principal (Drive)", enlace, use_container_width=True)
            if estado == "En correcciones":
                try:
                    eventos_directos = _eventos_expediente(folio, numero)
                    for evento_directo in eventos_directos:
                        accion_evento = str(evento_directo.get("Accion", ""))
                        if accion_evento == ETAPA_CORRECCIONES:
                            st.info("Observaciones de la Tte.: " + str(evento_directo.get("Detalle", "")))
                        elif accion_evento == "Adjunto de correcciones directo":
                            try:
                                detalle_adjunto = json.loads(str(evento_directo.get("Detalle", "")))
                                if str(detalle_adjunto.get("url", "")).startswith("https://"):
                                    st.link_button("Documento devuelto: " + str(detalle_adjunto.get("nombre", "")),
                                                   detalle_adjunto["url"], use_container_width=True)
                            except (ValueError, TypeError, AttributeError):
                                pass
                except Exception:
                    st.caption("La bitácora de observaciones no está disponible temporalmente.")
                with st.form(key=f"directo_reentregar_{clave}"):
                    nombre_nuevo = st.text_input("Nombre del documento actualizado", value=str(m.get("Nombre principal", "")))
                    url_nueva = st.text_input("Enlace del documento actualizado", value=enlace)
                    confirmar = st.checkbox("Confirmo la nueva entrega a la Tte.")
                    enviar = st.form_submit_button("Reenviar a revisión", type="primary", use_container_width=True)
                if enviar:
                    if not confirmar:
                        st.warning("Confirma el reenvío del documento actualizado.")
                    else:
                        try:
                            _reenviar_tramite_directo(folio, numero, nombre_nuevo, url_nueva)
                            st.success("Oficio reenviado a la Tte.")
                            st.rerun()
                        except Exception as error:
                            st.error(str(error))
            else:
                resultado = st.radio("Resultado de firma externa", ["Regresó firmado", "Regresó con correcciones"],
                                     key=f"directo_resultado_{clave}", horizontal=True)
                with st.form(key=f"directo_resultado_form_{clave}"):
                    if resultado == "Regresó firmado":
                        nombre_f = st.text_input("Nombre del oficio firmado", key=f"directo_firm_nombre_{clave}")
                        url_f = st.text_input("Enlace de Drive del oficio firmado", key=f"directo_firm_url_{clave}")
                        fecha_f = st.date_input("Fecha real de recepción", value=None,
                                                 format="DD/MM/YYYY", key=f"directo_firm_fecha_{clave}")
                    else:
                        nombre_f, url_f, fecha_f = "", "", None
                    comentario = st.text_area("Observaciones", key=f"directo_firm_obs_{clave}")
                    confirmar = st.checkbox("Confirmo la recepción y el resultado")
                    guardar = st.form_submit_button("Registrar resultado", type="primary", use_container_width=True)
                if guardar:
                    if not confirmar:
                        st.warning("Confirma el resultado del trámite.")
                    else:
                        try:
                            _guardar_resultado_directo(folio, numero, resultado,
                                                       nombre_f, url_f, fecha_f, comentario)
                            st.success("Resultado registrado.")
                            st.rerun()
                        except Exception as error:
                            st.error(str(error))


def _adjuntar_correccion_directo_tte(folio, numero, archivo):
    """Word/PDF opcional en trámite directo, sin exigir una versión BORRADORES."""
    if archivo is None:
        return None
    exigir_permiso_revision_final()
    registro = _modalidad_expediente(folio, numero)
    if not registro or registro.get("Tipo de trámite") != "Directo" or registro.get("Estado directo") != "Pendiente de autorización":
        raise ValueError("El trámite directo ya no está pendiente de revisión.")
    nombre_original = Path(str(archivo.name)).name
    extension = Path(nombre_original).suffix.lower()
    tipos = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".pdf": "application/pdf"}
    if extension not in tipos:
        raise ValueError("Adjunta únicamente Word .docx o PDF .pdf.")
    contenido = archivo.getvalue()
    if len(contenido) > 40 * 1024 * 1024 or not contenido.startswith(b"PK" if extension == ".docx" else b"%PDF-"):
        raise ValueError("El documento debe ser Word/PDF válido y no superar 40 MB.")
    carpeta = _drive_carpeta_expediente(folio, numero)
    nombre = "tte_directo_" + re.sub(r"[^A-Za-z0-9._-]", "_", nombre_original)[:155]
    sesion = _drive_sesion()
    tipo_mime = tipos[extension]
    inicio = sesion.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "resumable", "fields": "id,name,webViewLink",
                "supportsAllDrives": "true"},
        json={"name": nombre, "parents": [carpeta], "mimeType": tipo_mime},
        headers={"X-Upload-Content-Type": tipo_mime,
                 "X-Upload-Content-Length": str(len(contenido))}, timeout=30,
    )
    if not inicio.ok:
        _drive_json(inicio, "preparar devolución de trámite directo")
    enlace_subida = inicio.headers.get("Location", "")
    if not enlace_subida.startswith("https://www.googleapis.com/"):
        raise RuntimeError("Drive no devolvió una sesión de subida segura.")
    fin = sesion.put(enlace_subida, data=contenido,
                     headers={"Content-Type": tipo_mime,
                              "Content-Length": str(len(contenido))}, timeout=120)
    subido = _drive_json(fin, "guardar documento de corrección directa")
    url = str(subido.get("webViewLink") or f"https://drive.google.com/file/d/{subido['id']}/view")
    _registrar_evento_borrador(folio, numero, 0, "Adjunto de correcciones directo",
                               json.dumps({"nombre": nombre, "url": url}, ensure_ascii=False))
    return {"nombre": nombre, "url": url}


def _panel_tramites_directos_tte():
    """Expedientes directos pendientes de Alondra: consulta y autorización."""
    if not puede_realizar_revision_final():
        return
    try:
        directos = [m for m in _leer_modalidades()
                    if m.get("Tipo de trámite") == "Directo"
                    and m.get("Estado directo") == "Pendiente de autorización"]
        oficios = {(str(r.get("Folio", "")).strip(), str(r.get("No. de oficio", "")).strip()): r
                   for r in _leer_registros_oficios()}
    except Exception as error:
        st.warning("No se pudo consultar la bandeja de trámites directos: " + str(error))
        return
    pendientes = [m for m in directos
                  if str(oficios.get((str(m.get("Folio", "")).strip(),
                                     str(m.get("No. de oficio", "")).strip()), {}).get("Estatus", "")).strip() == "En revisión"]
    if not pendientes:
        return
    st.markdown("#### Trámites directos · Revisión de la Tte.")
    st.caption("Oficios sin etapas intermedias de borrador. Autorizar es una decisión; "
               "el registro por sí solo no cuenta como salida a firma.")
    for m in pendientes:
        folio = str(m.get("Folio", "")).strip()
        numero = str(m.get("No. de oficio", "")).strip()
        clave = hashlib.sha256(f"{folio}|{numero}".encode()).hexdigest()[:12]
        r = oficios.get((folio, numero), {})
        with st.container(border=True, key=f"directo_tte_{clave}"):
            st.markdown(f"**{html.escape(numero)}** · Folio {html.escape(folio)}")
            st.caption(f"Responsable: {r.get('Responsable', '')} · "
                       f"{r.get('Asunto', '')}")
            url = str(m.get("URL principal", "")).strip()
            if url.startswith("https://"):
                st.link_button("Ver oficio principal en Drive", url, use_container_width=True)
            nuevo_numero_directo = st.text_input("Número del nuevo oficio enviado a firma *", key=f"directo_num_nuevo_{clave}")
            nuevo_asunto_directo = st.text_area("Asunto del nuevo oficio *", key=f"directo_asunto_nuevo_{clave}")
            obs = st.text_area("Observaciones (opcional)", key=f"directo_obs_{clave}", height=68)
            adjunto_directo = st.file_uploader(
                "Word o PDF con correcciones (opcional)", type=["docx", "pdf"],
                key=f"directo_adj_tte_{clave}",
            )
            confirm = st.checkbox("Confirmo que revisé el oficio", key=f"directo_confirm_{clave}")
            a, b = st.columns(2)
            with a:
                aprobar = st.button("Autorizar envío a firma", key=f"directo_ok_{clave}",
                                    use_container_width=True, type="primary")
            with b:
                devolver = st.button("Solicitar correcciones", key=f"directo_regresar_{clave}",
                                     use_container_width=True)
            if aprobar or devolver:
                if not confirm:
                    st.warning("Confirma la revisión antes de emitir la decisión.")
                else:
                    try:
                        if devolver and adjunto_directo is not None:
                            _adjuntar_correccion_directo_tte(folio, numero, adjunto_directo)
                        _autorizar_tramite_directo(folio, numero, aprobar, obs, nuevo_numero_directo, nuevo_asunto_directo)
                        st.success("Decisión registrada y expediente actualizado.")
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))


# =========================================================
# FORMULARIO DE REGISTRO
# =========================================================

def _responsables_setf():
    """Retorna responsables válidos de las cuentas SETF configuradas en Secrets."""
    if _area_activa_validada() != "setf":
        return []
    nombres = []
    usuarios = st.secrets["usuarios"]
    for identificador, info in usuarios.items():
        if not hasattr(info, "get"):
            continue
        if str(info.get("area", "diseno")).strip().lower() != "setf":
            continue
        nombre = str(info.get("responsable") or info.get("nombre") or "").strip()
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    # Responsable adicional SETF para registrar oficios; no altera permisos de inicio de sesión.
    if "Ing. Arturo" not in nombres:
        nombres.append("Ing. Arturo")
    adicionales = st.secrets["setf"].get("responsables", [])
    if isinstance(adicionales, list):
        for nombre in adicionales:
            nombre = str(nombre).strip()
            if nombre and nombre not in nombres:
                nombres.append(nombre)
    return nombres


def formulario_nuevo_oficio():

    # Control de limpieza del formulario
    if "version_formulario" not in st.session_state:
        st.session_state.version_formulario = 0

    version = st.session_state.version_formulario

    # Confirmación del registro anterior
    if st.session_state.pop("oficio_guardado", False):
        st.success(
            "Oficio registrado correctamente en Google Sheets. "
            "Puedes capturar un nuevo oficio."
        )

    # Si el alta se guardó pero la escritura de ENVIOS_FIRMA falló,
    # ofrecer reintentar SOLO esa escritura, sin duplicar el oficio en OFICIOS.
    pendiente_firma = st.session_state.get("alta_envio_firma_pendiente")
    if pendiente_firma and pendiente_firma.get("area") == _area_activa_validada():
        st.warning(
            "El oficio ya fue guardado, pero falta registrar su envío a firma "
            "para que se refleje en los indicadores. No vuelvas a darlo de alta."
        )
        if st.button("Reintentar registro del envío a firma", key="reintentar_alta_envio_firma"):
            try:
                _registrar_envio_firma(
                    pendiente_firma["folio"], pendiente_firma["numero"],
                    pendiente_firma["nuevo_numero"], pendiente_firma["nuevo_asunto"],
                )
                st.session_state.pop("alta_envio_firma_pendiente", None)
                st.success("Envío a firma registrado con su fecha y hora automáticas.")
                st.rerun()
            except Exception as error:
                st.error("El registro del envío sigue pendiente: " + _mensaje_error_sheets(error))
        return

    # =========================================================
    # FORMULARIO NUEVO OFICIO - DISEÑO COMPACTO
    # =========================================================

    components.html(
        """
        <html>
        <body style="margin:0; padding:0; background:transparent; font-family:Arial,sans-serif;">

        <div style="
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:15px;
            width:100%;
            box-sizing:border-box;
            border-bottom:1px solid #526878;
            padding-bottom:14px;
        ">

            <div style="flex:1;">
                <div style="color:#D2A45F;font-size:10px;font-weight:bold;letter-spacing:2px;">
                    ADMINISTRACIÓN DOCUMENTAL
                </div>

                <div style="color:white;font-size:22px;font-weight:bold;margin-top:7px;">
                    Registrar nuevo oficio
                </div>

                <div style="color:#B8CAD5;font-size:12px;margin-top:5px;">
                    Alta y seguimiento de documentación
                </div>
            </div>

            <svg xmlns="http://www.w3.org/2000/svg"
                 viewBox="0 0 250 90"
                 style="width:180px;height:75px;flex-shrink:0;">

                <g stroke="#68879A" stroke-width=".7" opacity=".28">
                    <path d="M0 20H250 M0 45H250 M0 70H250"/>
                    <path d="M30 0V90 M80 0V90 M130 0V90 M180 0V90 M230 0V90"/>
                </g>

                <path d="M10 75 L65 75 L110 30 L185 30 L240 10"
                      fill="none" stroke="#D2A45F" stroke-width="2.5"/>

                <path d="M10 84 L70 84 L115 39 L190 39 L245 19"
                      fill="none" stroke="#91AFC0" stroke-width="1.5"/>

                <g fill="#27343F" stroke="#D2A45F" stroke-width="2">
                    <circle cx="65" cy="75" r="3"/>
                    <circle cx="110" cy="30" r="3"/>
                    <circle cx="185" cy="30" r="3"/>
                    <circle cx="240" cy="10" r="3"/>
                </g>
            </svg>

        </div>
        </body>
        </html>
        """,
        height=85,
        scrolling=False
    )

    # Un solo flujo institucional. Tener folio o antecedentes no cambia las etapas.
    # Los trámites directos LEGADOS siguen siendo consultables, pero no se crean
    # nuevos trámites directos desde este formulario.
    tramite_directo = False
    st.markdown("#### Identificación del oficio")
    tiene_folio = st.checkbox(
        "Este oficio tiene folio", value=True,
        key=f"tiene_folio_{version}",
        help="Si no existe folio, el expediente se identifica por su número de oficio.",
    )
    tiene_antecedentes = st.checkbox(
        "Este oficio tiene antecedentes", value=True,
        key=f"tiene_antecedentes_{version}",
        help="Si no tiene antecedentes, el registro continúa normalmente.",
    )
    # Caso especial: el oficio aún no tiene folio ni antecedentes. El número
    # definitivo de salida se captura en «Oficio que saldrá firmado» más abajo.
    # Sigue siendo un trámite ORDINARIO, sin saltar las etapas de revisión/firma.
    registro_solo_numero_salida = not tiene_folio and not tiene_antecedentes
    if registro_solo_numero_salida:
        st.info(
            "**Registro sin folio ni antecedentes.** Indica el proyecto y el número "
            "con el que saldrá el oficio firmado. No necesitas un enlace de Drive "
            "en este momento; podrás vincular el documento al recibirlo firmado."
        )
    elif not tiene_folio or not tiene_antecedentes:
        st.caption("Los campos que no correspondan se guardarán vacíos. "
                   "El estatus, la revisión y la firma siguen el proceso habitual.")

    # =========================================================
    # 01 · DATOS PRINCIPALES
    # =========================================================

    st.markdown(
        """
        <div style="
            color:#E8F0F5;
            font-size:13px;
            font-weight:800;
            margin-bottom:8px;
            text-align:center;
        ">
            01 &nbsp; Datos principales
        </div>
        """,
        unsafe_allow_html=True
    )

    responsables_disponibles = (
        _responsables_setf() if _area_activa_validada() == "setf"
        else ["Alejandra", "Alex", "Jesús", "Cecilia", "Tte. Barajas",
              "Ricardo", "Magali", "John", "Karina"]
    )
    if not responsables_disponibles:
        st.warning("No hay responsables SETF configurados. Agrega al menos un usuario con area = 'setf' en Secrets.")
    if registro_solo_numero_salida:
        # Estos campos no se dibujan arriba ni como placeholders.
        # «numero» recibirá el valor del campo de salida antes de guardar.
        folio = ""
        numero = ""
        st.markdown("**Proyecto al que pertenece el oficio**")
    else:
        col1, col2, col3 = st.columns([1, 1.25, 1])
        with col1:
            if tiene_folio:
                folio = st.text_input(
                    "Folio",
                    key=f"folio_{version}",
                    placeholder="Captura el folio",
                )
            else:
                folio = ""
                st.markdown("**Folio**")
                st.caption("No aplica · expediente sin folio")
        with col2:
            numero = st.text_input("No. de oficio", key=f"numero_{version}")
        with col3:
            responsable = st.selectbox(
                "Responsable", responsables_disponibles, index=None,
                placeholder="Seleccionar responsable", key=f"responsable_{version}"
            )

    col4, col5 = st.columns([1, 1.6])

    # SETF: catálogo plano, sin programa/fase/subproyecto.
    # Diseño Geométrico conserva íntegro su catálogo y jerarquía anteriores.
    if _area_activa_validada() == "setf":
        with col4:
            proyecto_seleccionado = st.selectbox(
                "Proyecto",
                ["Tren Maya", "AIFA-Pachuca", "Extensión de Suburbanos Mex-Qro"],
                index=None,
                placeholder="Seleccionar proyecto",
                key=f"proyecto_setf_{version}",
            )
        with col5:
            proyecto_nuevo = st.text_input(
                "Otro proyecto (si no aparece en la lista)",
                placeholder="Escribe el nombre del nuevo proyecto...",
                key=f"proyecto_nuevo_setf_{version}",
            )
        proyecto = proyecto_nuevo.strip() or (proyecto_seleccionado or "")
        programa = proyecto
        fase = None
    else:
        with col4:
            programa = st.selectbox(
                "Programa",
                [
                    "Tren Maya",
                    "Trenes del Norte"
                ],
                index=None,
                placeholder="Seleccionar programa",
                key=f"programa_{version}"
            )

        with col5:
            proyecto_nuevo = st.text_input(
                "Proyecto nuevo (opcional)",
                placeholder="Escribe aquí si el proyecto no aparece en la lista...",
                key=f"proyecto_nuevo_{version}"
            )

        fase = None
        proyecto_seleccionado = None

        if programa == "Tren Maya":
            proyecto_seleccionado = st.selectbox(
                "Proyecto (Tren Maya)",
                [
                    "Terminales de Carga",
                    "Bases de Mantenimiento",
                    "Vialidades de Acceso a las Edificaciones"
                ],
                index=None,
                placeholder="Seleccionar proyecto",
                key=f"proyecto_maya_{version}"
            )
        elif programa == "Trenes del Norte":
            fase = st.selectbox(
                "Fase",
                [
                    "Ingeniería a Detalle, Fase 1",
                    "Ingeniería Básica, Fase 2"
                ],
                index=None,
                placeholder="Seleccionar fase",
                key=f"fase_norte_{version}"
            )

            if fase == "Ingeniería a Detalle, Fase 1":
                proyecto_seleccionado = st.selectbox(
                    "Proyecto",
                    [
                        "AIFA-Pachuca",
                        "México-Querétaro"
                    ],
                    index=None,
                    placeholder="Seleccionar proyecto",
                    key=f"proyecto_norte_fase1_{version}"
                )
            elif fase == "Ingeniería Básica, Fase 2":
                proyecto_seleccionado = st.selectbox(
                    "Proyecto",
                    [
                        "San Luis Potosí-Saltillo",
                        "Querétaro-San Luis Potosí",
                        "Mazatlán-Los Mochis",
                        "Irapuato-Guadalajara"
                    ],
                    index=None,
                    placeholder="Seleccionar proyecto",
                    key=f"proyecto_norte_fase2_{version}"
                )

        if proyecto_nuevo.strip():
            proyecto = proyecto_nuevo.strip()
        elif programa == "Tren Maya" and proyecto_seleccionado:
            proyecto = f"Tren Maya - {proyecto_seleccionado}"
        elif programa == "Trenes del Norte" and fase and proyecto_seleccionado:
            proyecto = f"Trenes del Norte - {fase} - {proyecto_seleccionado}"
        else:
            proyecto = ""

    if registro_solo_numero_salida:
        # El responsable sigue siendo necesario para permisos y seguimiento.
        responsable = st.selectbox(
            "Responsable del oficio", responsables_disponibles, index=None,
            placeholder="Seleccionar responsable", key=f"responsable_{version}"
        )

    asunto = st.text_area(
        "Asunto",
        height=75,
        key=f"asunto_{version}"
    )

    st.markdown(
        """
        <div style="
            height:1px;
            background:#607789;
            margin:18px 0 16px 0;
        "></div>
        """,
        unsafe_allow_html=True
    )

    # =========================================================
    # 02 · SEGUIMIENTO
    # =========================================================

    st.markdown(
        """
        <div style="
            color:#E8F0F5;
            font-size:13px;
            font-weight:800;
            margin-bottom:8px;
            text-align:center;
        ">
            02 &nbsp; Seguimiento del trámite
        </div>
        """,
        unsafe_allow_html=True
    )

    col6, col7, col8 = st.columns(3)

    with col6:
        if tramite_directo:
            estatus = "En revisión"
            st.text_input("Estatus", value="En revisión · trámite directo",
                          disabled=True, key=f"estado_directo_{version}")
        else:
            # Un oficio sin folio ni antecedentes se registra antes de que
            # exista la versión firmada. Su estado se actualizará después.
            estados_registro = (
                [e for e in ESTATUS_OFICIOS if e != "Firmado"]
                if registro_solo_numero_salida else ESTATUS_OFICIOS
            )
            estatus = st.selectbox(
                "Estatus", estados_registro, key=f"estatus_{version}"
            )

    es_firmado = estatus == "Firmado"

    with col7:
        fecha_recepcion = st.date_input(
            "Fecha de recepción",
            value=None,
            key=f"recepcion_{version}"
        )

    with col8:
        fecha_entrega = st.date_input(
            "Fecha de entrega",
            value=None,
            key=f"entrega_{version}",
            disabled=not es_firmado
        )

    # El envío a firma registra la identidad del documento en ENVIOS_FIRMA.
    # El enlace de Drive permanece bloqueado hasta que el oficio esté firmado.
    es_en_firma = estatus == "En firma"
    numero_envio_firma = ""
    asunto_envio_firma = ""

    if es_en_firma:
        st.markdown("**▣ Identificación del oficio enviado a firma**")
        col_firmado_nombre, col_firmado_link = st.columns([1, 1.6])
        with col_firmado_nombre:
            if registro_solo_numero_salida:
                st.caption("Se utilizará el número de salida como número del oficio enviado a firma.")
                numero_envio_firma = st.text_input(
                    "Nombre o número del oficio enviado a firma *",
                    placeholder="Ej. LFSLPSTL-IB-26-2119",
                    key=f"nuevo_numero_salida_{version}",
                )
                numero = numero_envio_firma.strip()
            else:
                numero_envio_firma = st.text_input(
                    "Nombre o número del oficio enviado a firma *",
                    placeholder="Ej. LFSLPSTL-IB-26-2119",
                    key=f"alta_numero_envio_firma_{version}",
                )
        with col_firmado_link:
            st.text_input(
                "Enlace de Drive · disponible después de firmar",
                value="", disabled=True,
                placeholder="Se habilita al registrar el documento firmado",
                key=f"alta_liga_envio_firma_{version}",
            )
        asunto_envio_firma = st.text_area(
            "Asunto del oficio enviado a firma *",
            key=f"alta_asunto_envio_firma_{version}", height=75,
        )
        nombre_firmado, liga_firmado, firmado = "", "", ""
        st.caption("La fecha y hora de envío se registrarán automáticamente al guardar el oficio.")
    elif registro_solo_numero_salida:
        st.markdown("**▣ Identificación del oficio por firmar**")
        col_firmado_nombre, col_firmado_link = st.columns([1, 1.6])
        with col_firmado_nombre:
            numero_salida = st.text_input(
                "Nombre o número del oficio que saldrá firmado",
                placeholder="Ej. LFSLPSTL-IB-26-2119",
                key=f"nuevo_numero_salida_{version}",
                help="Este será el número de oficio con el que se identificará el expediente.",
            )
        with col_firmado_link:
            st.text_input(
                "Enlace de Drive · disponible después de firmar",
                value="", disabled=True,
                placeholder="Se habilita al registrar el documento firmado",
                key=f"nuevo_liga_firmado_sin_folio_{version}",
            )
        numero = numero_salida.strip()
        nombre_firmado, liga_firmado, firmado = "", "", ""
        st.caption("El número de salida se guardará como número de oficio. "
                   "El enlace podrá añadirse cuando el documento regrese firmado.")
    else:
        st.markdown("**▣ Oficio firmado**")
        col_firmado_nombre, col_firmado_link = st.columns([1, 1.6])
        with col_firmado_nombre:
            nombre_firmado = st.text_input(
                "Nombre del documento",
                placeholder="Ej. LFSLPSTL-IB-26-2119",
                key=f"nuevo_nombre_firmado_{version}",
                disabled=not es_firmado,
            )
        with col_firmado_link:
            liga_firmado = st.text_input(
                "Enlace de Drive",
                placeholder="Pega aquí el enlace de Drive...",
                key=f"nuevo_liga_firmado_{version}",
                disabled=not es_firmado,
            )
        if es_firmado and (nombre_firmado.strip() or liga_firmado.strip()):
            firmado = json.dumps([{
                "nombre": nombre_firmado.strip(), "url": liga_firmado.strip(),
            }], ensure_ascii=False)
        else:
            firmado = ""

    st.markdown(
        """
        <div style="
            height:1px;
            background:#607789;
            margin:18px 0 16px 0;
        "></div>
        """,
        unsafe_allow_html=True
    )

    # =========================================================
    # 03 · DOCUMENTACIÓN
    # =========================================================

    st.markdown(
        """
        <div style="
            color:#E8F0F5;
            font-size:13px;
            font-weight:800;
            margin-bottom:2px;
            text-align:center;
        ">
            03 &nbsp; Documentación vinculada
        </div>

        <div style="
            color:#B9CAD6;
            font-size:10px;
            margin-bottom:12px;
        ">
            Agrega los documentos relacionados con el expediente
        </div>
        """,
        unsafe_allow_html=True
    )

    nombre_principal = ""
    url_principal = ""
    if tramite_directo:
        st.markdown("**Oficio principal · enlace del archivo a tramitar**")
        col_nombre_principal, col_url_principal = st.columns([1, 1.8])
        with col_nombre_principal:
            nombre_principal = st.text_input(
                "Nombre del oficio", key=f"directo_nombre_{version}",
                placeholder="Ej. Oficio 19327")
        with col_url_principal:
            url_principal = st.text_input(
                "Enlace del oficio (Drive)", key=f"directo_url_{version}",
                placeholder="https://drive.google.com/...")

    # Sin folio y sin antecedentes: ocultar también el bloque vacío de
    # antecedentes; anexos continúa disponible si algún expediente los requiere.
    if registro_solo_numero_salida:
        antecedentes = []
        st.markdown("**Anexos (opcionales)**")
        anexos = documentos_formulario(f"anexos_{version}")
    else:
        col_doc1, col_doc2 = st.columns(2)
        with col_doc1:
            st.markdown(
                """
                <div style="
                    color:#E8F0F5;
                    font-size:11px;
                    font-weight:700;
                    margin-bottom:4px;
                ">
                    ▣ &nbsp; Antecedentes
                </div>
                """,
                unsafe_allow_html=True
            )
            if tiene_antecedentes:
                antecedentes = documentos_formulario(f"antecedentes_{version}")
            else:
                antecedentes = []
                st.caption("Sin antecedentes · este campo quedará vacío en Google Sheets.")
        with col_doc2:
            st.markdown(
                """
                <div style="
                    color:#E8F0F5;
                    font-size:11px;
                    font-weight:700;
                    margin-bottom:4px;
                ">
                    ◇ &nbsp; Anexos
                </div>
                """,
                unsafe_allow_html=True
            )
            anexos = documentos_formulario(f"anexos_{version}")

    observaciones = st.text_area(
        "Observaciones",
        height=70,
        key=f"observaciones_{version}"
    )

    st.markdown(
        """
        <div style="
            height:1px;
            background:#607789;
            margin:18px 0 14px 0;
        "></div>
        """,
        unsafe_allow_html=True
    )

    # =====================================================
    # GUARDAR OFICIO
    # =====================================================

    if st.button(
        "Guardar oficio",
        key="guardar_nuevo_oficio",
        use_container_width=True,
        type="primary"
    ):

        # El número de oficio identifica también a los registros sin folio.
        if not numero.strip():
            st.error(
                "Indica el nombre o número con el que saldrá el oficio firmado."
                if registro_solo_numero_salida
                else "Debes capturar el número de oficio."
            )
            return
        if registro_solo_numero_salida and not proyecto:
            st.error("Selecciona el proyecto al que pertenece el oficio.")
            return
        if tiene_folio and not folio.strip():
            st.error("Captura el folio o desmarca «Este oficio tiene folio».")
            return

        if not responsable:
            st.error("Debes seleccionar un responsable.")
            return

        if es_firmado and fecha_entrega is None:
            st.error("Para marcar un oficio como Firmado debes indicar su fecha de entrega.")
            return

        if es_en_firma:
            if not numero_envio_firma.strip() or not asunto_envio_firma.strip():
                st.error("Para registrar un oficio en firma debes indicar el nombre o número y el asunto del oficio enviado a firma.")
                return
            if len(numero_envio_firma.strip()) > 180 or len(asunto_envio_firma.strip()) > 1000:
                st.error("El número o el asunto del oficio enviado a firma supera la longitud permitida.")
                return

        if tramite_directo:
            from urllib.parse import urlparse
            link = urlparse(url_principal.strip())
            if not nombre_principal.strip() or link.scheme != "https" or (link.hostname or "").lower() not in (
                    "drive.google.com", "docs.google.com"):
                st.error("El trámite directo requiere el nombre y una liga válida de Google Drive "
                         "para el oficio principal.")
                return

        try:
            with st.spinner("Guardando oficio en Google Sheets..."):

                # Consultar registros existentes
                existentes = obtener_oficios()

                # Verificar identidad sin confundir todos los folios vacíos.
                # No inventar un folio artificial: el número de oficio distingue
                # los expedientes que carecen de él.
                if not existentes.empty:
                    if "Folio" in existentes.columns and folio.strip():
                        if folio.strip() in existentes["Folio"].fillna("").astype(str).str.strip().values:
                            st.error("Ya existe un oficio registrado con ese folio.")
                            return
                    if "No. de oficio" in existentes.columns:
                        numeros = existentes["No. de oficio"].fillna("").astype(str).str.strip()
                        folios = (existentes["Folio"].fillna("").astype(str).str.strip()
                                  if "Folio" in existentes.columns else None)
                        # Para nuevos oficios sin folio, la referencia debe ser
                        # inequívoca aun si otro registro sí tiene folio.
                        if not folio.strip() and numero.strip() in numeros.values:
                            st.error("Ya existe un expediente con ese número de oficio. "
                                     "Revísalo antes de guardar un registro sin folio.")
                            return
                        if folios is not None and ((numeros == numero.strip()) &
                                                   (folios == folio.strip())).any():
                            st.error("Ese oficio ya está registrado.")
                            return

                # Verificar que existe la hoja auxiliar antes de registrar en OFICIOS.
                # No cambia las 12 columnas originales.
                _hoja_modalidades()
                if _modalidad_expediente(folio.strip(), numero.strip()):
                    st.error("Este número de oficio ya tiene modalidad registrada.")
                    return

                # Preparar información
                proyecto_str = str(proyecto) if proyecto else ""
                datos = [
                    folio.strip(),
                    numero.strip(),
                    asunto.strip(),
                    proyecto_str,
                    responsable.strip(),
                    estatus,
                    fecha_recepcion.isoformat()
                        if fecha_recepcion else "",
                    fecha_entrega.isoformat()
                        if es_firmado and fecha_entrega else "",
                    json.dumps(
                        antecedentes,
                        ensure_ascii=False
                    ),
                    json.dumps(
                        anexos,
                        ensure_ascii=False
                    ),
                    firmado,
                    observaciones.strip()
                ]

                # Validación anticipada de la hoja de envíos antes de crear el expediente.
                # Así se evita guardar un alta cuyos datos de firma ya están ocupados.
                if es_en_firma:
                    for envio in _leer_envios_firma():
                        if normalizar_texto(envio.get("No. de oficio nuevo", "")) == normalizar_texto(numero_envio_firma):
                            st.error("Este número de oficio enviado a firma ya está registrado en esta mesa.")
                            return

                # Registrar el oficio y su modalidad. Son dos hojas de un mismo libro;
                # si la segunda escritura falla se muestra una advertencia específica
                # para conciliar sin volver a duplicar el oficio.
                guardar_oficio(datos)
                try:
                    _registrar_modalidad(
                        folio.strip(), numero.strip(),
                        "Directo" if tramite_directo else "Ordinario",
                        tiene_antecedentes, nombre_principal.strip(), url_principal.strip(),
                    )
                except Exception as error:
                    st.error("El oficio quedó guardado en OFICIOS, pero falló el registro "
                             "de modalidad. No lo captures otra vez: solicita la "
                             "conciliación de MODALIDADES_TRAMITE al administrador. "
                             f"Detalle: {error}")
                    return

            # El envío se registra después de guardar el expediente, utilizando
            # el mismo registro y sello horario que alimenta los indicadores.
            if es_en_firma:
                try:
                    _registrar_envio_firma(
                        folio.strip(), numero.strip(),
                        numero_envio_firma.strip(), asunto_envio_firma.strip(),
                    )
                except Exception as error:
                    st.session_state.alta_envio_firma_pendiente = {
                        "area": _area_activa_validada(),
                        "folio": folio.strip(), "numero": numero.strip(),
                        "nuevo_numero": numero_envio_firma.strip(),
                        "nuevo_asunto": asunto_envio_firma.strip(),
                    }
                    st.error(
                        "El oficio se guardó en OFICIOS, pero no se pudo registrar "
                        "el envío a firma. No lo captures otra vez; utiliza el botón "
                        "de reintento que aparecerá arriba. Detalle: "
                        + _mensaje_error_sheets(error)
                    )
                    return

            # Reiniciar formulario únicamente
            # después de completar el guardado
            st.session_state.oficio_guardado = True
            st.session_state.version_formulario += 1

            st.rerun()

        except Exception as error:
            st.error(
                "No se pudo confirmar el registro. "
                f"Detalle: {error}"
            )


# =========================================================
# CONSULTA DE DOCUMENTOS
# =========================================================

def leer_documentos(valor):
    """Acepta JSON de varios documentos o enlaces pegados en Sheets."""
    if not valor:
        return []
    if isinstance(valor, list):
        elementos = valor
    else:
        cadena = str(valor).strip()
        try:
            elementos = json.loads(cadena)
            if not isinstance(elementos, list):
                elementos = [elementos]
        except (json.JSONDecodeError, TypeError):
            elementos = [parte.strip() for parte in cadena.replace(";", "\n").splitlines() if parte.strip()]

    documentos = []
    for i, elemento in enumerate(elementos, 1):
        if isinstance(elemento, dict):
            url = str(elemento.get("url", "")).strip()
            nombre = str(elemento.get("nombre", "")).strip() or f"Documento {i}"
        else:
            url = str(elemento).strip()
            nombre = f"Documento {i}"
        if url.startswith(("https://", "http://")):
            documentos.append({"nombre": nombre, "url": url})
    return documentos


# Gestión segura de archivos vinculados (independiente del flujo de borradores).
COLUMNAS_EVENTOS_VINCULOS = [
    "Folio", "No. de oficio", "Categoria", "Accion", "Usuario", "Fecha",
    "Documento anterior", "Documento nuevo",
]

def _hoja_eventos_vinculos():
    return _hoja_eventos_vinculos_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def _hoja_eventos_vinculos_por_area(area):
    libro = conectar_google_sheets_por_area(area).spreadsheet
    try:
        hoja = libro.worksheet("EVENTOS_VINCULOS")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="EVENTOS_VINCULOS", rows=1000, cols=8)
        hoja.append_row(COLUMNAS_EVENTOS_VINCULOS, value_input_option="RAW")
    if hoja.row_values(1) != COLUMNAS_EVENTOS_VINCULOS:
        raise ValueError("La estructura de EVENTOS_VINCULOS no coincide; no se realizaron cambios.")
    return hoja


def _permiso_vinculos(oficio):
    if _area_activa_validada() == "setf":
        return es_administrador_documental_setf()
    return es_administrador() or puede_editar_responsable(oficio.get("Responsable", ""))


def _documento_vinculado(nombre, url):
    """Normaliza nombre y enlace, sin cargar archivos ni alterar Google Drive."""
    from urllib.parse import urlparse
    nombre = str(nombre or "").strip()
    url = str(url or "").strip()
    if not nombre:
        raise ValueError("Escribe el nombre del documento.")
    if len(nombre) > 250:
        raise ValueError("El nombre del documento no puede superar 250 caracteres.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Introduce un enlace HTTPS válido de Google Drive.")
    if (parsed.hostname or "").lower() not in ("drive.google.com", "docs.google.com"):
        raise ValueError("El enlace debe pertenecer a Google Drive o Google Docs.")
    return {"nombre": nombre, "url": url}


def _modificar_vinculos(folio, numero, categoria, accion, *, indice=None,
                        documento=None, confirmacion=False):
    """Modifica únicamente una celda de OFICIOS; jamás borra archivos de Drive."""
    exigir_sesion()
    if categoria not in ("Antecedente", "Anexo", "Firmado"):
        raise ValueError("Categoría no permitida.")
    if accion not in ("Agregar", "Reemplazar", "Desvincular"):
        raise ValueError("Operación no permitida.")
    if accion == "Desvincular" and not confirmacion:
        raise ValueError("Debes confirmar la desvinculación.")
    hoja = conectar_google_sheets()
    filas = hoja.get_all_values()
    if not filas:
        raise ValueError("La hoja OFICIOS está vacía.")
    cab = [str(x).strip() for x in filas[0]]
    campos = ("Folio", "No. de oficio", "Responsable", categoria)
    if any(x not in cab for x in campos):
        raise ValueError("Faltan columnas necesarias en OFICIOS.")
    def valor(fila, campo):
        i = cab.index(campo)
        return str(fila[i]).strip() if i < len(fila) else ""
    coincidencias = [(i, fila) for i, fila in enumerate(filas[1:], start=2)
                     if valor(fila, "Folio") == str(folio).strip()
                     and valor(fila, "No. de oficio") == str(numero).strip()]
    if len(coincidencias) != 1:
        raise ValueError("El expediente no existe o hay registros duplicados.")
    fila_num, fila = coincidencias[0]
    oficio = {"Responsable": valor(fila, "Responsable")}
    if not _permiso_vinculos(oficio):
        raise PermissionError("No tienes permiso para modificar estos documentos.")
    actuales = leer_documentos(valor(fila, categoria))
    anterior = None
    if accion != "Agregar":
        if indice is None or not (0 <= indice < len(actuales)):
            raise ValueError("El documento seleccionado ya no está disponible. Actualiza la página.")
        anterior = actuales[indice].copy()
    if accion in ("Agregar", "Reemplazar"):
        if not documento or not str(documento.get("url", "")).startswith("https://"):
            raise ValueError("Se necesita un documento con enlace HTTPS válido.")
        nuevo = {"nombre": str(documento.get("nombre", "")).strip() or "Documento",
                 "url": str(documento["url"]).strip()}
        if accion == "Agregar":
            actuales.append(nuevo)
        else:
            actuales[indice] = nuevo
    else:
        nuevo = None
        actuales.pop(indice)
    # Registrar primero la intención de cambio y su estado anterior para auditoría.
    eventos = _hoja_eventos_vinculos()
    fecha = datetime.now().astimezone().isoformat(timespec="seconds")
    eventos.append_row([
        str(folio), str(numero), categoria, "INICIO " + accion,
        identificador_sesion(), fecha,
        json.dumps(anterior, ensure_ascii=False) if anterior else "",
        json.dumps(nuevo, ensure_ascii=False) if nuevo else "",
    ], value_input_option="RAW")
    hoja.update_cell(fila_num, cab.index(categoria) + 1,
                     json.dumps(actuales, ensure_ascii=False))
    _invalidar_cache_documental(oficios=True)
    verificado = hoja.cell(fila_num, cab.index(categoria) + 1).value or ""
    if leer_documentos(verificado) != actuales:
        raise RuntimeError("No se pudo verificar el cambio en Sheets. Revisa el expediente antes de repetir.")
    try:
        eventos.append_row([
            str(folio), str(numero), categoria, accion, identificador_sesion(),
            datetime.now().astimezone().isoformat(timespec="seconds"),
            json.dumps(anterior, ensure_ascii=False) if anterior else "",
            json.dumps(nuevo, ensure_ascii=False) if nuevo else "",
        ], value_input_option="RAW")
    except Exception as error:
        st.warning(f"El documento se actualizó, pero faltó cerrar el evento de auditoría: {error}")


def _panel_gestion_vinculos(oficio, categoria, etiqueta, icono, clave):
    """Edición de nombre y enlace como en el registro inicial del oficio."""
    folio = str(oficio.get("Folio", "")).strip()
    numero = str(oficio.get("No. de oficio", "")).strip()
    documentos = leer_documentos(oficio.get(categoria, ""))
    editable = _permiso_vinculos(oficio)
    with st.expander(f"{icono}  {etiqueta} ({len(documentos)})"):
        if not documentos:
            st.caption("Sin documentos registrados.")
        for i, doc in enumerate(documentos):
            st.link_button(doc["nombre"], doc["url"], use_container_width=True)
            if editable:
                with st.expander(f"Editar documento {i + 1}", expanded=False):
                    modo = st.radio("Acción", ["Modificar nombre o enlace", "Desvincular"],
                                    horizontal=True, key=f"vinc_modo_{clave}_{i}")
                    if modo == "Modificar nombre o enlace":
                        with st.form(f"vinc_reemplazar_{clave}_{i}"):
                            nombre = st.text_input("Nombre del documento", value=doc["nombre"],
                                                   key=f"vinc_nombre_rep_{clave}_{i}")
                            enlace = st.text_input("Enlace de Google Drive", value=doc["url"],
                                                   key=f"vinc_url_rep_{clave}_{i}")
                            guardar = st.form_submit_button("Guardar cambios", use_container_width=True)
                        if guardar:
                            try:
                                nuevo = _documento_vinculado(nombre, enlace)
                                _modificar_vinculos(folio, numero, categoria, "Reemplazar",
                                                   indice=i, documento=nuevo)
                                st.success("Nombre y enlace actualizados en el expediente.")
                                st.rerun()
                            except Exception as error:
                                st.error(f"No se pudo actualizar el vínculo: {error}")
                    else:
                        confirmar = st.checkbox(
                            "Confirmo que quiero quitar este vínculo del expediente. "
                            "El archivo original no se eliminará de Google Drive.",
                            key=f"vinc_confirmar_{clave}_{i}")
                        if st.button("Desvincular documento", key=f"vinc_eliminar_{clave}_{i}",
                                     disabled=not confirmar):
                            try:
                                _modificar_vinculos(folio, numero, categoria, "Desvincular",
                                                   indice=i, confirmacion=confirmar)
                                st.success("Documento desvinculado; el archivo de Drive no se modificó.")
                                st.rerun()
                            except Exception as error:
                                st.error(str(error))
        if editable:
            with st.expander("＋ Agregar documento", expanded=False):
                with st.form(f"vinc_agregar_{clave}"):
                    nombre = st.text_input("Nombre del documento", key=f"vinc_nombre_add_{clave}")
                    enlace = st.text_input("Enlace de Google Drive", key=f"vinc_url_add_{clave}",
                                           placeholder="https://drive.google.com/file/d/...")
                    guardar = st.form_submit_button("Agregar documento", use_container_width=True)
                if guardar:
                    try:
                        nuevo = _documento_vinculado(nombre, enlace)
                        _modificar_vinculos(folio, numero, categoria, "Agregar", documento=nuevo)
                        st.success("Documento vinculado correctamente.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"No se pudo agregar el vínculo: {error}")


def mostrar_documentos(oficio, titulo=True):
    if titulo:
        st.subheader("Expediente documental")

    firmado = oficio.get("Firmado", "")

    for documento in leer_documentos(firmado):
        st.link_button(documento["nombre"], documento["url"])

    antecedentes = leer_documentos(
        oficio.get("Antecedente", "")
    )

    with st.expander("Ver antecedentes"):
        if not antecedentes:
            st.info("No hay antecedentes registrados.")

        for documento in antecedentes:
            if documento.get("url"):
                st.link_button(
                    documento.get("nombre") or "Ver antecedente",
                    documento["url"]
                )

    anexos = leer_documentos(
        oficio.get("Anexo", "")
    )

    with st.expander("Ver anexos"):
        if not anexos:
            st.info("No hay anexos registrados.")

        for documento in anexos:
            if documento.get("url"):
                st.link_button(
                    documento.get("nombre") or "Ver anexo",
                    documento["url"]
                )


def _panel_revision_tte(registro):
    """Vista ligera exclusiva para la Tte.; conserva el flujo de Sheets/Drive."""
    import html

    folio = str(registro.get("Folio", "")).strip()
    numero = str(registro.get("No. de oficio", "")).strip()
    version = int(registro.get("Version") or 0)
    nombre = str(registro.get("Nombre", "Documento Word"))
    usuario = str(registro.get("Usuario", ""))
    url = str(registro.get("URL", ""))
    drive_id = str(registro.get("Drive ID", ""))
    clave = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{folio}_{numero}")

    # Acabado institucional para la Tte.: títulos centrados y enmarcados.
    # Esta hoja se inserta después del CSS general del tablero.
    st.markdown("""
    <style>
    div[class*="st-key-tte_expediente_"] {
        background:linear-gradient(145deg,#e6e7e7,#dadddf) !important;
        border:1px solid #b9bdc0 !important;
        border-left:4px solid #c39a56 !important;
        border-radius:13px !important;
        box-shadow:0 6px 20px rgba(39,40,39,.12) !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button {
        background:#484A49 !important;color:#fff !important;
        border:1px solid #c49b59 !important;
        border-left:4px solid #d5af70 !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button:hover {
        background:#5b5a55 !important;color:#fff !important;
    }
    </style>
    """, unsafe_allow_html=True)

    # Estilo de módulos segregados (únicamente revisión final de Alondra).
    # Se coloca después de las hojas anteriores y usa claves de Streamlit.
    st.markdown("""
    <style>
    /* Expediente principal: ligero aumento del límite entre oficios. */
    .stMainBlockContainer div[class*="st-key-tte_expediente_"] {
        background:linear-gradient(145deg,#F3EFE6,#EBE5D9)!important;
        border:1px solid #BBA77F!important;
        border-left:4px solid #BF9653!important;
        border-radius:16px!important;
        padding:16px 18px 14px!important;
        margin-top:16px!important;
        margin-bottom:42px!important;
        box-shadow:0 13px 30px rgba(54,42,28,.13)!important;
    }
    /* PASO 01: mesa de consulta, salvia/gris cálido. */
    .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] {
        position:relative!important;
        background:linear-gradient(135deg,#EBF0E9 0%,#F3F4ED 65%,#EDF0E6 100%)!important;
        border:1.5px solid #9CAD91!important;
        border-left:5px solid #70866A!important;
        border-radius:13px!important;
        padding:14px 16px 15px!important;
        margin-top:15px!important;
        margin-bottom:4px!important;
        box-shadow:0 5px 14px rgba(58,81,57,.08)!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] [data-testid="stVerticalBlock"] {
        gap:.55rem!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] .tte-document-line {
        background:#FAFBF8!important;
        border:1px solid #B7C5B0!important;
        border-left:4px solid #748B6D!important;
        padding:11px 13px!important;
        margin:0 0 2px!important;
        border-radius:9px!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] [data-testid="stExpander"] {
        background:#FAFBF6!important;
        border-color:#B4C1A8!important;
        border-radius:9px!important;
    }
    /* Secciones Word/PDF: que se lean como parte de consulta documental. */
    .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] [class*="st-key-tte_doc_"] {
        border-color:#AEB9A0!important;
    }
    .tte-module-heading {
        display:flex;align-items:center;gap:13px;
        margin:0 0 10px;position:relative;
        padding-bottom:10px;border-bottom:1px solid #BECBB7;
    }
    .tte-module-icon {
        display:flex;align-items:center;justify-content:center;flex:none;
        width:36px;height:36px;border-radius:9px;
        color:#4C6348;background:#DCE8D7;border:1px solid #A7BA9E;
        font-size:20px;font-weight:700;
    }
    .tte-module-copy {display:flex;flex-direction:column;gap:2px;min-width:0;}
    .tte-module-overline {
        color:#607A5E!important;font-size:9px!important;font-weight:850!important;
        letter-spacing:1.65px!important;
    }
    .tte-module-copy strong {
        color:#304A36!important;font-size:16px!important;line-height:1.3!important;
    }
    .tte-module-copy small {
        color:#617568!important;font-size:11px!important;line-height:1.45!important;
    }
    .tte-module-tag {
        margin-left:auto;align-self:center;white-space:nowrap;
        border:1px solid #ADBF9E;border-radius:20px;
        background:#E1EADB;color:#4C6D4A;
        font-size:9px;font-weight:850;letter-spacing:1px;padding:6px 9px;
    }
    .tte-stage-separator {
        display:flex;align-items:center;gap:12px;
        margin:18px 2px 13px;color:#99715E;
        font-weight:800;font-size:9px;letter-spacing:1.35px;text-align:center;
    }
    .tte-stage-separator span {
        height:1px;flex:1;min-width:15px;
        background:linear-gradient(90deg,transparent,#BD9881,transparent);
    }
    /* PASO 02: una superficie deliberadamente distinta, durazno marfil. */
    .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] {
        background:linear-gradient(145deg,#FCF3ED,#F8EAE0)!important;
        border:1.5px solid #C69B82!important;
        border-left:5px solid #B6795B!important;
        border-top:3px solid #BA8164!important;
        border-radius:13px!important;
        box-shadow:0 7px 17px rgba(117,70,49,.09)!important;
        margin:0 0 11px!important;
        padding:17px 18px 16px!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] .dictamen-kicker {
        color:#9C6048!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] .dictamen-titulo {
        color:#583B35!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] .dictamen-descripcion {
        color:#846358!important;
    }
    .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] {
        background:rgba(255,252,248,.64)!important;
        border:1px solid #D9B9A6!important;
        border-radius:10px!important;
        padding:12px 13px!important;
        margin-top:9px!important;
    }
    .tte-expediente-cierre {
        display:flex;align-items:center;gap:12px;margin:4px 0 0;
        padding:8px 0 0;color:#8B7967;text-align:center;
        font-weight:750;letter-spacing:1.45px;font-size:9px;
        border-top:1px solid #C9BFAE;
    }
    .tte-fin-linea {
        height:1px;flex:1;min-width:10px;
        background:linear-gradient(90deg,transparent,#C2AE8D,transparent);
    }
    @media(max-width:760px) {
        .stMainBlockContainer div[class*="st-key-tte_expediente_"] {padding:12px!important;margin-bottom:28px!important;}
        .stMainBlockContainer div[class*="st-key-tte_consulta_panel_"] {padding:12px!important;}
        .tte-module-tag {display:none;}
        .tte-module-copy strong {font-size:14px!important;}
        .tte-stage-separator {letter-spacing:.65px;font-size:8px;}
        .tte-expediente-cierre {letter-spacing:.5px;font-size:8px;}
    }
    </style>
    """, unsafe_allow_html=True)

    with st.container(key=f"tte_expediente_{clave}", border=True):
        # Encabezado del expediente: se conserva el diseño original, sin centrarlo.
        st.markdown(
            '<div class="tte-file-top">'
            '<span class="tte-file-label">EXPEDIENTE PARA AUTORIZACIÓN</span>'
            '<span class="tte-file-chip">● Requiere su decisión</span>'
            '</div>', unsafe_allow_html=True
        )
        a, b = st.columns([4, 1], vertical_alignment="center")
        with a:
            st.markdown(f'<div class="tte-file-title">{html.escape(numero)}</div>'
                        f'<div class="tte-file-meta">Folio {html.escape(folio)} · '
                        f'Versión V{version:02d} · Enviado por {html.escape(usuario)}</div>',
                        unsafe_allow_html=True)
        with b:
            st.markdown('<div class="tte-step-badge">ETAPA 2 DE 3<br><strong>Revisión final</strong></div>',
                        unsafe_allow_html=True)

        st.markdown("""<div class="tte-timeline">
            <div class="tte-step done"><span>✓</span> Borrador recibido</div>
            <div class="tte-step current"><span>2</span> Revisión de la Tte.</div>
            <div class="tte-step"><span>3</span> Firma externa</div>
        </div>""", unsafe_allow_html=True)

        # PASO 01 — Consulta documental. El contenedor solo cambia presentación;
        # no interviene en la descarga, la conversión o los permisos de Drive.
        with st.container(key=f"tte_consulta_panel_{clave}", border=True):
            st.markdown(
                '<div class="tte-module-heading">'
                '<div class="tte-module-icon" aria-hidden="true">▤</div>'
                '<div class="tte-module-copy">'
                '<span class="tte-module-overline">PASO 01 · CONSULTA</span>'
                '<strong>Documentos del oficio</strong>'
                '</div></div>',
                unsafe_allow_html=True,
            )
            # Nombre íntegro en el tooltip; en pantalla no domina toda la ficha.
            nombre_breve = nombre if len(nombre) <= 86 else nombre[:82] + "…"
            st.markdown(
                '<div class="tte-document-line notranslate" translate="no" lang="es-MX">'
                '<span aria-hidden="true">📄</span><div>'
                '<strong>Versión vigente</strong>'
                f'<small title="{html.escape(nombre, quote=True)}">{html.escape(nombre_breve)}</small>'
                '</div></div>', unsafe_allow_html=True,
            )

            # Descargar/visualizar el mismo original privado; lógica sin cambios.
            _botones_word_y_pdf(drive_id, nombre, f"tte_{folio}_{numero}_{version}")

            with st.expander("Historial PDF · versiones anteriores", expanded=False):
                try:
                    versiones = _borradores_expediente(folio, numero)
                    anteriores = [v for v in versiones if int(v.get("Version") or 0) < version]
                    eventos = _eventos_expediente(folio, numero)
                    _mostrar_historial_pdf_por_version(
                        folio, numero, anteriores, eventos,
                        actual=next((v for v in versiones if int(v.get("Version") or 0) == version), None),
                    )
                except Exception as error:
                    st.warning(f"Historial temporalmente no disponible: {_mensaje_error_sheets(error)}")

        st.markdown(
            '<div class="tte-stage-separator" aria-hidden="true">'
            '<span></span><strong>REVISIÓN REALIZADA · EMITIR RESOLUCIÓN</strong><span></span>'
            '</div>',
            unsafe_allow_html=True,
        )

        # Panel de dictamen V2: dos botones-tarjeta NATIVOS de Streamlit.
        # Evita los selectores CSS de radio que no funcionaron en versiones previas.
        st.markdown("""
        <style>
        /* Panel de dictamen, aislado por identificador de expediente. */
        div[class*="st-key-tte_dictamen_panel_"] {
            position:relative!important;
            padding:17px 20px 18px!important;
            margin-top:12px!important;
            border:1px solid #C7AE96!important;
            border-left:4px solid #B88A66!important;
            border-radius:14px!important;
            background:linear-gradient(145deg,#FFFEFB 0%,#F5F0E8 100%)!important;
            box-shadow:0 6px 18px rgba(61,43,46,.075)!important;
            overflow:hidden!important;
        }
        div[class*="st-key-tte_dictamen_panel_"]::before {
            content:"";position:absolute;inset:0 0 auto auto;
            width:44%;height:76px;pointer-events:none;
            background:
              repeating-linear-gradient(165deg,transparent 0 17px,rgba(188,147,107,.15) 18px,transparent 19px 35px),
              linear-gradient(90deg,transparent,rgba(194,153,113,.09));
            mask-image:linear-gradient(90deg,transparent,black);
            opacity:.8;
        }
        div[class*="st-key-tte_dictamen_panel_"] .dictamen-cabecera {
            display:flex;align-items:center;justify-content:space-between;
            position:relative;z-index:1;gap:14px;margin-bottom:14px;
        }
        div[class*="st-key-tte_dictamen_panel_"] .dictamen-kicker {
            font-size:10px!important;font-weight:850!important;
            letter-spacing:1.8px!important;color:#9D7055!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] .dictamen-titulo {
            font-size:23px!important;font-weight:800!important;
            line-height:1.18!important;color:#403540!important;margin-top:4px;
        }
        div[class*="st-key-tte_dictamen_panel_"] .dictamen-descripcion {
            color:#81746F!important;font-size:12px!important;margin-top:5px;
        }
        div[class*="st-key-tte_dictamen_panel_"] .dictamen-sello {
            width:38px;height:38px;flex-shrink:0;
            display:flex;align-items:center;justify-content:center;
            border:1px solid #C9AB8D;border-radius:10px;
            background:#F1E6D9;color:#946F50;font-size:20px;
        }

        /* TARJETAS DE DECISIÓN */
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_"] button,
        div[class*="st-key-tte_dict_choice_"] button {
            position:relative!important;
            display:block!important;
            width:100%!important;
            min-height:106px!important;
            margin:0!important;
            padding:22px 18px 16px 78px!important;
            border:1.6px solid #CBBFB2!important;
            border-radius:15px!important;
            text-align:left!important;
            white-space:normal!important;
            box-shadow:inset 0 1px 0 rgba(255,255,255,.82)!important;
            transition:border-color .18s, background .18s, transform .18s, box-shadow .18s!important;
            overflow:hidden!important;
        }
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_"] button:hover,
        div[class*="st-key-tte_dict_choice_"] button:hover {
            transform:translateY(-1px)!important;
            box-shadow:0 6px 16px rgba(74,56,52,.10)!important;
        }
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_"] button:focus-visible,
        div[class*="st-key-tte_dict_choice_"] button:focus-visible {
            outline:2px solid #B98B65!important;outline-offset:2px!important;
        }
        div[class*="st-key-tte_dict_choice_"] button > div,
        div[class*="st-key-tte_dict_choice_"] button > div > div,
        div[class*="st-key-tte_dict_choice_"] button span,
        div[class*="st-key-tte_dict_choice_"] button p {
            display:block!important;
            width:100%!important;
            text-align:left!important;
            justify-content:flex-start!important;
            align-items:flex-start!important;
        }
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_"] button p,
        div[class*="st-key-tte_dict_choice_"] button p {
            margin:0!important;
            color:#43393C!important;
            font-size:15px!important;
            font-weight:790!important;
            line-height:1.15!important;
            letter-spacing:.1px!important;
            text-align:left!important;
        }
        div[class*="st-key-tte_dict_choice_"] button::before {
            position:absolute;left:18px;top:20px;
            width:34px;height:34px;border-radius:10px;
            display:grid;place-items:center;
            font-size:20px;font-weight:850;
            box-shadow:inset 0 1px 0 rgba(255,255,255,.65);
        }
        div[class*="st-key-tte_dict_choice_"] button::after {
            position:absolute;left:78px;right:18px;top:57px;
            font-size:12px;font-weight:520;line-height:1.35;text-align:left;
        }

        /* AUTORIZAR */
        div[class*="st-key-tte_dict_choice_accept_"] button {
            background:linear-gradient(180deg,#EEF5EA 0%,#E6EFE2 100%)!important;
            border-color:#A9BAA3!important;
        }
        div[class*="st-key-tte_dict_choice_accept_"] button::before {
            content:"✓";background:#D9E8D3;color:#547253;
        }
        div[class*="st-key-tte_dict_choice_accept_"] button::after {
            content:"Documento validado para continuar con el proceso de firma.";
            color:#5F7860;
        }
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_accept_selected_"] button,
        div[class*="st-key-tte_dict_choice_accept_selected_"] button {
            background:linear-gradient(180deg,#DCECD8 0%,#D3E5CF 100%)!important;
            border:2px solid #6F9573!important;
            box-shadow:inset 5px 0 0 #6F9573, 0 6px 16px rgba(70,103,75,.10)!important;
        }
        div[class*="st-key-tte_dict_choice_accept_selected_"] button p {
            color:#304C34!important;
        }
        div[class*="st-key-tte_dict_choice_accept_selected_"] button::after {
            color:#48664B!important;
        }

        /* CORRECCIONES */
        div[class*="st-key-tte_dict_choice_return_"] button {
            background:linear-gradient(180deg,#FBF0EE 0%,#F8E8E6 100%)!important;
            border-color:#D3A9A1!important;
        }
        div[class*="st-key-tte_dict_choice_return_"] button::before {
            content:"↶";background:#F0D9D4;color:#9D5A53;
        }
        div[class*="st-key-tte_dict_choice_return_"] button::after {
            content:"Devolver el documento con las observaciones necesarias.";
            color:#8D645D;
        }
        .stMainBlockContainer div[class*="st-key-tte_dict_choice_return_selected_"] button,
        div[class*="st-key-tte_dict_choice_return_selected_"] button {
            background:linear-gradient(180deg,#F5DCD8 0%,#F1D2CD 100%)!important;
            border:2px solid #B96F67!important;
            box-shadow:inset 5px 0 0 #B96F67, 0 6px 16px rgba(146,83,75,.10)!important;
        }
        div[class*="st-key-tte_dict_choice_return_selected_"] button p {
            color:#6E3935!important;
        }
        div[class*="st-key-tte_dict_choice_return_selected_"] button::after {
            color:#86514D!important;
        }

        /* FORMULARIO */
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] {
            margin-top:10px!important;padding:14px 0 0!important;
            background:transparent!important;border:0!important;
            border-top:1px solid #DDCDBD!important;border-radius:0!important;
            box-shadow:none!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] [data-testid="stWidgetLabel"] p {
            font-size:12px!important;font-weight:680!important;color:#584A4A!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] [data-baseweb="textarea"] > div,
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] [data-testid="stFileUploaderDropzone"] {
            background:#FFFEFC!important;border:1px solid #CDBDAD!important;
            border-radius:10px!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stForm"] textarea {
            background:#FFFEFC!important;color:#463941!important;
            min-height:74px!important;font-size:13px!important;line-height:1.45!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stCheckbox"] p {
            font-size:12px!important;color:#605454!important;
        }
        .stMainBlockContainer div[class*="st-key-tte_dictamen_panel_"] [data-testid="stFormSubmitButton"] button,
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stFormSubmitButton"] button {
            background:#5D564D!important;color:#FFFDFB!important;
            border:1px solid #BD9169!important;
            border-left:4px solid #D0A675!important;
            border-radius:10px!important;
            min-height:48px!important;font-size:14px!important;font-weight:770!important;
            box-shadow:0 4px 12px rgba(49,36,47,.13)!important;
        }
        div[class*="st-key-tte_dictamen_panel_"] [data-testid="stFormSubmitButton"] button:hover {
            background:#70675C!important;color:#fff!important;
        }

        @media(max-width:780px) {
            div[class*="st-key-tte_dictamen_panel_"]{padding:14px 12px!important;}
            div[class*="st-key-tte_dict_choice_"] button{
                min-height:114px!important;
                padding:19px 12px 14px 64px!important;
            }
            div[class*="st-key-tte_dict_choice_"] button::before{
                left:14px;top:18px;width:30px;height:30px;font-size:18px;
            }
            div[class*="st-key-tte_dict_choice_"] button::after{
                left:64px;right:12px;top:54px;font-size:11.5px;
            }
            div[class*="st-key-tte_dict_choice_"] button p{
                font-size:14px!important;
            }
        }

        /* COLORES DE RELLENO: verde / rojo compatibles con marfil y cobre */
        .stMainBlockContainer [class*="st-key-tte_dict_choice_accept_"] button,
        [class*="st-key-tte_dict_choice_accept_"] button {
            background:linear-gradient(135deg,#DCEBD7 0%,#CEE2C9 100%)!important;
            border:1.8px solid #84A482!important;
        }
        .stMainBlockContainer [class*="st-key-tte_dict_choice_accept_selected_"] button,
        [class*="st-key-tte_dict_choice_accept_selected_"] button {
            background:linear-gradient(135deg,#CCE4C8 0%,#B5D6B3 100%)!important;
            border:2px solid #5D8B65!important;
            box-shadow:inset 5px 0 0 #5D8B65,0 5px 15px rgba(56,102,67,.13)!important;
        }
        .stMainBlockContainer [class*="st-key-tte_dict_choice_return_"] button,
        [class*="st-key-tte_dict_choice_return_"] button {
            background:linear-gradient(135deg,#F5DDD9 0%,#ECCDC7 100%)!important;
            border:1.8px solid #CB8A80!important;
        }
        .stMainBlockContainer [class*="st-key-tte_dict_choice_return_selected_"] button,
        [class*="st-key-tte_dict_choice_return_selected_"] button {
            background:linear-gradient(135deg,#F0CCC6 0%,#E9B3AC 100%)!important;
            border:2px solid #AD5E58!important;
            box-shadow:inset 5px 0 0 #AD5E58,0 5px 15px rgba(130,66,62,.14)!important;
        }
        /* El texto pertenece a una fila distinta de la descripción; no se superponen. */
        .stMainBlockContainer [class*="st-key-tte_dict_choice_"] button,
        [class*="st-key-tte_dict_choice_"] button {
            display:flex!important;
            align-items:flex-start!important;
            justify-content:flex-start!important;
            min-height:112px!important;
            padding:22px 15px 44px 68px!important;
        }
        .stMainBlockContainer [class*="st-key-tte_dict_choice_"] button > div,
        [class*="st-key-tte_dict_choice_"] button > div {
            align-self:flex-start!important;
            display:flex!important;
            align-items:flex-start!important;
            justify-content:flex-start!important;
            min-width:0!important;
        }
        .stMainBlockContainer [class*="st-key-tte_dict_choice_"] button p,
        [class*="st-key-tte_dict_choice_"] button p {
            text-align:left!important;
            font-size:14px!important;
            font-weight:760!important;
            line-height:1.32!important;
            margin:0!important;
            padding:0!important;
            overflow-wrap:break-word!important;
        }
        [class*="st-key-tte_dict_choice_"] button::before {
            left:17px!important;top:20px!important;
            width:33px!important;height:33px!important;
        }
        [class*="st-key-tte_dict_choice_"] button::after {
            left:68px!important;right:14px!important;
            top:auto!important;bottom:18px!important;
            font-size:11.5px!important;
            line-height:1.35!important;
            white-space:normal!important;
        }
        /* Cada acción obtiene sus colores de título, sin alterar la jerarquía tipográfica. */
        [class*="st-key-tte_dict_choice_accept_"] button p {color:#355439!important;}
        [class*="st-key-tte_dict_choice_return_"] button p {color:#763D39!important;}
        @media(max-width:780px) {
            .stMainBlockContainer [class*="st-key-tte_dict_choice_"] button,
            [class*="st-key-tte_dict_choice_"] button {
                min-height:122px!important;padding:18px 11px 50px 59px!important;
            }
            [class*="st-key-tte_dict_choice_"] button::before {
                left:12px!important;top:16px!important;
            }
            [class*="st-key-tte_dict_choice_"] button::after {
                left:59px!important;right:9px!important;bottom:12px!important;
                font-size:10.8px!important;
            }
        }
        </style>
        """, unsafe_allow_html=True)

        with st.container(key=f"tte_dictamen_panel_{clave}"):
            st.markdown(
                '<div class="dictamen-cabecera">'
                '<div><div class="dictamen-kicker">PASO 02 · EMISIÓN DE DICTAMEN</div>'
                '<div class="dictamen-titulo">Dictamen de revisión</div>'
                '<div class="dictamen-descripcion">Registra la autorización o devuelve el documento con observaciones.</div>'
                '</div><div class="dictamen-sello" aria-hidden="true">◇</div></div>',
                unsafe_allow_html=True,
            )
            decision_key = f"tte_decision_{clave}"
            if st.session_state.get(decision_key) not in ("Autorizar para firma", "Solicitar correcciones"):
                st.session_state[decision_key] = "Autorizar para firma"

            # Botones auténticos, enteramente clicables. Se guarda únicamente el
            # resultado seleccionado en Session State; no se registra ningún cambio
            # documental hasta el submit confirmado que está más abajo.
            col_autorizar, col_correcciones = st.columns(2, gap="medium")
            with col_autorizar:
                with st.container(key=f"tte_dict_choice_accept_{'selected' if st.session_state[decision_key] == 'Autorizar para firma' else 'idle'}_{clave}"):
                    if st.button(
                        "Autorizar para firma",
                        key=f"tte_elegir_autorizar_{clave}",
                        use_container_width=True,
                        help="Documento validado para continuar con el proceso de firma.",
                    ):
                        st.session_state[decision_key] = "Autorizar para firma"
                        st.rerun()
            with col_correcciones:
                with st.container(key=f"tte_dict_choice_return_{'selected' if st.session_state[decision_key] == 'Solicitar correcciones' else 'idle'}_{clave}"):
                    if st.button(
                        "Solicitar correcciones",
                        key=f"tte_elegir_correcciones_{clave}",
                        use_container_width=True,
                        help="Devolver el documento con las observaciones necesarias.",
                    ):
                        st.session_state[decision_key] = "Solicitar correcciones"
                        st.rerun()
            decision = st.session_state[decision_key]
            with st.form(key=f"tte_form_{clave}"):
                if decision == "Autorizar para firma":
                    nuevo_numero_firma = st.text_input("Número del nuevo oficio enviado a firma *", key=f"tte_num_nuevo_{clave}")
                    nuevo_asunto_firma = st.text_area("Asunto del nuevo oficio *", key=f"tte_asunto_nuevo_{clave}")
                else:
                    nuevo_numero_firma, nuevo_asunto_firma = "", ""
                if decision == "Solicitar correcciones":
                    archivo_tte = st.file_uploader(
                        "Word o PDF con modificaciones de la Tte. (opcional)",
                        type=["docx", "pdf"], key=f"tte_word_devolucion_{clave}",
                        help=("Adjunto opcional. El personal autorizado de SETF podrá consultarlo en Drive como Word o PDF."
                              if _area_activa_validada() == "setf" else
                              "Adjunto opcional. John y Alejandra podrán consultarlo en Drive como Word o PDF."),
                    )
                    comentario = st.text_area(
                        "Comentarios u observaciones (opcional)",
                        placeholder="Indica las correcciones necesarias para esta versión...",
                        key=f"tte_observaciones_{clave}", height=76,
                    )
                else:
                    comentario = st.text_area(
                        "Observaciones adicionales (opcional)",
                        placeholder="Agrega una indicación para el trámite de firma, si hace falta...",
                        key=f"tte_observaciones_{clave}", height=76,
                    )
                confirmado = st.checkbox(
                    "Confirmo que revisé el documento y deseo registrar esta decisión.",
                    key=f"tte_confirmar_{clave}",
                )
                enviar = st.form_submit_button(
                    "✓ Autorizar y enviar a firma →" if decision == "Autorizar para firma"
                    else "↶ Devolver para correcciones →",
                    type="primary", use_container_width=True,
                )
        if enviar:
            if not confirmado:
                st.warning("Marque la confirmación antes de registrar su decisión.")
            else:
                try:
                    nueva_etapa = ETAPA_FIRMA if decision == "Autorizar para firma" else ETAPA_CORRECCIONES
                    with st.spinner("Registrando decisión..."):
                        # Guardar primero el adjunto opcional para que quede asociado
                        # a la devolución vigente en EVENTOS_BORRADORES.
                        if nueva_etapa == ETAPA_CORRECCIONES and archivo_tte is not None:
                            _adjuntar_documento_corregido_tte(folio, numero, archivo_tte)
                        if nueva_etapa == ETAPA_FIRMA:
                            _registrar_envio_firma(folio, numero, nuevo_numero_firma, nuevo_asunto_firma)
                        _actualizar_oficio_flujo(folio, numero, {
                            "Estatus": "En firma" if nueva_etapa == ETAPA_FIRMA else "En correcciones"
                        }, autorizacion_tte=True)
                        _cambiar_etapa_borrador(folio, numero, nueva_etapa, comentario.strip())
                    st.success("Decisión registrada correctamente.")
                    st.rerun()
                except Exception as error:
                    st.error(_mensaje_error_sheets(error))

        # Cierre visual: identifica el límite del expediente cuando hay varios.
        st.markdown(
            '<div class="tte-expediente-cierre" aria-hidden="true">'
            '<span class="tte-fin-linea"></span>'
            '<span>FIN DEL EXPEDIENTE · SIGUIENTE REGISTRO</span>'
            '<span class="tte-fin-linea"></span>'
            '</div>',
            unsafe_allow_html=True,
        )


def _resumen_revision_tte(ultimos, recibidos):
    """Indicadores reales, sin contar varias veces una misma versión.

    - Por revisar: última versión de cada expediente en revisión de la Tte.
    - Recibidos semana: evento de ingreso a revisión de la Tte. en la semana
      calendario local de Ciudad de México (lunes a domingo).
    - Pendientes totales: última versión en revisión de Tte. o en firma.
    """
    from zoneinfo import ZoneInfo
    from datetime import timedelta

    total_pendientes = sum(
        str(registro.get("Etapa", "")).strip() in {ETAPA_TTE, ETAPA_FIRMA}
        for registro in ultimos.values()
    )
    try:
        eventos = _leer_registros_eventos()
    except Exception:
        # No sustituimos información no disponible por un cero engañoso.
        return len(recibidos), None, total_pendientes

    ahora = datetime.now(ZoneInfo("America/Mexico_City"))
    lunes = (ahora - timedelta(days=ahora.weekday())).date()
    siguientes_lunes = lunes + timedelta(days=7)
    versiones_recibidas = set()
    for evento in eventos:
        if str(evento.get("Accion", "")).strip() != ETAPA_TTE:
            continue
        fecha_raw = str(evento.get("Fecha", "")).strip()
        try:
            fecha = datetime.fromisoformat(fecha_raw.replace("Z", "+00:00"))
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=ZoneInfo("America/Mexico_City"))
            fecha_local = fecha.astimezone(ZoneInfo("America/Mexico_City")).date()
        except (ValueError, TypeError, OverflowError):
            continue
        if lunes <= fecha_local < siguientes_lunes:
            versiones_recibidas.add((
                str(evento.get("Folio", "")).strip(),
                str(evento.get("No. de oficio", "")).strip(),
                str(evento.get("Version", "")).strip(),
            ))
    return len(recibidos), len(versiones_recibidas), total_pendientes


def _bandeja_borradores_compartida():
    """Bandeja operativa con acciones para cada revisor."""
    if not puede_realizar_revision_final():
        st.markdown("#### Documentos que requieren atención")
    try:
        registros = _leer_registros_borradores()
    except Exception as error:
        st.error(f"No se pudo cargar la bandeja: {error}")
        return
    if puede_realizar_revision_final():
        etapas = {ETAPA_TTE}
    elif puede_gestionar_borradores():
        etapas = {ETAPA_BORRADOR, ETAPA_ENVIADO, ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA, ETAPA_FIRMA}
    else:
        st.info("Bandeja disponible para revisores autorizados.")
        return
    ultimos = {}
    for r in registros:
        clave = (str(r.get("Folio", "")), str(r.get("No. de oficio", "")))
        if clave not in ultimos or int(r.get("Version") or 0) > int(ultimos[clave].get("Version") or 0):
            ultimos[clave] = r
    recibidos = [r for r in ultimos.values() if r.get("Etapa") in etapas]
    if puede_realizar_revision_final():
        imagen_centro = _imagen_selector("banner_centro_autorizaciones.png")
        imagen_centro_html = (
            f'<span class="tte-queue-illustration" style="background-image:url(\'{imagen_centro}\')" aria-hidden="true"></span>'
            if imagen_centro else ''
        )
        st.markdown(
            '<div class="tte-queue-header">'
            + imagen_centro_html
            + '<div class="tte-queue-content">'
            '<div class="tte-queue-eyebrow">CONTROL Y SEGUIMIENTO / AUTORIZACIONES</div>'
            '<div class="tte-queue-heading">Centro de autorizaciones</div>'
            '<div class="tte-queue-description">Revisa los documentos recibidos y registra tu decisión en cada expediente.</div>'
            '</div>'
            '<div class="tte-queue-rail">DOCUMENTACIÓN &nbsp; / &nbsp; AUTORIZACIONES &nbsp; / &nbsp; CONTROL</div>'
            '</div>', unsafe_allow_html=True,
        )
        # Tres indicadores para la Tte. basados en registros y bitácora reales.
        por_revisar, recibidos_semana, pendientes_totales = _resumen_revision_tte(ultimos, recibidos)
        try:
            oficios_actuales = {(str(x.get("Folio", "")).strip(),
                                 str(x.get("No. de oficio", "")).strip()): x
                                for x in _leer_registros_oficios()}
            modalidades_directas = [x for x in _leer_modalidades()
                                   if x.get("Tipo de trámite") == "Directo"]

            def _estado_real_directo(m):
                return str(oficios_actuales.get((str(m.get("Folio", "")).strip(),
                    str(m.get("No. de oficio", "")).strip()), {}).get("Estatus", "")).strip()

            directos_en_revision = [m for m in modalidades_directas
                                    if m.get("Estado directo") == "Pendiente de autorización"
                                    and _estado_real_directo(m) == "En revisión"]
            directos_en_firma = [m for m in modalidades_directas
                                if m.get("Estado directo") == "En firma"
                                and _estado_real_directo(m) == "En firma"]
            por_revisar += len(directos_en_revision)
            pendientes_totales += len(directos_en_revision) + len(directos_en_firma)
            # Ingresos de esta semana: también se cuentan los ya autorizados,
            # no solo los que permanecen pendientes al consultarlos.
            if recibidos_semana is not None:
                from zoneinfo import ZoneInfo
                from datetime import timedelta
                hoy_tte = datetime.now(ZoneInfo("America/Mexico_City")).date()
                lunes_tte = hoy_tte - timedelta(days=hoy_tte.weekday())
                for direct in modalidades_directas:
                    if (str(direct.get("Folio", "")).strip(),
                        str(direct.get("No. de oficio", "")).strip()) not in oficios_actuales:
                        continue
                    try:
                        fecha_directa = datetime.fromisoformat(str(direct.get("Fecha registro", "")))
                        if fecha_directa.tzinfo and lunes_tte <= fecha_directa.astimezone(ZoneInfo("America/Mexico_City")).date() <= hoy_tte:
                            recibidos_semana += 1
                    except (ValueError, TypeError):
                        pass
        except Exception as error:
            st.warning("No se pudieron incluir los trámites directos en los indicadores: " + str(error))
        st.markdown("""
        <style>
        /* Indicadores exclusivos de la bandeja de revisión final. */
        .tte-summary-card {
            background:linear-gradient(145deg,#F8F3EB,#EEE7DC);
            border:1px solid #C7B8A1;
            border-left:4px solid #B48B55;
            border-radius:12px;
            min-height:114px;
            padding:15px 14px 13px;
            box-shadow:0 5px 14px rgba(60,48,34,.10);
            display:flex;flex-direction:column;align-items:center;
            justify-content:center;text-align:center;gap:5px;
        }
        .tte-summary-label {
            color:#4D463E!important;font-size:12px!important;
            font-weight:750!important;line-height:1.3!important;
        }
        .tte-summary-value {
            color:#34312C!important;font-size:28px!important;
            line-height:1.15!important;font-weight:800!important;
        }
        .tte-summary-icon {font-size:18px;line-height:1;color:#A47D47;}
        .tte-summary-card--weekly {border-left-color:#A86C4A;}
        .tte-summary-card--weekly .tte-summary-icon {color:#A86C4A;}
        .tte-summary-card--total {border-left-color:#6E7F57;}
        .tte-summary-card--total .tte-summary-icon {color:#6E7F57;}
        @media(max-width:640px){
            .tte-summary-card {min-height:100px;padding:12px 6px;}
            .tte-summary-label {font-size:10px!important;}
            .tte-summary-value {font-size:23px!important;}
        }
        </style>
        """, unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3, gap="small")
        with m1:
            st.markdown(
                f'<div class="tte-summary-card"><span class="tte-summary-icon">▤</span>'
                f'<div class="tte-summary-value">{por_revisar:02d}</div>'
                '<div class="tte-summary-label">Por revisar</div></div>',
                unsafe_allow_html=True,
            )
        with m2:
            semanal_texto = f"{recibidos_semana:02d}" if recibidos_semana is not None else "—"
            st.markdown(
                '<div class="tte-summary-card tte-summary-card--weekly">'
                '<span class="tte-summary-icon">◷</span>'
                f'<div class="tte-summary-value">{semanal_texto}</div>'
                '<div class="tte-summary-label">Documentos recibidos en la semana</div></div>',
                unsafe_allow_html=True,
            )
        with m3:
            st.markdown(
                '<div class="tte-summary-card tte-summary-card--total">'
                '<span class="tte-summary-icon">✓</span>'
                f'<div class="tte-summary-value">{pendientes_totales:02d}</div>'
                '<div class="tte-summary-label">Pendientes totales (firma/revisión)</div></div>',
                unsafe_allow_html=True,
            )
        if recibidos_semana is None:
            st.caption("El dato semanal no está disponible: no se pudo consultar la bitácora.")
        if recibidos:
            busqueda = st.text_input("Buscar documento", placeholder="Buscar por folio o número de oficio...",
                                     key="tte_busqueda_revision")
            if busqueda.strip():
                filtro = normalizar_texto(busqueda)
                recibidos = [r for r in recibidos if filtro in normalizar_texto(
                    f"{r.get('Folio', '')} {r.get('No. de oficio', '')} {r.get('Nombre', '')}")]
        if not recibidos:
            st.success("No hay borradores pendientes de su revisión.")
        else:
            st.caption(f"{len(recibidos)} documento(s) en la bandeja · Seleccione la decisión dentro de cada expediente.")
            for r in recibidos:
                _panel_revision_tte(r)
        # Una entrada directa no tiene filas en BORRADORES: se consulta aquí
        # independientemente del proceso ordinario.
        _panel_tramites_directos_tte()
        return
    if not recibidos:
        st.info("No hay documentos esperando tu intervención.")
        return
    # Una tarjeta por expediente; las acciones y el historial se despliegan
    # solo al abrirlo. Así pueden verse numerosos pendientes a la vez.
    import html
    st.markdown(
        '<div class="rev-list-intro"><div><span class="rev-list-kicker">BANDEJA DE SEGUIMIENTO</span>'
        '<h4>Expedientes por atender</h4><p>Abre únicamente el documento en el que vas a trabajar.</p></div>'
        f'<span class="rev-list-count">{len(recibidos)} pendientes</span></div>',
        unsafe_allow_html=True,
    )
    filtro_revisor = st.text_input(
        "Buscar entre mis pendientes", placeholder="Folio, número de oficio o archivo...",
        key="revisor_buscar_pendientes",
    )
    if filtro_revisor.strip():
        termino = normalizar_texto(filtro_revisor)
        recibidos = [r for r in recibidos if termino in normalizar_texto(
            f"{r.get('Folio', '')} {r.get('No. de oficio', '')} {r.get('Nombre', '')}")]
    prioridad = {ETAPA_BORRADOR: 0, ETAPA_CORRECCIONES: 1, ETAPA_RETORNO_FIRMA: 2,
                 ETAPA_ENVIADO: 2, ETAPA_FIRMA: 3}
    recibidos.sort(key=lambda r: (prioridad.get(str(r.get("Etapa", "")), 9),
                                  str(r.get("No. de oficio", ""))))
    if not recibidos:
        st.info("No se encontraron expedientes con esa búsqueda.")
        return
    for r in recibidos:
        folio, numero = str(r.get("Folio", "")), str(r.get("No. de oficio", ""))
        clave = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{folio}_{numero}")
        etapa = str(r.get("Etapa", ""))
        correcciones = etapa in (ETAPA_CORRECCIONES, ETAPA_RETORNO_FIRMA)
        if etapa == ETAPA_BORRADOR:
            etiqueta, clase = "VERSIÓN LISTA PARA ENVÍO", "revision"
            accion = "Enviar la nueva versión a la Tte. Alondra"
        elif correcciones:
            etiqueta, clase = "REQUIERE CORRECCIONES", "ajustes"
            accion = "Revisar observaciones y cargar nueva versión"
        elif etapa == ETAPA_FIRMA:
            etiqueta, clase = "EN TRÁMITE DE FIRMA", "firma"
            accion = "Registrar resultado de firma"
        else:
            etiqueta, clase = "PENDIENTE DE ENVÍO", "revision"
            accion = "Preparar y enviar a revisión"
        # Encabezado compacto; no repite el título del centro documental.
        with st.container(key=f"revisor_expediente_{clave}_{clase}", border=True):
            st.markdown(
                '<div class="rev-compact-head">'
                '<div class="rev-compact-left">'
                '<span class="rev-compact-kicker">EXPEDIENTE · CONTROL DOCUMENTAL</span>'
                f'<div class="rev-compact-title">{html.escape(numero)}</div>'
                f'<div class="rev-compact-meta">Folio {html.escape(folio)}'
                f' <span class="rev-dot">·</span> V{int(r.get("Version") or 0):02d}'
                f' <span class="rev-dot">·</span> {html.escape(str(r.get("Usuario", "")))}</div>'
                '</div>'
                f'<span class="rev-compact-badge {clase}">{etiqueta}</span>'
                '</div>', unsafe_allow_html=True,
            )
            if correcciones:
                # Solo una vista previa, no toda la bitácora ni el formulario.
                try:
                    eventos = _eventos_expediente(folio, numero)
                    ultimo_comentario = next((e for e in reversed(eventos)
                                              if str(e.get("Accion", "")).strip() == etapa), None)
                    if ultimo_comentario:
                        comentario = str(ultimo_comentario.get("Detalle", "")).strip()
                        if comentario:
                            resumen = comentario[:165] + ("…" if len(comentario) > 165 else "")
                            st.markdown(
                                '<div class="rev-compact-note"><strong>↩ Observaciones de la Tte.</strong>'
                                f'<span>{html.escape(resumen)}</span></div>',
                                unsafe_allow_html=True,
                            )
                except Exception:
                    pass  # El detalle completo se consulta al desplegar el expediente.
            st.markdown(f'<div class="rev-compact-next">{html.escape(accion)}</div>',
                        unsafe_allow_html=True)
            with st.expander("Abrir expediente · documentos y acciones  ↗", expanded=False):
                mostrar_borradores_pendiente(
                    folio, numero, str(r.get("Responsable", "")), compacto=True)


def _vista_firma_externa(registros):
    """Bandeja de consulta de los oficios en firma externa (sin acciones de edición).

    Pensada para ~20 registros: búsqueda local y paginación de cinco tarjetas.
    Usa únicamente los datos ya existentes en la tabla de oficios.
    """
    import html

    st.markdown("""
    <style>
    /* Diseño exclusivo de la bandeja de firma. No modifica otros módulos. */
    .firma-bandeja-head {
        display:flex;align-items:center;justify-content:space-between;
        gap:18px;flex-wrap:wrap;
        padding:18px 21px 16px;
        background:linear-gradient(110deg,#F9F5EF 0%,#EFE8DD 100%);
        border:1px solid #C9B79F;border-left:4px solid #78917D;
        border-radius:13px;box-shadow:0 5px 16px rgba(52,47,40,.065);
        margin:7px 0 14px;
    }
    .firma-bandeja-eyebrow {
        color:#9A7453;font-size:10px;letter-spacing:1.5px;
        font-weight:800;margin-bottom:4px;
    }
    .firma-bandeja-title {
        font-weight:800;color:#443944;font-size:23px;line-height:1.2;
        margin:0 0 4px;
    }
    .firma-bandeja-desc {font-size:12px;color:#81746B;line-height:1.45;}
    .firma-bandeja-count {
        display:flex;align-items:center;gap:10px;
        border:1px solid #ACC0AA;background:#E7EFE4;
        border-radius:11px;padding:10px 14px;
    }
    .firma-bandeja-count strong {
        font-size:26px;line-height:1;font-weight:800;color:#49634F;
        font-variant-numeric:tabular-nums;
    }
    .firma-bandeja-count span {
        font-size:11px;color:#59715C;line-height:1.25;font-weight:700;
    }
    .firma-list-count {margin:0 0 8px;color:#877569;font-size:11px;letter-spacing:.2px;}
    .firma-seguimiento-card {
        position:relative;overflow:hidden;
        background:linear-gradient(118deg,#FFFCF8 0%,#FAF5EE 100%);
        border:1px solid #C8B7A3;border-left:4px solid #78907B;
        border-radius:13px;box-shadow:0 5px 16px rgba(57,49,44,.075);
        padding:17px 20px 15px;margin:0 0 13px;
    }
    .firma-seguimiento-card::after {
        content:"";position:absolute;top:0;right:0;
        width:39%;height:72px;pointer-events:none;opacity:.45;
        background:repeating-linear-gradient(164deg,transparent 0 17px,rgba(173,144,109,.18) 18px,transparent 19px 32px);
        mask-image:linear-gradient(90deg,transparent,black);
    }
    .firma-card-top {
        display:flex;justify-content:space-between;align-items:flex-start;
        gap:14px;position:relative;z-index:1;
    }
    .firma-card-left {min-width:0;flex:1;}
    .firma-card-kicker {
        color:#A07C58;font-size:9px;letter-spacing:1.4px;
        font-weight:800;margin-bottom:6px;
    }
    .firma-card-oficio {
        color:#453842;font-size:19px;line-height:1.25;font-weight:800;
        overflow-wrap:anywhere;
    }
    .firma-card-folio {font-size:11px;color:#89776C;margin-top:4px;}
    .firma-card-status {
        flex-shrink:0;display:flex;gap:7px;align-items:center;
        color:#4E6C54;background:#E5EFE3;border:1px solid #A5BBA6;
        padding:7px 11px;border-radius:100px;font-size:11px;font-weight:750;
        white-space:nowrap;
    }
    .firma-card-status::before {content:"◷";font-size:14px;line-height:1;}
    .firma-card-body {
        display:grid;grid-template-columns:minmax(160px,1fr) minmax(0,2fr);
        gap:16px;border-top:1px solid #E5D9CD;
        margin-top:13px;padding-top:11px;
    }
    .firma-data-label {
        color:#9B785B;font-size:9px;font-weight:800;
        letter-spacing:1.1px;margin-bottom:4px;
    }
    .firma-data-value {
        color:#4B4143;font-size:12.5px;font-weight:650;
        line-height:1.45;overflow-wrap:anywhere;
    }
    .firma-card-timeline {
        margin-top:13px;padding:11px 14px 9px;
        border-radius:9px;background:#F3EFE8;
        border:1px solid #E0D5C7;
    }
    .firma-timeline-title {
        color:#98785C;font-size:9px;font-weight:800;
        letter-spacing:1.2px;margin-bottom:9px;
    }
    .firma-stages {
        display:grid;grid-template-columns:1fr auto 1fr auto 1fr;
        gap:9px;align-items:center;
    }
    .firma-stage {display:flex;align-items:center;gap:7px;min-width:0;}
    .firma-stage-circle {
        flex-shrink:0;width:22px;height:22px;
        display:grid;place-items:center;border-radius:50%;
        font-size:12px;font-weight:800;
    }
    .firma-stage.done .firma-stage-circle {background:#64866A;color:white;}
    .firma-stage.active .firma-stage-circle {
        background:#C39B67;color:#FFFDF7;
        box-shadow:0 0 0 3px rgba(195,155,103,.16);
    }
    .firma-stage.future .firma-stage-circle {border:1px solid #C6BBAE;color:#A3978D;}
    .firma-stage-label {font-size:10px;line-height:1.25;font-weight:700;color:#77675E;}
    .firma-stage.done .firma-stage-label {color:#64826A;}
    .firma-stage.active .firma-stage-label {color:#9E753F;}
    .firma-stage-link {height:1px;width:22px;background:#CDBCA6;}
    .firma-stage-link.done {background:#7F9F83;}
    @media(max-width:720px) {
        .firma-bandeja-head {padding:13px 14px;}
        .firma-bandeja-title {font-size:19px;}
        .firma-seguimiento-card {padding:13px 13px 12px;}
        .firma-card-status {font-size:10px;padding:6px 8px;}
        .firma-card-oficio {font-size:16px;}
        .firma-card-body {grid-template-columns:1fr;gap:10px;}
        .firma-stages {gap:4px;}
        .firma-stage {gap:4px;}
        .firma-stage-circle {width:19px;height:19px;font-size:11px;}
        .firma-stage-label {font-size:9px;}
        .firma-stage-link {width:11px;}
    }
    </style>
    """, unsafe_allow_html=True)

    # Mantener el criterio de estado original. Esta pantalla no muta los datos.
    firmando = registros.loc[
        registros["Estatus"].fillna("").astype(str).str.strip() == "En firma"
    ].copy()
    total = len(firmando)

    st.markdown(
        '<div class="firma-bandeja-head">'
        '<div><div class="firma-bandeja-eyebrow">CONTROL DOCUMENTAL / SEGUIMIENTO</div>'
        '<div class="firma-bandeja-title">Oficios en proceso de firma</div>'
        '<div class="firma-bandeja-desc">Consulta de documentos enviados para firma externa.</div></div>'
        '<div class="firma-bandeja-count">'
        f'<strong>{total:02d}</strong><span>En proceso<br>de firma</span>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    if firmando.empty:
        st.info("No hay oficios registrados actualmente en firma.")
        return

    # Hasta unos 20 expedientes: búsqueda y 5 fichas por página evitan scroll excesivo.
    busqueda = st.text_input(
        "Buscar oficio en firma",
        key="firma_externa_busqueda",
        placeholder="Folio, número, responsable o asunto…",
    ).strip()
    if busqueda:
        termino = normalizar_texto(busqueda)
        firmando = firmando.loc[
            firmando.apply(
                lambda r: termino in normalizar_texto(
                    " ".join(str(r.get(c, "") or "") for c in
                             ("No. de oficio", "Folio", "Responsable", "Asunto"))
                ), axis=1
            )
        ]
    encontrados = len(firmando)
    if not encontrados:
        st.info("No se encontraron oficios en firma con esos datos.")
        return

    por_pagina = 5
    paginas = (encontrados + por_pagina - 1) // por_pagina
    numeros_paginas = list(range(1, paginas + 1))
    pag_key = "firma_externa_pagina"
    if st.session_state.get(pag_key) not in numeros_paginas:
        st.session_state[pag_key] = 1

    if paginas > 1:
        etiqueta, control = st.columns([4, 1], vertical_alignment="center")
        with etiqueta:
            st.caption(f"{encontrados} oficio(s) · Mostrando 5 por página · Solo consulta")
        with control:
            pagina = st.selectbox(
                "Página", numeros_paginas, key=pag_key,
                format_func=lambda n: f"{n} de {paginas}",
            )
    else:
        pagina = 1
        st.caption(f"{encontrados} oficio(s) · Solo consulta")

    inicio = (pagina - 1) * por_pagina
    pagina_registros = firmando.iloc[inicio:inicio + por_pagina]
    tarjetas = []
    for _, registro in pagina_registros.iterrows():
        def campo(columna, defecto="No registrado"):
            valor = registro.get(columna, "")
            if pd.isna(valor) or not str(valor).strip():
                return html.escape(defecto)
            return html.escape(str(valor).strip(), quote=True)

        tarjetas.append(
            '<article class="firma-seguimiento-card">'
            '<div class="firma-card-top">'
            '<div class="firma-card-left">'
            '<div class="firma-card-kicker">EXPEDIENTE EN SEGUIMIENTO</div>'
            f'<div class="firma-card-oficio">{campo("No. de oficio")}</div>'
            f'<div class="firma-card-folio">Folio {campo("Folio")}</div>'
            '</div>'
            '<span class="firma-card-status">En firma externa</span>'
            '</div>'
            '<div class="firma-card-body">'
            '<div><div class="firma-data-label">RESPONSABLE</div>'
            f'<div class="firma-data-value">{campo("Responsable", "No asignado")}</div></div>'
            '<div><div class="firma-data-label">ASUNTO</div>'
            f'<div class="firma-data-value">{campo("Asunto", "Sin asunto registrado")}</div></div>'
            '</div>'
            '<div class="firma-card-timeline">'
            '<div class="firma-timeline-title">SEGUIMIENTO DEL TRÁMITE</div>'
            '<div class="firma-stages">'
            '<div class="firma-stage done"><span class="firma-stage-circle">✓</span>'
            '<span class="firma-stage-label">Revisión</span></div>'
            '<div class="firma-stage-link done"></div>'
            '<div class="firma-stage active"><span class="firma-stage-circle">◷</span>'
            '<span class="firma-stage-label">Firma externa</span></div>'
            '<div class="firma-stage-link"></div>'
            '<div class="firma-stage future"><span class="firma-stage-circle">○</span>'
            '<span class="firma-stage-label">Finalizado</span></div>'
            '</div></div></article>'
        )
    st.markdown("".join(tarjetas), unsafe_allow_html=True)

# =========================================================
# SETF - CENTRO DE PENDIENTES (CONSULTA COMPARTIDA)
# =========================================================

@st.cache_data(ttl=30, show_spinner=False)
def _setf_borradores_compartidos():
    # Solo SETF: no reutilizar datos ni caché de Diseño Geométrico.
    return _leer_registros_borradores_por_area("setf")


def _setf_ultimo_word_enviado(folio, numero, borradores):
    """Devuelve el último Word efectivamente enviado (no el borrador aún sin enviar).

    El estado se lee de BORRADORES y no se deduce del orden de carga.
    """
    elegibles = []
    for b in borradores:
        if (str(b.get("Folio", "")).strip(), str(b.get("No. de oficio", "")).strip()) != (folio, numero):
            continue
        etapa = str(b.get("Etapa", "")).strip()
        # Archivo con etapa Borrador cargado aún NO fue enviado oficialmente.
        if not etapa or etapa == ETAPA_BORRADOR:
            continue
        ident = str(b.get("Drive ID", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", ident):
            continue
        try:
            version = int(b.get("Version") or 0)
        except (ValueError, TypeError):
            continue
        elegibles.append((version, b))
    return max(elegibles, key=lambda par: par[0])[1] if elegibles else None


def _setf_documento_compartido(folio, numero, borradores, identificador, mostrar_titulo=True):
    """Consulta/descarga; no expone Drive ID ni habilita escritura documental."""
    ultimo = _setf_ultimo_word_enviado(folio, numero, borradores)
    if not ultimo:
        st.caption("No hay una versión Word enviada oficialmente para este oficio.")
        return
    nombre = Path(str(ultimo.get("Nombre") or "oficio.docx")).name
    if not nombre.lower().endswith(".docx"):
        nombre += ".docx"
    if mostrar_titulo:
        st.markdown("**Último Word enviado**")
    st.caption(f"Versión {ultimo.get('Version')} · {html.escape(nombre)} · {ultimo.get('Etapa', '')}")
    clave = f"setf_word_{identificador}"
    if st.button("Preparar descarga del Word editable", key=f"{clave}_preparar"):
        try:
            exigir_sesion()
            if _area_activa_validada() != "setf":
                raise PermissionError("Este acceso es exclusivo de SETF.")
            # Revalidar que el oficio sigue activo, que pertenece a SETF y
            # que el archivo sigue siendo el último enviado en BORRADORES.
            vigentes = obtener_oficios_por_area("setf")
            actual = vigentes.loc[
                (vigentes["Folio"].fillna("").astype(str).str.strip() == folio) &
                (vigentes["No. de oficio"].fillna("").astype(str).str.strip() == numero)
            ]
            if len(actual) != 1 or str(actual.iloc[0].get("Estatus", "")).strip() not in {
                "En elaboración", "En revisión", "En correcciones", "En firma", "Recaído"
            }:
                raise ValueError("El expediente ya no está disponible entre los pendientes activos.")
            _leer_registros_borradores_por_area.clear()
            confirmado = _setf_ultimo_word_enviado(folio, numero, _setf_borradores_compartidos())
            if not confirmado or str(confirmado.get("Drive ID", "")) != str(ultimo.get("Drive ID", "")):
                raise ValueError("El documento cambió. Actualiza la página antes de descargar.")
            contenido = _descargar_docx_drive(str(confirmado["Drive ID"]))
            st.session_state[clave] = {"bytes": contenido, "nombre": nombre,
                                           "version": str(confirmado.get("Version", ""))}
        except Exception as error:
            st.session_state.pop(clave, None)
            st.error(f"No se pudo preparar el Word: {error}")
    listo = st.session_state.get(clave)
    if listo and listo["version"] == str(ultimo.get("Version", "")):
        st.download_button("⬇ Descargar última versión enviada (.docx)", data=listo["bytes"],
                           file_name=listo["nombre"],
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           key=f"{clave}_descargar", use_container_width=True)


def _setf_vista_pendientes_generales():
    """Directorio visual SETF en modo consulta, sin modificar permisos ni flujo documental."""
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("El directorio general solo está disponible para SETF.")

    # Diseño restringido a esta vista. Los botones son widgets reales de Streamlit.
    st.markdown("""
    <style>
      .setf-mesa-cabecera {margin:0 0 14px;}
      .setf-mesa-cabecera .eyebrow {font-size:10px;letter-spacing:1.9px;color:#A47D50;font-weight:800;margin-bottom:6px;}
      .setf-mesa-cabecera h2 {font-size:clamp(23px,2.1vw,30px);line-height:1.12;margin:0 0 5px;color:#203946;font-weight:800;}
      .setf-mesa-cabecera p {font-size:13px;line-height:1.45;color:#647681;margin:0;}
      .setf-mesa-kpi {background:rgba(255,253,249,.97);border:1px solid #DCD7CE;
          border-top:3px solid #BA9561;border-radius:13px;min-height:102px;padding:15px 18px;
          display:flex;align-items:center;justify-content:space-between;gap:16px;
          box-shadow:0 5px 18px rgba(24,44,49,.075);}
      .setf-mesa-kpi .numero {font-size:30px;color:#203B45;font-weight:850;line-height:1;
          font-variant-numeric:tabular-nums;letter-spacing:-.7px;}
      .setf-mesa-kpi .rotulo {margin-top:9px;font-size:12.5px;font-weight:700;color:#536A73;}
      .setf-mesa-kpi .simbolo {width:39px;height:39px;border-radius:12px;display:grid;
          place-items:center;background:#F2E9DC;color:#A07540;font-size:21px;flex-shrink:0;}
      .setf-mesa-kpi.revision {border-top-color:#7797B1;}
      .setf-mesa-kpi.revision .simbolo {background:#EAF1F6;color:#5A7E99;}
      .setf-mesa-kpi.firma {border-top-color:#78A08B;}
      .setf-mesa-kpi.firma .simbolo {background:#E8F1EB;color:#547E63;}
      .setf-mesa-directorio {margin:15px 0 11px;display:flex;align-items:center;
          justify-content:space-between;gap:10px;flex-wrap:wrap;}
      .setf-mesa-directorio strong {font-size:17px;font-weight:800;color:#203946;}
      .setf-mesa-directorio .contador {font-size:11px;font-weight:700;
          color:#755B37;background:#F6EFE4;border:1px solid #E5D7C3;border-radius:999px;
          padding:5px 11px;white-space:nowrap;}
      [class*="st-key-setf_mesa_filtros"] {padding:14px 17px 10px!important;
          background:rgba(255,253,250,.90)!important;border:1px solid #E5DFD4!important;
          border-radius:14px!important;margin-bottom:10px!important;}
      [class*="st-key-setf_mesa_filtros"] label {font-weight:650!important;color:#36505C!important;}
      [class*="st-key-setf_mesa_tarjeta_"] {background:rgba(255,254,251,.98)!important;
          border:1px solid #E1D9CD!important;border-left:4px solid #BB915C!important;
          border-radius:15px!important;padding:15px 18px!important;margin-bottom:11px!important;
          box-shadow:0 5px 18px rgba(29,48,54,.065)!important;}
      [class*="st-key-setf_mesa_tarjeta_"][class*="_abierta"] {
          border-color:#D5C3A8!important;border-left-color:#A77741!important;
          box-shadow:0 8px 22px rgba(29,48,54,.10)!important;}
      .setf-mesa-exp {display:flex;align-items:flex-start;gap:15px;min-width:0;}
      .setf-mesa-exp-icon {display:flex;align-items:center;justify-content:center;
          width:45px;height:45px;min-width:45px;border-radius:12px;background:#F3EDE4;
          color:#9B7548;font-size:21px;}
      .setf-mesa-exp-datos {min-width:0;flex:1;}
      .setf-mesa-exp-top {display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:5px;}
      .setf-mesa-exp-numero {font-size:16px;line-height:1.2;font-weight:800;color:#263C47;}
      .setf-mesa-exp-estado {font-size:10px;font-weight:750;line-height:1.3;
          background:#FAF2E5;color:#976A33;border:1px solid #E8D9BD;
          border-radius:100px;padding:4px 9px;white-space:nowrap;}
      .setf-mesa-exp-estado.revision {background:#E9F0F5;border-color:#C6D6E0;color:#4B738A;}
      .setf-mesa-exp-estado.firma {background:#E8F2EA;border-color:#BDD7C4;color:#4B7859;}
      .setf-mesa-exp-estado.correcciones {background:#FAECE8;border-color:#EBC5B8;color:#98624F;}
      .setf-mesa-exp-estado.recaido {background:#F5EAEE;border-color:#DFC8D1;color:#885A70;}
      .setf-mesa-exp-asunto {margin:0 0 10px;font-size:13.5px;line-height:1.35;color:#38505B;
          overflow-wrap:anywhere;}
      .setf-mesa-exp-meta {display:flex;align-items:center;flex-wrap:wrap;gap:5px 15px;
          color:#657682;font-size:11.5px;line-height:1.45;}
      .setf-mesa-exp-meta strong {color:#415B66;font-weight:750;}
      .setf-mesa-exp-word {font-weight:750;color:#46785B;}
      .setf-mesa-exp-word.no-word {color:#927765;}
      [class*="st-key-setf_mesa_tarjeta_"] [data-testid="stButton"] button {
          min-height:42px!important;border-radius:10px!important;font-size:13px!important;
          font-weight:750!important;border:1px solid #C7D2D9!important;
          background:#FAFCFD!important;color:#284653!important;box-shadow:none!important;}
      [class*="st-key-setf_mesa_tarjeta_"] [data-testid="stButton"] button:hover {
          background:#EAF1F3!important;border-color:#7E9EA8!important;color:#173945!important;}
      [class*="st-key-setf_mesa_archivo_"] {border-top:1px solid #E7DED1!important;
          padding:13px 3px 3px!important;margin-top:12px!important;}
      .setf-mesa-word-head {display:flex;align-items:center;gap:10px;color:#263F4B;
          font-weight:750;font-size:14px;margin-bottom:5px;}
      .setf-mesa-word-head span {font-size:18px;color:#688C9A;}
      [class*="st-key-setf_mesa_archivo_"] .stDownloadButton button {
          background:#26454C!important;color:white!important;border-color:#26454C!important;}
      .setf-mesa-readonly {color:#6F7A81;font-size:11px;margin-top:4px;}
      @media (max-width:850px) {
        .setf-mesa-kpi {min-height:89px;padding:12px 13px;}
        .setf-mesa-kpi .numero {font-size:26px;}
        .setf-mesa-kpi .simbolo {width:34px;height:34px;min-width:34px;}
        [class*="st-key-setf_mesa_tarjeta_"] {padding:12px!important;}
        .setf-mesa-exp {gap:10px;}
        .setf-mesa-exp-icon {width:38px;height:38px;min-width:38px;}
      }
    </style>
    """, unsafe_allow_html=True)

    st.markdown(
        '<div class="setf-mesa-cabecera">'
        '<div class="eyebrow">SETF · SEGUIMIENTO COMPARTIDO</div>'
        '<h2>Pendientes de la mesa</h2>'
        '<p>Consulta los oficios pendientes de SETF y descarga la última versión Word enviada oficialmente.</p>'
        '</div>', unsafe_allow_html=True
    )

    df = obtener_oficios_por_area("setf").copy()
    activos = {"En elaboración", "En correcciones", "En revisión", "En firma", "Recaído"}
    requeridas = {"Folio", "No. de oficio", "Estatus", "Responsable"}
    if not requeridas.issubset(df.columns):
        st.error("No fue posible consultar las columnas necesarias del libro SETF.")
        return
    df = df.loc[df["Estatus"].fillna("").astype(str).str.strip().isin(activos)].copy()

    totales = (
        ("Pendientes totales", len(df), "total", "▤"),
        ("En revisión", int((df["Estatus"].fillna("").astype(str).str.strip() == "En revisión").sum()), "revision", "◷"),
        ("En firma", int((df["Estatus"].fillna("").astype(str).str.strip() == "En firma").sum()), "firma", "✎"),
    )
    columnas_resumen = st.columns(3, gap="small")
    for columna, (etiqueta, cantidad, clase, icono) in zip(columnas_resumen, totales):
        with columna:
            st.markdown(
                f'<div class="setf-mesa-kpi {clase}">'
                f'<div><div class="numero">{cantidad:02d}</div>'
                f'<div class="rotulo">{html.escape(etiqueta)}</div></div>'
                f'<div class="simbolo" aria-hidden="true">{icono}</div></div>',
                unsafe_allow_html=True,
            )
    if df.empty:
        st.info("No hay oficios pendientes en SETF.")
        return

    # La cabecera se rellena en su posición original DESPUÉS de filtrar.
    titulo_directorio = st.empty()
    with st.container(key="setf_mesa_filtros"):
        qcol, rcol, ecol = st.columns([2.1, 1.1, 1.1], gap="small")
        with qcol:
            buscar = st.text_input("Buscar expediente", placeholder="Folio, asunto, responsable o proyecto…",
                                   key="setf_gral_buscar")
        with rcol:
            responsables = sorted({str(v).strip() for v in df["Responsable"].dropna()
                                   if str(v).strip()})
            elegido = st.selectbox("Responsable", ["Todos"] + responsables, key="setf_gral_responsable")
        with ecol:
            estado = st.selectbox("Estado", ["Todos"] + sorted(activos), key="setf_gral_estado")

    if elegido != "Todos":
        df = df.loc[df["Responsable"].fillna("").astype(str).str.strip() == elegido]
    if estado != "Todos":
        df = df.loc[df["Estatus"].fillna("").astype(str).str.strip() == estado]
    if buscar.strip():
        columnas = [col for col in ("Folio", "No. de oficio", "Asunto", "Proyecto", "Responsable") if col in df.columns]
        consulta = normalizar_texto(buscar)
        df = df.loc[df[columnas].fillna("").astype(str).apply(
            lambda fila: any(consulta in normalizar_texto(campo) for campo in fila), axis=1)]

    total_filtrado = len(df)
    titulo_directorio.markdown(
        '<div class="setf-mesa-directorio"><strong>Directorio de expedientes</strong>'
        f'<span class="contador">{total_filtrado} registro(s) · Solo lectura</span></div>',
        unsafe_allow_html=True,
    )
    if df.empty:
        st.info("No hay expedientes que coincidan con los filtros actuales.")
        return

    try:
        borradores = _setf_borradores_compartidos()
        word_disponible = True
    except Exception as error:
        st.warning(f"No fue posible consultar las versiones Word: {_mensaje_error_sheets(error)}")
        borradores = []
        word_disponible = False

    registros_por_pagina = 8
    paginas = (total_filtrado + registros_por_pagina - 1) // registros_por_pagina
    if paginas > 1:
        infocol, pagcol = st.columns([3.6, 1.0], gap="small", vertical_alignment="center")
        with infocol:
            st.caption(f"Mostrando hasta {registros_por_pagina} expedientes por página")
        with pagcol:
            pagina = st.selectbox("Página", list(range(1, paginas + 1)),
                                  key="setf_mesa_pagina", format_func=lambda x: f"{x} de {paginas}")
    else:
        pagina = 1
    desde = (pagina - 1) * registros_por_pagina
    pagina_df = df.iloc[desde:desde + registros_por_pagina]

    def texto_campo(fila, clave, predeterminado="No registrado"):
        valor = fila.get(clave)
        if valor is None or (not isinstance(valor, (list, dict)) and pd.isna(valor)):
            return predeterminado
        valor = str(valor).strip()
        return valor or predeterminado

    for indice, (_, oficio) in enumerate(pagina_df.iterrows(), start=desde):
        folio = texto_campo(oficio, "Folio", "")
        numero = texto_campo(oficio, "No. de oficio", "")
        asunto = texto_campo(oficio, "Asunto", "Sin asunto registrado")
        responsable = texto_campo(oficio, "Responsable", "Sin asignar")
        proyecto = texto_campo(oficio, "Proyecto", "Sin proyecto")
        estatus = texto_campo(oficio, "Estatus", "Sin estado")
        token = hashlib.sha1(f"{folio}|{numero}|{indice}".encode("utf-8")).hexdigest()[:14]
        id_expediente = f"{folio}|{numero}|{token}"
        abierto = st.session_state.get("setf_mesa_expediente_abierto") == id_expediente
        ultimo = _setf_ultimo_word_enviado(folio, numero, borradores) if word_disponible else None
        estado_clase = {
            "En revisión": "revision", "En firma": "firma", "En correcciones": "correcciones",
            "Recaído": "recaido",
        }.get(estatus, "normal")
        if not word_disponible:
            word_texto, word_clase = "Información Word no disponible", "no-word"
        elif ultimo:
            word_texto, word_clase = f"Word enviado · versión {ultimo.get('Version', '—')}", ""
        else:
            word_texto, word_clase = "Sin Word enviado oficialmente", "no-word"

        sufijo = "abierta" if abierto else "cerrada"
        with st.container(key=f"setf_mesa_tarjeta_{token}_{sufijo}"):
            contenido, accion = st.columns([4.4, 1.12], gap="medium", vertical_alignment="center")
            with contenido:
                st.markdown(
                    '<div class="setf-mesa-exp">'
                    '<span class="setf-mesa-exp-icon" aria-hidden="true">▤</span>'
                    '<div class="setf-mesa-exp-datos">'
                    '<div class="setf-mesa-exp-top">'
                    f'<span class="setf-mesa-exp-numero">Oficio {html.escape(numero or folio or "Sin número")}</span>'
                    f'<span class="setf-mesa-exp-estado {estado_clase}">{html.escape(estatus)}</span>'
                    '</div>'
                    f'<p class="setf-mesa-exp-asunto">{html.escape(asunto)}</p>'
                    '<div class="setf-mesa-exp-meta">'
                    f'<span><strong>Responsable:</strong> {html.escape(responsable)}</span>'
                    f'<span><strong>Proyecto:</strong> {html.escape(proyecto)}</span>'
                    f'<span><strong>Folio:</strong> {html.escape(folio or "No registrado")}</span>'
                    f'<span class="setf-mesa-exp-word {word_clase}">{html.escape(word_texto)}</span>'
                    '</div></div></div>',
                    unsafe_allow_html=True,
                )
            with accion:
                if st.button("Cerrar expediente" if abierto else "Ver expediente",
                             key=f"setf_mesa_abrir_{token}", use_container_width=True):
                    st.session_state["setf_mesa_expediente_abierto"] = None if abierto else id_expediente
                    st.rerun()
            if abierto:
                with st.container(key=f"setf_mesa_archivo_{token}"):
                    st.markdown(
                        '<div class="setf-mesa-word-head"><span aria-hidden="true">▤</span>'
                        'Último documento Word enviado</div>', unsafe_allow_html=True,
                    )
                    _setf_documento_compartido(folio, numero, borradores, token, mostrar_titulo=False)
                    st.markdown('<div class="setf-mesa-readonly">Consulta y descarga. La revisión, edición y firma mantienen los permisos originales.</div>',
                                unsafe_allow_html=True)

def mostrar_mis_pendientes():
    """Acceso rápido a los oficios activos, respetando los permisos existentes."""
    import html

    mostrar_sesion()
    if _area_activa_validada() == "setf":
        st.markdown("""<style>
        [class*="st-key-setf_pend_vista"] [data-baseweb="tab"] {font-weight:650}
        </style>""", unsafe_allow_html=True)
        st.markdown("""<div class="setf-pend-switch-title">CENTRO DE PENDIENTES</div>
        <div class="setf-pend-switch-sub">Elige entre tu trabajo y el seguimiento de toda la mesa</div>""", unsafe_allow_html=True)
        st.markdown("""<style>
        .setf-pend-switch-title {font-size:13px;font-weight:850;letter-spacing:2px;color:#825C38;margin:0 0 4px}
        .setf-pend-switch-sub {font-size:13px;color:#64717A;margin-bottom:8px}
        [class*="st-key-pend_nav_"] {border-width:2px!important;box-shadow:0 8px 22px rgba(38,47,49,.14)!important;background:linear-gradient(140deg,rgba(255,252,246,.98),rgba(245,238,227,.94))!important}
        [class*="st-key-pend_nav_"][class*="_revision_"] {background:linear-gradient(135deg,#edf3f8,#e3edf4)!important;border-color:#809EB2!important}
        [class*="st-key-pend_nav_"][class*="_firma_"] {background:linear-gradient(135deg,#eaf3ec,#dceade)!important;border-color:#7DAB8C!important}
        [class*="st-key-pend_nav_"] .pend-tile-name {font-size:clamp(18px,1.45vw,23px)!important}
        [class*="st-key-pend_nav_"] button {min-height:43px!important;font-weight:750!important;border-radius:9px!important}
        /* SETF: tres accesos equilibrados, sin afectar la navegación de DG. */
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] {
            min-height: 202px !important;
            height: 100% !important;
            padding: 18px 19px !important;
            border-radius: 15px !important;
            box-sizing: border-box !important;
            display: flex !important;
            flex-direction: column !important;
            justify-content: space-between !important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-head {
            min-height: 66px !important;
            display:flex !important;
            align-items:center !important;
            gap:12px !important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-name {
            font-family:inherit !important;
            font-size:18px !important;
            font-weight:750 !important;
            letter-spacing:-.25px !important;
            line-height:1.24 !important;
            margin:0 !important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-sub {
            font-size:12px !important;
            font-weight:650 !important;
            line-height:1.4 !important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-copy {
            font-size:12px !important;
            line-height:1.5 !important;
            min-height:36px !important;
            margin:7px 0 10px !important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] button {
            min-height:42px !important;
            font-size:13px !important;
            font-weight:700 !important;
            width:100% !important;
        }
        /* Separación más corta entre cabecera y bandejas. */
        .stMainBlockContainer:has(.pend-hero) .pend-nav-head {
            margin-top:8px !important;
            margin-bottom:8px !important;
        }
        .stMainBlockContainer:has(.pend-hero) .pend-hero {margin-bottom:2px !important;}
        /* Sección Word destacada y disponible sin desplegar otro acordeón. */
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_word_acciones_"] {
            background:#FBFAF7 !important;
            border:1px solid #D7C8B3 !important;
            border-left:4px solid #B78F58 !important;
            border-radius:12px !important;
            padding:16px 20px !important;
            margin:12px 0 !important;
        }
        .pend-word-heading {display:flex;flex-direction:column;gap:4px;margin:0 0 12px;}
        .pend-word-heading span {color:#916D43;font-size:10px;font-weight:800;letter-spacing:1.8px;}
        .pend-word-heading strong {color:#20394A;font-size:17px;font-weight:750;}
        .stMainBlockContainer:has(.pend-hero) [data-testid="stExpander"]:has(summary) {
            border-color:#D5D5D0 !important;border-radius:10px !important;
            background:rgba(255,253,248,.94) !important;
        }
        @media (max-width:1000px) {
            .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] {min-height:195px!important;}
        }
        /* SETF: quitar el hueco heredado bajo la navegación superior. */
        @media (min-width: 900px) {
          body .stMainBlockContainer:has(.setf-pend-switch-title) {
            padding-top: 0 !important;
            margin-top: -18px !important;
          }
          body .stMainBlockContainer:has(.setf-pend-switch-title) > div:first-child {
            padding-top: 0 !important;
          }
        }
        .stMainBlockContainer:has(.setf-pend-switch-title) .setf-pend-switch-title {
          margin-top:0!important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title) .setf-pend-switch-sub {
          margin-bottom:5px!important;
        }
        /* Tres tarjetas de igual altura, incluso si cambia la descripción. */
        .stMainBlockContainer:has(.pend-hero) [data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) {
          align-items:stretch!important;
          gap:16px!important;
        }
        .stMainBlockContainer:has(.pend-hero) [data-testid="column"]:has([class*="st-key-pend_nav_"]) {
          display:flex!important;
          flex-direction:column!important;
          align-self:stretch!important;
          min-width:0!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] {
          height:206px!important;
          min-height:206px!important;
          max-height:206px!important;
          width:100%!important;
          display:flex!important;
          flex-direction:column!important;
          justify-content:flex-start!important;
          padding:17px 19px!important;
          box-sizing:border-box!important;
          overflow:hidden!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] > div[data-testid="stVerticalBlock"] {
          height:100%!important;
          flex:1!important;
          display:flex!important;
          flex-direction:column!important;
          justify-content:flex-start!important;
          gap:0!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] [data-testid="stElementContainer"]:has([data-testid="stButton"]) {
          margin-top:auto!important;
          width:100%!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-head {
          min-height:60px!important;
          height:60px!important;
          margin:0!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-name {
          font-size:18px!important;
          line-height:1.2!important;
          font-weight:750!important;
          font-family:inherit!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] .pend-tile-copy {
          min-height:31px!important;
          margin:4px 0 7px!important;
          font-size:12px!important;
        }
        .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] [data-testid="stButton"] button {
          height:42px!important;
          min-height:42px!important;
          font-size:13px!important;
          font-weight:700!important;
        }
        @media (max-width:700px) {
          .stMainBlockContainer:has(.pend-hero) [class*="st-key-pend_nav_"] {
            height:auto!important;
            min-height:190px!important;
            max-height:none!important;
          }
        }
        </style>""", unsafe_allow_html=True)
        # Selector interactivo nativo de Streamlit. El estado se conserva entre reruns.
        vista_actual = st.session_state.get("setf_pend_vista", "Mis pendientes")
        if vista_actual not in ("Mis pendientes", "Pendientes de la mesa"):
            vista_actual = "Mis pendientes"
            st.session_state["setf_pend_vista"] = vista_actual
        st.markdown("""<style>
        /* Solo SETF y exclusivamente el selector de este módulo. */
        [class*="st-key-setf_pend_btn_mis"] button,
        [class*="st-key-setf_pend_btn_mesa"] button {
            min-height: 56px !important;
            border-radius: 12px !important;
            font-size: 15px !important;
            font-weight: 740 !important;
            letter-spacing: .05px !important;
            box-shadow: 0 3px 10px rgba(27,43,50,.08) !important;
            border: 1px solid #D1C0AB !important;
            transition: background-color .15s, border-color .15s, box-shadow .15s !important;
        }
        [class*="st-key-setf_pend_btn_mis"] button[kind="primary"],
        [class*="st-key-setf_pend_btn_mesa"] button[kind="primary"] {
            background: #263F47 !important;
            color: #FFFFFF !important;
            border-color: #C6A16E !important;
            box-shadow: 0 5px 16px rgba(24,50,57,.19) !important;
        }
        [class*="st-key-setf_pend_btn_mis"] button[kind="secondary"],
        [class*="st-key-setf_pend_btn_mesa"] button[kind="secondary"] {
            background: #FBF8F2 !important;
            color: #253F47 !important;
        }
        [class*="st-key-setf_pend_btn_mis"] button:hover,
        [class*="st-key-setf_pend_btn_mesa"] button:hover {
            border-color: #B88E55 !important;
            box-shadow: 0 7px 18px rgba(31,45,50,.15) !important;
        }
        .setf-pend-switch-title {margin:0 0 3px !important;}
        .setf-pend-switch-sub {margin:0 0 10px !important;}
        </style>""", unsafe_allow_html=True)
        boton_mis, boton_mesa = st.columns(2, gap="small")
        with boton_mis:
            if st.button("▤  Mis pendientes",
                         key="setf_pend_btn_mis", use_container_width=True,
                         type="primary" if vista_actual == "Mis pendientes" else "secondary"):
                st.session_state["setf_pend_vista"] = "Mis pendientes"
                st.rerun()
        with boton_mesa:
            if st.button("♧  Pendientes de la mesa",
                         key="setf_pend_btn_mesa", use_container_width=True,
                         type="primary" if vista_actual == "Pendientes de la mesa" else "secondary"):
                st.session_state["setf_pend_vista"] = "Pendientes de la mesa"
                st.rerun()
        if vista_actual == "Pendientes de la mesa":
            _setf_vista_pendientes_generales()
            return
    # Compactar 75 % el espacio entre la cabecera institucional y el panel
    # principal de Mis pendientes, tanto en Diseño Geométrico como en SETF.
    # Mayor especificidad para prevalecer sobre los estilos heredados.
    st.markdown("""<style>
      @media (min-width: 1050px) {
        html body .stMainBlockContainer:has(.pend-hero) .pend-hero {
          margin-top: -59px !important;
        }
      }
      @media (min-width: 720px) and (max-width: 1049px) {
        html body .stMainBlockContainer:has(.pend-hero) .pend-hero {
          margin-top: -42px !important;
        }
      }
      @media (max-width: 719px) {
        html body .stMainBlockContainer:has(.pend-hero) .pend-hero {
          margin-top: 0 !important;
        }
      }
    </style>""", unsafe_allow_html=True)
    # Ajuste FINAL de composición SETF, sin trasladar la cabecera con márgenes negativos.
    if _area_activa_validada() == "setf":
        st.markdown("""<style>
        /* El hueco era padding heredado y márgenes incompatibles; no mover el banner. */
        body [data-testid="stAppViewContainer"] .block-container:has(.setf-pend-switch-title) {
            padding-top: 0.35rem !important;
            margin-top: 0 !important;
        }
        body .stMainBlockContainer:has(.setf-pend-switch-title) {
            padding-top: 0 !important;
            margin-top: 0 !important;
        }
        body .stMainBlockContainer:has(.setf-pend-switch-title) .setf-pend-switch-title {
            margin: 0 0 3px !important;
        }
        body .stMainBlockContainer:has(.setf-pend-switch-title) .setf-pend-switch-sub {
            margin: 0 0 6px !important;
        }
        body .stMainBlockContainer:has(.setf-pend-switch-title) .pend-hero {
            margin-top: 12px !important;
            margin-bottom: 10px !important;
            transform: none !important;
        }
        body .stMainBlockContainer:has(.setf-pend-switch-title) .pend-nav-head {
            margin: 5px 2px 8px !important;
        }
        </style>""", unsafe_allow_html=True)
    # Imagen con transparencia solicitada; un SVG técnico queda como respaldo.
    imagen_pendientes = (_imagen_selector("banner_mis_pendientes.png")
                          or _imagen_selector("trazo_pendientes.png"))
    # The illustration is an explicit child layer: avoids CSS-only image placement
    # failing due to old pseudo-element rules. When absent, the technical line
    # drawing in the hero remains as an intentional fallback.
    ilustracion_pendientes = (
        f'<span class="pend-hero-illustration" style="background-image:url(\'{imagen_pendientes}\')" aria-hidden="true"></span>'
        if imagen_pendientes else ''
    )
    st.markdown(_rotulos_area(f"""
    <div class="pend-hero">
      {ilustracion_pendientes}
      <div class="pend-hero-copy">
        <div class="pend-hero-kicker">MESA DE DISEÑO GEOMÉTRICO · CONTROL DOCUMENTAL</div>
        <div class="pend-hero-title">Mis pendientes</div>
        <div class="pend-hero-desc">Revisión, validación y seguimiento de documentos</div>
        <div class="pend-hero-line"></div>
      </div>
      <div class="pend-hero-track">TRAZO &nbsp; / &nbsp; ALINEAMIENTO &nbsp; / &nbsp; RASANTE</div>
    </div>
    """), unsafe_allow_html=True)

    registros = obtener_oficios()
    if registros.empty:
        st.info("No hay registros disponibles o no se pudo consultar la hoja OFICIOS.")
        return

    if not {"Responsable", "Estatus"}.issubset(registros.columns):
        st.warning("La hoja OFICIOS necesita las columnas Responsable y Estatus.")
        return

    if not es_administrador():
        asignado = responsable_de_sesion()
        if not asignado:
            st.warning("Tu cuenta no tiene un responsable de oficios configurado.")
            return
        registros = registros.loc[
            registros["Responsable"].fillna("").astype(str).map(normalizar_texto)
            == normalizar_texto(asignado)
        ].copy()

    activos = {"En elaboración", "En correcciones", "En revisión", "En firma", "Recaído"}
    pendientes = registros.loc[
        registros["Estatus"].fillna("").astype(str).str.strip().isin(activos)
    ].copy()

    # Las bandejas representan quién debe actuar, no solo el estatus general.
    # Un Word enviado a revisión final corresponde a la bandeja de la Tte.,
    # no a «Mis oficios». BORRADORES es la fuente de verdad de la etapa.
    try:
        registros_word = _leer_registros_borradores()
        ultimas_etapas = {}
        for registro_word in registros_word:
            clave_word = (str(registro_word.get("Folio", "")).strip(),
                          str(registro_word.get("No. de oficio", "")).strip())
            try:
                version_word = int(registro_word.get("Version") or 0)
            except (ValueError, TypeError):
                version_word = 0
            anterior = ultimas_etapas.get(clave_word)
            if anterior is None or version_word >= anterior[0]:
                ultimas_etapas[clave_word] = (version_word, str(registro_word.get("Etapa", "")).strip())
        if {"Folio", "No. de oficio"}.issubset(pendientes.columns):
            en_revision_tte = pendientes.apply(
                lambda fila: ultimas_etapas.get(
                    (str(fila["Folio"]).strip(), str(fila["No. de oficio"]).strip()),
                    (0, ""))[1] == ETAPA_TTE, axis=1)
            pendientes = pendientes.loc[~en_revision_tte].copy()
            # En la cuenta de la Tte. «Mis oficios» no es una segunda
            # bandeja de revisión: los documentos pendientes de su decisión
            # se consultan exclusivamente desde «Por revisar».
            if puede_realizar_revision_final():
                pendientes = pendientes.iloc[0:0].copy()
    except Exception as error:
        # No ocultar expedientes si Google Sheets no pudo cargar BORRADORES.
        st.warning(f"No se pudo sincronizar la clasificación de bandejas: {_mensaje_error_sheets(error)}")

    etiquetas = [] if puede_realizar_revision_final() else ["Mis oficios"]
    if puede_gestionar_borradores() or puede_realizar_revision_final():
        etiquetas.append("Por revisar")
    etiquetas.append("En proceso de firma")
    # Navegación por bandejas: controles reales, sin pestañas estrechas.
    # Alondra abre directamente su bandeja de autorizaciones.
    # El resto conserva «Mis oficios» como pantalla inicial.
    if "pend_bandeja" not in st.session_state or st.session_state.pend_bandeja not in etiquetas:
        st.session_state.pend_bandeja = (
            "Por revisar" if puede_realizar_revision_final() else "Mis oficios"
        )
    bandeja_actual = st.session_state.pend_bandeja
    st.markdown('<div class="pend-nav-head"><span>BANDEJAS DE TRABAJO</span><small>ACCESOS SEGÚN TU ROL</small></div>',
                unsafe_allow_html=True)
    opciones_nav = [
        ("Mis oficios", "▤", "Expedientes asignados", "Consulta y seguimiento de tus oficios."),
        ("Por revisar", "▣", "Validación documental", "Revisa los documentos que esperan autorización."),
        ("En proceso de firma", "✎", "Seguimiento de firmas", "Consulta los documentos enviados a firma."),
    ]
    # Solo se dibujan las bandejas autorizadas: ninguna tarjeta deshabilitada.
    opciones_visibles = [(i, nombre, icono, subtitulo, descripcion)
                         for i, (nombre, icono, subtitulo, descripcion) in enumerate(opciones_nav)
                         if nombre in etiquetas]
    # Ilustraciones locales para la navegación; no se envían a Drive.
    imagenes_bandejas = {
        "Por revisar": _imagen_selector("tarjeta_por_revisar.png"),
        "En proceso de firma": _imagen_selector("tarjeta_proceso_firma.png"),
    }
    # Only show a notice if a required file really is missing in the local assets/.
    # This distinguishes a missing image from one whose lines are simply too faint.
    imagenes_requeridas = (
        "tarjeta_por_revisar.png", "tarjeta_proceso_firma.png",
        "banner_centro_autorizaciones.png", "banner_mis_pendientes.png",
    )
    recursos_faltantes = [
        nombre_imagen for nombre_imagen in imagenes_requeridas
        if not (Path(__file__).resolve().parent / "assets" / nombre_imagen).is_file()
    ]
    if recursos_faltantes and puede_realizar_revision_final():
        st.caption("Imágenes no encontradas en assets/: " + ", ".join(recursos_faltantes))
    # Bandejas SETF V2: diseño único, sin los estilos heredados que se contradecían.
    # Los botones siguen siendo st.button y conservan la navegación y los permisos.
    vista_setf = _area_activa_validada() == "setf"
    if vista_setf:
        st.markdown("""<style>
        /* Alcance estricto: solo Centro de pendientes SETF. Se usan llaves nuevas
           para no heredar reglas antiguas de st-key-pend_nav_. */
        .stMainBlockContainer:has(.setf-pend-switch-title)
          [data-testid="stHorizontalBlock"]:has([class*="st-key-setf_pend_card_"]) {
            gap: clamp(12px, 1.2vw, 20px) !important;
            align-items: stretch !important;
            margin: 0 !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          [data-testid="stHorizontalBlock"]:has([class*="st-key-setf_pend_card_"])
          > [data-testid="stColumn"] {
            min-width:0 !important;
            display:flex !important;
            flex-direction:column !important;
            align-self:stretch !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"] {
            box-sizing:border-box !important;
            position:relative !important;
            isolation:isolate !important;
            width:100% !important;
            height:218px !important;
            min-height:218px !important;
            max-height:218px !important;
            padding:18px 19px 15px !important;
            border:1.5px solid #c99c78 !important;
            border-left:4px solid #c19164 !important;
            border-radius:17px !important;
            background:rgba(252,248,243,.97) !important;
            box-shadow:0 8px 20px rgba(48,47,44,.09) !important;
            overflow:hidden !important;
            display:flex !important;
            flex-direction:column !important;
            justify-content:flex-start !important;
            flex:1 1 auto !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"][class*="_revision_"] {
            border-color:#d1a988 !important;
            border-left-color:#b8875e !important;
            background:#fbf5ed !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"][class*="_firma_"] {
            border-color:#a9c1ac !important;
            border-left-color:#6b9880 !important;
            background:#f2f7f2 !important;
        }
        /* Cubrir las dos variantes habituales de estructura interna de Streamlit. */
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"] :is(
            [data-testid="stVerticalBlock"],
            [data-testid="stVerticalBlockBorderWrapper"]
          ) {
            min-height:0 !important;
            height:100% !important;
            display:flex !important;
            flex-direction:column !important;
            justify-content:flex-start !important;
            gap:0 !important;
            flex:1 1 auto !important;
            width:100% !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"]
          [data-testid="stElementContainer"]:has(.setf-pend-tile) {
            width:100% !important;
            min-width:0 !important;
            flex:1 1 auto !important;
            margin:0 !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"]
          [data-testid="stElementContainer"]:has([data-testid="stButton"]) {
            width:100% !important;
            flex:0 0 42px !important;
            margin-top:auto !important;
            margin-bottom:0 !important;
            position:static !important;
            inset:auto !important;
        }
        .setf-pend-tile {
            position:relative !important;
            margin:0 !important;
            padding:0 !important;
            width:100% !important;
            display:flex !important;
            flex-direction:column !important;
            z-index:1 !important;
        }
        .setf-pend-tile-heading {
            display:grid !important;
            grid-template-columns:54px minmax(0,1fr) !important;
            align-items:center !important;
            gap:13px !important;
            height:67px !important;
            min-height:67px !important;
            max-height:67px !important;
            margin:0 !important;
        }
        .setf-pend-tile-icon {
            width:54px !important;
            height:54px !important;
            border-radius:50% !important;
            border:1px solid #dfbfa8 !important;
            background:#ecdbce !important;
            color:#8a5f40 !important;
            display:flex !important;
            align-items:center !important;
            justify-content:center !important;
            font-size:26px !important;
            line-height:1 !important;
            flex-shrink:0 !important;
            margin:0 !important;
        }
        div[class*="st-key-setf_pend_card_"][class*="_firma_"] .setf-pend-tile-icon {
            color:#416a55 !important;
            background:#dcebdd !important;
            border-color:#bfd6c0 !important;
        }
        .setf-pend-tile-text {min-width:0 !important; align-self:center !important;}
        .setf-pend-tile-title {
            font-family:inherit !important;
            font-size:18px !important;
            font-weight:750 !important;
            line-height:1.18 !important;
            letter-spacing:0 !important;
            color:#3a343b !important;
            margin:0 0 5px !important;
            padding:0 !important;
        }
        .setf-pend-tile-sub {
            display:block !important;
            font-family:inherit !important;
            font-size:12px !important;
            font-weight:650 !important;
            line-height:1.25 !important;
            color:#9b6848 !important;
            margin:0 !important;
            padding:0 !important;
        }
        div[class*="st-key-setf_pend_card_"][class*="_firma_"] .setf-pend-tile-sub {
            color:#527c5d !important;
        }
        .setf-pend-tile-desc {
            position:relative !important;
            margin:9px 0 0 !important;
            padding:0 !important;
            font-family:inherit !important;
            font-size:12px !important;
            font-weight:400 !important;
            line-height:1.45 !important;
            color:#655d5a !important;
            max-width:96% !important;
            min-height:35px !important;
        }
        .setf-pend-tile-art {
            position:absolute !important;
            right:0 !important;
            top:14px !important;
            width:38% !important;
            height:110px !important;
            background-size:contain !important;
            background-repeat:no-repeat !important;
            background-position:right center !important;
            opacity:.10 !important;
            pointer-events:none !important;
            z-index:-1 !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"] [data-testid="stButton"] {
            width:100% !important;
            margin:0 !important;
            position:static !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"] [data-testid="stButton"] button {
            width:100% !important;
            height:42px !important;
            min-height:42px !important;
            margin:0 !important;
            border:1px solid #e0bea4 !important;
            border-radius:9px !important;
            background:#f8f0e9 !important;
            color:#905838 !important;
            font-family:inherit !important;
            font-size:13px !important;
            font-weight:680 !important;
            display:flex !important;
            align-items:center !important;
            justify-content:center !important;
        }
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"] [data-testid="stButton"] button:hover {
            background:#f1e1d2 !important;
            border-color:#b98964 !important;
        }
        /* La tarjeta activa conserva su identidad en el borde; el botón siempre
           mantiene el mismo estilo y contraste que los de las otras bandejas. */
        .stMainBlockContainer:has(.setf-pend-switch-title)
          div[class*="st-key-setf_pend_card_"][class*="_activo"] [data-testid="stButton"] button {
            background:#f8f0e9 !important;
            color:#905838 !important;
            border-color:#e0bea4 !important;
        }
        /* Compactar únicamente el hueco ENTRE la barra de navegación y
           'Centro de pendientes'. No se desplaza la cabecera ni el banner.
           La compensación se aplica al elemento de flujo que inicia la sección:
           el contenido siguiente acompaña el cambio naturalmente. */
        @media (min-width: 900px) {
          body .stMainBlockContainer:has(.setf-pend-switch-title)
            [data-testid="stElementContainer"]:has(.setf-pend-switch-title) {
              margin-top: -44px !important;
          }
        }
        @media (min-width: 650px) and (max-width: 899px) {
          body .stMainBlockContainer:has(.setf-pend-switch-title)
            [data-testid="stElementContainer"]:has(.setf-pend-switch-title) {
              margin-top: -22px !important;
          }
        }
        /* Mantener aire bajo el selector y antes de la tarjeta principal. */
        .stMainBlockContainer:has(.setf-pend-switch-title)
          [data-testid="stHorizontalBlock"]:has([class*="st-key-setf_pend_btn_"]) {
            margin-bottom: 8px !important;
        }
        /* Eliminar el salto exagerado entre las bandejas y los indicadores. */
        .stMainBlockContainer:has(.setf-pend-switch-title) .pend-section-label {
            margin-top:12px !important;
            margin-bottom:7px !important;
        }
        @media(max-width:850px) {
            .stMainBlockContainer:has(.setf-pend-switch-title)
              div[class*="st-key-setf_pend_card_"] {
                min-height:218px !important;
                height:218px !important;
                max-height:218px !important;
            }
        }
        </style>""", unsafe_allow_html=True)

    cols_nav = st.columns(len(opciones_visibles), gap="small")
    for columna, (i, nombre, icono, subtitulo, descripcion) in zip(cols_nav, opciones_visibles):
        with columna:
            activo = nombre == bandeja_actual
            categoria_visual = {"Por revisar": "revision", "En proceso de firma": "firma"}.get(nombre, "general")
            tarjeta_imagen = imagenes_bandejas.get(nombre, "")
            if vista_setf:
                # El bloque de información ocupa una altura uniforme y el botón
                # queda aparte como control nativo, sin capas absolutas.
                fondo = (
                    f'<span class="setf-pend-tile-art" style="background-image:url(\'{tarjeta_imagen}\')" '
                    'aria-hidden="true"></span>' if tarjeta_imagen else ''
                )
                with st.container(key=f"setf_pend_card_{i}_{categoria_visual}_{'activo' if activo else 'normal'}"):
                    st.markdown(
                        '<div class="setf-pend-tile">'
                        + fondo
                        + '<div class="setf-pend-tile-heading">'
                        f'<span class="setf-pend-tile-icon">{html.escape(icono)}</span>'
                        '<div class="setf-pend-tile-text">'
                        f'<div class="setf-pend-tile-title">{html.escape(nombre)}</div>'
                        f'<span class="setf-pend-tile-sub">{html.escape(subtitulo)}</span>'
                        '</div></div>'
                        f'<p class="setf-pend-tile-desc">{html.escape(descripcion)}</p>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    # Mismo texto para la bandeja seleccionada y las demás.
                    # El estado activo se indica en la propia tarjeta, nunca en el botón.
                    etiqueta_boton = "Abrir bandeja  →"
                    if st.button(etiqueta_boton, key=f"pend_action_{i}", use_container_width=True):
                        st.session_state.pend_bandeja = nombre
                        st.session_state.pend_expediente_abierto = None
                        st.rerun()
            else:
                # El resto del proyecto mantiene su presentación original.
                fondo_tarjeta = (
                    f'<span class="pend-tile-art" style="background-image:url(\'{tarjeta_imagen}\')" aria-hidden="true"></span>'
                    if tarjeta_imagen else ''
                )
                with st.container(key=f"pend_nav_{i}_{categoria_visual}_{'activo' if activo else 'normal'}"):
                    st.markdown(
                        fondo_tarjeta + '<div class="pend-tile-head">'
                        f'<span class="pend-tile-icon">{icono}</span>'
                        '<div class="pend-tile-body">'
                        f'<h3 class="pend-tile-name">{html.escape(nombre)}</h3>'
                        f'<span class="pend-tile-sub">{html.escape(subtitulo)}</span>'
                        '</div></div>'
                        f'<p class="pend-tile-copy">{html.escape(descripcion)}</p>',
                        unsafe_allow_html=True,
                    )
                    etiqueta_boton = "Bandeja activa  →" if activo else "Abrir bandeja  →"
                    if st.button(etiqueta_boton, key=f"pend_action_{i}", use_container_width=True):
                        st.session_state.pend_bandeja = nombre
                        st.session_state.pend_expediente_abierto = None
                        st.rerun()
    if bandeja_actual == "Mis oficios":
        st.markdown('<div class="pend-section-label">RESUMEN DE ACTIVIDAD</div>', unsafe_allow_html=True)
        columnas = st.columns(3, gap="medium")
        indicadores = (
            ("Por atender", len(pendientes), "Total activo", "#B99A65", "▤"),
            ("En revisión", int((pendientes["Estatus"] == "En revisión").sum()),
             "En seguimiento", "#688AA1", "◉"),
            ("En correcciones", int((pendientes["Estatus"] == "En correcciones").sum()),
             "Requieren ajustes", "#C17E59", "✎"),
        )
        for columna, (titulo, cantidad, detalle, color, icono) in zip(columnas, indicadores):
            with columna:
                st.markdown(
                    f'<div class="pend-kpi" style="--accent:{color}">'
                    f'<div class="pend-kpi-icon">{icono}</div>'
                    f'<div class="pend-kpi-content"><div class="pend-kpi-number">{cantidad:02d}</div>'
                    f'<div class="pend-kpi-title">{html.escape(titulo)}</div>'
                    f'<div class="pend-kpi-desc">{html.escape(detalle)}</div></div></div>',
                    unsafe_allow_html=True
                )

        st.markdown('<div class="pend-section-label">ACCESO DIRECTO</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="pend-list-title">Oficios que requieren atención</div>',
            unsafe_allow_html=True
        )
        st.caption(f"{len(pendientes)} oficio(s) activo(s) · Abre el expediente para consultar sus detalles")
        buscar_col, filtro_col = st.columns([3, 1], gap="small")
        with buscar_col:
            texto_busqueda = st.text_input("Buscar expedientes", placeholder="Folio, oficio, asunto o responsable...", key="pend_busqueda")
        with filtro_col:
            filtro_estado = st.selectbox("Filtrar por estado", ["Todos"] + sorted(activos), key="pend_filtro_estado")
        if texto_busqueda.strip():
            consulta = normalizar_texto(texto_busqueda)
            campos = [c for c in ("Folio", "No. de oficio", "Asunto", "Responsable") if c in pendientes.columns]
            if campos:
                mascara = pendientes[campos].fillna("").astype(str).apply(
                    lambda fila: any(consulta in normalizar_texto(v) for v in fila), axis=1)
                pendientes = pendientes.loc[mascara].copy()
        if filtro_estado != "Todos":
            pendientes = pendientes.loc[pendientes["Estatus"] == filtro_estado].copy()

        if st.session_state.pop("pend_guardado", False):
            st.success("Cambios guardados en Google Sheets. La consulta general ya refleja los nuevos datos.")

        if pendientes.empty:
            st.success("No tienes oficios pendientes.")
        else:
            # Primero los documentos en correcciones; después el resto de los activos.
            prioridad = {
                "En correcciones": 0, "Recaído": 1, "En elaboración": 2,
                "En revisión": 3, "En firma": 4,
            }
            pendientes["_prioridad"] = pendientes["Estatus"].map(prioridad).fillna(9)
            pendientes = pendientes.sort_values("_prioridad", kind="stable")
            for indice, (_, oficio) in enumerate(pendientes.iterrows()):
                def campo(nombre):
                    valor = oficio.get(nombre, "")
                    if valor is None or (not isinstance(valor, (dict, list)) and pd.isna(valor)):
                        return ""
                    return str(valor).strip()

                folio = campo("Folio")
                numero = campo("No. de oficio")
                asunto = campo("Asunto") or "Sin asunto registrado"
                responsable = campo("Responsable") or "Sin responsable"
                fecha = campo("Fecha de recepción") or "Sin fecha"
                estatus = campo("Estatus")
                estilo = {
                    "En correcciones": "correcciones", "En revisión": "revision",
                    "En elaboración": "elaboracion", "En firma": "firma",
                    "Recaído": "recaido",
                }.get(estatus, "normal")

                with st.container(border=True, key=f"pend_ficha_{indice}"):
                    col_info, col_accion = st.columns([5, 1.25], gap="medium", vertical_alignment="center")
                    with col_info:
                        st.markdown(
                            '<div class="pend-oficio">'
                            '<div class="pend-oficio-top">'
                            f'<strong>Folio {html.escape(folio or "Sin folio")}</strong>'
                            f'<span class="pend-estado {estilo}">{html.escape(estatus)}</span>'
                            '</div>'
                            f'<div class="pend-numero">{html.escape(numero)}</div>'
                            f'<div class="pend-asunto">{html.escape(asunto)}</div>'
                            '<div class="pend-meta">'
                            f'<span>Responsable: {html.escape(responsable)}</span>'
                            f'<span>Recepción: {html.escape(fecha)}</span>'
                            '</div></div>',
                            unsafe_allow_html=True
                        )
                    with col_accion:
                        identidad = (folio, numero)
                        abierto = st.session_state.get("pend_expediente_abierto") == identidad
                        etiqueta = "Cerrar expediente" if abierto else "Abrir expediente →"
                        if st.button(etiqueta, key=f"pend_abrir_{indice}",
                                     use_container_width=True, type="primary"):
                            st.session_state.pend_expediente_abierto = None if abierto else identidad
                            st.rerun()

                    if st.session_state.get("pend_expediente_abierto") == identidad:
                        autorizado = puede_editar_responsable(responsable)
                        edicion_general = (autorizado if _area_activa_validada() != "setf"
                                           else es_administrador_documental_setf())
                        if not autorizado:
                            st.info("Tienes acceso de consulta, pero no permiso para editar este oficio.")
                        if _area_activa_validada() == "setf":
                            # Ficha SETF según boceto aprobado: una sola tarjeta Word (65 %)
                            # y una tarjeta de documentación relacionada (35 %).
                            # No se tocan los permisos ni las funciones de Drive/Sheets.
                            panel_word, panel_adjuntos = st.columns([1.85, 1], gap="small", vertical_alignment="top")
                            with panel_word:
                                with st.container(border=True, key=f"setf_word_panel_{indice}"):
                                    st.markdown(
                                        '<div class="setf-exp-panel-title">'
                                        '<span class="setf-exp-panel-icon">▣</span>'
                                        '<strong>Documento Word</strong>'
                                        '</div>', unsafe_allow_html=True
                                    )
                                    try:
                                        documentos_setf = _setf_borradores_compartidos()
                                        enviado = _setf_ultimo_word_enviado(folio, numero, documentos_setf)
                                        if enviado:
                                            st.markdown(
                                                '<div class="setf-word-version">'
                                                '<span>VERSIÓN ACTUAL · ÚLTIMA ENVIADA</span>'
                                                f'<strong>Versión {html.escape(str(enviado.get("Version", "—")))}</strong>'
                                                '<small>Disponible para consulta y descarga.</small>'
                                                '</div>', unsafe_allow_html=True
                                            )
                                            ident_word = hashlib.sha1(f"{folio}|{numero}".encode()).hexdigest()[:12]
                                            _setf_documento_compartido(folio, numero, documentos_setf, "personal_" + ident_word, mostrar_titulo=False)
                                        else:
                                            st.markdown(
                                                '<div class="setf-word-version">'
                                                '<span>VERSIÓN ACTUAL</span>'
                                                '<strong>Sin documento enviado</strong>'
                                                '<small>Carga un Word para comenzar el flujo documental.</small>'
                                                '</div>', unsafe_allow_html=True
                                            )
                                    except Exception as error:
                                        st.warning(f"No fue posible consultar el Word enviado: {_mensaje_error_sheets(error)}")
                                    if autorizado:
                                        # El modo compacto quita las cabeceras duplicadas y
                                        # contrae la carga inicial, pero mantiene todas las acciones.
                                        mostrar_borradores_pendiente(folio, numero, responsable, compacto=True)
                                    else:
                                        st.caption("Solo consulta. La carga y autorización respetan los permisos existentes.")
                            with panel_adjuntos:
                                with st.container(border=True, key=f"setf_documentos_laterales_{indice}"):
                                    st.markdown(
                                        '<div class="setf-exp-panel-title">'
                                        '<span class="setf-exp-panel-icon">▱</span>'
                                        '<strong>Documentación relacionada</strong>'
                                        '</div>', unsafe_allow_html=True
                                    )
                                    with st.expander("Antecedentes", expanded=False):
                                        enlaces = leer_documentos(oficio.get("Antecedente", ""))
                                        if not enlaces:
                                            st.caption("Sin antecedentes registrados")
                                        for doc in enlaces:
                                            if doc.get("url"):
                                                st.link_button(doc.get("nombre") or "Abrir antecedente", doc["url"], use_container_width=True)
                                    with st.expander("Anexos", expanded=False):
                                        enlaces = leer_documentos(oficio.get("Anexo", ""))
                                        if not enlaces:
                                            st.caption("Sin anexos registrados")
                                        for doc in enlaces:
                                            if doc.get("url"):
                                                st.link_button(doc.get("nombre") or "Abrir anexo", doc["url"], use_container_width=True)
                                    st.markdown('<div class="setf-exp-state-label">ESTADO DEL EXPEDIENTE</div>', unsafe_allow_html=True)
                                    st.markdown(f'<div class="setf-exp-state">{html.escape(estatus)}</div>', unsafe_allow_html=True)
                            # Editor completo bajo ambas columnas, plegado de forma predeterminada.
                            if edicion_general:
                                with st.expander("✎ Editar oficio · datos y estado", expanded=False):
                                    # Se conserva el encabezado del oficio; solo cambia el panel editable.
                                    with st.container(border=True, key="pend_editor"):
                                        st.markdown(
                                            '<div class="pend-editor-header">'
                                            '<span class="pend-editor-kicker">ADMINISTRACIÓN DOCUMENTAL</span>'
                                            '<div class="pend-editor-title">Editar oficio</div>'
                                            '<div class="pend-editor-sub">Actualización y seguimiento del expediente</div>'
                                            '</div>', unsafe_allow_html=True
                                        )
                                        st.markdown('<div class="pend-editor-section">01 &nbsp; Datos principales</div>',
                                                    unsafe_allow_html=True)
                                        # Selector fuera del formulario para que Firmado despliegue campos al instante.
                                        opciones = ESTATUS_OFICIOS.copy()
                                        if estatus not in opciones:
                                            opciones.insert(0, estatus)
                                        clave_estado = f"pend_estado_{folio}_{numero}"
                                        if clave_estado not in st.session_state:
                                            st.session_state[clave_estado] = estatus
                                        nuevo_estado = st.selectbox("Estatus", opciones, key=clave_estado)

                                        documentos_actuales = leer_documentos(oficio.get("Firmado", ""))
                                        firmado_actual = documentos_actuales[0] if documentos_actuales else {"nombre": "", "url": ""}
                                        fecha_guardada = campo("Fecha de entrega")
                                        fecha_inicial = None
                                        for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                                            try:
                                                fecha_inicial = datetime.strptime(fecha_guardada, formato).date()
                                                break
                                            except ValueError:
                                                pass

                                        with st.form(key=f"pend_form_{indice}"):
                                            nuevo_asunto = st.text_area("Asunto", value=campo("Asunto"), height=90)
                                            nuevo_proyecto = st.text_input("Proyecto", value=campo("Proyecto"))
                                            st.markdown('<div class="pend-editor-section">02 &nbsp; Seguimiento del trámite</div>',
                                                        unsafe_allow_html=True)
                                            if nuevo_estado == "Firmado":
                                                st.markdown('<div class="pend-editor-sign">▣ &nbsp; Oficio firmado</div>',
                                                            unsafe_allow_html=True)
                                                col_nombre, col_url = st.columns([1, 1.6])
                                                with col_nombre:
                                                    nombre_firmado = st.text_input(
                                                        "Nombre del documento", value=firmado_actual["nombre"],
                                                        placeholder="Ej. Oficio firmado 21845"
                                                    )
                                                with col_url:
                                                    enlace_firmado = st.text_input(
                                                        "Enlace de Drive", value=firmado_actual["url"],
                                                        placeholder="https://drive.google.com/..."
                                                    )
                                                fecha_entrega = st.date_input(
                                                    "Fecha de entrega", value=fecha_inicial, format="DD/MM/YYYY"
                                                )
                                            else:
                                                nombre_firmado = firmado_actual["nombre"]
                                                enlace_firmado = firmado_actual["url"]
                                                fecha_entrega = fecha_inicial
                                                st.caption("El nombre, enlace y fecha de entrega se habilitan al seleccionar Firmado.")
                                            nuevas_observaciones = st.text_area(
                                                "Observaciones", value=campo("Observaciones"), height=90
                                            )
                                            st.markdown('<div class="pend-editor-divider"></div>', unsafe_allow_html=True)
                                            guardar = st.form_submit_button(
                                                "Guardar cambios en la base general", type="primary",
                                                use_container_width=True
                                            )

                                        if guardar:
                                            error_validacion = None
                                            if nuevo_estado == "Firmado":
                                                if not nombre_firmado.strip():
                                                    error_validacion = "Indica el nombre del documento firmado."
                                                elif not enlace_firmado.strip().startswith(("https://", "http://")):
                                                    error_validacion = "Introduce un enlace válido del documento firmado (https://...)."
                                                elif fecha_entrega is None:
                                                    error_validacion = "Indica la fecha de entrega del oficio firmado."
                                            if error_validacion:
                                                st.error(error_validacion)
                                            else:
                                                cambios = {
                                                    "Estatus": nuevo_estado,
                                                    "Asunto": nuevo_asunto.strip(),
                                                    "Proyecto": nuevo_proyecto.strip(),
                                                    "Observaciones": nuevas_observaciones.strip(),
                                                    "Fecha de entrega": (
                                                        fecha_entrega.isoformat() if nuevo_estado == "Firmado" else ""
                                                    ),
                                                }
                                                if nuevo_estado == "Firmado":
                                                    cambios["Firmado"] = json.dumps(
                                                        [{"nombre": nombre_firmado.strip(),
                                                          "url": enlace_firmado.strip()}], ensure_ascii=False
                                                    )
                                                try:
                                                    with st.spinner("Actualizando el expediente en Google Sheets..."):
                                                        guardar_edicion_pendiente(folio, numero, cambios)
                                                    st.session_state.pend_guardado = True
                                                    st.rerun()
                                                except Exception as error:
                                                    st.error(f"No fue posible completar el guardado: {error}")
                        else:
                            if edicion_general:
                                # Se conserva el encabezado del oficio; solo cambia el panel editable.
                                with st.container(border=True, key="pend_editor"):
                                    st.markdown(
                                        '<div class="pend-editor-header">'
                                        '<span class="pend-editor-kicker">ADMINISTRACIÓN DOCUMENTAL</span>'
                                        '<div class="pend-editor-title">Editar oficio</div>'
                                        '<div class="pend-editor-sub">Actualización y seguimiento del expediente</div>'
                                        '</div>', unsafe_allow_html=True
                                    )
                                    st.markdown('<div class="pend-editor-section">01 &nbsp; Datos principales</div>',
                                                unsafe_allow_html=True)
                                    # Selector fuera del formulario para que Firmado despliegue campos al instante.
                                    opciones = ESTATUS_OFICIOS.copy()
                                    if estatus not in opciones:
                                        opciones.insert(0, estatus)
                                    clave_estado = f"pend_estado_{folio}_{numero}"
                                    if clave_estado not in st.session_state:
                                        st.session_state[clave_estado] = estatus
                                    nuevo_estado = st.selectbox("Estatus", opciones, key=clave_estado)

                                    documentos_actuales = leer_documentos(oficio.get("Firmado", ""))
                                    firmado_actual = documentos_actuales[0] if documentos_actuales else {"nombre": "", "url": ""}
                                    fecha_guardada = campo("Fecha de entrega")
                                    fecha_inicial = None
                                    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                                        try:
                                            fecha_inicial = datetime.strptime(fecha_guardada, formato).date()
                                            break
                                        except ValueError:
                                            pass

                                    with st.form(key=f"pend_form_{indice}"):
                                        nuevo_asunto = st.text_area("Asunto", value=campo("Asunto"), height=90)
                                        nuevo_proyecto = st.text_input("Proyecto", value=campo("Proyecto"))
                                        st.markdown('<div class="pend-editor-section">02 &nbsp; Seguimiento del trámite</div>',
                                                    unsafe_allow_html=True)
                                        if nuevo_estado == "Firmado":
                                            st.markdown('<div class="pend-editor-sign">▣ &nbsp; Oficio firmado</div>',
                                                        unsafe_allow_html=True)
                                            col_nombre, col_url = st.columns([1, 1.6])
                                            with col_nombre:
                                                nombre_firmado = st.text_input(
                                                    "Nombre del documento", value=firmado_actual["nombre"],
                                                    placeholder="Ej. Oficio firmado 21845"
                                                )
                                            with col_url:
                                                enlace_firmado = st.text_input(
                                                    "Enlace de Drive", value=firmado_actual["url"],
                                                    placeholder="https://drive.google.com/..."
                                                )
                                            fecha_entrega = st.date_input(
                                                "Fecha de entrega", value=fecha_inicial, format="DD/MM/YYYY"
                                            )
                                        else:
                                            nombre_firmado = firmado_actual["nombre"]
                                            enlace_firmado = firmado_actual["url"]
                                            fecha_entrega = fecha_inicial
                                            st.caption("El nombre, enlace y fecha de entrega se habilitan al seleccionar Firmado.")
                                        nuevas_observaciones = st.text_area(
                                            "Observaciones", value=campo("Observaciones"), height=90
                                        )
                                        st.markdown('<div class="pend-editor-divider"></div>', unsafe_allow_html=True)
                                        guardar = st.form_submit_button(
                                            "Guardar cambios en la base general", type="primary",
                                            use_container_width=True
                                        )

                                    if guardar:
                                        error_validacion = None
                                        if nuevo_estado == "Firmado":
                                            if not nombre_firmado.strip():
                                                error_validacion = "Indica el nombre del documento firmado."
                                            elif not enlace_firmado.strip().startswith(("https://", "http://")):
                                                error_validacion = "Introduce un enlace válido del documento firmado (https://...)."
                                            elif fecha_entrega is None:
                                                error_validacion = "Indica la fecha de entrega del oficio firmado."
                                        if error_validacion:
                                            st.error(error_validacion)
                                        else:
                                            cambios = {
                                                "Estatus": nuevo_estado,
                                                "Asunto": nuevo_asunto.strip(),
                                                "Proyecto": nuevo_proyecto.strip(),
                                                "Observaciones": nuevas_observaciones.strip(),
                                                "Fecha de entrega": (
                                                    fecha_entrega.isoformat() if nuevo_estado == "Firmado" else ""
                                                ),
                                            }
                                            if nuevo_estado == "Firmado":
                                                cambios["Firmado"] = json.dumps(
                                                    [{"nombre": nombre_firmado.strip(),
                                                      "url": enlace_firmado.strip()}], ensure_ascii=False
                                                )
                                            try:
                                                with st.spinner("Actualizando el expediente en Google Sheets..."):
                                                    guardar_edicion_pendiente(folio, numero, cambios)
                                                st.session_state.pend_guardado = True
                                                st.rerun()
                                            except Exception as error:
                                                st.error(f"No fue posible completar el guardado: {error}")
                            # Borradores: solo usuarios con acceso al oficio.
                            if autorizado:
                                with st.container(border=True):
                                    mostrar_borradores_pendiente(folio, numero, responsable)


        # Actividad real desde la bitácora; la ausencia de eventos no bloquea la bandeja.
        if puede_gestionar_borradores() or puede_realizar_revision_final():
            with st.expander("◷  Actividad documental reciente", expanded=False):
                try:
                    eventos_recientes = _leer_registros_eventos()
                    eventos_recientes = sorted(eventos_recientes,
                        key=lambda e: str(e.get("Fecha", "")), reverse=True)[:8]
                    if not eventos_recientes:
                        st.caption("Todavía no hay movimientos registrados.")
                    for evento in eventos_recientes:
                        st.markdown(f"**{html.escape(str(evento.get('Accion', 'Movimiento')))}** · "
                                    f"Folio {html.escape(str(evento.get('Folio', '')))}")
                        st.caption(f"{evento.get('Fecha', '')} · {evento.get('Usuario', '')} · "
                                   f"{evento.get('Detalle', '')}")
                except Exception as error:
                    st.caption(f"Actividad no disponible por el momento: {error}")
        col_vuelta, col_todos = st.columns([1, 1], gap="medium")
        with col_vuelta:
            if st.button("← Menú principal", key="pendientes_menu"):
                navegar_a("home")
        with col_todos:
            if st.button("Ver todos los oficios →", key="pendientes_todos",
                         use_container_width=True):
                navegar_a("oficios")

    if bandeja_actual == "Por revisar" and "Por revisar" in etiquetas:
        _bandeja_borradores_compartida()
        # Regresos de firma de los oficios que no tuvieron borradores Word.
        _panel_directos_para_revisores()
    if bandeja_actual == "En proceso de firma":
        _vista_firma_externa(registros)


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

def cargar_imagen_base64(ruta):
    ruta_completa = BASE_DIR / ruta if not Path(ruta).is_absolute() else Path(ruta)
    if not ruta_completa.exists():
        return ""
    with open(ruta_completa, "rb") as archivo:
        return base64.b64encode(archivo.read()).decode()


fondo_planos = obtener_imagen_base64("fondo_planos.jpg")
icono_documento = obtener_imagen_base64("icono_documento.png")
icono_oficios = obtener_imagen_base64("icono_oficios.png")
icono_diseno = obtener_imagen_base64("icono_diseno.png")


# =========================================================
# CSS GENERAL
# =========================================================

st.markdown(
    """
<style>

html, body, [class*="css"] {
    font-family: Arial, Helvetica, sans-serif;
}

.stApp {
    background-image: url(""" + obtener_imagen_base64("fondo_planos.jpg") + """");
    background-size: cover;
    background-position: center center;
    background-repeat: no-repeat;
    background-attachment: fixed;
}

header[data-testid="stHeader"] {
    background: transparent;
}

footer {
    visibility: hidden;
}

#MainMenu {
    visibility: hidden;
}

.block-container {
    max-width: 1200px !important;
    padding-top: 2rem !important;
    padding-left: 40px !important;
    padding-right: 40px !important;
    margin: 0 auto !important;
}

/* Botones principales en azul marino institucional */
div.stButton > button[kind="primary"] {
    background-color: #263B49 !important;
    border: 1px solid #263B49 !important;
    color: #FFFFFF !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
}

div.stButton > button[kind="primary"]:hover {
    background-color: #354F60 !important;
    border-color: #B49A62 !important;
    color: #FFFFFF !important;
}

/* Botones de antecedentes y anexos */
div.st-key-agregar_antecedentes button,
div.st-key-agregar_anexos button {
    background-color: #263B49 !important;
    color: #FFFFFF !important;
    border: 1px solid #263B49 !important;
    border-radius: 6px !important;
    padding: 9px 16px !important;
    font-weight: 500 !important;
    transition: all 0.2s ease !important;
}

/* Efecto al pasar el cursor */
div.st-key-agregar_antecedentes button:hover,
div.st-key-agregar_anexos button:hover {
    background-color: #354F60 !important;
    border-color: #B49A62 !important;
    color: #FFFFFF !important;
}

/* Efecto al seleccionar */
div.st-key-agregar_antecedentes button:focus,
div.st-key-agregar_anexos button:focus {
    border-color: #B49A62 !important;
    color: #FFFFFF !important;
}

/* Campos de texto */
div[data-testid="stTextInput"] input,
div[data-testid="stTextArea"] textarea {
    background-color: #E3E9EE !important;
    border: 1px solid #B8C5CF !important;
    border-radius: 6px !important;
    color: #20394A !important;
}

/* Menús desplegables */
div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background-color: #E3E9EE !important;
    border: 1px solid #B8C5CF !important;
    border-radius: 6px !important;
}

/* Selectores de fecha */
div[data-testid="stDateInput"] input {
    background-color: #E3E9EE !important;
    border: 1px solid #B8C5CF !important;
    border-radius: 6px !important;
}

/* Resaltar campo seleccionado */
div[data-testid="stTextInput"] input:focus,
div[data-testid="stTextArea"] textarea:focus,
div[data-testid="stDateInput"] input:focus {
    border-color: #B49A62 !important;
    box-shadow: 0 0 0 1px #B49A62 !important;
}

@media (max-width: 900px) {
    .block-container {
        padding-left: 20px !important;
        padding-right: 20px !important;
    }
}

/* =========================================================
   FORMULARIO DE REGISTRO - TEXTO LEGIBLE
   ========================================================= */

/* Títulos del formulario */
[data-testid="stMain"] h1,
[data-testid="stMain"] h2,
[data-testid="stMain"] h3,
[data-testid="stMain"] h4 {
    color: #10283b !important;
}

/* Etiquetas de campos */
[data-testid="stMain"] label,
[data-testid="stMain"] label p {
    color: #10283b !important;
}

/* Texto auxiliar */
[data-testid="stMain"] .stMarkdown p {
    color: #10283b;
}

/* Texto escrito en campos normales */
[data-testid="stMain"] input,
[data-testid="stMain"] textarea {
    color: #10283b !important;
}

/* Evitar que el texto de los botones cambie */
[data-testid="stMain"] button p {
    color: inherit !important;
}

/* =========================================================
   BARRA DE CONSULTA DE EXPEDIENTES
   ========================================================= */

.consulta-panel {
    background: rgba(255, 255, 255, 0.82);
    border: 1px solid rgba(189,155,93,.45);
    border-top: 3px solid #B08A4A;
    border-radius: 13px;
    padding: 16px 22px 14px 22px;
    margin-top: 10px;
    margin-bottom: 10px;
    box-shadow: 0 4px 14px rgba(16, 40, 59, 0.04);
}

.consulta-encabezado {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 4px;
}

.consulta-icono {
    width: 39px;
    height: 39px;

    border-radius: 50%;

    background: #EEF3F6;
    border: 1px solid #D8E0E5;

    display: flex;
    align-items: center;
    justify-content: center;

    color: #10283B;
    font-size: 18px;

    flex-shrink: 0;
}

.consulta-titulo {
    color: #10283B;
    font-size: 16px;
    font-weight: 750;
    line-height: 1.15;
}

.consulta-subtitulo {
    color: #7D8D98;
    font-size: 10px;
    margin-top: 4px;
}


/* =========================================================
   INPUT DE BÚSQUEDA
   ========================================================= */

div[data-testid="stTextInput"] input {
    border: 1px solid #D5DEE5 !important;
    border-radius: 9px !important;
    background: #F8FAFB !important;
    min-height: 46px !important;
    box-shadow: none !important;
}

div[data-testid="stTextInput"] input:focus {
    border-color: #B08A4A !important;
    box-shadow: 0 0 0 1px rgba(176,138,74,0.18) !important;
}


/* =========================================================
   BOTONES DE LA BARRA DE CONSULTA
   ========================================================= */

div[data-testid="stButton"] > button {
    min-height: 46px;
    border-radius: 9px;
    font-weight: 650;
    transition:
        transform 0.15s ease,
        box-shadow 0.15s ease,
        background 0.15s ease;
}

div[data-testid="stButton"] > button:hover {
    transform: translateY(-1px);
    box-shadow:
        0 5px 12px rgba(16, 40, 59, 0.10);
}


/* Botón primario */
button[kind="primary"] {
    background: #10283B !important;
    border: 1px solid #10283B !important;
    color: white !important;
}

button[kind="primary"]:hover {
    background: #19394F !important;
    border-color: #19394F !important;
}


/* Botón secundario */
button[kind="secondary"] {
    background: rgba(255,255,255,0.92) !important;
    border: 1px solid #B7C3CB !important;
    color: #10283B !important;
}

button[kind="secondary"]:hover {
    border-color: #B08A4A !important;
    background: #FFFFFF !important;
}

/* Reduce espacios verticales innecesarios */
div[data-testid="stHorizontalBlock"] {
    gap: 12px;
}

/* =========================================================
   PANEL DE INDICADORES Y CABECERAS
   ========================================================= */

.of-panel-header {
    margin-top: 4px;
    margin-bottom: 12px;
    text-align: center;
}

</style>
""",
    unsafe_allow_html=True,
)


# =========================================================
# ENCABEZADO INSTITUCIONAL — MÓDULOS DOCUMENTALES
# Diseño Geométrico: cabecera compartida, sin cambiar las acciones internas.
# =========================================================
def _cerrar_sesion_desde_cabecera():
    _cookies_sesion["sesion"] = ""
    _cookies_sesion.save()
    st.session_state.autenticado = False
    st.session_state.usuario_actual = None
    st.session_state.usuario_id = None
    st.session_state.rol = "auxiliar"
    st.session_state.cargo_institucional = ""
    st.session_state.responsable_oficios = ""
    st.session_state.menu_activo = "home"
    st.session_state.pop("area_actual", None)
    st.session_state.pop("pend_bandeja", None)
    st.rerun()


def mostrar_cabecera_documental():
    """Franja superior única para Oficios, Pendientes, Urgentes y Firmados."""
    import html as _html

    st.markdown("""
    <style>
      /* Únicamente las vistas con cabecera documental */
      /* Acercar la barra a la parte superior sin afectar los demás módulos. */
      [data-testid="stMainBlockContainer"]:has(.st-key-cabecera_documental),
      .block-container:has(.st-key-cabecera_documental) {
        padding-top: .35rem !important;
      }
      /* Streamlit envuelve los containers en otros bloques: hacer sticky
         también su wrapper impide que se despegue al desplazarse. */
      [data-testid="stVerticalBlockBorderWrapper"]:has(> .st-key-cabecera_documental),
      [data-testid="stElementContainer"]:has(> .st-key-cabecera_documental) {
        position: sticky !important;
        top: .35rem !important;
        z-index: 1000 !important;
      }
      .st-key-cabecera_documental {
        background: linear-gradient(110deg, #12273A 0%, #1C3547 70%, #223F4C 100%) !important;
        border: 1px solid rgba(214,173,107,.33) !important;
        border-radius: 15px !important;
        padding: 12px 18px 12px 18px !important;
        box-shadow: 0 6px 22px rgba(10,28,40,.17) !important;
        position: sticky !important;
        top: .35rem !important;
        z-index: 1000 !important;
        /* Levantar cabecera y contenido unos 68 px respecto a la versión anterior.
           Se aplica únicamente a los módulos documentales. */
        margin-top: -80px !important;
      }
      .st-key-cabecera_documental [data-testid="stHorizontalBlock"] {
        align-items: center !important;
        gap: 13px !important;
      }
      .st-key-cabecera_documental [data-testid="stImage"] img {
        max-height: 62px !important;
        max-width: 62px !important;
        object-fit: contain !important;
      }
      .st-key-cabecera_documental p { margin-bottom: 0 !important; }
      .st-key-cabecera_documental [data-testid="stButton"] button {
        min-height: 39px !important;
        width: 100% !important;
        border-radius: 8px !important;
        background: rgba(255,255,255,.035) !important;
        color: #FFF !important;
        border: 1px solid #BCA06B !important;
        font-size: .80rem !important;
        font-weight: 600 !important;
        white-space: nowrap !important;
      }
      .st-key-cabecera_documental [data-testid="stButton"] button:hover {
        background: #294658 !important;
        color: #FFF !important;
      }
      .st-key-cabecera_documental [data-testid="stButton"] button p,
      .st-key-cabecera_documental [data-testid="stButton"] button span {color:#FFF !important;}
      @media (max-width: 1050px) {
        .st-key-cabecera_documental {padding: 9px !important;}
        .st-key-cabecera_documental [data-testid="stHorizontalBlock"] {gap:6px !important;}
      }
    </style>
    """, unsafe_allow_html=True)

    # Logotipo de SETF: tamaño apropiado para la rodela circular, sin afectar a DG.
    if _area_activa_validada() == "setf":
        st.markdown("""<style>
          .st-key-cabecera_documental [data-testid="stImage"] {
            display: flex !important;
            justify-content: center !important;
            align-items: center !important;
          }
          .st-key-cabecera_documental [data-testid="stImage"] img {
            max-height: 94px !important;
            max-width: 94px !important;
            height: 94px !important;
            width: 94px !important;
            object-fit: contain !important;
            margin: 0 auto !important;
            filter: drop-shadow(0 2px 3px rgba(0,0,0,.18));
          }
        </style>""", unsafe_allow_html=True)

    nombre = str(st.session_state.get("usuario_actual") or "").strip()
    if identificador_sesion() == "alondra":
        nombre = "Tte. Alondra Barajas"
    # SOLO SETF: usar el emblema institucional ya guardado como
    # assets/logo_selector.png, sin renombrarlo ni moverlo. No afecta al
    # selector de áreas ni a la rodela SETF de la barra lateral.
    # Diseño Geométrico conserva su logotipo y las medidas originales.
    es_setf = _area_activa_validada() == "setf"
    if es_setf:
        logo = ASSETS_DIR / "logo_selector.png"
        if not logo.is_file():
            st.error("Falta assets/logo_selector.png para el encabezado de SETF. "
                     "Comprueba que esté dentro de la carpeta assets.")
    else:
        logo = ASSETS_DIR / "logo_diseno_geometrico.png"
        if not logo.is_file():
            logo = ASSETS_DIR / "logo_selector.png"  # Respaldo original de DG.

    with st.container(key="cabecera_documental"):
        columnas_cabecera = ([0.85, 3.0, 1.8, 1.65, 1.4]
                            if es_setf else [0.65, 3.2, 1.8, 1.65, 1.4])
        col_logo, col_marca, col_sesion, col_area, col_salir = st.columns(
            columnas_cabecera, vertical_alignment="center", gap="small"
        )
        with col_logo:
            if logo.is_file():
                st.image(str(logo), width=94 if es_setf else 59)
        with col_marca:
            st.markdown(_rotulos_area("""<div style="font-weight:800;font-size:21px;letter-spacing:1.2px;line-height:1.2;color:#fff">CONTROL DOCUMENTAL</div>
            <div style="margin-top:4px;color:#B8C8D3;font-size:10px;font-weight:700;letter-spacing:2px">MESA DE DISEÑO GEOMÉTRICO</div>"""), unsafe_allow_html=True)
        with col_sesion:
            st.markdown(
                '<div style="text-align:right;color:#C7D6E0;font-size:11px">Sesión activa</div>'
                f'<div translate="no" class="notranslate" style="text-align:right;color:white;font-size:13px;font-weight:700">{_html.escape(nombre)}</div>',
                unsafe_allow_html=True,
            )
        with col_area:
            if len(areas_autorizadas()) == 2:
                if st.button("⇄ Cambiar de área", key="doc_cabecera_cambiar_area", use_container_width=True):
                    st.session_state.area_actual = None
                    st.session_state.menu_activo = "home"
                    st.session_state.pop("pend_bandeja", None)
                    st.rerun()
        with col_salir:
            if st.button("↪ Cerrar sesión", key="doc_cabecera_salir", use_container_width=True):
                _cerrar_sesion_desde_cabecera()


# =========================================================
# SETF · FONDOS INDEPENDIENTES POR APARTADO
# =========================================================
# Solo cambia la presentación. Las consultas, permisos, los widgets y los
# procesos de Google Sheets/Drive son los mismos que en la versión anterior.
# Los archivos se guardan en assets/ y NO se incluyen en este app.py.
_FONDOS_SETf_POR_PAGINA = {
    "oficios": "fondo_setf_oficios.png",
    "pendientes": "fondo_setf_pendientes.png",
    "urgentes": "fondo_setf_urgentes.png",
    "firmados": "fondo_setf_firmados.png",
    "minutas": "fondo_minutas_setf.png",  # Fondo exclusivo del módulo Minutas SETF.
    "reuniones": "fondo_minutas_setf.png",  # Agenda SETF, reutiliza fondo existente.
}


@st.cache_data(show_spinner=False)
def _imagen_setf_pagina_cacheada(ruta_absoluta, fecha_modificacion_ns):
    """Evita recodificar los PNG grandes ante cada interacción de Streamlit."""
    ruta = Path(ruta_absoluta)
    if not ruta.is_file():
        return ""
    return base64.b64encode(ruta.read_bytes()).decode("ascii")


def aplicar_fondo_setf_por_pagina():
    """Fondo de TODA la página, nunca del banner ni de las tarjetas."""
    if _area_activa_validada() != "setf":
        return
    nombre = _FONDOS_SETf_POR_PAGINA.get(st.session_state.get("menu_activo"))
    if not nombre:
        return  # Home y Módulo de planos conservan sus fondos actuales.

    ruta = ASSETS_DIR / nombre
    if ruta.is_file():
        imagen = _imagen_setf_pagina_cacheada(str(ruta), ruta.stat().st_mtime_ns)
        imagen_css = (
            'linear-gradient(rgba(250,252,253,.13),rgba(250,252,253,.17)),'
            f'url("data:image/png;base64,{imagen}")'
        )
    else:
        # No utilizar accidentalmente el plano de DG cuando falte un PNG SETF.
        imagen_css = 'linear-gradient(140deg,#fcfcfb 0%,#f4f8f9 100%)'

    # Especificidad superior a los estilos heredados de DG y a los fondos
    # de Urgentes/Firmados que se inyectan después, también con !important.
    selector = ('html body [data-testid="stAppViewContainer"]'
                '[data-testid="stAppViewContainer"]'
                '[data-testid="stAppViewContainer"]')
    st.markdown(f"""<style>
    {selector} {{
        background-color: #f9fbfc !important;
        background-image: {imagen_css} !important;
        background-size: cover !important;
        background-position: center center !important;
        background-repeat: no-repeat !important;
        background-attachment: fixed !important;
    }}
    {selector} [data-testid="stMain"],
    {selector} > .main,
    {selector} [data-testid="stHeader"] {{
        background-color: transparent !important;
    }}
    @media (max-width: 760px) {{
        {selector} {{
            background-attachment: scroll !important;
            background-position: center top !important;
        }}
    }}
    </style>""", unsafe_allow_html=True)


if st.session_state.menu_activo in ("oficios", "pendientes", "urgentes", "firmados", "minutas", "reuniones"):
    aplicar_fondo_setf_por_pagina()
    if _area_activa_validada() != "setf":
        mostrar_cabecera_documental()


# =========================================================
# SETF: NAVEGACIÓN SUPERIOR Y CENTRO DE NOTIFICACIONES
# No se ejecuta para Diseño Geométrico.
# =========================================================
_SETf_NAV = [
    ("home", "⌂ Inicio"), ("oficios", "▤ Oficios"),
    ("pendientes", "☷ Pendientes"), ("urgentes", "⚠ Urgentes"),
    ("firmados", "✓ Firmados"), ("minutas", "▣ Minutas"),
    ("reuniones", "▦ Reuniones"),
]


def _setf_notificaciones_actuales():
    """Avisos derivados de registros existentes. No escribe en hojas operativas."""
    exigir_sesion()
    if _area_activa_validada() != "setf":
        return []
    eventos = []
    usuario = identificador_sesion()
    responsable = str(st.session_state.get("responsable_oficios") or "").strip()
    # No usar marcas de tiempo inventadas. IDs estables por registro y tipo.
    try:
        from datetime import timedelta
        hoy = _reuniones_ahora().date()
        libro, ws = _reuniones_hoja()
        for r in _reuniones_leer(libro, ws):
            if r["estado"] in ("Cancelada", "Realizada") or not (hoy <= r["fecha"] <= hoy + timedelta(days=7)):
                continue
            clave = str(r.get("id") or r["huella"])
            eventos.append({"id": "reunion:" + clave, "tipo": "general",
                "titulo": "Reunión próxima", "detalle": f'{r["asunto"]} · {r["fecha"].strftime("%d/%m/%Y")} · {r["hora"]}',
                "destino": "reuniones", "fecha": r["fecha"].isoformat()})
    except Exception:
        pass  # Otros eventos siguen disponibles si hay problemas en la agenda.
    try:
        df = obtener_oficios_por_area("setf")
        if not df.empty and "Responsable" in df.columns:
            for _, r in df.iterrows():
                nombre = str(r.get("Responsable", "")).strip()
                if not responsable or normalizar_texto(nombre) != normalizar_texto(responsable):
                    continue
                estatus = str(r.get("Estatus", r.get("Estado", ""))).strip()
                if normalizar_texto(estatus) in ("firmado", "firmados", "cancelado"):
                    continue
                ident = str(r.get("Folio") or r.get("No. de oficio") or "").strip()
                if not ident:
                    continue
                eventos.append({"id": "asignacion:" + ident + ":" + usuario,
                    "tipo": "personal", "titulo": "Oficio asignado",
                    "detalle": str(r.get("No. de oficio") or ident),
                    "destino": "oficios", "fecha": ""})
    except Exception:
        pass
    try:
        # Solo se notifica un envío cuando su oficio de origen continúa vigente.
        originales = obtener_oficios_por_area("setf")
        validos = set()
        if not originales.empty:
            validos = {(str(r.get("Folio", "")).strip(), str(r.get("No. de oficio", "")).strip())
                       for _, r in originales.iterrows()}
        for r in _leer_envios_firma():
            ident = (str(r.get("Folio", "")).strip(), str(r.get("No. de oficio origen", "")).strip())
            if ident not in validos:
                continue
            nuevo = str(r.get("No. de oficio nuevo") or "").strip()
            marca = str(r.get("Fecha envío") or "").strip()
            if not nuevo:
                continue
            eventos.append({"id": "firma:" + hashlib.sha256(repr((ident, nuevo, marca)).encode()).hexdigest()[:20],
                "tipo": "general", "titulo": "Oficio enviado a firma", "detalle": nuevo,
                "destino": "firmados", "fecha": marca})
    except Exception:
        pass
    # Quitar duplicados sin cambiar el orden.
    return list({e["id"]: e for e in eventos}.values())[:80]


def _setf_hoja_lecturas():
    libro = conectar_google_sheets_por_area("setf").spreadsheet
    try:
        hoja = libro.worksheet("NOTIFICACIONES_LECTURAS")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="NOTIFICACIONES_LECTURAS", rows=2000, cols=3)
        hoja.append_row(["Usuario", "ID", "Fecha lectura"], value_input_option="RAW")
    if hoja.row_values(1)[:3] != ["Usuario", "ID", "Fecha lectura"]:
        raise ValueError("NOTIFICACIONES_LECTURAS tiene encabezados distintos; no se modificaron registros.")
    return hoja


def _setf_leidas(usuario):
    hoja = _setf_hoja_lecturas()
    return {str(r.get("ID", "")) for r in hoja.get_all_records() if str(r.get("Usuario", "")) == usuario}


def _setf_marcar_leida(evento_id):
    usuario = identificador_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Solo SETF puede usar estas notificaciones.")
    hoja = _setf_hoja_lecturas()
    if evento_id not in _setf_leidas(usuario):
        from zoneinfo import ZoneInfo
        ahora = datetime.now(ZoneInfo("America/Mexico_City")).isoformat(timespec="seconds")
        hoja.append_row([usuario, evento_id, ahora], value_input_option="RAW")


def _setf_campana():
    if "setf_avisos_abiertos" not in st.session_state:
        st.session_state.setf_avisos_abiertos = False
    eventos = _setf_notificaciones_actuales()
    usuario = identificador_sesion()
    try:
        leidas = _setf_leidas(usuario)
    except Exception as error:
        st.caption("Avisos: almacenamiento de lecturas no disponible.")
        leidas = set()
    pendientes = [e for e in eventos if e["id"] not in leidas]
    titulo = f"🔔 {len(pendientes)}" if pendientes else "🔔"
    if st.button(titulo, key="setf_campana_toggle", help="Centro de notificaciones", use_container_width=True):
        st.session_state.setf_avisos_abiertos = not st.session_state.setf_avisos_abiertos
    if st.session_state.setf_avisos_abiertos:
        with st.popover("Ver avisos", use_container_width=True):
            st.markdown("**Centro de notificaciones SETF**")
            tipo = st.radio("Mostrar", ["No leídas", "Todas"], horizontal=True, key="setf_aviso_filtro")
            vistos = pendientes if tipo == "No leídas" else eventos
            if not vistos:
                st.caption("No hay avisos en esta categoría.")
            for e in vistos[:20]:
                st.markdown(f'**{html.escape(e["titulo"])}**')
                st.caption(e["detalle"])
                a, b = st.columns(2)
                with a:
                    if st.button("Ver módulo", key="setf_aviso_ir_" + e["id"]):
                        navegar_a(e["destino"])
                with b:
                    if e["id"] not in leidas and st.button("Marcar leído", key="setf_aviso_leer_" + e["id"]):
                        _setf_marcar_leida(e["id"])
                        st.rerun()
                st.divider()


def _setf_barra_superior():
    """Cabecera panorámica exclusiva de las subáreas SETF."""
    if _area_activa_validada() != "setf":
        return
    banner = ASSETS_DIR / "banner_header_setf.png"
    banner_css = "linear-gradient(110deg,#112b39,#203c44)"
    if banner.is_file():
        encoded = base64.b64encode(banner.read_bytes()).decode("ascii")
        banner_css = (
            'linear-gradient(90deg,rgba(9,28,40,.70),rgba(10,30,42,.45) 45%,rgba(9,28,40,.76)),'
            f'url("data:image/png;base64,{encoded}")'
        )
    else:
        st.warning("No se encontró assets/banner_header_setf.png; se muestra un fondo alternativo.")
    st.markdown("""<style>
    /* Navegación SETF: solo subáreas, sin modificar Diseño Geométrico o portada. */
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
    [data-testid="stSidebarNav"] {display:none!important;}
    [data-testid="stAppViewContainer"] .block-container {
       max-width:1640px!important;padding-left:1.65rem!important;
       padding-right:1.65rem!important;padding-top:0!important;}
    /* No desplazar la cabecera fuera del viewport: -6rem recortaba el
       escudo y el tren en la parte superior. El bloque ya tiene padding-top:0. */
    .st-key-setf_header_superior {
       margin-top:0!important;
       scroll-margin-top:12px!important;
    }
    @media(max-width:700px){
       .st-key-setf_header_superior{margin-top:0!important;}
    }
    [data-testid="stHeader"] {background:transparent!important;height:0!important;min-height:0!important;}
    [data-testid="stDecoration"] {display:none!important;}
    [data-testid="stAppViewContainer"] > section.main {padding-top:0!important;}
    .st-key-setf_header_superior {
       background-image:__BANNER__!important;
       background-color:#15303d!important;background-size:cover!important;
       background-position:center 48%!important;
       border:1px solid rgba(196,161,104,.47)!important;
       border-radius:14px 14px 0 0!important;
       padding:14px 24px!important;margin-bottom:0!important;
       min-height:132px!important;
       box-shadow:0 7px 20px rgba(20,28,28,.15)!important;}
    .st-key-setf_header_superior [data-testid="stHorizontalBlock"] {align-items:center!important;}
    .setf-header-title {font-size:clamp(20px,2.1vw,29px);font-weight:850;color:white;
       letter-spacing:1.15px;text-shadow:0 1px 5px rgba(0,0,0,.3);}
    .setf-header-sub {font-size:10px;font-weight:750;letter-spacing:1.8px;color:#dfc79e;}
    .st-key-setf_header_superior button {background:rgba(12,30,39,.40)!important;
       border:1px solid rgba(219,191,146,.45)!important;color:white!important;
       border-radius:9px!important;min-height:44px!important;font-weight:650!important;}
    .st-key-setf_header_superior button:hover {background:rgba(56,77,79,.91)!important;border-color:#d3af70!important;}
    .st-key-setf_perfil_popover button {text-align:left!important;}
    .st-key-setf_navbar {background:linear-gradient(110deg,#303333,#3f4140)!important;
       border:1px solid #A58D62!important;border-top:0!important;border-radius:0 0 14px 14px!important;
       padding:7px 9px!important;margin-top:-.7rem!important;
       box-shadow:0 7px 18px rgba(20,28,28,.16)!important;
       position:sticky!important;top:0!important;z-index:1100!important;}
    .st-key-setf_navbar [data-testid="stHorizontalBlock"] {gap:5px!important;align-items:center!important;}
    .st-key-setf_navbar button {background:transparent!important;color:#fff!important;
       border:1px solid transparent!important;border-radius:7px!important;
       font-weight:650!important;min-height:42px!important;padding:6px 5px!important;
       white-space:nowrap!important;font-size:13px!important;}
    .st-key-setf_navbar button:hover {background:#57544b!important;border-color:#b99a65!important;}
    /* Contenido de CSS no pasa por el traductor automático de Chrome.
       Se conserva el texto original accesible en el botón de Streamlit. */
    .st-key-setf_navbar [class*="st-key-setf_nav_reuniones"] button,
    .st-key-setf_navbar [class*="st-key-setf_nav_ACTIVA_reuniones"] button {position:relative!important;}
    .st-key-setf_navbar [class*="st-key-setf_nav_reuniones"] button p,
    .st-key-setf_navbar [class*="st-key-setf_nav_ACTIVA_reuniones"] button p {visibility:hidden!important;}
    .st-key-setf_navbar [class*="st-key-setf_nav_reuniones"] button::after,
    .st-key-setf_navbar [class*="st-key-setf_nav_ACTIVA_reuniones"] button::after {
       content:"▦ Reuniones"!important;position:absolute!important;
       inset:0!important;display:flex!important;align-items:center!important;
       justify-content:center!important;color:inherit!important;
       pointer-events:none!important;font-weight:650!important;font-size:13px!important;
       font-family:inherit!important;letter-spacing:normal!important;line-height:1.2!important;
    }
    /* Igualar el tamaño y peso de todas las etiquetas del menú. */
    .st-key-setf_navbar button p,
    .st-key-setf_navbar button::after {
       font-family:inherit!important;font-size:13px!important;font-weight:650!important;
       line-height:1.2!important;letter-spacing:0!important;
    }
    .st-key-setf_navbar [class*="st-key-setf_nav_ACTIVA"] button {
       background:linear-gradient(110deg,#b38e58,#84683d)!important;
       color:white!important;border-color:#c5ab7a!important;}
    @media(max-width:880px){
      .st-key-setf_navbar{margin-top:0!important;}
      .st-key-setf_navbar{position:relative!important;}
      .st-key-setf_navbar [data-testid="stHorizontalBlock"]{flex-wrap:wrap!important;}
      .st-key-setf_navbar [data-testid="column"]{min-width:100px!important;}
      .setf-header-sub{font-size:8px!important;}
    }
    </style>""".replace("__BANNER__", banner_css), unsafe_allow_html=True)
    with st.container(key="setf_header_superior"):
        logo_col, titulo_col, avisos_col, usuario_col = st.columns(
            [1.05, 4.75, 0.7, 1.9], gap="small", vertical_alignment="center")
        with logo_col:
            logo = ASSETS_DIR / "logo_setf.png"
            if logo.is_file():
                st.image(str(logo), width=112)
        with titulo_col:
            st.markdown('<div class="setf-header-title">CONTROL DOCUMENTAL</div>'
                '<div class="setf-header-sub">MESA DE SISTEMAS ELÉCTRICOS · SETF</div>',
                unsafe_allow_html=True)
        with avisos_col:
            _setf_campana()
        with usuario_col:
            nombre = str(st.session_state.get("usuario_actual") or "Usuario")
            with st.popover(f"◉  {nombre}  ▾", use_container_width=True):
                st.markdown("**Cuenta institucional**")
                st.caption(nombre)
                if len(areas_autorizadas()) == 2:
                    if st.button("⇄ Cambiar de área", key="setf_perfil_cambiar", use_container_width=True):
                        st.session_state.area_actual = None
                        st.session_state.menu_activo = "home"
                        st.session_state.pop("pend_bandeja", None)
                        st.rerun()
                if st.button("↪ Cerrar sesión", key="setf_perfil_cerrar", use_container_width=True):
                    _cerrar_sesion_desde_cabecera()
    with st.container(key="setf_navbar"):
        cols = st.columns([1] * 7, gap="small", vertical_alignment="center")
        for (destino, etiqueta), columna in zip(_SETf_NAV, cols):
            with columna:
                clave = ("setf_nav_ACTIVA_" if st.session_state.menu_activo == destino
                         else "setf_nav_") + destino
                if st.button(etiqueta, key=clave, use_container_width=True):
                    if st.session_state.menu_activo != destino:
                        navegar_a(destino)


# =========================================================
# MENÚ LATERAL COMPARTIDO: OFICIOS / MIS PENDIENTES
# =========================================================
# Mantener la misma estructura de widgets y las mismas claves entre
# pantallas evita desmontajes innecesarios durante la navegación.
if _area_activa_validada() != "setf" and st.session_state.menu_activo in ("oficios", "pendientes", "urgentes", "firmados", "minutas", "reuniones"):
    # Imagen de la barra lateral: independiente del fondo del área principal.
    # Se incluye dentro del CSS para que funcione en Streamlit Cloud y localhost.
    if _area_activa_validada() == "setf":
        # No mostrar el fondo lateral de Diseño Geométrico dentro de SETF.
        ruta_setf_sidebar = ASSETS_DIR / "fondo_sidebar_setf.png"
        ruta_fondo_sidebar = ruta_setf_sidebar if ruta_setf_sidebar.is_file() else None
    else:
        ruta_fondo_sidebar = next((ruta for ruta in (
            *(ASSETS_DIR / f"fondo_sidebar{ext}" for ext in
              (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")),
            ASSETS_DIR / "fondo_ferroviario.png",
            ASSETS_DIR / "fondo_pendientes.png",
            ASSETS_DIR / "fondo_oficios.png",
        ) if ruta.is_file()), None)
    fondo_sidebar_css = ""
    if ruta_fondo_sidebar is not None:
        extension = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png",
                     ".webp": "webp"}[ruta_fondo_sidebar.suffix.lower()]
        imagen_sidebar = base64.b64encode(ruta_fondo_sidebar.read_bytes()).decode("ascii")
        # La imagen personalizada ya es clara: evitar que una capa opaca la oculte.
        es_personalizado = ruta_fondo_sidebar.stem.lower() in ("fondo_sidebar", "fondo_sidebar_setf")
        opacidad = ".20" if es_personalizado else ".87"
        opacidad_final = ".25" if es_personalizado else ".91"
        fondo_sidebar_css = (
            f'background-image: linear-gradient(rgba(250,249,246,{opacidad}), '
            f'rgba(250,249,246,{opacidad_final})), '
            f'url("data:image/{extension};base64,{imagen_sidebar}") !important;'
            'background-size: cover !important;'
            'background-position: center center !important;'
            'background-repeat: no-repeat !important;'
        )
    st.markdown("""
    <style>
    [data-testid="stSidebar"] {
        border-right: 1px solid #cbb88f !important;
        background-color: #f8f7f3 !important;
        __FONDO_SIDEBAR__
    }
    [data-testid="stSidebar"] > div,
    [data-testid="stSidebar"] [data-testid="stSidebarContent"],
    [data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        background: transparent !important;
    }
    /* Navegación institucional: grafito cálido, acento dorado y estados claros. */
    [data-testid="stSidebar"] .stButton button {
        background: linear-gradient(120deg,#363B39,#404540) !important;
        color: #F7F5F0 !important;
        border: 1px solid #51544D !important;
        border-left: 3px solid #9D865C !important;
        border-radius: 10px !important;
        min-height: 48px !important;
        font-weight: 650 !important;
        font-size: 14px !important;
        letter-spacing: .12px !important;
        box-shadow: 0 3px 9px rgba(39,38,32,.13) !important;
        transition: background .17s ease, transform .17s ease, box-shadow .17s ease !important;
    }
    [data-testid="stSidebar"] .stButton button:hover {
        background: linear-gradient(120deg,#4C5148,#55594F) !important;
        color: #FFFDF7 !important;
        border-color: #B99A65 !important;
        border-left-color: #D2B47A !important;
        box-shadow: 0 5px 13px rgba(39,38,32,.20) !important;
        transform: translateY(-1px);
    }
    [data-testid="stSidebar"] .stButton button:focus-visible {
        outline: 2px solid #B99A65 !important;
        outline-offset: 2px !important;
    }
    [data-testid="stSidebar"] .stButton button p,
    [data-testid="stSidebar"] .stButton button span {
        color: #F7F5F0 !important;
    }
    /* Se marca la sección activa, sin alterar los callbacks. */
    __ACTIVE_NAV_CSS__
    [data-testid="stSidebar"] [data-testid="stImage"] {
        display:flex !important;
        justify-content:center !important;
    }
    .cd-sidebar-title {
        color: #283e50;
        text-align: center;
        font-size: 17px;
        font-weight: 750;
        margin: 7px 0 3px;
    }
    .cd-sidebar-subtitle {
        color: #687782;
        text-align: center;
        font-size: 12px;
        margin: 0 0 21px;
    }
    </style>
    """.replace("__FONDO_SIDEBAR__", fondo_sidebar_css).replace(
        "__ACTIVE_NAV_CSS__",
        '[data-testid="stSidebar"] [class*="st-key-nav_compartida_' +
        {"oficios": "oficios", "pendientes": "pendientes", "urgentes": "urgentes", "firmados": "firmados", "minutas": "minutas", "reuniones": "reuniones"}.get(
            st.session_state.menu_activo, "inicio") +
        '"] button {background:linear-gradient(120deg,#4A4C44,#55574D) !important;'
        'border:1px solid #C7A465 !important;border-left:6px solid #E2B66B !important;'
        'box-shadow:inset 3px 0 0 #E2B66B, 0 5px 15px rgba(48,44,35,.22) !important;'
        'transform:translateX(2px) !important;}'
    ), unsafe_allow_html=True)
    with st.sidebar:
        ruta_logo = BASE_DIR / "assets" / ("logo_setf.png" if _area_activa_validada() == "setf" else "logo_diseno_geometrico.png")
        if ruta_logo.is_file():
            # Widget nativo en vez de inyectar <img> con una cadena base64.
            _, columna_logo, _ = st.columns(
                [0.10, 5.8, 0.10] if _area_activa_validada() == "setf"
                else [0.35, 5.3, 0.35]
            )
            with columna_logo:
                if _area_activa_validada() == "setf":
                    st.image(str(ruta_logo), width=186)
                else:
                    st.image(str(ruta_logo), use_container_width=True)
        st.markdown(
            _rotulos_area('<div class="cd-sidebar-title">CONTROL DOCUMENTAL</div>'
            '<div class="cd-sidebar-subtitle">Mesa de Diseño Geométrico</div>'),
            unsafe_allow_html=True,
        )
        if st.button("⌂ Menú principal", key="nav_compartida_inicio", use_container_width=True):
            navegar_a("home")
        # Acceso directo desde la barra lateral a Oficios en trámite, en DG y SETF.
        if st.button("▦ Oficios en trámite", key="nav_compartida_oficios", use_container_width=True):
            if st.session_state.menu_activo != "oficios":
                navegar_a("oficios")
        if st.button("▤ Mis pendientes", key="nav_compartida_pendientes", use_container_width=True):
            if st.session_state.menu_activo != "pendientes":
                navegar_a("pendientes")
        if st.button("⚠ Urgentes", key="nav_compartida_urgentes", use_container_width=True):
            if st.session_state.menu_activo != "urgentes":
                navegar_a("urgentes")
        if st.button("▣ Firmados", key="nav_compartida_firmados", use_container_width=True):
            if st.session_state.menu_activo != "firmados":
                navegar_a("firmados")
        if st.button("▤ Minutas", key="nav_compartida_minutas", use_container_width=True):
            if st.session_state.menu_activo != "minutas":
                navegar_a("minutas")
        if _area_activa_validada() == "setf":
            if st.button("▦ Gestión de reuniones", key="nav_compartida_reuniones", use_container_width=True):
                if st.session_state.menu_activo != "reuniones":
                    navegar_a("reuniones")
        st.divider()

# Fondo de trabajo: conserva el plano ferroviario visible detrás de las tarjetas.
# Los módulos mantienen sus propias imágenes y la navegación principal no cambia.


# =========================================================
# MÓDULOS LATERALES: URGENTES Y FIRMADOS
# =========================================================
def _hoja_urgencias():
    return _hoja_urgencias_por_area(_area_activa_validada())


@st.cache_resource(show_spinner=False)
def _hoja_urgencias_por_area(area):
    libro = conectar_google_sheets_por_area(area).spreadsheet
    columnas = ["Folio", "No. de oficio", "Urgente", "Actualizado por", "Fecha actualización"]
    try:
        hoja = libro.worksheet("URGENCIAS")
    except gspread.WorksheetNotFound:
        hoja = libro.add_worksheet(title="URGENCIAS", rows=1000, cols=5)
        hoja.append_row(columnas, value_input_option="RAW")
    if hoja.row_values(1) != columnas:
        raise ValueError("La hoja URGENCIAS tiene encabezados inesperados; no se modificaron datos.")
    return hoja


def _marcas_urgentes():
    return {(str(r.get("Folio", "")).strip(), str(r.get("No. de oficio", "")).strip()):
            str(r.get("Urgente", "")).strip().lower() == "si"
            for r in _hoja_urgencias().get_all_records()}


def _actualizar_urgencia(folio, numero, activar):
    if not es_administrador():
        raise PermissionError("Solo administradores pueden modificar la prioridad de un oficio.")
    hoja = _hoja_urgencias()
    filas = hoja.get_all_values()
    coincidencias = [i for i, fila in enumerate(filas[1:], 2)
                    if len(fila) >= 2 and fila[0].strip() == str(folio).strip()
                    and fila[1].strip() == str(numero).strip()]
    datos = [str(folio), str(numero), "SI" if activar else "NO",
             identificador_sesion(), datetime.now().astimezone().isoformat(timespec="seconds")]
    if len(coincidencias) > 1:
        raise ValueError("Hay marcas duplicadas para este oficio. Corrige URGENCIAS antes de continuar.")
    if coincidencias:
        hoja.update(f"A{coincidencias[0]}:E{coincidencias[0]}", [datos], value_input_option="RAW")
    else:
        hoja.append_row(datos, value_input_option="RAW")


def _dias_desde_recepcion(valor):
    """Días naturales desde recepción; ISO AAAA-MM-DD se interpreta sin invertir mes/día."""
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}(?:$|[T\s])", texto):
            fecha = pd.to_datetime(texto, yearfirst=True, errors="coerce")
        else:
            fecha = pd.to_datetime(texto, dayfirst=True, errors="coerce")
        if pd.isna(fecha):
            return None
        return max(0, (datetime.now().date() - fecha.date()).days)
    except (ValueError, TypeError, OverflowError):
        return None


def _documentos_seccion(oficio):
    try:
        modalidad = _modalidad_expediente(oficio.get("Folio", ""), oficio.get("No. de oficio", ""))
    except Exception:
        modalidad = None
    if modalidad and modalidad.get("Tipo de trámite") == "Directo":
        enlace = str(modalidad.get("URL principal", "")).strip()
        if enlace.startswith("https://"):
            st.link_button("Oficio principal · " + str(modalidad.get("Nombre principal", "")),
                           enlace, use_container_width=True)
    grupos = (("Oficio firmado", "Firmado"), ("Oficio original / antecedentes", "Antecedente"),
              ("Anexos", "Anexo"))
    for titulo, campo in grupos:
        docs = leer_documentos(oficio.get(campo, ""))
        with st.expander(f"{titulo} ({len(docs)})", expanded=(campo == "Firmado")):
            if not docs:
                st.caption("Sin documentos vinculados en el registro.")
            for doc in docs:
                st.link_button(doc["nombre"], doc["url"], use_container_width=True)


def _modulo_lateral_documentos(tipo):
    aplicar_fondo()
    mostrar_sesion()
    if tipo != "urgentes":
        # Fondo propio del módulo: se carga desde assets, sin afectar el sidebar.
        candidatos = [ASSETS_DIR / f"fondo_firmados{ext}"
                      for ext in (".png", ".jpg", ".jpeg", ".webp",
                                  ".PNG", ".JPG", ".JPEG", ".WEBP")]
        fondo = next((p for p in candidatos if p.is_file()), None)
        if fondo:
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp"}[fondo.suffix.lower()]
            imagen = base64.b64encode(fondo.read_bytes()).decode("ascii")
            fondo_css = (
                'background-image:linear-gradient(rgba(240,244,241,.17),rgba(240,244,241,.25)),'
                f'url("data:{mime};base64,{imagen}") !important;'
                'background-size:cover !important;'
                'background-position:center center !important;'
                'background-repeat:no-repeat !important;'
                'background-attachment:fixed !important;'
            )
        else:
            fondo_css = 'background:#edf0ed !important;'
            st.caption("Fondo opcional: agrega assets/fondo_firmados.png a tu proyecto y reinicia Streamlit.")

        st.markdown(_rotulos_area("""
        <style>
        [data-testid="stAppViewContainer"]:has(.firm-hero) { __FONDO__ }
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stMain"],
        [data-testid="stAppViewContainer"]:has(.firm-hero) > .main {
            background:transparent !important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stHeader"] {
            background:transparent !important;
        }
        .firm-hero {
            background:linear-gradient(115deg,#34413f,#43534e);
            border:1px solid #a88e62;border-left:5px solid #b99b67;
            border-radius:15px;padding:27px 30px;margin:8px 0 26px;
            box-shadow:0 10px 25px rgba(33,48,42,.16);
        }
        .firm-hero .eyebrow {
            font-size:11px;letter-spacing:2px;font-weight:750;color:#e5d2a9;
        }
        [data-testid="stAppViewContainer"] .firm-hero h1 {
            color:#fffaf0 !important;font-size:37px;line-height:1.2;
            margin:9px 0 5px;font-weight:800;
        }
        [data-testid="stAppViewContainer"] .firm-hero p {
            color:#e4eae4 !important;font-size:14px;margin:0;
        }
        .firm-label {
            color:#415b50 !important;font-weight:800;letter-spacing:1.9px;
            font-size:12px;margin:20px 0 11px;
        }
        .firm-kpi {
            background:rgba(220,227,223,.97);
            border:1px solid #b7c7bd;border-left:4px solid #7e9a87;
            border-radius:12px;padding:18px 20px;min-height:124px;
            box-shadow:0 6px 17px rgba(39,58,49,.10);
        }
        .firm-kpi .number {color:#243d35 !important;font-size:34px;font-weight:800;line-height:1.15;}
        .firm-kpi .title {color:#243d35 !important;font-size:14px;font-weight:750;margin-top:4px;}
        .firm-kpi .desc {color:#586e61 !important;font-size:12px;margin-top:4px;}
        /* Indicadores navegables: toda la superficie es un botón de Streamlit. */
        [class*="st-key-firm_kpi_"] button {
            display:flex!important;align-items:center!important;
            justify-content:flex-start!important;text-align:left!important;
            width:100%!important;min-height:124px!important;
            position:relative!important;overflow:hidden!important;
            padding:14px 17px 29px 20px!important;
            background:#DFE8E0!important;
            border:1.5px solid #B0C2B3!important;
            border-left:5px solid #819F89!important;
            border-radius:12px!important;
            box-shadow:0 6px 18px rgba(39,58,49,.10)!important;
            color:#274335!important;
            transition:transform .18s,box-shadow .18s,border-color .18s!important;
        }
        [class*="st-key-firm_kpi_"] button:hover {
            transform:translateY(-2px)!important;
            box-shadow:0 9px 20px rgba(39,58,49,.15)!important;
            border-color:#6A9176!important;
        }
        [class*="st-key-firm_kpi_"] button:focus-visible {
            outline:2px solid #BC9D69!important;outline-offset:2px!important;
        }
        [class*="st-key-firm_kpi_"] button [data-testid="stMarkdownContainer"],
        [class*="st-key-firm_kpi_"] button p {
            width:100%!important;margin:0!important;text-align:left!important;
            color:#284437!important;font-weight:750!important;
            font-size:13px!important;line-height:1.5!important;
        }
        [class*="st-key-firm_kpi_"] button strong {
            font-size:33px!important;font-weight:850!important;
            line-height:1.12!important;display:block!important;
            color:#243D35!important;
        }
        [class*="st-key-firm_kpi_"] button::after {
            position:absolute;left:21px;bottom:10px;
            font-size:11px;font-weight:500;color:#596F60;
            letter-spacing:0;pointer-events:none;
        }
        [class*="st-key-firm_kpi_hoy"] button::after {content:"Envíos registrados hoy";}
        [class*="st-key-firm_kpi_semana"] button::after {content:"Salidas del lunes a hoy";}
        [class*="st-key-firm_kpi_firmados"] button::after {content:"Regresaron con firma";}
        [class*="st-key-firm_kpi_hoy"] button {border-left-color:#BE9560!important;}
        [class*="st-key-firm_kpi_semana"] button {border-left-color:#63837A!important;}
        [class*="st-key-firm_kpi_firmados"] button {border-left-color:#628C6D!important;}
        /* Estado seleccionado: diferencia inequívoca entre resumen y filtro activo. */
        [class*="st-key-firm_kpi_"] button[kind="primary"],
        [class*="st-key-firm_kpi_"] button[data-testid="stBaseButton-primary"] {
            background:#D0E1D2!important;
            border-color:#6D9678!important;
            box-shadow:inset 0 0 0 1px #6D9678,0 9px 20px rgba(39,58,49,.13)!important;
        }
        [class*="st-key-firm_kpi_"] button[kind="primary"]::before,
        [class*="st-key-firm_kpi_"] button[data-testid="stBaseButton-primary"]::before {
            content:"● Filtro activo";position:absolute;top:12px;right:13px;
            font-size:10px;font-weight:800;color:#466D51;
        }
        .firm-vista-activa {
            display:flex;justify-content:space-between;align-items:center;
            background:#EBF1EB;border:1px solid #BACDBD;border-left:4px solid #638A6A;
            border-radius:10px;padding:10px 13px;margin:10px 0 4px;
            color:#315240;font-size:12px;font-weight:650;
        }
        [class*="st-key-firm_kpi_ver_todos"] button {
            background:#F7F9F5!important;border:1px solid #90AA96!important;
            color:#3D6149!important;border-radius:9px!important;font-weight:700!important;
        }

        .firm-caption {color:#42584e;font-size:13px;margin:8px 0 16px;}
        .firm-original-ref {
            background:rgba(245,247,244,.95);border:1px solid #b7c7bd;
            border-radius:9px;padding:13px 12px;color:#243d35;
            font-size:13px;font-weight:600;overflow-wrap:anywhere;
        }
        .firm-empty {
            background:rgba(239,244,240,.96);border:1px dashed #b7c7bd;
            padding:35px 20px;text-align:center;border-radius:13px;color:#344a3e;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stExpander"] {
            background:rgba(237,243,238,.96) !important;
            border:1px solid #b9c9bf !important;border-radius:12px !important;
            box-shadow:0 4px 12px rgba(39,58,49,.07) !important;
            overflow:hidden;margin-bottom:7px;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stExpander"] summary {
            color:#253f35 !important;font-weight:650 !important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stTextInput"] input,
        [data-testid="stAppViewContainer"]:has(.firm-hero) [data-testid="stSelectbox"] [data-baseweb="select"] > div {
            background:#f5f7f4 !important;border:1px solid #b9c8bf !important;
            color:#243d35 !important;border-radius:9px !important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) .stLinkButton a {
            background:#f7f9f6 !important;color:#284339 !important;
            border:1px solid #b7c7bd !important;border-radius:9px !important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) .stLinkButton a:hover {
            border-color:#779481 !important;background:#e4eee6 !important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero)
        [class*="st-key-firm_limpiar"] button {
            background:#34413f !important;color:#fff !important;
            border:1px solid #a88e62 !important;border-radius:9px !important;
        }
        /* ARCHIVO FIRMADO: registro horizontal sin desplegable general. */
        div[class*="st-key-firm_reg_row_"] {
            background:linear-gradient(100deg,#FCFEFB 0%,#EDF3EC 100%)!important;
            border:1px solid #ADC3B1!important;
            border-left:4px solid #678C72!important;
            border-radius:12px!important;
            padding:12px 16px!important;
            margin:9px 0 11px!important;
            box-shadow:0 3px 11px rgba(45,67,52,.09)!important;
        }
        div[class*="st-key-firm_reg_row_"] [data-testid="stHorizontalBlock"] {
            align-items:center!important;
            column-gap:10px!important;
        }
        .firm-inline-file {
            min-width:0;padding:0 4px 0 2px;
        }
        .firm-inline-name {
            font-size:13px;font-weight:800;line-height:1.3;
            letter-spacing:.05px;color:#263F34;
            overflow-wrap:anywhere;margin-bottom:4px;
        }
        .firm-inline-meta {
            font-size:10.5px;color:#677E6E;line-height:1.35;
            overflow-wrap:anywhere;
        }
        div[class*="st-key-firm_reg_row_"] .stLinkButton a,
        div[class*="st-key-firm_reg_row_"] [data-testid="stPopover"] button,
        div[class*="st-key-firm_reg_row_"] .stButton button {
            display:flex!important;align-items:center!important;justify-content:center!important;
            min-height:40px!important;width:100%!important;
            padding:6px 10px!important;
            border:1px solid #9EB7A5!important;
            border-radius:9px!important;
            background:#F8FBF7!important;
            color:#375B43!important;
            font-size:12px!important;font-weight:750!important;
            white-space:nowrap!important;
            box-shadow:0 2px 4px rgba(42,69,48,.04)!important;
            transition:background .17s,border-color .17s,box-shadow .17s!important;
        }
        div[class*="st-key-firm_reg_row_"] .stLinkButton a:hover,
        div[class*="st-key-firm_reg_row_"] [data-testid="stPopover"] button:hover,
        div[class*="st-key-firm_reg_row_"] .stButton button:not(:disabled):hover {
            background:#E3EDE2!important;
            border-color:#648D6C!important;
            box-shadow:0 3px 9px rgba(42,69,48,.10)!important;
        }
        /* Acción principal: Firmado en verde sólido. */
        div[class*="st-key-firm_reg_row_"] [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:nth-child(2) .stLinkButton a,
        div[class*="st-key-firm_reg_row_"] [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:nth-child(2) [data-testid="stPopover"] button {
            background:#345846!important;color:#FFFFFF!important;
            border:1px solid #345846!important;
        }
        div[class*="st-key-firm_reg_row_"] [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:nth-child(2) .stLinkButton a:hover,
        div[class*="st-key-firm_reg_row_"] [data-testid="stHorizontalBlock"]
        > [data-testid="stColumn"]:nth-child(2) [data-testid="stPopover"] button:hover {
            background:#497158!important;color:#FFFFFF!important;
        }
        div[class*="st-key-firm_reg_row_"] .stButton button:disabled {
            background:#EEF0EB!important;color:#9AAA9D!important;
            border-color:#D4DED3!important;opacity:.8!important;
        }
        .firm-admin-edit {margin:18px 0 7px;}
        .firm-caption {margin:7px 0 12px!important;}

        /* ARCHIVO FIRMADO / BARRA DE CONSULTA INSTITUCIONAL */
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] {
            position:relative!important;
            isolation:isolate!important;
            background:linear-gradient(115deg,rgba(252,254,251,.98),rgba(237,245,238,.98))!important;
            border:1px solid #A9BEB0!important;
            border-left:4px solid #B79965!important;
            border-radius:15px!important;
            padding:18px 22px 20px!important;
            margin:14px 0 23px!important;
            box-shadow:0 7px 20px rgba(33,59,45,.09)!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] .firm-search-head {
            display:flex;justify-content:space-between;align-items:flex-end;
            gap:16px;margin-bottom:16px;padding-bottom:12px;
            border-bottom:1px solid rgba(131,157,139,.29);
        }
        .firm-search-head-left {display:flex;flex-direction:column;gap:4px;min-width:0;}
        .firm-search-eyebrow {
            font-size:9px!important;letter-spacing:1.7px;font-weight:800;
            color:#A07D52!important;
        }
        .firm-search-title {
            color:#294B3B!important;font-size:18px!important;
            line-height:1.15!important;font-weight:800!important;
        }
        .firm-search-hint {
            font-size:10px!important;letter-spacing:.25px;
            color:#658071!important;text-align:right;white-space:nowrap;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stHorizontalBlock"] {
            align-items:flex-end!important;gap:16px!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] label,
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] label p {
            color:#476457!important;font-size:11px!important;
            font-weight:700!important;letter-spacing:.1px;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stTextInput"] input {
            background-color:#FFFEFC!important;
            background-image:url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyMCIgaGVpZ2h0PSIyMCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiM2NTg1NzMiIHN0cm9rZS13aWR0aD0iMS45IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxjaXJjbGUgY3g9IjEwLjgiIGN5PSIxMC44IiByPSI2LjgiLz48cGF0aCBkPSJtMTYgMTYgNC41IDQuNSIvPjwvc3ZnPg==")!important;
            background-repeat:no-repeat!important;
            background-position:15px center!important;
            background-size:19px 19px!important;
            padding-left:44px!important;
            border:1.5px solid #B4C7B8!important;
            border-radius:10px!important;
            min-height:47px!important;
            color:#243D32!important;
            box-shadow:0 2px 6px rgba(39,63,48,.035)!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stTextInput"] input::placeholder {
            color:#8B9F92!important;opacity:1!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stTextInput"] input:focus {
            border-color:#5B8368!important;
            box-shadow:0 0 0 3px rgba(95,136,108,.15)!important;
            outline:none!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stSelectbox"] [data-baseweb="select"] > div {
            min-height:47px!important;
            background:#FFFEFC!important;
            border:1.5px solid #B4C7B8!important;
            border-radius:10px!important;
            box-shadow:0 2px 6px rgba(39,63,48,.035)!important;
            color:#294B3B!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within {
            border-color:#5B8368!important;
            box-shadow:0 0 0 3px rgba(95,136,108,.15)!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [class*="st-key-firm_limpiar"] button {
            min-height:47px!important;
            border:1px solid #365C48!important;
            background:#365C48!important;
            color:#FFFFFF!important;
            border-radius:10px!important;
            font-size:12px!important;font-weight:750!important;
            box-shadow:0 3px 10px rgba(37,67,49,.14)!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_search_panel"] [class*="st-key-firm_limpiar"] button:hover {
            background:#46745A!important;border-color:#46745A!important;
            box-shadow:0 5px 13px rgba(37,67,49,.18)!important;
        }
        @media(max-width:900px) {
            div[class*="st-key-firm_search_panel"] {padding:15px!important;}
            div[class*="st-key-firm_search_panel"] .firm-search-head {align-items:flex-start;flex-wrap:wrap;}
            .firm-search-hint {text-align:left;white-space:normal;}
            div[class*="st-key-firm_search_panel"] [data-testid="stHorizontalBlock"] {gap:9px!important;}
        }
        @media(max-width:900px){
            div[class*="st-key-firm_reg_row_"] {padding:11px!important;}
            div[class*="st-key-firm_reg_row_"] .stLinkButton a,
            div[class*="st-key-firm_reg_row_"] [data-testid="stPopover"] button,
            div[class*="st-key-firm_reg_row_"] .stButton button {
                font-size:11px!important;padding:6px!important;
            }
        }
        /* ===== FIRMADOS / nueva jerarquía visual: tinta ciruela + cobre ===== */
        [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero {
            background:linear-gradient(114deg,#42383B 0%,#574A45 68%,#6D584B 100%)!important;
            border:1px solid #A98668!important;border-left:5px solid #C68D60!important;
            border-radius:19px!important;padding:24px 30px!important;
            margin:5px 0 17px!important;box-shadow:0 10px 27px rgba(61,43,42,.17)!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero h1 {
            font-size:32px!important;letter-spacing:-.7px!important;color:#FFF9F2!important;
        }
        [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero p {color:#F0E7DD!important;font-size:13px!important;}
        .firm-hero .eyebrow {color:#E4B989!important;}
        .firm-hero .firm-ornament {opacity:.33!important;width:27%!important;}
        .firm-hero .firm-signature {color:#F1CC9F!important;}
        .firm-label {color:#644A45!important;margin:10px 0 8px!important;font-size:10px!important;}
        [class*="st-key-firm_kpi_"] button {
            background:rgba(255,252,247,.98)!important;color:#493B3B!important;
            border:1px solid #DECFC1!important;border-top:3px solid #B98763!important;
            border-left:1px solid #DECFC1!important;border-radius:15px!important;
            min-height:107px!important;padding:12px 16px 22px!important;
            box-shadow:0 5px 16px rgba(66,50,42,.09)!important;
        }
        [class*="st-key-firm_kpi_"] button strong {color:#48383B!important;font-size:30px!important;}
        [class*="st-key-firm_kpi_"] button p {color:#514243!important;font-size:12px!important;}
        [class*="st-key-firm_kpi_"] button::after {left:17px!important;bottom:7px!important;color:#9A8072!important;font-size:10px!important;}
        [class*="st-key-firm_kpi_"] button[kind="primary"],
        [class*="st-key-firm_kpi_"] button[data-testid="stBaseButton-primary"] {
            background:#F6E9DD!important;border-color:#C49170!important;
            box-shadow:inset 0 0 0 1px #C49170,0 7px 19px rgba(85,56,45,.12)!important;
        }
        [class*="st-key-firm_kpi_"] button[kind="primary"]::before,
        [class*="st-key-firm_kpi_"] button[data-testid="stBaseButton-primary"]::before {
            content:"● Seleccionado"!important;color:#875A43!important;font-size:9px!important;top:7px!important;
        }
        .firm-summary-foot {margin:8px 0 12px!important;color:#76655C!important;font-size:11px!important;}
        .firm-vista-activa {
            background:#F5EEE7!important;border:1px solid #DFD1C3!important;
            border-left:3px solid #B88768!important;color:#634A42!important;
            margin:10px 0 8px!important;padding:9px 13px!important;
        }
        [class*="st-key-firm_kpi_ver_todos"] button {
            min-height:35px!important;width:auto!important;padding:6px 14px!important;
            border-radius:10px!important;color:#644A43!important;
            background:#FFF9F2!important;border:1px solid #D6BCAB!important;
            font-size:11px!important;box-shadow:none!important;
        }
        .firm-envios-head {margin:14px 0 9px!important;color:#6E5045!important;font-size:11px!important;}
        .firm-envio-tarjeta {
            background:#FFFCF8!important;border:1px solid #E2D1C2!important;
            border-left:4px solid #B98158!important;border-radius:13px!important;
            padding:18px 23px!important;box-shadow:0 5px 16px rgba(78,54,44,.08)!important;
        }
        .firm-envio-nombre {color:#47393B!important;}
        @media(max-width:750px) {
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero {padding:19px!important;}
            .firm-hero .firm-ornament {display:none!important;}
        }
        /* Ornamentación técnica propia del archivo de firmados */
        .firm-hero {position:relative;overflow:hidden;isolation:isolate;}
        .firm-hero > .eyebrow,.firm-hero > h1,.firm-hero > p {position:relative;z-index:2;max-width:67%;}
        .firm-hero .firm-ornament {position:absolute;right:22px;top:15px;width:33%;height:calc(100% - 30px);opacity:.84;pointer-events:none;z-index:1;}
        .firm-hero .firm-signature {position:absolute;right:31px;bottom:15px;font-size:9px;letter-spacing:2.1px;color:#d7c597;font-weight:700;z-index:2;}
        @media(max-width:800px){.firm-hero .firm-ornament{opacity:.25;width:45%;right:0}.firm-hero > .eyebrow,.firm-hero > h1,.firm-hero > p{max-width:92%}.firm-hero .firm-signature{display:none}}
        </style>
        <div class="firm-hero">
          <div class="eyebrow">MESA DE DISEÑO GEOMÉTRICO · ARCHIVO DOCUMENTAL</div>
          <h1>Firmados</h1>
          <p>Consulta de expedientes finalizados · oficios, antecedentes y anexos</p>
          <svg class="firm-ornament" viewBox="0 0 360 155" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
            <g fill="none" stroke="#b5cbb9" stroke-width=".7" opacity=".33">
              <path d="M25 28H345M25 52H345M25 76H345M25 100H345M25 124H345"/>
              <path d="M55 15V137M100 15V137M145 15V137M190 15V137M235 15V137M280 15V137M325 15V137"/>
            </g>
            <path d="M42 113H321" stroke="#cdb583" stroke-width="1.3" fill="none" opacity=".7"/>
            <path d="M42 113V26" stroke="#cdb583" stroke-width="1.3" fill="none" opacity=".7"/>
            <path d="M62 98L123 73L182 86L260 39L308 46" stroke="#d9c18f" stroke-width="2.6" fill="none" stroke-linejoin="round"/>
            <g fill="#344842" stroke="#d9c18f" stroke-width="1.5"><circle cx="123" cy="73" r="4"/><circle cx="182" cy="86" r="4"/></g>
            <circle cx="260" cy="39" r="13" fill="#668a75" stroke="#d9c18f" stroke-width="1.5"/>
            <path d="M254 39L259 44L267 34" stroke="#f5f8f4" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="firm-signature">REGISTRO · VALIDACIÓN · ARCHIVO</span>
        </div>
        """.replace("__FONDO__", fondo_css)), unsafe_allow_html=True)

        # Presentación SETF: un archivo compacto sobre superficie blanca. CSS aislado
        # para que Diseño Geométrico no cambie su distribución.
        if _area_activa_validada() == "setf":
            st.markdown("""
            <style>
            /* Encabezado secundario más proporcionado; sin márgenes negativos. */
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero {
                min-height:0!important;
                padding:17px 25px!important;
                margin:14px 0 13px!important;
                border-radius:16px!important;
                background:linear-gradient(112deg,#F8F4EC 0%,#F0F1EA 100%)!important;
                border:1px solid #DFD2C0!important;
                border-left:4px solid #AD8756!important;
                box-shadow:0 6px 18px rgba(40,57,44,.07)!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero h1 {
                font-size:28px!important;line-height:1.16!important;
                margin:5px 0 5px!important;color:#214336!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero .eyebrow {
                font-size:10px!important;letter-spacing:1.6px!important;
                color:#946E44!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero p {
                font-size:12px!important;line-height:1.5!important;
                color:#69766F!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero .firm-signature {
                color:#9D825E!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-hero .firm-ornament {
                opacity:.40!important;
            }
            /* El archivo se encapsula en una superficie blanca continua. */
            [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_panel"] {
                background:#FFFFFF!important;
                border:1px solid #DDE5DC!important;
                border-radius:18px!important;
                box-shadow:0 8px 27px rgba(34,60,45,.085)!important;
                padding:21px 22px 17px!important;
                margin:12px 0 16px!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) .firm-archive-head {
                display:flex;align-items:center;justify-content:space-between;
                gap:14px;padding-bottom:13px;margin-bottom:7px;
                border-bottom:1px solid #E5ECE4;
            }
            .firm-archive-title {
                color:#214436!important;font-size:17px!important;
                font-weight:800!important;letter-spacing:-.15px;
            }
            .firm-archive-subtitle {
                color:#738176!important;font-size:12px!important;margin-top:3px;
            }
            .firm-archive-count {
                background:#E6F0E8;border:1px solid #C4D7C8;
                color:#35614C;border-radius:100px;padding:5px 10px;
                font-size:12px;font-weight:750;white-space:nowrap;
            }
            /* Cada oficio es una fila corta, sin el fondo técnico entre registros. */
            [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_row_"] {
                background:#FBFCFA!important;
                border:1px solid #E0E9DF!important;
                border-left:3px solid #8FAE98!important;
                border-radius:11px!important;
                padding:12px 13px!important;
                margin:6px 0!important;
                box-shadow:none!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_row_"]:nth-of-type(even) {
                background:#FFFFFF!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_row_"] [data-testid="stHorizontalBlock"] {
                align-items:center!important;column-gap:9px!important;
            }
            .firm-archive-name {
                color:#234137!important;font-size:14px!important;font-weight:750;
                line-height:1.35;overflow-wrap:anywhere;
            }
            .firm-archive-meta {
                color:#728277!important;font-size:11.5px!important;
                line-height:1.45;margin-top:4px;overflow-wrap:anywhere;
            }
            .firm-signed-state {
                background:#E4F0E7;border:1px solid #B8D2C1;
                border-radius:100px;color:#326547;font-size:11px;
                font-weight:750;text-align:center;padding:6px 9px;
                white-space:nowrap;display:inline-block;
            }
            .firm-pending-state {
                background:#F3EDE4;border:1px solid #D6C7B7;
                border-radius:100px;color:#80654D;font-size:11px;
                font-weight:750;text-align:center;padding:6px 9px;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] .stLinkButton a,
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] [data-testid="stPopover"] button,
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] .stButton button {
                min-height:38px!important;width:100%!important;
                border:1px solid #CBDACD!important;
                border-radius:9px!important;background:#FFFEFC!important;
                color:#42604D!important;font-size:12px!important;
                font-weight:650!important;padding:6px 9px!important;
                box-shadow:none!important;white-space:normal!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] [data-testid="stHorizontalBlock"]
                > [data-testid="stColumn"]:nth-child(3) .stLinkButton a,
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] [data-testid="stHorizontalBlock"]
                > [data-testid="stColumn"]:nth-child(3) [data-testid="stPopover"] button {
                background:#2E5947!important;color:white!important;
                border-color:#2E5947!important;font-weight:750!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] .stLinkButton a:hover,
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] [data-testid="stPopover"] button:hover {
                border-color:#6C997A!important;
            }
            [data-testid="stAppViewContainer"]:has(.firm-hero)
                div[class*="st-key-firm_archive_row_"] .stButton button:disabled {
                background:#F1F3EF!important;color:#91A092!important;
                border-color:#E1E5DC!important;opacity:1!important;
            }
            @media(max-width:880px){
                [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_panel"]{
                    padding:14px 12px!important;
                }
                [data-testid="stAppViewContainer"]:has(.firm-hero) div[class*="st-key-firm_archive_row_"]{
                    padding:13px 11px!important;
                }
                .firm-archive-head{align-items:flex-start!important;}
                .firm-archive-name{font-size:13px!important;}
            }
            </style>
            """, unsafe_allow_html=True)

        try:
            registros = _leer_registros_oficios()
        except Exception as error:
            st.error(f"No se pudo consultar el registro: {_mensaje_error_sheets(error)}")
            return
        lista = [r for r in registros
                 if normalizar_texto(r.get("Estatus", "")) == "firmado"]

        def _fecha_firma(registro):
            """Fecha registrada al firmar: admite AAAA-MM-DD y DD/MM/AAAA."""
            valor = str(registro.get("Fecha de entrega", "") or "").strip()
            if not valor:
                return None
            for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d",
                            "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    return datetime.strptime(valor, formato).date()
                except ValueError:
                    continue
            fecha = pd.to_datetime(valor, errors="coerce", dayfirst=True)
            return None if pd.isna(fecha) else fecha.date()

        # No confundir envío a firma con devolución del oficio ya firmado.
        # 1) Salidas a firma: EVENTOS_BORRADORES -> ETAPA_FIRMA.
        # 2) Regresos firmados: OFICIOS -> Estatus Firmado + Fecha de entrega.
        from zoneinfo import ZoneInfo
        zona_local = ZoneInfo("America/Mexico_City")
        hoy = datetime.now(zona_local).date()
        lunes = hoy - __import__("datetime").timedelta(days=hoy.weekday())

        def _fecha_evento_firma(valor):
            texto = str(valor or "").strip()
            if not texto:
                return None
            try:
                fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
                if fecha.tzinfo is not None:
                    return fecha.astimezone(zona_local).date()
                # Eventos sin zona: no se puede asumir que pertenecen a México.
                # El código propio siempre escribe fechas con offset.
                return None
            except (ValueError, TypeError, OverflowError):
                return None

        try:
            registros_envios_nuevos = _leer_envios_firma()
        except Exception as error:
            registros_envios_nuevos = []
            st.warning("No se pudo leer ENVIOS_FIRMA. Revisa la conexión: " + _mensaje_error_sheets(error))
        # Los registros eliminados de OFICIOS NO deben mantenerse en los
        # indicadores de actividad vigente, aunque la bitácora histórica persista.
        identidades_vigentes = {
            (str(r.get("Folio", "")).strip(), str(r.get("No. de oficio", "")).strip())
            for r in registros
        }
        mapa_envios = {}
        for registro_envio in registros_envios_nuevos:
            identidad_envio = (str(registro_envio.get("Folio", "")).strip(),
                              str(registro_envio.get("No. de oficio origen", "")).strip())
            if identidad_envio[0] or identidad_envio[1]:
                mapa_envios[identidad_envio] = registro_envio

        firmas_enviadas_hoy = None
        firmas_enviadas_semana = None
        claves_hoy, claves_semana = set(), set()
        try:
            bitacora_firmas = _leer_registros_eventos()
            # Un oficio se cuenta una sola vez por periodo aunque haya
            # reenvíos/actualizaciones registrados en la bitácora.
            for evento in bitacora_firmas:
                if str(evento.get("Accion", "")).strip() != ETAPA_FIRMA:
                    continue
                fecha_evento = _fecha_evento_firma(evento.get("Fecha"))
                folio_evento = str(evento.get("Folio", "")).strip()
                numero_evento = str(evento.get("No. de oficio", "")).strip()
                if not fecha_evento or not (folio_evento or numero_evento):
                    continue
                identidad = (folio_evento, numero_evento)
                if identidad not in identidades_vigentes:
                    continue
                if fecha_evento == hoy:
                    claves_hoy.add(identidad)
                if lunes <= fecha_evento <= hoy:
                    claves_semana.add(identidad)
            # ENVIOS_FIRMA guarda fecha y hora automáticamente en zona México.
            # Se incluyen los envíos cuyo evento de etapa todavía no sea legible.
            for identidad_envio, registro_envio in mapa_envios.items():
                if identidad_envio not in identidades_vigentes:
                    continue
                fecha_envio = _fecha_evento_firma(registro_envio.get("Fecha envío"))
                if fecha_envio == hoy:
                    claves_hoy.add(identidad_envio)
                if fecha_envio is not None and lunes <= fecha_envio <= hoy:
                    claves_semana.add(identidad_envio)
            firmas_enviadas_hoy = len(claves_hoy)
            firmas_enviadas_semana = len(claves_semana)
        except Exception as error:
            st.warning("No se pudo consultar la bitácora de envíos a firma; "
                       "las primeras dos cifras se mostrarán sin dato. "
                       f"Detalle: {_mensaje_error_sheets(error)}")

        # Los devueltos con firma se cuentan por la fecha de entrega/recepción
        # registrada, NO por el evento de autorización para enviar a firma.
        firmados_semana_claves = {
            (str(r.get("Folio", "")).strip(), str(r.get("No. de oficio", "")).strip())
            for r in lista
            if (fecha := _fecha_firma(r)) is not None and lunes <= fecha <= hoy
        }
        st.markdown('<div class="firm-label">RESUMEN DE FIRMAS · SELECCIONA UNA TARJETA PARA VER LOS OFICIOS</div>',
                    unsafe_allow_html=True)

        # Cada indicador conduce al listado correspondiente; no se confunden
        # los ENVÍOS a firma con los documentos que REGRESARON firmados.
        nombres_vista_firmados = {
            "todos": "Archivo completo de oficios firmados",
            "hoy": "Oficios enviados a firma hoy",
            "semana": "Oficios enviados a firma esta semana",
            "firmados": "Oficios recibidos firmados esta semana",
        }
        if st.session_state.get("firmados_kpi_filtro") not in nombres_vista_firmados:
            st.session_state["firmados_kpi_filtro"] = "todos"

        def _cambiar_vista_firmados(nueva_vista):
            st.session_state["firmados_kpi_filtro"] = nueva_vista
            # Reutilizar los filtros de búsqueda existentes, sin esconder datos
            # por una consulta escrita en una vista anterior.
            st.session_state["buscar_firmados"] = ""

        indicadores = (
            ("hoy", firmas_enviadas_hoy, "Enviados a firma hoy"),
            ("semana", firmas_enviadas_semana, "Enviados a firma en la semana"),
            ("firmados", len(firmados_semana_claves), "Oficios firmados en la semana"),
        )
        for columna, (filtro, numero, titulo) in zip(st.columns(3, gap="large"), indicadores):
            with columna:
                numero_texto = "—" if numero is None else f"{numero:02d}"
                st.button(
                    f"**{numero_texto}**  \n{titulo}",
                    key=f"firm_kpi_{filtro}",
                    use_container_width=True,
                    type="primary" if st.session_state["firmados_kpi_filtro"] == filtro else "secondary",
                    on_click=_cambiar_vista_firmados,
                    args=(filtro,),
                    help=f"Ver el listado de: {titulo.lower()}",
                )
        st.markdown(
            f'<div class="firm-summary-foot">Semana del {lunes.strftime("%d/%m/%Y")} '
            f'al {hoy.strftime("%d/%m/%Y")} · '
            'Se muestran únicamente oficios existentes · Firmados: fecha de recepción registrada.</div>',
            unsafe_allow_html=True)

        vista_firmados = st.session_state["firmados_kpi_filtro"]
        # El listado predeterminado es el archivo histórico ya firmado.
        # Enviados hoy/semana incluye documentos que todavía están en firma.
        if vista_firmados == "hoy":
            lista = [r for r in registros if (
                str(r.get("Folio", "")).strip(),
                str(r.get("No. de oficio", "")).strip()
            ) in claves_hoy]
        elif vista_firmados == "semana":
            lista = [r for r in registros if (
                str(r.get("Folio", "")).strip(),
                str(r.get("No. de oficio", "")).strip()
            ) in claves_semana]
        elif vista_firmados == "firmados":
            lista = [r for r in lista if (
                str(r.get("Folio", "")).strip(),
                str(r.get("No. de oficio", "")).strip()
            ) in firmados_semana_claves]

        st.markdown(
            '<div class="firm-vista-activa">'
            f'Vista: {html.escape(nombres_vista_firmados[vista_firmados])}'
            '</div>', unsafe_allow_html=True)
        if vista_firmados != "todos":
            st.button("← Ver todos los oficios firmados", key="firm_kpi_ver_todos",
                      on_click=_cambiar_vista_firmados, args=("todos",))

        def _limpiar_filtros_firmados():
            # Los callbacks se ejecutan antes de instanciar los widgets del rerun.
            st.session_state["buscar_firmados"] = ""
            st.session_state["firm_orden"] = "Más recientes"

        # VISTA DE ENVÍOS A FIRMA: tarjetas independientes del archivo OFICIOS.
        # La bitácora y ENVIOS_FIRMA son las fuentes de estos dos indicadores.
        # No exigimos que exista una ficha de origen ni mostramos anexos,
        # antecedentes, asunto, responsable o fechas adicionales.
        if vista_firmados in ("hoy", "semana"):
            claves_vista = claves_hoy if vista_firmados == "hoy" else claves_semana
            tarjetas_envio = []
            for identidad in claves_vista:
                registro_envio = mapa_envios.get(identidad, {})
                nombre_oficio = str(registro_envio.get("No. de oficio nuevo", "") or "").strip()
                # Las entradas históricas de EVENTOS_BORRADORES pueden no contar
                # con nombre nuevo en ENVIOS_FIRMA. Mostrar entonces el número
                # de oficio que consta en la propia bitácora, sin inventar datos.
                if not nombre_oficio:
                    nombre_oficio = identidad[1] or (f"Folio {identidad[0]}" if identidad[0] else "Sin nombre registrado")
                tarjetas_envio.append(nombre_oficio)
            tarjetas_envio.sort(key=normalizar_texto)
            st.markdown("""
                <style>
                .firm-envios-head {margin:18px 0 12px;color:#314F3D;
                    font-size:12px;font-weight:800;letter-spacing:1.3px;}
                .firm-envio-tarjeta {background:rgba(255,255,253,.97);
                    border:1px solid #CAD7C9;border-left:5px solid #B9985F;
                    border-radius:13px;padding:22px 26px;margin:0 0 12px;
                    box-shadow:0 7px 20px rgba(34,58,43,.09);}
                .firm-envio-nombre {color:#183A2D;font-size:17px;
                    line-height:1.4;font-weight:750;overflow-wrap:anywhere;}
                @media(max-width:700px){.firm-envio-tarjeta{padding:17px 18px}}
                </style>
                """, unsafe_allow_html=True)
            st.markdown('<div class="firm-envios-head">DOCUMENTOS DEL PERIODO</div>',
                        unsafe_allow_html=True)
            if not tarjetas_envio:
                st.info("No se registraron envíos a firma en este periodo.")
            else:
                for nombre_oficio in tarjetas_envio:
                    st.markdown(
                        '<div class="firm-envio-tarjeta"><div class="firm-envio-nombre">'
                        + html.escape(nombre_oficio) + '</div></div>',
                        unsafe_allow_html=True,
                    )
            return

        # Barra institucional de consulta: un solo contenedor visual para
        # búsqueda, ordenamiento y limpieza. Conserva las claves y callbacks.
        with st.container(key="firm_search_panel"):
            st.markdown("""
              <div class="firm-search-head">
                <div class="firm-search-head-left">
                  <span class="firm-search-eyebrow">ARCHIVO DOCUMENTAL / CONSULTA</span>
                  <div class="firm-search-title">Localizar expediente</div>
                </div>
                <span class="firm-search-hint">Folio · Oficio · Asunto · Responsable</span>
              </div>
              """, unsafe_allow_html=True)
            buscar_col, orden_col, limpiar_col = st.columns([5.1, 2.1, 1.55], gap="medium", vertical_alignment="bottom")
            with buscar_col:
                consulta = st.text_input(
                    "Buscar documento",
                    placeholder="Buscar por folio, número, asunto o responsable...",
                    key="buscar_firmados",
                )
            with orden_col:
                orden = st.selectbox(
                    "Ordenar por", ["Más recientes", "Más antiguos", "Número de oficio"],
                    key="firm_orden",
                )
            with limpiar_col:
                st.button(
                    "Limpiar filtros", key="firm_limpiar", use_container_width=True,
                    on_click=_limpiar_filtros_firmados,
                )

        if consulta.strip():
            q = normalizar_texto(consulta)
            lista = [r for r in lista if q in normalizar_texto(
                " ".join(str(mapa_envios.get((str(r.get("Folio", "")).strip(),
                   str(r.get("No. de oficio", "")).strip()), {}).get(k, ""))
                   for k in ("No. de oficio nuevo", "Asunto nuevo")) + " " +
                " ".join(str(r.get(k, "")) for k in
                         ("Folio", "No. de oficio", "Asunto", "Responsable")) + " " + " ".join(
                d["nombre"] for d in leer_documentos(r.get("Firmado", ""))))]
        if orden == "Número de oficio":
            lista.sort(key=lambda r: normalizar_texto(r.get("No. de oficio", "")))
        elif orden == "Más recientes":
            # Fechas válidas primero: del día más nuevo al más antiguo.
            lista.sort(key=lambda r: (_fecha_firma(r) is not None,
                                      _fecha_firma(r) or datetime.min.date()), reverse=True)
        else:
            # Fechas válidas primero: del día más antiguo al más nuevo.
            lista.sort(key=lambda r: (_fecha_firma(r) is None,
                                      _fecha_firma(r) or datetime.max.date()))

        if _area_activa_validada() == "setf":
            # Archivo SETF: una superficie continua y filas compactas, sin
            # duplicar la información ni convertir el estado en un botón.
            if not lista:
                st.info("No se encontraron expedientes para el indicador o filtros aplicados.")
            else:
                with st.container(key="firm_archive_panel"):
                    st.markdown(
                        '<div class="firm-archive-head">'
                        '<div><div class="firm-archive-title">Archivo completo de oficios</div>'
                        '<div class="firm-archive-subtitle">Documentos firmados, antecedentes y anexos disponibles</div></div>'
                        f'<span class="firm-archive-count">{len(lista)} expedientes</span>'
                        '</div>', unsafe_allow_html=True)
                    for indice, oficio in enumerate(lista):
                        folio = str(oficio.get("Folio", "")).strip()
                        numero = str(oficio.get("No. de oficio", "")).strip()
                        responsable = str(oficio.get("Responsable", "") or "No asignado").strip()
                        documentos = {
                            "firmado": leer_documentos(oficio.get("Firmado", "")),
                            "antecedentes": leer_documentos(oficio.get("Antecedente", "")),
                            "anexos": leer_documentos(oficio.get("Anexo", "")),
                        }
                        fecha = _fecha_firma(oficio)
                        firmado_realmente = normalizar_texto(oficio.get("Estatus", "")) == "firmado"
                        fecha_texto = fecha.strftime("%d/%m/%Y") if fecha else "Sin fecha registrada"
                        titulo = (str(documentos["firmado"][0].get("nombre", "")).strip()
                                  if documentos["firmado"] else "")
                        if not titulo or re.fullmatch(r"Documento \d+", titulo, re.I):
                            titulo = numero or (f"Folio {folio}" if folio else "Sin número")
                        token = hashlib.sha256(
                            (folio + "|" + numero + "|" + str(indice)).encode("utf-8")
                        ).hexdigest()[:16]
                        with st.container(key=f"firm_archive_row_{token}"):
                            col_info, col_estado, col_firmado, col_ante, col_anex = st.columns(
                                [4.3, 0.9, 1.45, 1.35, 1.05], gap="small", vertical_alignment="center"
                            )
                            with col_info:
                                st.markdown(
                                    f'<div class="firm-archive-name">{html.escape(titulo)}</div>'
                                    f'<div class="firm-archive-meta">'
                                    f'Folio {html.escape(folio or "—")} · '
                                    f'{html.escape(responsable)} · '
                                    f'{html.escape(fecha_texto)}'
                                    '</div>', unsafe_allow_html=True)
                            with col_estado:
                                if firmado_realmente:
                                    st.markdown('<span class="firm-signed-state">Firmado</span>', unsafe_allow_html=True)
                                else:
                                    st.markdown('<span class="firm-pending-state">En trámite</span>', unsafe_allow_html=True)
                            for columna, categoria, etiqueta in (
                                (col_firmado, "firmado", "Abrir firmado"),
                                (col_ante, "antecedentes", "Antecedentes"),
                                (col_anex, "anexos", "Anexos"),
                            ):
                                archivos = documentos[categoria]
                                with columna:
                                    if categoria == "firmado" and not firmado_realmente:
                                        st.button("Firmado pendiente", key=f"firm_arch_bloq_{token}",
                                                  disabled=True, use_container_width=True)
                                    elif len(archivos) == 1:
                                        st.link_button(
                                            etiqueta, archivos[0]["url"], use_container_width=True,
                                            help=f'Abrir en Drive: {archivos[0]["nombre"]}',
                                        )
                                    elif len(archivos) > 1:
                                        with st.popover(etiqueta, use_container_width=True):
                                            st.caption(f"{len(archivos)} archivos · {etiqueta}")
                                            for archivo in archivos:
                                                st.link_button(archivo["nombre"], archivo["url"],
                                                               use_container_width=True)
                                    else:
                                        st.button(etiqueta, key=f"firm_arch_vacio_{categoria}_{token}",
                                                  disabled=True, use_container_width=True,
                                                  help="No hay documentos vinculados")
        else:
            st.markdown(
                f'<div class="firm-label">{html.escape(nombres_vista_firmados[vista_firmados].upper())}</div>',
                unsafe_allow_html=True)
            st.markdown(f'<div class="firm-caption">{len(lista)} expediente(s) · '
                        'Documentos disponibles según su estado y vínculos</div>',
                        unsafe_allow_html=True)
            if not lista:
                st.markdown('<div class="firm-empty">No se encontraron expedientes '
                            'para este indicador o los filtros aplicados.</div>', unsafe_allow_html=True)
                return

            # UNA FILA POR OFICIO: nombre a la izquierda y tres accesos a la derecha.
            # Con un solo enlace, abre directamente el archivo en Drive.
            # Si una categoría contiene varios, permite escoger el archivo desde
            # un popover pequeño, sin desplegar el expediente ni desplazar la lista.
            for indice, oficio in enumerate(lista):
                folio = str(oficio.get("Folio", "")).strip()
                numero = str(oficio.get("No. de oficio", "")).strip()
                responsable = str(oficio.get("Responsable", "") or "No asignado").strip()
                documentos = {
                    "firmado": leer_documentos(oficio.get("Firmado", "")),
                    "antecedentes": leer_documentos(oficio.get("Antecedente", "")),
                    "anexos": leer_documentos(oficio.get("Anexo", "")),
                }
                fecha = _fecha_firma(oficio)
                firmado_realmente = normalizar_texto(oficio.get("Estatus", "")) == "firmado"
                fecha_texto = fecha.strftime("%d/%m/%Y") if fecha else "Sin fecha registrada"
                estado_texto = (f"Firmado: {fecha_texto}" if firmado_realmente else
                                f"Estado: {str(oficio.get('Estatus', '')) or 'En trámite'}")
                # Título de la fila: nombre del OFICIO FIRMADO (no del antecedente).
                # Si falta el archivo/nombre, conservar el título del registro.
                archivos_firmados = documentos["firmado"]
                titulo_firmado = (str(archivos_firmados[0].get("nombre", "")).strip()
                                 if archivos_firmados else "")
                if not titulo_firmado or re.fullmatch(r"Documento \d+", titulo_firmado, re.I):
                    titulo_firmado = numero or (f"Folio {folio}" if folio else "Sin número")
                ficha_envio = mapa_envios.get((folio, numero), {})
                if vista_firmados in ("hoy", "semana") and ficha_envio:
                    titulo_firmado = str(ficha_envio.get("No. de oficio nuevo", "")).strip() or titulo_firmado
                asunto_envio = (str(ficha_envio.get("Asunto nuevo", "")).strip()
                                if vista_firmados in ("hoy", "semana") else "")
                token = hashlib.sha256((folio + "|" + numero + "|" + str(indice)).encode("utf-8")).hexdigest()[:16]

                with st.container(key=f"firm_reg_row_{token}"):
                    col_info, col_firmado, col_antecedentes, col_anexos = st.columns(
                        [4.8, 1.2, 1.45, 1.1], gap="small")
                    with col_info:
                        st.markdown(
                            f'<div class="firm-inline-file">'
                            f'<div class="firm-inline-name">{html.escape(titulo_firmado)}</div>'
                            + (f'<div class="firm-inline-meta">Asunto: {html.escape(asunto_envio)}</div>' if asunto_envio else '')
                            + f'<div class="firm-inline-meta">Folio {html.escape(folio or "—")} '
                            f'· {html.escape(responsable)} · {html.escape(estado_texto)}</div>' 
                            f'</div>', unsafe_allow_html=True)

                    acciones = (
                        (col_firmado, "firmado", "Firmado"),
                        (col_antecedentes, "antecedentes", "Antecedentes"),
                        (col_anexos, "anexos", "Anexos"),
                    )
                    for columna, categoria, etiqueta in acciones:
                        archivos = documentos[categoria]
                        with columna:
                            if categoria == "firmado" and not firmado_realmente:
                                st.button("Firmado pendiente", key=f"firm_bloq_{token}",
                                          disabled=True, use_container_width=True,
                                          help="La descarga se habilitará cuando regrese el oficio firmado.")
                            elif len(archivos) == 1:
                                st.link_button(
                                    etiqueta, archivos[0]["url"],
                                    use_container_width=True,
                                    help=f'Abrir en Drive: {archivos[0]["nombre"]}',
                                )
                            elif len(archivos) > 1:
                                with st.popover(etiqueta, use_container_width=True):
                                    st.caption(f"{len(archivos)} archivos · {etiqueta}")
                                    for archivo in archivos:
                                        st.link_button(
                                            archivo["nombre"], archivo["url"],
                                            use_container_width=True,
                                        )
                            else:
                                st.button(
                                    etiqueta, key=f"firm_vacio_{categoria}_{token}",
                                    disabled=True, use_container_width=True,
                                    help="Sin documentos vinculados en esta categoría",
                                )

        # La edición autorizada de la fecha conserva su funcionalidad, pero
        # queda fuera de las tres acciones principales de cada oficio.
        editables = [r for r in lista if normalizar_texto(r.get("Estatus", "")) == "firmado"
                     and puede_editar_responsable(r.get("Responsable", ""))]
        if editables:
            with st.expander("Administración · fecha de recepción del oficio firmado", expanded=False):
                registros_editables = {
                    f"{str(r.get('No. de oficio', '')).strip()} · Folio {str(r.get('Folio', '')).strip()}": r
                    for r in editables
                }
                seleccion_edit = st.selectbox(
                    "Seleccionar oficio", list(registros_editables), key="firmado_fecha_seleccion")
                elegido_edit = registros_editables[seleccion_edit]
                folio_edit = str(elegido_edit.get("Folio", "")).strip()
                numero_edit = str(elegido_edit.get("No. de oficio", "")).strip()
                fecha_edit = _fecha_firma(elegido_edit)
                clave_edicion = hashlib.sha256((folio_edit + "|" + numero_edit).encode("utf-8")).hexdigest()[:14]
                with st.form(key="firm_fecha_form_global"):
                    nueva_fecha = st.date_input(
                        "Fecha de firma / entrega", value=fecha_edit or hoy,
                        format="DD/MM/YYYY", key=f"firm_fecha_input_global_{clave_edicion}")
                    guardar_fecha = st.form_submit_button("Guardar fecha", use_container_width=True)
                if guardar_fecha:
                    try:
                        exigir_permiso_edicion(elegido_edit.get("Responsable", ""))
                        actualizar_estatus_y_entrega(
                            folio_edit, numero_edit, "Firmado", nueva_fecha.isoformat())
                        st.success("Fecha actualizada correctamente.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"No se pudo actualizar la fecha: {error}")
        return

    # MESA DE ATENCIÓN PRIORITARIA: solo presentación, mismas reglas y datos.
    # Conserva el fondo ferroviario si está disponible en assets.
    candidatos = [ASSETS_DIR / f"fondo_urgentes{ext}"
                  for ext in (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")]
    fondo_urgentes = next((p for p in candidatos if p.is_file()), None)
    if fondo_urgentes:
        tipos = {".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime = tipos[fondo_urgentes.suffix.lower()]
        img = base64.b64encode(fondo_urgentes.read_bytes()).decode("ascii")
        css_fondo = (
            'background-image:linear-gradient(rgba(248,246,241,.24),rgba(248,246,241,.32)),'
            f'url("data:{mime};base64,{img}") !important;'
            'background-size:cover !important;'
            'background-position:center center !important;'
            'background-repeat:no-repeat !important;'
            'background-attachment:fixed !important;'
        )
    else:
        css_fondo = 'background:linear-gradient(135deg,#f5f1e9,#e9e5dd) !important;'
    st.markdown(f'<style>[data-testid="stAppViewContainer"]:has(.urg-hero) {{{css_fondo}}}</style>',
                unsafe_allow_html=True)
    st.markdown("""
    <style>
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stMain"],
    [data-testid="stAppViewContainer"]:has(.urg-hero) > .main,
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stHeader"] {
        background:transparent!important;
    }
    /* La cabecera compartida conserva su altura real: no desplazarla bajo Chrome. */
    [data-testid="stAppViewContainer"]:has(.urg-hero) .st-key-cabecera_documental {
        margin-top:0!important;
        margin-bottom:0!important;
        top:10px!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
      [data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"] .urg-hero) {
        row-gap:4px!important;
    }
    /* El panel empieza tras el encabezado y no se monta sobre él. */
    /* Encabezado: pequeño, identificable, y con identidad ferroviaria. */
    .urg-hero {
        position:relative;isolation:isolate;overflow:hidden;
        background:linear-gradient(112deg,#362D2A 0%,#544036 100%);
        border:1px solid #B88C5B;border-left:5px solid #D39C5C;
        border-radius:13px;padding:18px 26px 18px 22px;margin:-10px 0 20px;
        box-shadow:0 10px 25px rgba(42,34,32,.13);
        min-height:126px;display:flex;align-items:center;justify-content:space-between;gap:22px;
    }
    .urg-hero .urg-copy {position:relative;z-index:2;max-width:59%;}
    .urg-hero .eyebrow {font-size:10px;letter-spacing:1.7px;font-weight:800;color:#E5C399!important;}
    [data-testid="stAppViewContainer"] .urg-hero h1 {
        color:#FFF9F4!important;-webkit-text-fill-color:#FFF9F4!important;
        margin:7px 0 3px!important;font-size:30px!important;
        line-height:1.18!important;font-weight:800!important;
    }
    [data-testid="stAppViewContainer"] .urg-hero p {
        font-size:12.5px!important;color:#EADFD6!important;
        -webkit-text-fill-color:#EADFD6!important;margin:0!important;
    }
    .urg-hero .urg-count {
        z-index:3;position:relative;flex:0 0 auto;text-align:center;
        background:rgba(238,217,194,.12);border:1px solid rgba(234,200,156,.45);
        border-radius:12px;padding:12px 16px;min-width:114px;
    }
    .urg-hero .urg-count strong {display:block;font-size:30px;line-height:1.05;color:#FCEAD8;}
    .urg-hero .urg-count span {display:block;font-size:10px;letter-spacing:.65px;color:#EADFD6;}
    /* Trazo dentro de una franja independiente, centrado y separado del contador. */
    .urg-hero .urg-trazo {
        position:absolute!important;
        right:160px!important;
        top:50%!important;
        bottom:auto!important;
        transform:translateY(-50%)!important;
        width:34%!important;
        height:110px!important;
        max-height:75%!important;
        opacity:.35!important;
        pointer-events:none!important;
        z-index:1!important;
        overflow:visible!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 13%,#000 88%,transparent 100%);
        mask-image:linear-gradient(90deg,transparent 0%,#000 13%,#000 88%,transparent 100%);
    }
    .urg-kpis-heading, .urg-seccion {
        color:#815C3D!important;letter-spacing:1.5px;font-weight:850;
        font-size:11px;margin:18px 0 10px;
    }
    /* Contadores: bandas de color sólidas, con contraste real en lugar de bordes débiles. */
    .urg-kpi {
        position:relative;isolation:isolate;overflow:hidden;
        min-height:112px;
        display:grid;grid-template-columns:78px minmax(0,1fr);
        grid-template-rows:repeat(3,min-content);
        column-gap:16px;align-content:center;
        background:var(--urg-tint,#FFF8F3)!important;
        border:1.5px solid var(--urg-border,#CDAF9B)!important;
        border-radius:14px;padding:0 16px 0 0!important;
        box-shadow:0 10px 20px rgba(50,36,32,.10);
        margin:0 0 6px!important;
    }
    .urg-kpi--urgent {--urg-accent:#B44E37;--urg-tint:#FFF0E9;--urg-border:#D9A48E;}
    .urg-kpi--soon {--urg-accent:#DA982F;--urg-tint:#FFF6E5;--urg-border:#E7C082;}
    .urg-kpi--manual {--urg-accent:#607A5E;--urg-tint:#EEF5EB;--urg-border:#AABDA7;}
    .urg-kpi .urg-kpi-icon {
        grid-column:1;grid-row:1/4;width:78px;height:100%;min-height:110px;
        display:grid;place-items:center;align-self:stretch;
        color:#FFFFFF!important;background:var(--urg-accent)!important;
        border-right:1px solid rgba(48,36,26,.09);
        font-size:27px;font-weight:850;line-height:1;
        text-shadow:0 1px 2px rgba(0,0,0,.15);
    }
    .urg-kpi .value {grid-column:2;grid-row:1;color:#322B2C!important;
        font-size:27px;font-weight:850;line-height:1.08;align-self:end;}
    .urg-kpi .label {grid-column:2;grid-row:2;color:#3E3434!important;
        font-size:12px;font-weight:800;line-height:1.3;margin-top:3px;}
    .urg-kpi .small {grid-column:2;grid-row:3;color:#736761!important;
        font-size:10px;line-height:1.3;margin-top:2px;}
    [data-testid="stAppViewContainer"]:has(.urg-hero)
      [data-testid="stHorizontalBlock"]:has(.urg-kpi) {gap:22px!important; margin:6px 0 10px!important;}
    .urg-section-row {
        display:flex;justify-content:space-between;align-items:end;gap:14px;margin:34px 0 18px;
    }
    .urg-section-row .kicker {font-size:10px;letter-spacing:1.5px;color:#A2764E;font-weight:800;}
    .urg-section-row h2 {font-size:21px!important;line-height:1.18!important;color:#44383E!important;margin:4px 0 0!important;}
    .urg-section-row .urg-antiguedad {color:#755D4A;font-size:11px;background:#EDE3D7;border:1px solid #D5C3B0;border-radius:7px;padding:7px 10px;white-space:nowrap;}
    /* Fichas con rail rojo ladrillo más luminoso, información bien separada y más aire. */
    div[class*="st-key-urg_ficha_"] {
        position:relative!important;isolation:isolate!important;
        background:#FFFCF8!important;
        border:1.5px solid #D1B39D!important;
        border-radius:16px!important;
        padding:0 0 14px!important;
        margin:16px 0 34px!important;
        box-shadow:0 12px 28px rgba(64,37,29,.12)!important;
        overflow:hidden!important;
    }
    div[class*="st-key-urg_ficha_"]::before {content:none!important;display:none!important;}
    .urg-ficha {
        display:grid;grid-template-columns:158px minmax(0,1fr);
        min-height:188px;color:#392F30;position:relative;z-index:1;
    }
    .urg-rail {
        display:flex;flex-direction:column;justify-content:center;align-items:center;
        gap:4px;background:linear-gradient(155deg,#B7472D 0%,#872819 100%);
        color:#FFF8EE;padding:17px 12px;text-align:center;
        border-right:2px solid #D6A470;
    }
    .urg-rail--manual {background:linear-gradient(145deg,#6A8365,#3C624B);}
    .urg-rail .rail-kicker {font-size:9px;font-weight:820;letter-spacing:1.55px;color:#FFE1C9;}
    .urg-rail .rail-number {font-size:43px;line-height:1.04;font-weight:880;letter-spacing:-1.2px;}
    .urg-rail .rail-units {font-size:10px;font-weight:750;letter-spacing:.5px;}
    .urg-rail .rail-type {margin-top:8px;border:1px solid rgba(255,234,207,.82);
        border-radius:20px;padding:6px 12px;font-size:10px;font-weight:800;letter-spacing:.4px;}
    .urg-ficha-content {padding:20px 26px 18px;min-width:0;}
    .urg-ficha-head {display:flex;align-items:flex-start;justify-content:space-between;gap:16px;}
    .urg-ficha-kicker {font-size:10px;font-weight:850;letter-spacing:1.4px;color:#AE573E;}
    .urg-ficha-numero {margin:5px 0 2px;font-size:20px;font-weight:835;line-height:1.24;
        overflow-wrap:anywhere;color:#312A31;}
    .urg-ficha-folio {font-size:11px;color:#8A736C;}
    .urg-status {font-size:10px;font-weight:780;color:#943E2E;background:#FCEBE4;
        border:1px solid #D9A48D;border-radius:20px;padding:7px 11px;white-space:nowrap;}
    .urg-ficha-asunto {background:#F8F4EC;border-left:3px solid #B85136;
        border-radius:0 8px 8px 0;margin:14px 0 14px;
        padding:10px 12px;color:#554744;font-size:12px;line-height:1.5;overflow-wrap:anywhere;}
    .urg-ficha-grid {display:grid;grid-template-columns:1fr 1fr;gap:18px;
        border-top:1px solid #E4D2C3;padding-top:12px;}
    .urg-ficha-field .label {font-size:10px;font-weight:830;color:#A46950;letter-spacing:.75px;}
    .urg-ficha-field .text {font-size:12px;font-weight:720;color:#3B3336;
        margin-top:4px;overflow-wrap:anywhere;}
    .urg-ficha-meta {font-size:11px;color:#866D59;margin:8px 16px 0;}
    /* CTA sigue siendo el expander nativo, sin enlaces simulados. */
    /* CTA nativo: abre los documentos dentro del mismo expediente.
       No es un enlace decorativo ni altera la lógica de la consulta. */
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] {
        margin:2px 16px 1px!important;border:0!important;border-radius:9px!important;
        background:transparent!important;box-shadow:none!important;overflow:hidden!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] details {
        border:1px solid #D2A275!important;border-radius:9px!important;
        background:#FFFCF8!important;overflow:hidden!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary {
        min-height:45px!important;padding:10px 17px!important;
        background:linear-gradient(100deg,#793626,#522E28)!important;
        color:#FFF8ED!important;border-radius:8px!important;
        display:flex!important;align-items:center!important;
        font-size:12px!important;font-weight:780!important;letter-spacing:.1px!important;
        transition:background .16s,box-shadow .16s!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary:hover {
        background:linear-gradient(100deg,#923F2B,#633228)!important;
        box-shadow:inset 0 0 0 1px #D1AD7D!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary *,
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary svg {
        color:#FFF8ED!important;fill:none!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] details[open] summary {
        border-radius:8px 8px 0 0!important;
        border-bottom:1px solid #C8A77D!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stTextInput"] input,
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stSelectbox"] [data-baseweb="select"] > div {
        background:#FFFCF8!important;border:1px solid #CDBDAC!important;
        border-radius:9px!important;color:#453D40!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stExpander"] {
        background:rgba(253,250,245,.96)!important;
        border:1px solid #D4C2B0!important;border-radius:9px!important;
        box-shadow:0 2px 8px rgba(60,40,35,.04);
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero) [data-testid="stExpander"] summary {color:#483C3C!important;}
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    [class*="st-key-urgencia_guardar"] button,
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    [class*="st-key-urg_retirar_"] button {
        background:#483D3E!important;color:#FFF9F0!important;
        border:1px solid #BF9870!important;border-radius:8px!important;font-weight:750!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    [class*="st-key-urgentes_limpiar"] button {
        background:#FCFAF5!important;color:#584B45!important;
        border:1px solid #C8B7A5!important;border-radius:9px!important;
    }
    /* Scope native expanders specifically to urgent cards, not other module accordions. */
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] details > summary,
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] details > summary:hover {
        background:#71382C!important;color:#FFF9ED!important;
        border:1px solid #C7A075!important;
    }
    [data-testid="stAppViewContainer"]:has(.urg-hero)
    div[class*="st-key-urg_ficha_"] details > summary p {
        color:#FFF9ED!important;font-size:12px!important;font-weight:770!important;
    }
    @media(max-width:800px) {
        .urg-ficha {grid-template-columns:108px minmax(0,1fr);}
        .urg-rail .rail-number{font-size:31px;}
        .urg-ficha-content{padding:14px 14px 12px;}
        .urg-ficha-grid{grid-template-columns:1fr 1fr;}
        .urg-kpi{grid-template-columns:49px minmax(0,1fr);column-gap:9px;min-height:95px;padding-right:8px!important;}
        .urg-kpi .urg-kpi-icon{display:grid;width:49px;min-height:94px;font-size:20px;}
        .urg-kpi .value,.urg-kpi .label,.urg-kpi .small{grid-column:2;}
        .urg-kpi .value{font-size:22px;}
    }
    @media(max-width:520px) {
        .urg-ficha {grid-template-columns:1fr;}
        .urg-rail {flex-direction:row;justify-content:flex-start;gap:10px;padding:11px 15px;}
        .urg-rail .rail-number{font-size:21px;}
        .urg-rail .rail-type{margin:0 0 0 auto;}
    }
    .urg-empty {background:#FFFDF8;border:1px dashed #CAB5A2;color:#67554D;
        border-radius:11px;padding:30px 16px;text-align:center;margin:13px 0 22px;}
    .urg-empty strong {display:block;font-size:14px;color:#493E41;}
    .urg-empty p {font-size:12px;color:#81736D;margin-top:4px;}
    @media(max-width:900px){
        .urg-hero {padding:18px 18px;min-height:126px;margin-top:-4px!important;}
        .urg-hero .urg-trazo {
            right:116px!important;
            top:50%!important;
            bottom:auto!important;
            width:31%!important;
            height:95px!important;
            transform:translateY(-50%)!important;
            opacity:.24!important;
        }
        .urg-hero .urg-copy {max-width:72%;}
        .urg-hero h1 {font-size:26px!important;}
        .urg-section-row {align-items:flex-start;}
    }
    @media(max-width:620px){
        .urg-hero {gap:10px;}
        .urg-hero .urg-count {padding:9px 11px;min-width:73px;}
        .urg-hero .urg-count strong {font-size:24px;}
        .urg-hero .urg-copy {max-width:74%;}
        .urg-hero .eyebrow {font-size:8px;}
        .urg-hero .urg-trazo {display:none!important;}
        .urg-ficha-grid {grid-template-columns:1fr 1fr;}
        .urg-section-row {display:block;}
        .urg-section-row .urg-antiguedad {display:inline-block;margin-top:8px;}
    }
    </style>
    """, unsafe_allow_html=True)

    # Imágenes opcionales. Sin archivos extra, se usan los trazos CSS/SVG.
    # Se insertan después de los estilos base para que su visibilidad sea predecible.
    _imagen_hero_urg = ASSETS_DIR / "banner_urgentes.png"
    _imagen_ficha_urg = ASSETS_DIR / "tarjeta_urgentes.png"
    if _imagen_hero_urg.is_file():
        _url_hero_urg = "data:image/png;base64," + base64.b64encode(_imagen_hero_urg.read_bytes()).decode("ascii")
        st.markdown(
            '<style>.urg-hero{background-image:'
            'linear-gradient(90deg,rgba(61,56,56,.98) 0%,rgba(64,55,53,.92) 42%,rgba(64,55,53,.58) 58%,rgba(64,55,53,.22) 100%),'
            f'url("{_url_hero_urg}")!important;'
            'background-position:left top,right center!important;'
            'background-size:100% 100%, 42% auto!important;'
            'background-repeat:no-repeat,no-repeat!important;}'
            '.urg-hero .urg-trazo{display:none!important;}'
            '</style>', unsafe_allow_html=True)
    if _imagen_ficha_urg.is_file():
        _url_ficha_urg = "data:image/png;base64," + base64.b64encode(_imagen_ficha_urg.read_bytes()).decode("ascii")
        st.markdown(
            '<style>div[class*="st-key-urg_ficha_"]::before{'
            f'background-image:url("{_url_ficha_urg}")!important;'
            'background-size:contain!important;background-position:right top!important;'
            'background-repeat:no-repeat!important;opacity:.23!important;}'
            '</style>', unsafe_allow_html=True)

    try:
        registros = _leer_registros_oficios()
        marcas = _marcas_urgentes()
    except Exception as error:
        st.error(f"No se pudo consultar el registro: {_mensaje_error_sheets(error)}")
        return

    # Mismas reglas institucionales: urgente después de 5 días naturales o marca manual.
    UMBRAL_URGENCIA_DIAS = 5
    ESTADOS_TERMINADOS = {"firmado", "firmados", "cerrado", "cerrados", "archivado", "archivados",
                          "concluido", "concluidos", "cancelado", "cancelados"}

    def _finalizado_urgencia(registro):
        estado_normalizado = normalizar_texto(str(registro.get("Estatus", ""))).strip()
        return estado_normalizado in ESTADOS_TERMINADOS

    def _prioridad_urgencia(registro):
        clave = (str(registro.get("Folio", "")).strip(), str(registro.get("No. de oficio", "")).strip())
        dias = _dias_desde_recepcion(registro.get("Fecha de recepción"))
        manual = marcas.get(clave, False)
        automatico = dias is not None and dias > UMBRAL_URGENCIA_DIAS
        return {"dias": dias, "manual": manual, "automatico": automatico,
                "urgente": (manual or automatico) and not _finalizado_urgencia(registro)}

    lista = [r for r in registros if _prioridad_urgencia(r)["urgente"]]
    lista.sort(key=lambda r: (_prioridad_urgencia(r)["dias"] is not None,
                              _prioridad_urgencia(r)["dias"] or -1), reverse=True)
    antiguedades = [d for r in lista if (d := _prioridad_urgencia(r)["dias"]) is not None]
    manuales = sum(_prioridad_urgencia(r)["manual"] for r in lista)
    proximos = sum(4 <= d <= 5 for r in registros if not _finalizado_urgencia(r)
                  if (d := _dias_desde_recepcion(r.get("Fecha de recepción"))) is not None)
    max_antiguedad = max(antiguedades) if antiguedades else None

    # SETF: vista de atención prioritaria rediseñada; el resto de áreas conserva su cabecera.
    es_setf_urgentes = _area_activa_validada() == "setf"
    if es_setf_urgentes:
        # Espacio REAL entre el menú horizontal y el primer bloque del módulo.
        # No utilizar top / transform / márgenes negativos: antes cortaban la tarjeta.
        st.markdown('<div class="setf-urg-separation" aria-hidden="true"></div>',
                    unsafe_allow_html=True)
        st.markdown(f"""
        <div class="urg-hero setf-urg-hero" lang="es" translate="no">
          <div class="setf-urg-title-block">
            <div class="setf-urg-eyebrow">SETF · CONTROL DOCUMENTAL</div>
            <h1>Atención prioritaria</h1>
            <p>Oficios que requieren seguimiento inmediato.</p>
          </div>
          <div class="setf-urg-schematic" aria-hidden="true">
            <svg viewBox="0 0 360 116" xmlns="http://www.w3.org/2000/svg" focusable="false">
              <g fill="none" stroke="#B99A73" stroke-width=".9" opacity=".45">
                <path d="M8 23H354M8 47H354M8 71H354M8 95H354"/>
                <path d="M32 11V106M83 11V106M134 11V106M185 11V106M236 11V106M287 11V106M338 11V106"/>
              </g>
              <path d="M10 92C70 90 99 80 149 55S246 25 348 25" fill="none" stroke="#B28B58" stroke-width="2.4"/>
              <path d="M10 103C70 101 99 91 149 66S246 36 348 36" fill="none" stroke="#B28B58" stroke-width="1.1" opacity=".7"/>
              <g fill="#B28B58"><circle cx="149" cy="55" r="4"/><circle cx="287" cy="27" r="4"/></g>
            </svg>
          </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(_rotulos_area(f"""
        <div class="urg-hero">
          <svg class="urg-trazo" viewBox="0 0 600 160" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
            <g fill="none" stroke="#D8C6B4" stroke-width=".7" opacity=".43">
              <path d="M2 24H600M2 55H600M2 86H600M2 117H600M2 148H600"/>
              <path d="M30 10L5 155M95 10L70 155M160 10L135 155M225 10L200 155M290 10L265 155M355 10L330 155M420 10L395 155M485 10L460 155M550 10L525 155"/>
            </g>
            <path d="M0 139C128 137 164 115 235 82S364 20 600 22" fill="none" stroke="#D7B477" stroke-width="2.3"/>
            <path d="M0 152C128 150 164 128 235 95S364 33 600 35" fill="none" stroke="#D7B477" stroke-width="1"/>
            <path d="M0 125C128 123 164 101 235 68S364 6 600 8" fill="none" stroke="#D7B477" stroke-dasharray="4 7" stroke-width="1"/>
            <circle cx="235" cy="82" r="4" fill="#F0D6A7"/>
            <circle cx="450" cy="26" r="4" fill="#F0D6A7"/>
          </svg>
          <div class="urg-copy">
            <div class="eyebrow">DISEÑO GEOMÉTRICO · CONTROL DOCUMENTAL</div>
            <h1>Atención prioritaria</h1>
            <p>Identifica y consulta los oficios que necesitan trámite rápido.</p>
          </div>
          <div class="urg-count"><strong>{len(lista):02d}</strong><span>Urgentes activos</span></div>
        </div>
        """), unsafe_allow_html=True)

    # Estilos exclusivos de Urgentes SETF; no afectan Diseño Geométrico.
    if es_setf_urgentes:
        st.markdown("""<style>
        /* 2026-10-29: espacio antes del encabezado sin cortar el hero ni tapar la barra. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-separation {
            display:block!important;height:24px!important;min-height:24px!important;
            margin:0!important;padding:0!important;visibility:hidden!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-hero {
            min-height:150px!important;margin:0 0 15px!important;
            padding:23px 30px!important;align-items:center!important;
            background:#F7F2E9!important;background-image:none!important;
            border:1px solid #D9C7AD!important;border-left:5px solid #B9905B!important;
            border-radius:16px!important;
            box-shadow:0 8px 24px rgba(45,43,38,.06)!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-hero::before {
            content:"";position:absolute;right:12%;top:0;bottom:0;width:38%;pointer-events:none;
            background:repeating-linear-gradient(0deg,transparent 0 17px,rgba(178,154,121,.11) 18px,transparent 19px),
                       repeating-linear-gradient(90deg,transparent 0 38px,rgba(178,154,121,.11) 39px,transparent 40px);
            mask-image:linear-gradient(90deg,transparent,#000 38%,transparent);
            opacity:.65;
        }
        .setf-urg-hero .setf-urg-title-block {position:relative;z-index:2;max-width:75%;}
        .setf-urg-hero .setf-urg-eyebrow {font-size:10px;letter-spacing:1.5px;font-weight:850;color:#987145;}
        [data-testid="stAppViewContainer"] .setf-urg-hero h1 {
            color:#263A42!important;-webkit-text-fill-color:#263A42!important;
            margin:8px 0 7px!important;font-size:31px!important;font-weight:850!important;line-height:1.14!important;
        }
        [data-testid="stAppViewContainer"] .setf-urg-hero p {
            color:#647178!important;-webkit-text-fill-color:#647178!important;
            font-size:14px!important;line-height:1.45!important;margin:0!important;
        }
        .setf-urg-counter {position:relative;z-index:2;min-width:125px;display:flex;
            flex-direction:column;align-items:center;justify-content:center;
            border-radius:13px;border:1px solid #E2B6A7;background:#F9E6DF;
            padding:15px 17px;text-align:center;gap:4px;}
        .setf-urg-counter strong {font-size:32px;line-height:1;font-weight:850;color:#A24635;}
        .setf-urg-counter span {font-size:11px;font-weight:750;color:#8B574B;}

        /* KPIs: misma estructura y altura; el primero resalta sin una franja pesada. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) [data-testid="stHorizontalBlock"]:has(.setf-urg-stat) {
            gap:13px!important;margin:0 0 10px!important;align-items:stretch!important;
        }
        .setf-urg-stat {min-height:106px;border:1px solid #DED5C9;border-top:3px solid var(--tone);
            border-radius:13px;background:rgba(255,253,250,.96);box-shadow:0 4px 13px rgba(43,38,35,.05);
            padding:17px 19px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;}
        .setf-urg-stat--urgent {--tone:#B75B45;background:#FBF1EC;border-color:#DCA996;border-top-color:var(--tone);}
        .setf-urg-stat--soon {--tone:#B18C5E;}
        .setf-urg-stat--manual {--tone:#7B9A86;}
        .setf-urg-stat .value-row {display:flex;align-items:center;justify-content:space-between;gap:8px;}
        .setf-urg-stat .value {font-size:30px;font-weight:850;line-height:1.05;color:#283E47;}
        .setf-urg-stat .stat-icon {font-size:21px;color:var(--tone);line-height:1;}
        .setf-urg-stat .label {font-size:14px;font-weight:750;color:#34444A;line-height:1.3;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) [data-testid="stExpander"]:has(summary p) {
            border-radius:10px!important;
        }

        /* Encabezado de directorio y filtros son un único bloque funcional. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-dir-title {
            margin:19px 0 11px;display:flex;justify-content:space-between;align-items:center;gap:14px;
        }
        .setf-urg-dir-title .kicker {font-size:10px;letter-spacing:1.3px;font-weight:800;color:#A27A4D;}
        .setf-urg-dir-title h2 {font-size:21px!important;color:#293C43!important;margin:4px 0 0!important;}
        .setf-urg-dir-title .urg-antiguedad {font-size:11px;color:#92765A;background:#F4EAE0;
            border:1px solid #E0CDBC;border-radius:7px;padding:6px 10px;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf {
            background:rgba(255,253,250,.9)!important;border:1px solid #DED3C5!important;
            border-radius:13px!important;margin:4px 0 8px!important;padding:15px 18px 16px!important;
            box-shadow:0 4px 12px rgba(48,37,32,.045)!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf label p {
            color:#50636C!important;font-size:13px!important;font-weight:650!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf input,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf [data-baseweb="select"]>div {
            min-height:40px!important;background:#FFFEFC!important;border:1px solid #D9D0C8!important;
            border-radius:9px!important;color:#2B414B!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf [class*="st-key-urgentes_limpiar"] button {
            min-height:40px!important;background:#FBF7F1!important;color:#695344!important;
            border:1px solid #CBB8A0!important;border-radius:9px!important;box-shadow:none!important;
        }

        /* Ficha de urgencia: indicador pequeño en lugar de una barra roja alta. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) div[class*="st-key-urg_ficha_"] {
            border:1px solid #DED3C5!important;border-left:4px solid #B45E48!important;
            border-radius:13px!important;background:#FFFDF9!important;
            box-shadow:0 6px 17px rgba(54,42,35,.07)!important;
            padding:18px 21px 15px!important;margin:11px 0 14px!important;overflow:hidden!important;
        }
        .setf-urg-file {display:flex;align-items:center;gap:16px;justify-content:space-between;}
        .setf-urg-days {min-width:65px;max-width:65px;padding:10px 8px;text-align:center;
            background:#F6E8E0;border-radius:10px;color:#A74634;display:flex;flex-direction:column;gap:2px;}
        .setf-urg-days strong {font-size:26px;line-height:1;font-weight:850;color:#AB4B38;}
        .setf-urg-days small {font-size:10px;font-weight:850;color:#915846;letter-spacing:1px;}
        .setf-urg-file-body {min-width:0;flex:1;}
        .setf-urg-file-head {display:flex;flex-wrap:wrap;align-items:center;gap:8px;}
        .setf-urg-file-title {font-weight:800;font-size:17px;color:#283D46;overflow-wrap:anywhere;}
        .setf-urg-pill {font-size:10px;font-weight:800;padding:4px 8px;border-radius:999px;
            background:#F9EEE3;border:1px solid #E2C5A3;color:#826341;}
        .setf-urg-pill--urgent {background:#FAE7E0;border-color:#E8B9A9;color:#A24836;}
        .setf-urg-asunto {font-size:14px;line-height:1.5;color:#46565E;margin:7px 0 9px;}
        .setf-urg-meta {font-size:12px;line-height:1.5;color:#637680;display:flex;flex-wrap:wrap;gap:6px 15px;}
        .setf-urg-manual-flag {font-size:11px;color:#8B6B47;margin:8px 0 2px;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] {
            margin:11px 0 0!important;border:0!important;background:transparent!important;box-shadow:none!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] details {
            background:#F8F5EF!important;border:1px solid #DACABD!important;border-radius:9px!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] details>summary,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] details>summary:hover {
            background:#294C48!important;color:#FFFFFF!important;border:1px solid #527A72!important;
            border-radius:8px!important;min-height:43px!important;box-shadow:none!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] details>summary p,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] details>summary svg {color:#FFFFFF!important;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
        div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary * {color:#FFFFFF!important;}
        .setf-urg-priority-meta {margin:10px 0 11px;font-size:12px;
            line-height:1.5;color:#65757C;font-weight:550;}
        /* Chrome puede traducir selectivamente texto nativo de Streamlit.
           Mostrar la etiqueta visual CSS preserva el texto real accesible y la acción. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urgencia_guardar"] button {
            background:#294C48!important;color:#FFFFFF!important;
            border:1px solid #49756B!important;border-radius:9px!important;
            min-height:43px!important;font-weight:750!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urgencia_guardar"] button:hover {background:#35635C!important;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urgencia_guardar"] button p {font-size:0!important;line-height:0!important;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urgencia_guardar"] button::after {
            content:"Marcar prioridad manual";font-size:13px;line-height:1.4;
            font-family:inherit;font-weight:750;color:#FFFFFF;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero):has(.setf-urg-mode-remove)
          [class*="st-key-urgencia_guardar"] button::after {content:"Quitar marca manual";}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urgencia_guardar"] button:focus-visible {
            outline:3px solid #A8C7B9!important;outline-offset:2px!important;
        }
        /* Contraste del desplegable en los expedientes, sin cambiar su función. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary {
            background:#294C48!important;border:1px solid #527A72!important;
            min-height:43px!important;color:#FFFFFF!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary:hover {
            background:#35635C!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary p,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary svg,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stExpander"] summary * {
            color:#FFFFFF!important;-webkit-text-fill-color:#FFFFFF!important;
        }
        /* Pulido final SETF: una única jerarquía, sin duplicar el contador del hero. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-hero {
            min-height:132px!important;
            padding:20px 27px!important;
            margin-bottom:13px!important;
            gap:18px!important;
        }
        .setf-urg-hero .setf-urg-title-block {max-width:66%!important;}
        .setf-urg-schematic {
            position:relative;z-index:2;width:31%;max-width:330px;
            align-self:center;pointer-events:none;opacity:.7;
        }
        .setf-urg-schematic svg {width:100%;height:auto;display:block;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [data-testid="stHorizontalBlock"]:has(.setf-urg-stat) {
            gap:13px!important;margin:0 0 12px!important;
        }
        .setf-urg-stat {min-height:108px!important;padding:16px 18px!important;}
        .setf-urg-stat .value {font-size:33px!important;}
        .setf-urg-stat .label {font-size:14px!important;}
        /* Una sola zona de filtros, más legible, con controles alineados. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .setf-urg-dir-title {
            margin:17px 0 9px!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf {
            padding:14px 18px 15px!important;margin:3px 0 7px!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf input,
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) .st-key-urgentes_busqueda_setf [data-baseweb="select"]>div {
            min-height:43px!important;
        }
        /* Expedientes compactos con información a la izquierda y acción real a la derecha. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero) div[class*="st-key-urg_ficha_"] {
            padding:16px 19px!important;margin:9px 0 12px!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          div[class*="st-key-urg_ficha_"] [data-testid="stHorizontalBlock"] {
            align-items:center!important;gap:18px!important;
        }
        .setf-urg-file {gap:15px!important;}
        .setf-urg-days {min-width:61px!important;max-width:61px!important;padding:10px 8px!important;}
        .setf-urg-file-title {font-size:17px!important;line-height:1.25!important;}
        .setf-urg-asunto {font-size:14px!important;margin:5px 0 7px!important;}
        .setf-urg-meta {font-size:12px!important;gap:5px 13px!important;}
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urg_setf_toggle_"] button {
            background:#294C48!important;color:#FFFFFF!important;
            border:1px solid #52766D!important;border-radius:10px!important;
            min-height:43px!important;white-space:nowrap!important;
            font-size:13px!important;font-weight:750!important;
            box-shadow:0 3px 8px rgba(38,70,63,.11)!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urg_setf_toggle_"] button:hover {
            background:#346459!important;border-color:#346459!important;
        }
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urg_setf_toggle_"] button p {
            color:#FFFFFF!important;-webkit-text-fill-color:#FFFFFF!important;
            font-size:13px!important;line-height:1.35!important;
        }
        /* El contenido expuesto no afecta la tarjeta mientras siga cerrado. */
        [data-testid="stAppViewContainer"]:has(.setf-urg-hero)
          [class*="st-key-urg_setf_docs_"] {
            background:#FBF7F1!important;border:1px solid #DED1C2!important;
            border-radius:11px!important;padding:13px 16px!important;
            margin-top:11px!important;
        }
        @media(max-width:850px){
            .setf-urg-schematic{width:27%;}
            .setf-urg-hero .setf-urg-title-block{max-width:70%!important;}
        }
        @media(max-width:650px){
            .setf-urg-schematic{display:none!important;}
            .setf-urg-hero .setf-urg-title-block{max-width:100%!important;}
        }
        @media(max-width:800px) {
            .setf-urg-file {gap:10px;}
            .setf-urg-hero .setf-urg-title-block {max-width:65%;}
            .setf-urg-hero h1 {font-size:24px!important;}
            .setf-urg-counter {min-width:94px;padding:11px;}
        }
        @media(max-width:520px){
            .setf-urg-separation {height:18px!important;min-height:18px!important;}
            .setf-urg-file {flex-wrap:wrap;}
            .setf-urg-hero {padding:17px!important;margin-top:0!important;}
            .setf-urg-counter strong {font-size:23px;}
        }
        </style>""", unsafe_allow_html=True)

    kpis = (
        (len(lista), "Urgentes activos", "En seguimiento prioritario", "urgent"),
        (proximos, "Próximos a ser urgentes" if es_setf_urgentes else "Próximos a urgente",
         "Entre 4 y 5 días naturales", "soon"),
        (manuales, "Prioridad manual", "Casos especiales marcados", "manual"),
    )
    for columna, (numero, titulo, detalle, tipo_kpi) in zip(st.columns(3, gap="medium"), kpis):
        with columna:
            if es_setf_urgentes:
                icono_stat = {"urgent": "⚠", "soon": "◷", "manual": "⚑"}[tipo_kpi]
                st.markdown(
                    f'<div class="setf-urg-stat setf-urg-stat--{tipo_kpi}">'
                    '<div class="value-row">'
                    f'<span class="value">{numero:02d}</span>'
                    f'<span class="stat-icon" aria-hidden="true">{icono_stat}</span>'
                    '</div>'
                    f'<div class="label">{html.escape(titulo)}</div></div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    f'<div class="urg-kpi urg-kpi--{tipo_kpi}">'
                    f'<span class="urg-kpi-icon" aria-hidden="true">{ {"urgent":"!","soon":"◷","manual":"✓"}[tipo_kpi] }</span>'
                    f'<div class="value">{numero:02d}</div>'
                    f'<div class="label">{html.escape(titulo)}</div>'
                    f'<div class="small">{html.escape(detalle)}</div>'
                    '</div>', unsafe_allow_html=True)

    # SETF: un solo desplegable de ayuda; acciones administrativas internas mantienen permisos.
    # Las opciones de la cuenta administrativa son exactamente las originales.
    with st.expander(
        "Criterios y administración de prioridad" if es_setf_urgentes
        else "Criterios de prioridad · 6 días / 4–5 días / marca manual",
        expanded=False):
        st.markdown(
            "Los oficios **activos** se consideran urgentes automáticamente cuando han transcurrido "
            "**más de 5 días naturales** desde su recepción. Los días 4 y 5 se muestran "
            "como próximos a urgente. También pueden marcarse casos especiales de forma manual."
        )
        st.caption(
            "Los oficios firmados, cerrados, archivados, concluidos o cancelados no aparecen. "
            "Este indicador no altera el estatus registrado en Google Sheets."
        )
        if es_setf_urgentes and es_administrador():
            st.markdown("**Prioridad manual · casos especiales**")
            st.caption("La urgencia por antigüedad no puede desactivarse retirando una marca manual.")
            opciones = {f"{r.get('Folio', '')} · {r.get('No. de oficio', '')} · {str(r.get('Asunto', ''))[:75]}": r
                        for r in registros if not _finalizado_urgencia(r)}
            if opciones:
                seleccion = st.selectbox("Selecciona un oficio activo", list(opciones), key="urgencia_seleccion")
                oficio = opciones[seleccion]
                folio = str(oficio.get("Folio", "")).strip()
                numero = str(oficio.get("No. de oficio", "")).strip()
                marcado = marcas.get((folio, numero), False)
                dias = _dias_desde_recepcion(oficio.get("Fecha de recepción"))
                st.markdown(
                    '<div class="setf-urg-priority-meta" lang="es" translate="no">'
                    f'Antigüedad: {dias if dias is not None else "sin fecha"} días · '
                    f'Prioridad manual: {"Sí" if marcado else "No"}'
                    '</div>', unsafe_allow_html=True)
                st.markdown(
                    '<span class="setf-urg-mode-remove" aria-hidden="true"></span>'
                    if marcado else '<span class="setf-urg-mode-add" aria-hidden="true"></span>',
                    unsafe_allow_html=True)
                if st.button("Quitar marca manual" if marcado else "Marcar prioridad manual",
                             key="urgencia_guardar"):
                    try:
                        _actualizar_urgencia(folio, numero, not marcado)
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))

    # Otras áreas: el administrador conserva su panel anterior de marcas especiales.
    if not es_setf_urgentes and es_administrador():
        with st.expander("Prioridad manual · casos especiales", expanded=False):
            st.caption("La urgencia por antigüedad no puede desactivarse retirando una marca manual.")
            opciones = {f"{r.get('Folio', '')} · {r.get('No. de oficio', '')} · {str(r.get('Asunto', ''))[:75]}": r
                        for r in registros if not _finalizado_urgencia(r)}
            if opciones:
                seleccion = st.selectbox("Selecciona un oficio activo", list(opciones), key="urgencia_seleccion")
                oficio = opciones[seleccion]
                folio = str(oficio.get("Folio", "")).strip()
                numero = str(oficio.get("No. de oficio", "")).strip()
                marcado = marcas.get((folio, numero), False)
                dias = _dias_desde_recepcion(oficio.get("Fecha de recepción"))
                st.caption(f"Antigüedad: {dias if dias is not None else 'sin fecha'} días · "
                           f"Prioridad manual: {'Sí' if marcado else 'No'}")
                if st.button("Quitar marca manual" if marcado else "Marcar prioridad manual",
                             key="urgencia_guardar"):
                    try:
                        _actualizar_urgencia(folio, numero, not marcado)
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))

    if es_setf_urgentes:
        st.markdown(
            '<div class="setf-urg-dir-title"><div>'
            '<div class="kicker">EXPEDIENTES PRIORITARIOS</div>'
            '<h2>Requieren atención</h2></div>'
            f'<div class="urg-antiguedad">Mayor antigüedad: '
            f'{str(max_antiguedad) + " días" if max_antiguedad is not None else "—"}'
            '</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="urg-section-row"><div>'
            '<div class="kicker">EXPEDIENTES PRIORITARIOS</div>'
            '<h2>Requieren atención</h2></div>'
            f'<div class="urg-antiguedad">Mayor antigüedad: '
            f'{str(max_antiguedad) + " días" if max_antiguedad is not None else "—"}'
            '</div></div>', unsafe_allow_html=True)

    # Se mantienen los filtros, keys y lógica de búsqueda originales.
    # Solo SETF recibe la presentación de buscador institucional.
    from contextlib import nullcontext
    _panel_urg = (st.container(key="urgentes_busqueda_setf")
                  if _area_activa_validada() == "setf" else nullcontext())
    def _limpiar_filtros_urgentes_callback():
        # Streamlit ejecuta callbacks antes de construir los widgets del nuevo ciclo.
        # Así evitamos modificar el estado después de instanciar la búsqueda.
        st.session_state["buscar_urgentes"] = ""
        st.session_state["urgentes_estado"] = "Todos"

    with _panel_urg:
        # Un solo encabezado de campo, sin repetir «Buscar expediente» y «Buscar».
        col_buscar, col_estado, col_limpiar = st.columns([5, 2, 1.6], gap="small")
        with col_buscar:
            consulta = st.text_input("Buscar expediente" if es_setf_urgentes else "Buscar",
                                     placeholder="Folio, oficio, asunto o responsable...",
                                     key="buscar_urgentes")
        estados = sorted({str(r.get("Estatus", "")).strip() for r in lista
                          if str(r.get("Estatus", "")).strip()})
        with col_estado:
            estado = st.selectbox("Filtrar por estado", ["Todos"] + estados, key="urgentes_estado")
        with col_limpiar:
            st.write("")
            st.button("Limpiar filtros", key="urgentes_limpiar",
                      use_container_width=True, on_click=_limpiar_filtros_urgentes_callback)
    filtrados = lista
    if consulta.strip():
        buscado = normalizar_texto(consulta)
        filtrados = [r for r in filtrados if buscado in normalizar_texto(
            " ".join(str(r.get(k, "")) for k in
                     ("Folio", "No. de oficio", "Asunto", "Responsable")) + " " + " ".join(
                d["nombre"] for d in leer_documentos(r.get("Firmado", ""))))]
    if estado != "Todos":
        filtrados = [r for r in filtrados if str(r.get("Estatus", "")).strip() == estado]
    st.caption(f"{len(filtrados)} expediente(s) · Ordenados por antigüedad")
    if not filtrados:
        mensaje = ("No hay oficios activos con urgencia automática o manual."
                   if not lista else "No se encontraron oficios con estos filtros.")
        st.markdown(
            f'<div class="urg-empty"><strong>{html.escape(mensaje)}</strong>'
            '<p>Los expedientes prioritarios aparecerán aquí.</p></div>',
            unsafe_allow_html=True)
        return

    for indice, oficio in enumerate(filtrados):
        folio = str(oficio.get("Folio", "")).strip()
        numero = str(oficio.get("No. de oficio", "")).strip()
        prioridad = _prioridad_urgencia(oficio)
        dias = prioridad["dias"]
        fecha = str(oficio.get("Fecha de recepción", "")).strip() or "No registrada"
        fecha_visual = fecha
        if es_setf_urgentes and fecha != "No registrada":
            try:
                fecha_parseada = pd.to_datetime(
                    fecha, yearfirst=bool(re.match(r"^\d{4}[-/]", fecha)),
                    dayfirst=not bool(re.match(r"^\d{4}[-/]", fecha)),
                    errors="coerce")
                if not pd.isna(fecha_parseada):
                    fecha_visual = fecha_parseada.strftime("%d/%m/%Y")
            except (ValueError, TypeError, OverflowError):
                pass
        estatus = str(oficio.get("Estatus", "")).strip() or "Sin estado"
        asunto = str(oficio.get("Asunto", "")).strip() or "Sin asunto"
        responsable = str(oficio.get("Responsable", "")).strip() or "No asignado"
        es_manual = not prioridad["automatico"]
        motivo = "PRIORIDAD MANUAL" if es_manual else "URGENTE"
        edad = f"{dias} días naturales" if dias is not None else "Fecha por verificar"
        con_marca = prioridad["manual"] and prioridad["automatico"]
        # Rail de antigüedad + información; datos y permisos de origen intactos.
        rail_label = str(dias) if dias is not None else "—"
        rail_unidad = "DÍAS NATURALES" if dias is not None else "SIN FECHA"
        rail_clase = "urg-rail--manual" if es_manual else ""
        with st.container(key=f"urg_ficha_{'manual_' if es_manual else ''}{indice}"):
            if es_setf_urgentes:
                col_ficha, col_accion = st.columns([3.9, 1.2], gap="small")
                with col_ficha:
                    # Tarjeta de consulta compacta: sin banda vertical grande.
                    tag_edad = f"{dias}" if dias is not None else "—"
                    st.markdown(
                        '<div class="setf-urg-file">'
                        '<div class="setf-urg-days">'
                        f'<strong>{html.escape(tag_edad)}</strong><small>DÍAS</small>'
                        '</div><div class="setf-urg-file-body">'
                        '<div class="setf-urg-file-head">'
                        f'<span class="setf-urg-file-title">Oficio {html.escape(numero or folio or "Sin número")}</span>'
                        f'<span class="setf-urg-pill setf-urg-pill--urgent">{html.escape(motivo.title())}</span>'
                        f'<span class="setf-urg-pill">{html.escape(estatus)}</span>'
                        '</div>'
                        f'<div class="setf-urg-asunto">{html.escape(asunto[:230])}</div>'
                        '<div class="setf-urg-meta">'
                        f'<span>Responsable: <b>{html.escape(responsable)}</b></span>'
                        f'<span>Folio: {html.escape(folio or "—")}</span>'
                        f'<span>Recibido el {html.escape(fecha_visual)}</span>'
                        '</div></div></div>'
                        + ('<div class="setf-urg-manual-flag">También marcado como prioridad manual</div>'
                           if con_marca else ''),
                        unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div class="urg-ficha">'
                    f'<div class="urg-rail {rail_clase}">'
                    '<div class="rail-kicker">ANTIGÜEDAD</div>'
                    f'<div class="rail-number">{html.escape(rail_label)}</div>'
                    f'<div class="rail-units">{rail_unidad}</div>'
                    f'<div class="rail-type">{motivo}</div>'
                    '</div>'
                    '<div class="urg-ficha-content">'
                    '<div class="urg-ficha-head"><div>'
                    '<div class="urg-ficha-kicker">EXPEDIENTE PRIORITARIO</div>'
                    f'<div class="urg-ficha-numero">{html.escape(numero or folio or "Sin número")}</div>'
                    f'<div class="urg-ficha-folio">Folio {html.escape(folio or "—")}</div>'
                    '</div>'
                    f'<span class="urg-status">{html.escape(estatus)}</span>'
                    '</div>'
                    f'<div class="urg-ficha-asunto">{html.escape(asunto[:230])}</div>'
                    '<div class="urg-ficha-grid">'
                    f'<div class="urg-ficha-field"><div class="label">RESPONSABLE</div>'
                    f'<div class="text">{html.escape(responsable)}</div></div>'
                    f'<div class="urg-ficha-field"><div class="label">RECEPCIÓN</div>'
                    f'<div class="text">{html.escape(fecha)}</div></div>'
                    '</div></div></div>'
                    + ('<div class="urg-ficha-meta">También marcado como prioridad manual</div>' if con_marca else '')
                    , unsafe_allow_html=True)
            if es_setf_urgentes:
                # Un botón NATIVO de Streamlit, alineado a la derecha. La apertura
                # conserva los enlaces y permisos que antes vivían en st.expander.
                id_urg = hashlib.sha256(f"{folio}|{numero}".encode("utf-8")).hexdigest()[:14]
                clave_abierto = f"urg_setf_abierto_{id_urg}"
                with col_accion:
                    abierto = bool(st.session_state.get(clave_abierto, False))
                    if st.button("Cerrar expediente ↑" if abierto else "Ver expediente →",
                                 key=f"urg_setf_toggle_{indice}_{id_urg}",
                                 use_container_width=True):
                        st.session_state[clave_abierto] = not abierto
                if st.session_state.get(clave_abierto, False):
                    with st.container(key=f"urg_setf_docs_{indice}_{id_urg}"):
                        st.markdown(f"**Asunto:** {asunto}")
                        st.markdown(f"**Folio:** {folio}  |  **Responsable:** {responsable}")
                        _documentos_seccion(oficio)
                        if es_administrador():
                            if marcas.get((folio, numero), False) and st.button(
                                    "Quitar marca manual", key=f"urg_retirar_{indice}_{folio}"):
                                try:
                                    _actualizar_urgencia(folio, numero, False)
                                    st.rerun()
                                except Exception as error:
                                    st.error(str(error))
            else:
                with st.expander("▣ Consultar expediente y documentos", expanded=False):
                    st.markdown(f"**Asunto:** {asunto}")
                    st.markdown(f"**Folio:** {folio}  |  **Responsable:** {responsable}")
                    _documentos_seccion(oficio)
                    if es_administrador():
                        if marcas.get((folio, numero), False) and st.button(
                                "Quitar marca manual", key=f"urg_retirar_{indice}_{folio}"):
                            try:
                                _actualizar_urgencia(folio, numero, False)
                                st.rerun()
                            except Exception as error:
                                st.error(str(error))


# =========================================================
# HOME
# =========================================================

# =========================================================
# MINUTAS · CONTROL INTERNO SETF (INDEPENDIENTE DE OFICIOS)
# =========================================================
# [setf].minutas_spreadsheet_id apunta al archivo "Control Interno SETF".
# Su pestaña MINUTAS (A-D) se trata como registro maestro EXISTENTE.
# Si hay un folio reservado por sus fórmulas, se rellena B:D sin tocar A.
# Las minutas externas nuevas se guardan en MINUTAS_EXTERNAS_APP para NO
# interferir con el consecutivo que calcula la hoja original.

_MINUTAS_PATRON_SETf = re.compile(r"^AIFA-ITEyPF-(?:CAT|SETF)-(\d{4})-(\d{4})$", re.IGNORECASE)
_MINUTAS_EXTERNAS_CAMPOS = [
    "Clave externa", "Asunto", "Fecha", "Proyecto", "Enlace",
    "Registrado por", "Fecha de registro",
]


def _id_libro_minutas_setf():
    try:
        conf = st.secrets["setf"]
        valor = str(conf.get("minutas_spreadsheet_id", "")).strip()
    except (FileNotFoundError, KeyError, TypeError):
        valor = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", valor):
        raise ValueError(
            'Agrega minutas_spreadsheet_id = "ID_DEL_CONTROL_INTERNO_SETF" '
            'dentro de [setf] en .streamlit/secrets.toml.'
        )
    return valor


@st.cache_resource(show_spinner=False)
def _cliente_gspread_minutas_setf():
    """Cliente gspread real, compatible con gspread 5 y 6 en local/Cloud.

    En gspread 6 `worksheet.spreadsheet.client` es un HTTPClient: sirve
    para llamadas HTTP, pero NO ofrece `open_by_key`.
    Usamos `gspread.authorize` con las credenciales YA configuradas,
    sin cambiar el cliente ni las sesiones del módulo OFICIOS.
    """
    ruta_credenciales = BASE_DIR / "credentials.json"
    if ruta_credenciales.is_file():
        with ruta_credenciales.open("r", encoding="utf-8") as archivo:
            info_cuenta_servicio = json.load(archivo)
    else:
        try:
            info_cuenta_servicio = dict(st.secrets["gcp_service_account"])
        except (KeyError, FileNotFoundError) as exc:
            raise RuntimeError(
                "Faltan las credenciales de la cuenta de servicio de Google "
                "en [gcp_service_account]."
            ) from exc

    credenciales = Credentials.from_service_account_info(
        info_cuenta_servicio,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(credenciales)


def _hoja_minutas_setf():
    """Obtiene MINUTAS en su propio libro, sin acceder al libro de DG."""
    if _area_activa_validada() != "setf":
        raise PermissionError("La gestión de minutas de SETF no está disponible en DG.")
    identificador = _id_libro_minutas_setf()
    # La conexión de Oficios devuelve una Worksheet, no un Client.
    # No usar `worksheet.spreadsheet.client`: con gspread 6 es HTTPClient.
    cliente = _cliente_gspread_minutas_setf()
    libro = cliente.open_by_key(identificador)
    try:
        hoja = libro.worksheet("MINUTAS")
    except gspread.WorksheetNotFound as exc:
        raise ValueError('No se encontró la pestaña "MINUTAS" en Control Interno SETF.') from exc
    return libro, hoja


def _filas_minutas_setf(hoja):
    """Primera columna: folio; B asunto; C fecha; D proyecto.

    La fila 1 es encabezado. Evitar get_all_records porque hay códigos
    reservados y celdas con hipervínculos, no una tabla convencional.
    """
    valores = hoja.get_all_values()
    salida = []
    for idx, valores_fila in enumerate(valores[1:], start=2):
        datos = (list(valores_fila) + [""] * 4)[:4]
        clave, asunto, fecha, proyecto = (str(v).strip() for v in datos)
        if not any((clave, asunto, fecha, proyecto)):
            continue
        if not clave and not asunto:
            continue
        match = _MINUTAS_PATRON_SETf.fullmatch(clave)
        salida.append({
            "numero": clave, "asunto": asunto, "fecha": fecha,
            "proyecto": proyecto, "tipo": "Propia SETF" if match else "Otra reunión",
            "consecutivo": int(match.group(1)) if match else None,
            "anio": int(match.group(2)) if match else None,
            "fila": idx, "origen": "MINUTAS", "enlace": "",
            "registrada": bool(asunto or fecha or proyecto),
        })
    return salida


def _consecutivo_siguiente_setf(registros, anio):
    """Calcula el siguiente folio SIN contar las otras reuniones.

    Para 2026 el histórico confirmado al iniciar el módulo es 0058.
    No se adelanta el contador por fórmulas/preasignaciones vacías.
    """
    propios = [r for r in registros if r["tipo"] == "Propia SETF" and r["anio"] == anio]
    usados = [r["consecutivo"] for r in propios if r["registrada"]]
    piso = 58 if anio == 2026 else 0
    numero = max([piso, *usados]) + 1
    codigo = f"AIFA-ITEyPF-SETF-{numero:04d}-{anio}"
    candidatos = [r for r in propios if r["consecutivo"] == numero]
    if len(candidatos) > 1:
        raise ValueError(f"Folio {codigo} duplicado en Sheets. Revisa la tabla antes de registrar.")
    if candidatos and candidatos[0]["registrada"]:
        raise ValueError(f"El consecutivo {codigo} ya tiene datos. Actualiza el registro antes de continuar.")
    # Si hay otros consecutivos superiores ya preparados, no saltarse los vacíos.
    return codigo, (candidatos[0]["fila"] if candidatos else None), numero


@st.cache_resource(show_spinner=False)
def _candado_minutas_instancia():
    """Serializa altas dentro del mismo proceso Streamlit.

    Google Sheets carece de compare-and-swap. Para varios servidores/editores
    externos se necesitará una reserva transaccional adicional.
    """
    import threading
    return threading.RLock()


def _texto_sheets_seguro(texto):
    texto = str(texto or "").strip()
    if texto.startswith(("=", "+", "-", "@")):
        return "'" + texto
    return texto


def _url_minuta_segura(url):
    from urllib.parse import urlsplit
    valor = str(url or "").strip()
    if not valor:
        return ""
    partes = urlsplit(valor)
    if partes.scheme.lower() != "https" or not partes.hostname or partes.username or partes.password:
        raise ValueError("El enlace debe ser una dirección HTTPS válida.")
    if len(valor) > 1500:
        raise ValueError("El enlace del documento es demasiado largo.")
    return valor


def _guardar_minuta_propia_setf(asunto, fecha, proyecto, enlace):
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Solo es posible registrar minutas propias desde SETF.")
    asunto, proyecto = str(asunto).strip(), str(proyecto).strip()
    if not asunto or not proyecto:
        raise ValueError("Indica el asunto y el proyecto de la minuta.")
    enlace = _url_minuta_segura(enlace)
    anio = int(fecha.year)
    # La tabla mostrada corresponde a 2026; otros años comienzan en 0001,
    # pero siguen basándose en los registros reales del año correspondiente.
    with _candado_minutas_instancia():
        _, hoja = _hoja_minutas_setf()
        registros = _filas_minutas_setf(hoja)  # Lectura fresca antes de escribir.
        codigo, fila, consecutivo = _consecutivo_siguiente_setf(registros, anio)
        # No escribir una fórmula creada por el usuario como asunto.
        # Solo generar HYPERLINK para URL HTTPS validada.
        if enlace:
            asunto_celda = '=HYPERLINK("' + enlace.replace('"', '""') + '","' + asunto.replace('"', '""') + '")'
        else:
            asunto_celda = _texto_sheets_seguro(asunto)
        # En Sheets la lista validada usa "México-Querétaro".
        proyecto_hoja = (
            "México-Querétaro" if proyecto == "Extensión de Suburbanos Mex-Qro"
            else _texto_sheets_seguro(proyecto)
        )
        valores = [asunto_celda, fecha.strftime("%d/%m/%Y"), proyecto_hoja]
        if fila is not None:
            # La hoja histórica aún puede reservar el siguiente folio con CAT.
            # Nunca escribir B:D bajo una clave CAT si la app promete SETF.
            original = str(hoja.acell(f"A{fila}").value or "").strip()
            if original.casefold() != codigo.casefold():
                raise ValueError(
                    f"La fila {fila} sigue reservada como '{original}'. "
                    f"Para registrar con '{codigo}', primero actualiza la fórmula "
                    "de la columna A de MINUTAS para que genere SETF. "
                    "No se modificó ningún dato."
                )
            # Conservar A y su fórmula original, una vez que coincida el prefijo.
            hoja.update(range_name=f"B{fila}:D{fila}", values=[valores], value_input_option="USER_ENTERED")
        else:
            # Confirmar que la hoja NO tenga códigos superiores preparados:
            # esos espacios no deben desplazarse ni sobrescribirse.
            superiores = [r for r in registros if r["tipo"] == "Propia SETF"
                         and r["anio"] == anio and r["consecutivo"] > consecutivo]
            if superiores:
                raise ValueError(
                    f"No se encontró la fila reservada {codigo} y existen otros "
                    "consecutivos posteriores. Revisa la fórmula de MINUTAS; no se escribió nada."
                )
            # La hoja no tiene fila preasignada para el siguiente consecutivo.
            # Verificar antes de append para no sobrescribir una fórmula oculta.
            siguiente_fila = max(2, len(hoja.get_all_values()) + 1)
            valor_formula = hoja.acell(f"A{siguiente_fila}", value_render_option="FORMULA").value
            if valor_formula:
                raise ValueError(
                    f"La fila {siguiente_fila} contiene una fórmula de folio. "
                    "No se sobrescribió; verifica el consecutivo en la pestaña MINUTAS."
                )
            hoja.append_row([codigo, *valores], value_input_option="USER_ENTERED", table_range="A:D")
        # Verificar que la clave registrada exista UNA vez y no quede vacía.
        posteriores = _filas_minutas_setf(hoja)
        coincidencias = [r for r in posteriores if r["numero"].upper() == codigo.upper()]
        if len(coincidencias) != 1 or not coincidencias[0]["registrada"]:
            raise RuntimeError(
                "No se pudo confirmar la escritura. Revisa la pestaña MINUTAS antes de intentarlo otra vez."
            )
    return codigo


def _hoja_minutas_externas_setf(libro, crear=False):
    nombre = "MINUTAS_EXTERNAS_APP"
    try:
        hoja = libro.worksheet(nombre)
    except gspread.WorksheetNotFound:
        if not crear:
            return None
        hoja = libro.add_worksheet(title=nombre, rows=1000, cols=len(_MINUTAS_EXTERNAS_CAMPOS))
        hoja.append_row(_MINUTAS_EXTERNAS_CAMPOS, value_input_option="RAW")
    cabecera = hoja.row_values(1)
    if cabecera != _MINUTAS_EXTERNAS_CAMPOS:
        raise ValueError(f"La pestaña {nombre} tiene encabezados distintos; no se realizaron cambios.")
    return hoja


def _filas_minutas_externas_setf(libro):
    hoja = _hoja_minutas_externas_setf(libro)
    if hoja is None:
        return []
    salida = []
    for posicion, fila in enumerate(hoja.get_all_values()[1:], start=2):
        datos = (list(fila) + [""] * len(_MINUTAS_EXTERNAS_CAMPOS))[:len(_MINUTAS_EXTERNAS_CAMPOS)]
        clave, asunto, fecha, proyecto, enlace, *_ = datos
        if asunto or clave:
            salida.append({
                "numero": clave or "Sin clave", "asunto": asunto, "fecha": fecha,
                "proyecto": proyecto, "tipo": "Otra reunión", "consecutivo": None,
                "anio": None, "fila": posicion, "origen": "MINUTAS_EXTERNAS_APP",
                "enlace": enlace, "registrada": True,
            })
    return salida


def _guardar_minuta_externa_setf(clave, asunto, fecha, proyecto, enlace):
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Solo es posible registrar minutas externas desde SETF.")
    clave, asunto, proyecto = (str(x or "").strip() for x in (clave, asunto, proyecto))
    if not asunto or not proyecto:
        raise ValueError("Indica el asunto y el proyecto de la reunión.")
    enlace = _url_minuta_segura(enlace)
    if _MINUTAS_PATRON_SETf.fullmatch(clave):
        raise ValueError("Esta clave pertenece al consecutivo SETF. Utiliza 'Minuta propia SETF'.")
    with _candado_minutas_instancia():
        libro, _ = _hoja_minutas_setf()
        hoja = _hoja_minutas_externas_setf(libro, crear=True)
        if clave:
            existentes = hoja.col_values(1)[1:]
            if any(str(c).strip().casefold() == clave.casefold() for c in existentes):
                raise ValueError("Ya existe una minuta externa con esa clave.")
        hoja.append_row([
            _texto_sheets_seguro(clave), _texto_sheets_seguro(asunto),
            fecha.strftime("%d/%m/%Y"), _texto_sheets_seguro(proyecto), enlace,
            identificador_sesion(), datetime.now().astimezone().isoformat(timespec="seconds"),
        ], value_input_option="RAW")
    return clave or "Minuta externa registrada"


def _clave_vinculo_minuta_setf(registro):
    """Identificador estable: origen, fila y clave; no altera MINUTAS."""
    return f"{registro['origen']}|{registro['fila']}|{registro['numero']}"


def _hoja_vinculos_minutas_setf(libro, crear=False):
    """Enlaces manuales en pestaña auxiliar, sin editar fórmulas de MINUTAS."""
    nombre = "MINUTAS_ENLACES_APP"
    cabeceras = ["Identificador", "Enlace", "Modificado por", "Fecha de modificación"]
    try:
        ws = libro.worksheet(nombre)
    except gspread.WorksheetNotFound:
        if not crear:
            return None
        ws = libro.add_worksheet(title=nombre, rows=1000, cols=4)
        ws.append_row(cabeceras, value_input_option="RAW")
    if ws.row_values(1) != cabeceras:
        raise ValueError("La pestaña de enlaces tiene encabezados inesperados; no se guardó nada.")
    return ws


def _vinculos_minutas_setf(libro):
    ws = _hoja_vinculos_minutas_setf(libro)
    if ws is None:
        return {}
    return {str(r[0]).strip(): str(r[1]).strip()
            for fila in ws.get_all_values()[1:]
            if (r := (list(fila) + ["", ""]))[0].strip()}


def _guardar_vinculo_minuta_setf(registro, url):
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Esta acción es exclusiva de SETF.")
    url = _url_minuta_segura(url)
    if not url:
        raise ValueError("Pega el enlace HTTPS de la minuta para guardarlo.")
    ident = _clave_vinculo_minuta_setf(registro)
    with _candado_minutas_instancia():
        libro, _ = _hoja_minutas_setf()
        ws = _hoja_vinculos_minutas_setf(libro, crear=True)
        filas = ws.col_values(1)
        coincidencias = [i for i, texto in enumerate(filas, start=1) if i > 1 and texto == ident]
        if len(coincidencias) > 1:
            raise ValueError("Hay vínculos duplicados para esta minuta. Revisa MINUTAS_ENLACES_APP.")
        valores = [ident, url, str(identificador_sesion()),
                   datetime.now().astimezone().isoformat(timespec="seconds")]
        if coincidencias:
            ws.update(range_name=f"A{coincidencias[0]}:D{coincidencias[0]}",
                      values=[valores], value_input_option="RAW")
        else:
            ws.append_row(valores, value_input_option="RAW")
    return url




def _eliminar_vinculo_minuta_setf(registro):
    """Desvincula una minuta sin eliminar el archivo ni su registro maestro.

    Conserva una fila auxiliar con URL vacía para anular también enlaces
    originales de MINUTAS_EXTERNAS_APP y dejar trazabilidad de la acción.
    """
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Esta acción es exclusiva de SETF.")
    ident = _clave_vinculo_minuta_setf(registro)
    with _candado_minutas_instancia():
        libro, _ = _hoja_minutas_setf()
        ws = _hoja_vinculos_minutas_setf(libro, crear=True)
        coincidencias = [n for n, v in enumerate(ws.col_values(1), start=1)
                         if n > 1 and str(v).strip() == ident]
        if len(coincidencias) > 1:
            raise ValueError("Hay vínculos duplicados; consulta MINUTAS_ENLACES_APP.")
        valores = [ident, "", identificador_sesion(),
                   datetime.now().astimezone().isoformat(timespec="seconds")]
        if coincidencias:
            ws.update(range_name=f"A{coincidencias[0]}:D{coincidencias[0]}",
                      values=[valores], value_input_option="RAW")
        else:
            ws.append_row(valores, value_input_option="RAW")
    return True


def _proyecto_form_minuta_setf(clave):
    opciones = ["Tren Maya", "AIFA-Pachuca", "Extensión de Suburbanos Mex-Qro", "Otro proyecto"]
    eleccion = st.selectbox("Proyecto", opciones, key=f"minutas_proyecto_{clave}")
    if eleccion == "Otro proyecto":
        return st.text_input(
            "Especifica el nombre del otro proyecto",
            key=f"minutas_otro_{clave}",
            placeholder="Escribe el nombre completo del proyecto...",
            help="Este nombre se guardará como proyecto de la minuta.",
        ).strip()
    return eleccion


def _modulo_minutas_setf():
    st.markdown("""<style>
    .setf-min-hero {background:linear-gradient(110deg,#162E40,#285168);
        color:white;border-left:5px solid #C5A36D;border-radius:14px;
        padding:28px 34px;margin:4px 0 20px;
        box-shadow:0 9px 25px rgba(17,40,54,.16)}
    .setf-min-hero .tag {letter-spacing:2.2px;font-size:10px;color:#D8B77F;font-weight:800}
    .setf-min-hero h2 {font-size:31px;color:#FFF!important;margin:10px 0 5px!important}
    .setf-min-hero p {color:#DDE5EA!important;margin:0}
    .setf-min-hero {position:relative;isolation:isolate;overflow:hidden;min-height:178px;
        display:flex;flex-direction:column;justify-content:center;padding-right:39%;}
    .setf-min-hero .tag,.setf-min-hero h2,.setf-min-hero p {position:relative;z-index:2;}
    .setf-min-hero::after {content:"";position:absolute;inset:0;
        background:linear-gradient(90deg,#162E40 0%,rgba(22,46,64,.35) 58%,transparent 100%);
        pointer-events:none;z-index:1;}
    /* Selector de tipo de minuta con aspecto de tarjetas, conservando el estado
       y los valores originales del st.radio. Sólo SETF -> Minutas. */
    .st-key-minutas_tipo_registro [role="radiogroup"] {
        display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr));
        gap:13px!important;margin:9px 0 13px!important;
    }
    .st-key-minutas_tipo_registro [role="radiogroup"] > label {
        display:flex!important;align-items:flex-start!important;cursor:pointer!important;
        min-height:108px!important;border:1px solid #CCD9DF!important;
        border-radius:14px!important;background:linear-gradient(145deg,#fff,#F6F9F9)!important;
        padding:17px 15px!important;box-shadow:0 5px 13px rgba(24,55,75,.06)!important;
        transition:border-color .18s ease,background .18s ease,transform .18s ease!important;
    }
    .st-key-minutas_tipo_registro [role="radiogroup"] > label:hover {
        transform:translateY(-1px);border-color:#A78A5C!important;
    }
    .st-key-minutas_tipo_registro [role="radiogroup"] > label:has(input:checked),
    .st-key-minutas_tipo_registro [role="radiogroup"] > label:has([aria-checked="true"]) {
        border:2px solid #C29B5D!important;
        background:linear-gradient(130deg,#FCF8EF,#F1F6F7)!important;
        box-shadow:0 7px 17px rgba(149,112,55,.13)!important;
    }
    .st-key-minutas_tipo_registro [role="radiogroup"] > label p {
        white-space:pre-line!important;line-height:1.65!important;
        font-size:13px!important;color:#24475B!important;font-weight:600!important;
    }
    @media (max-width:720px) {
        .st-key-minutas_tipo_registro [role="radiogroup"] {grid-template-columns:1fr!important;}
        .st-key-minutas_tipo_registro [role="radiogroup"] > label {min-height:85px!important;}
        .setf-min-hero {padding-right:24px!important;min-height:155px!important;}
    }
    /* Indicadores de Minutas: cuatro tarjetas coherentes con el sistema SETF. */
    .setf-min-kpi {position:relative;box-sizing:border-box;
        border:1px solid var(--min-border,#C9D6DB);
        border-left:5px solid var(--min-accent,#628498);
        background:var(--min-bg,rgba(248,250,250,.98));
        padding:18px 17px 16px 19px;border-radius:14px;min-height:128px;
        box-shadow:0 7px 19px rgba(35,59,71,.075);
        display:flex;flex-direction:column;justify-content:center;gap:10px;
        transition:box-shadow .15s ease,transform .15s ease}
    .setf-min-kpi:hover {box-shadow:0 9px 23px rgba(35,59,71,.12);transform:translateY(-1px)}
    .setf-min-kpi strong {display:block;font-size:clamp(12px, 1.04vw, 18px);
        font-weight:800;color:#153447;overflow-wrap:anywhere;line-height:1.4;
        letter-spacing:.1px}
    .setf-min-kpi span {display:block;font-size:12px;font-weight:600;color:#516B79;line-height:1.45}
    .setf-min-kpi-ultima {--min-accent:#B39262;--min-border:#DACAB4;--min-bg:rgba(250,246,239,.97)}
    .setf-min-kpi-siguiente {--min-accent:#4F788F;--min-border:#B7CBD5;--min-bg:rgba(236,245,249,.97)}
    .setf-min-kpi-propias {--min-accent:#55816E;--min-border:#B6D2C3;--min-bg:rgba(238,247,242,.97)}
    .setf-min-kpi-otras {--min-accent:#8A7799;--min-border:#D3C7DC;--min-bg:rgba(246,242,249,.97)}
    /* Estilo premium localizado: no cambia otras páginas ni DG */
    .st-key-setf_minutas_registro, .st-key-setf_minutas_consulta {
        border:1px solid rgba(172,153,119,.40)!important;
        border-radius:18px!important;
        background:linear-gradient(150deg,rgba(255,255,255,.97),rgba(247,246,242,.96))!important;
        box-shadow:0 12px 30px rgba(22,46,62,.10)!important;
        padding:19px 22px 23px!important;
        margin:15px 0 21px!important;
    }
    .setf-min-section-tag {font-size:10px;letter-spacing:2px;
        color:#A17A3F;font-weight:850;text-transform:uppercase;margin-bottom:5px;}
    .setf-min-section-title {font-size:24px;line-height:1.2;
        color:#17394F;font-weight:850;margin:0 0 5px;}
    .setf-min-section-sub {font-size:13px;color:#617787;margin:0 0 15px;}
    .setf-min-registro-chip {font-size:11px;font-weight:800;color:#2D617A;
        background:#EDF4F7;border:1px solid #C3D5DC;border-radius:999px;
        padding:5px 12px;display:inline-block;margin:0 0 12px;}
    .st-key-setf_minutas_registro [data-testid="stExpander"] {
        border:1px solid #CCD9DF!important;border-radius:12px!important;
        background:rgba(255,255,255,.92)!important;overflow:hidden;
    }
    .st-key-setf_minutas_registro [data-testid="stExpander"] summary {
        background:#EDF4F7!important;color:#17394F!important;
        font-weight:750!important;padding:10px 15px!important;
    }
    .st-key-setf_minutas_registro [data-testid="stAlert"] {
        border-left:4px solid #4F788F;border-radius:10px;
    }
    .st-key-setf_minutas_registro [data-testid="stForm"] {
        border:1px solid #DAE1E3!important;border-radius:12px!important;
        background:#FCFCFA!important;padding:16px!important;
    }
    .st-key-setf_minutas_registro input,
    .st-key-setf_minutas_registro textarea,
    .st-key-setf_minutas_consulta input {border-radius:9px!important;}
    .st-key-setf_minutas_registro [data-testid="stFormSubmitButton"] button {
        background:linear-gradient(105deg,#173A51,#2B6379)!important;
        border:1px solid #1E4B62!important;color:#FFF!important;
        border-radius:10px!important;min-height:45px!important;font-weight:800!important;
        box-shadow:0 5px 14px rgba(22,57,77,.17)!important;
    }
    .st-key-setf_minutas_registro [data-testid="stFormSubmitButton"] button:hover {
        background:linear-gradient(105deg,#21516B,#33758E)!important;
    }
    .st-key-setf_minutas_consulta [data-testid="stTextInput"] input,
    .st-key-setf_minutas_consulta [data-baseweb="select"]>div {
        background:#FFF!important;border:1px solid #C9D5DC!important;
        border-radius:10px!important;min-height:42px!important;
    }
    .st-key-setf_minutas_consulta [data-testid="stTextInput"] input:focus {
        border-color:#527E92!important;box-shadow:0 0 0 2px #BBD2DC!important;
    }
    @media(max-width:780px) {
        .st-key-setf_minutas_registro,.st-key-setf_minutas_consulta {padding:13px!important;}
        .setf-min-section-title {font-size:21px;}
    }

    /* Minutas SETF · registro y vínculos: acabado institucional */
    .st-key-setf_minutas_registro [data-testid="stRadio"] > label {font-weight:750!important;}
    .st-key-setf_minutas_registro [data-testid="stRadio"] [role="radiogroup"] {
        gap:12px!important;align-items:stretch!important;
    }
    .st-key-setf_minutas_registro [data-testid="stRadio"] [role="radiogroup"] > label {
        flex:1 1 0!important;min-height:100px!important;
        border-radius:15px!important;border:1px solid #CDDCE3!important;
        background:linear-gradient(145deg,#FFFFFF,#F4F8F9)!important;
        padding:17px 15px!important;box-shadow:0 6px 17px rgba(20,51,70,.05)!important;
        font-weight:700!important;align-items:flex-start!important;
    }
    .st-key-setf_minutas_registro [data-testid="stRadio"] [role="radiogroup"] > label:has(input:checked) {
        border-color:#BE9554!important;background:linear-gradient(140deg,#FFFDF8,#F9F2E5)!important;
        box-shadow:0 5px 18px rgba(178,131,65,.15)!important;
    }
    .st-key-setf_minutas_registro [data-testid="stTextInput"] input:focus {
        border-color:#527E92!important;box-shadow:0 0 0 2px #BAD1DC!important;
    }
    [class*="st-key-setf_minutas_vinculo_"] [data-testid="stExpander"] {
        border:1px solid #CAD9E1!important;border-radius:12px!important;
        background:linear-gradient(110deg,rgba(255,255,255,.97),rgba(247,250,250,.95))!important;
        box-shadow:0 5px 15px rgba(22,48,64,.05)!important;overflow:hidden;
    }
    [class*="st-key-setf_minutas_vinculo_"] [data-testid="stExpander"] summary {
        font-weight:750!important;color:#22516B!important;padding:11px 15px!important;
    }
    [class*="st-key-setf_minutas_vinculo_"] [data-testid="stExpander"] summary:hover {
        background:rgba(234,243,247,.65)!important;
    }
    .setf-min-link-intro {display:flex;flex-direction:column;gap:3px;padding:6px 2px 11px;}
    .setf-min-link-intro strong {color:#183A50;font-size:14px;}
    .setf-min-link-intro span {color:#617787;font-size:12px;line-height:1.5;}
    [class*="st-key-setf_minutas_vinculo_"] [data-testid="stFormSubmitButton"] button {
        min-height:43px!important;background:linear-gradient(105deg,#1A4259,#27657E)!important;
        color:white!important;border:0!important;border-radius:10px!important;font-weight:750!important;
    }
    @media(max-width:750px) {
        .st-key-setf_minutas_registro [data-testid="stRadio"] [role="radiogroup"] {flex-direction:column!important;}
        .st-key-setf_minutas_registro [data-testid="stRadio"] [role="radiogroup"] > label {min-height:70px!important;}
    }
    /* Tarjeta única con acción a la derecha, sólo para minutas SETF. */
    [class*="st-key-setf_minutas_item_"] {
        background:linear-gradient(115deg,rgba(255,255,255,.98),rgba(250,249,246,.98))!important;
        border:1px solid #CCD9DE!important;border-left:4px solid #4E788A!important;
        border-radius:14px!important;padding:13px 17px!important;
        box-shadow:0 5px 15px rgba(20,49,65,.075)!important;
        margin:12px 0 4px!important;
    }
    .setf-min-item-text strong {font-size:14px;color:#153B50;overflow-wrap:anywhere;}
    .setf-min-item-text p {font-size:12px;color:#576F7E;margin:5px 0 0;line-height:1.5;}
    [class*="st-key-setf_minutas_item_"] [data-testid="stHorizontalBlock"] {
        align-items:center!important;
    }
    [class*="st-key-setf_minutas_item_"] [data-testid="stLinkButton"] a {
        background:linear-gradient(110deg,#C88727,#D69D45)!important;
        color:#fff!important;border:1px solid #B7791E!important;
        border-radius:10px!important;min-height:44px!important;
        font-weight:800!important;box-shadow:0 5px 12px rgba(155,92,22,.20)!important;
        display:flex!important;align-items:center!important;justify-content:center!important;
    }
    [class*="st-key-setf_minutas_item_"] [data-testid="stLinkButton"] a:hover {
        background:linear-gradient(110deg,#AD6C18,#C48427)!important;
        color:#fff!important;
    }
    [class*="st-key-setf_minutas_item_"] [data-testid="stButton"] button:disabled {
        background:#F0F1F1!important;color:#84919A!important;
        border:1px solid #D9DEE2!important;border-radius:10px!important;
        min-height:44px!important;
    }
    [class*="st-key-setf_minutas_vinculo_"] [data-testid="stExpander"] {
        border:1px solid #DBE2E4!important;border-radius:10px!important;
        background:rgba(255,255,255,.88)!important;
    }
    @media(max-width:750px) {
        [class*="st-key-setf_minutas_item_"] {padding:11px!important;}
    }
    .setf-min-item {background:rgba(253,253,251,.96);border:1px solid #D5DCDF;
        border-left:4px solid #6B8C9B;border-radius:12px;padding:15px 18px;margin:8px 0}
    .setf-min-item strong {color:#1B3A4F;font-size:14px}
    .setf-min-item p {color:#647985;font-size:12px;margin:5px 0 0}

    /* Panel de información: reemplaza captions sueltos con un único bloque.
       Estilos locales utilizados sólo por _modulo_minutas_setf. */
    .setf-min-info {margin:15px 0 18px!important;padding:16px 20px 15px!important;
        background:linear-gradient(120deg,rgba(251,248,242,.97),rgba(248,250,249,.97));
        border:1px solid #DDD1BE;border-left:4px solid #B99560;border-radius:14px;
        box-shadow:0 6px 20px rgba(23,48,62,.07);color:#24455A;}
    .setf-min-info-head {display:flex;align-items:center;gap:12px;
        padding-bottom:11px;border-bottom:1px solid #E7DFD2;}
    .setf-min-info-symbol {display:flex;align-items:center;justify-content:center;
        flex:0 0 39px;height:39px;border-radius:10px;background:#EEE4D3;
        color:#98703E;font-weight:800;font-size:21px;}
    .setf-min-info-kicker {display:block;color:#977343;font-size:10px;
        letter-spacing:1.7px;font-weight:850;line-height:1.5;}
    .setf-min-info-title {color:#19394B;font-weight:850;font-size:17px;line-height:1.35;}
    .setf-min-info-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
        gap:18px;padding-top:13px;}
    .setf-min-info-block {display:flex;align-items:flex-start;gap:11px;min-width:0;}
    .setf-min-info-ico {display:flex;justify-content:center;align-items:center;
        flex:0 0 29px;height:29px;border-radius:8px;background:#E8EFF1;
        color:#3E6B80;font-size:11px;font-weight:850;}
    .setf-min-info-name {font-size:14px;font-weight:800;color:#24475B;margin:2px 0 4px;}
    .setf-min-info-block p {font-size:12.5px!important;line-height:1.6!important;
        color:#516775!important;margin:0!important;}
    .setf-min-info-block strong {font-weight:800;color:#254E66;}
    .setf-min-info-note {margin-top:7px;padding:6px 9px;background:#F8EFE1;
        border-radius:7px;border-left:3px solid #C5A16C;
        color:#805F33;font-size:11.5px;line-height:1.5;}
    .setf-min-info-foot {margin-top:7px!important;color:#71858F!important;font-size:11.5px!important;}
    @media (max-width:900px) {
       .setf-min-info-grid {grid-template-columns:1fr;gap:14px;}
       .setf-min-info {padding:14px!important;margin:12px 0 15px!important;}
    }

    /* Pulido final de Minutas SETF: compacto y legible. */
    .st-key-cabecera_documental { margin-bottom:-38px!important; }
    .setf-min-hero { margin-top:0!important;margin-bottom:15px!important; }
    .setf-min-kpi {min-height:145px!important;padding:17px 16px 17px 19px!important;gap:9px!important;}
    .setf-min-kpi strong {font-size:clamp(14px,1.12vw,19px)!important;line-height:1.32!important;}
    .setf-min-kpi span {font-size:12.5px!important;}
    .setf-min-kpi-siguiente {
        border:2px solid #BB8D42!important;border-left:6px solid #D6A24D!important;
        background:linear-gradient(135deg,#183C51 0%,#27566C 100%)!important;
        box-shadow:0 12px 24px rgba(18,57,79,.22)!important;
        transform:translateY(-3px);
    }
    .setf-min-kpi-siguiente strong {color:#FFFFFF!important;font-size:clamp(14px,1.16vw,20px)!important;}
    .setf-min-kpi-siguiente span {color:#F2D5A4!important;font-weight:800!important;}
    .setf-min-kpi-siguiente::before {content:'PRÓXIMA MINUTA';display:block;
        font-size:9px;letter-spacing:1.5px;font-weight:850;color:#F3CC8C;}
    .setf-min-kpi-siguiente:hover {transform:translateY(-3px)!important;}
    [class*="st-key-setf_minutas_item_"] {padding:18px 20px!important;margin:12px 0 14px!important;}
    .setf-min-item-text strong {font-size:17px!important;line-height:1.35!important;}
    .setf-min-item-text p {font-size:14px!important;line-height:1.55!important;margin:7px 0 0!important;}
    .setf-min-item-text p:last-child {font-size:12.5px!important;color:#627787!important;}
    [class*="st-key-setf_minutas_item_"] [data-testid="stExpander"] {
        border:1px solid #DBE3E6!important;border-radius:10px!important;
        background:#F7F9F9!important;margin-top:9px!important;overflow:hidden!important;
    }
    [class*="st-key-setf_minutas_item_"] [data-testid="stExpander"] summary {
        color:#235269!important;font-weight:750!important;padding:9px 13px!important;
        font-size:13px!important;
    }
    [class*="st-key-setf_minutas_item_"] [data-testid="stFormSubmitButton"] button {
        background:#1F5069!important;color:white!important;border-radius:10px!important;
        border:0!important;font-weight:800!important;
    }
    @media (max-width:850px) {
        .st-key-cabecera_documental {margin-bottom:-15px!important;}
        .setf-min-kpi-siguiente {transform:none!important;}
    }
    </style>""", unsafe_allow_html=True)
    # Archivo decorativo opcional. Si no está en assets, el encabezado conserva
    # el degradado institucional y la aplicación sigue funcionando.
    imagen_hero_minutas = _imagen_selector("hero_setf_minutas.png")
    if imagen_hero_minutas:
        st.markdown(
            '<style>.setf-min-hero {background-image:'
            'linear-gradient(90deg,#162E40 0%,rgba(22,46,64,.98) 39%,rgba(22,46,64,.62) 69%,rgba(22,46,64,.12) 100%),'
            f'url("{imagen_hero_minutas}")!important;'
            'background-position:center,right center!important;'
            'background-size:cover,auto 118%!important;'
            'background-repeat:no-repeat!important;}</style>',
            unsafe_allow_html=True,
        )
    st.markdown("""<section class="setf-min-hero">
      <div class="tag">SISTEMAS ELÉCTRICOS · CONTROL DOCUMENTAL</div>
      <h2>Minutas de reunión</h2>
      <p>Control de consecutivos, consulta de reuniones y seguimiento documental</p>
    </section>""", unsafe_allow_html=True)
    try:
        id_libro = _id_libro_minutas_setf()
        libro, hoja = _hoja_minutas_setf()
        actuales = _filas_minutas_setf(hoja)
        externas_app = _filas_minutas_externas_setf(libro)
        total = actuales + externas_app
        vinculos_manuales = _vinculos_minutas_setf(libro)
        for registro in total:
            enlace_manual = vinculos_manuales.get(_clave_vinculo_minuta_setf(registro))
            if _clave_vinculo_minuta_setf(registro) in vinculos_manuales:
                registro["enlace"] = enlace_manual
        codigo, fila_reservada, prox = _consecutivo_siguiente_setf(actuales, 2026)
    except Exception as error:
        st.error(f"No se pudo abrir el control de minutas de SETF: {error}")
        if isinstance(error, ValueError) and "minutas_spreadsheet_id" in str(error):
            st.info("Falta configurar el ID del libro Control Interno SETF ")
            st.code('[setf]\nminutas_spreadsheet_id = "ID_DEL_CONTROL_INTERNO_SETF"', language="toml")
        else:
            st.info("Revisa que la cuenta de servicio tenga acceso al libro "
                    "Control Interno SETF y que exista la pestaña MINUTAS. "
                    "Los registros no se modificaron.")
        return
    propias = [r for r in actuales if r["tipo"] == "Propia SETF" and r["registrada"]]
    otras = [r for r in total if r["tipo"] == "Otra reunión" and r["registrada"]]
    k1,k2,k3,k4 = st.columns(4, gap="small")
    for columna,valor,etiqueta,clase in [
        (k1, f"AIFA-ITEyPF-SETF-{max([58, *[r['consecutivo'] for r in propias if r['anio'] == 2026]]):04d}-2026", "Última minuta propia · 2026", "ultima"),
        (k2, codigo, "Siguiente consecutivo · 2026", "siguiente"),
        (k3, str(len(propias)), "Minutas propias registradas", "propias"),
        (k4, str(len(otras)), "Minutas de otras reuniones", "otras"),
    ]:
        with columna:
            st.markdown(f'<div class="setf-min-kpi setf-min-kpi-{clase}"><strong>{valor}</strong><span>{etiqueta}</span></div>',unsafe_allow_html=True)
    # Panel visual explicativo de Minutas SETF: mantiene las mismas advertencias
    # y las reglas de numeración/Drive; sólo cambia su presentación.
    st.markdown("""<section class="setf-min-info" aria-label="Información sobre el control de minutas">
      <div class="setf-min-info-head">
        <span class="setf-min-info-symbol">▤</span>
        <div><span class="setf-min-info-kicker">INFORMACIÓN DEL SISTEMA</span>
        <div class="setf-min-info-title">Control y gestión de minutas</div></div>
      </div>
      <div class="setf-min-info-grid">
        <div class="setf-min-info-block">
          <span class="setf-min-info-ico">01</span>
          <div><div class="setf-min-info-name">Numeración institucional</div>
          <p>Las nuevas minutas utilizan la clave <strong>AIFA-ITEyPF-SETF</strong>.
          Los registros históricos con CAT siguen consultables sin modificar sus claves.</p>
          <div class="setf-min-info-note">Antes de registrar la minuta 0059, comprueba que
          su fórmula en Google Sheets utilice SETF y no CAT.</div></div>
        </div>
        <div class="setf-min-info-block">
          <span class="setf-min-info-ico">02</span>
          <div><div class="setf-min-info-name">Almacenamiento documental</div>
          <p>La hoja <strong>MINUTAS</strong> conserva el registro maestro de consecutivos.
          Guarda tus documentos en Drive y vincula sus enlaces manualmente desde aquí.</p>
          <p class="setf-min-info-foot">Los vínculos se registran por separado, sin alterar la tabla original.</p></div>
        </div>
      </div>
    </section>""", unsafe_allow_html=True)
    with st.container(key="setf_minutas_registro"):
        st.markdown('''''''''<div class="setf-min-section-tag">REGISTRO INSTITUCIONAL</div>
        <div class="setf-min-section-title">Nueva minuta de reunión</div>
        <div class="setf-min-section-sub">Captura los datos, conserva el consecutivo automático y vincula tu documento en Drive.</div>
        <span class="setf-min-registro-chip">SETF · Control de minutas</span>''''''''' , unsafe_allow_html=True)
        with st.expander("＋ Registrar nueva minuta", expanded=False):
            st.markdown('<div class="setf-min-section-tag">CLASIFICACIÓN DOCUMENTAL</div>', unsafe_allow_html=True)
            st.markdown('**¿Qué tipo de minuta deseas registrar?**')
            tipo = st.radio(
                "Tipo de registro",
                ["Minuta propia SETF", "Minuta de otra reunión"],
                horizontal=True,
                key="minutas_tipo_registro",
                label_visibility="collapsed",
                format_func=lambda valor: (
                    "▣  Minuta propia SETF\nReunión de la mesa · consecutivo automático"
                    if valor == "Minuta propia SETF" else
                    "♧  Minuta de otra reunión\nDocumento externo · no consume consecutivo"
                ),
            )
            if tipo == "Minuta propia SETF":
                st.info(f"Número propuesto: **{codigo}**" + (
                    " · ya preparado en la hoja." if fila_reservada else
                    " · se generará al registrar si no existe una fórmula que lo reserve."
                ))
            with st.form(key="form_nueva_minuta_setf", clear_on_submit=False):
                clave_externa = ""
                if tipo != "Minuta propia SETF":
                    clave_externa = st.text_input("Clave de otra institución (opcional)", key="minutas_clave_externa")
                asunto = st.text_area("Asunto de la reunión", key="minutas_asunto_nuevo", height=82)
                c1,c2 = st.columns([1,2])
                with c1:
                    fecha = st.date_input("Fecha de la reunión", key="minutas_fecha_nueva")
                with c2:
                    proyecto = _proyecto_form_minuta_setf("nuevo")
                    if not proyecto:
                        st.caption("Escribe el nombre del proyecto para continuar.")
                enlace = st.text_input("Enlace al documento (opcional, HTTPS)", key="minutas_enlace_nuevo",
                                       placeholder="https://drive.google.com/...")
                enviar = st.form_submit_button("Registrar minuta", type="primary", use_container_width=True)
            if enviar:
                try:
                    if tipo == "Minuta propia SETF":
                        guardado = _guardar_minuta_propia_setf(asunto, fecha, proyecto, enlace)
                    else:
                        guardado = _guardar_minuta_externa_setf(clave_externa, asunto, fecha, proyecto, enlace)
                    st.session_state["minutas_confirmacion_setf"] = f"Minuta registrada: {guardado}"
                    st.rerun()
                except Exception as error:
                    st.error(f"No se pudo confirmar el registro: {error}. Verifica Sheets antes de reintentar.")
    if st.session_state.get("minutas_confirmacion_setf"):
        st.success(st.session_state.pop("minutas_confirmacion_setf"))
    with st.container(key="setf_minutas_consulta"):
        st.markdown('''<div class="setf-min-section-tag">ARCHIVO Y CONSULTA DOCUMENTAL</div>
            <div class="setf-min-section-title">Consulta de minutas</div>
            <div class="setf-min-section-sub">Encuentra reuniones por clave, asunto o proyecto y filtra sus resultados.</div>''', unsafe_allow_html=True)
        c1,c2,c3 = st.columns([3,1.45,1.65], gap="small")
        with c1:
            buscar = st.text_input("Buscar por clave, asunto o proyecto", placeholder="Buscar minuta...", key="minutas_buscar_setf")
        with c2:
            filtro_tipo = st.selectbox("Tipo de minuta", ["Todas", "Propias SETF", "Otras reuniones"], key="minutas_filtro_tipo")
        with c3:
            proyectos = sorted({r["proyecto"] for r in total if r["proyecto"]})
            filtro_proyecto = st.selectbox("Proyecto", ["Todos", *proyectos], key="minutas_filtro_proyecto")
    registros = [r for r in total if r["registrada"]]
    if filtro_tipo == "Propias SETF":
        registros = [r for r in registros if r["tipo"] == "Propia SETF"]
    elif filtro_tipo == "Otras reuniones":
        registros = [r for r in registros if r["tipo"] == "Otra reunión"]
    if filtro_proyecto != "Todos":
        registros = [r for r in registros if r["proyecto"] == filtro_proyecto]
    if buscar.strip():
        buscado = buscar.casefold().strip()
        registros = [r for r in registros if buscado in " ".join(
            (r["numero"],r["asunto"],r["fecha"],r["proyecto"])
        ).casefold()]
    registros.sort(key=lambda r: (str(r["fecha"]), r["numero"]), reverse=True)
    st.caption(f"{len(registros)} minuta(s) encontradas · Los folios preparados sin asunto no se consideran registros.")
    for r in registros[:100]:
        titulo = html.escape(r["numero"] or "Sin clave")
        asunto = html.escape(r["asunto"] or "Sin asunto")
        proyecto = html.escape(r["proyecto"] or "Sin proyecto")
        fecha = html.escape(r["fecha"] or "Sin fecha")
        with st.container(key=f"setf_minutas_item_{r['origen']}_{r['fila']}"):
            columna_datos, columna_accion = st.columns([4.5, 1.15], gap="medium", vertical_alignment="center")
            with columna_datos:
                st.markdown(f'<div class="setf-min-item-text"><strong>{titulo}</strong>'
                            f'<p>{asunto}</p><p>{fecha} · {proyecto} · {html.escape(r["tipo"])}</p></div>',
                            unsafe_allow_html=True)
            with columna_accion:
                if r.get("enlace"):
                    st.link_button("Abrir minuta ↗", r["enlace"], use_container_width=True)
                else:
                    st.button("Sin enlace", disabled=True, use_container_width=True,
                              key=f"sin_enlace_minuta_{r['origen']}_{r['fila']}")
            with st.expander("🔗  Vincular o actualizar documento", expanded=False):
                st.markdown(
                    '<div class="setf-min-link-intro"><strong>Enlace directo a la minuta</strong>'
                    '<span>Guarda aquí la dirección del PDF o documento de Drive. '
                    'Al guardarla, podrás abrirlo desde el botón naranja de la tarjeta.</span></div>',
                    unsafe_allow_html=True,
                )
                with st.form(key=f"vinculo_minuta_{r['origen']}_{r['fila']}", clear_on_submit=False):
                    link_nuevo = st.text_input("URL del documento (HTTPS)",
                                               value=r.get("enlace", ""),
                                               key=f"url_minuta_{r['origen']}_{r['fila']}",
                                               placeholder="https://drive.google.com/file/d/...")
                    enviar_vinculo = st.form_submit_button("Guardar enlace de la minuta", use_container_width=True)
                if r.get("enlace"):
                    clave_quitar = f"quitar_vinculo_{r['origen']}_{r['fila']}"
                    if st.button("Quitar enlace de esta minuta", key=clave_quitar,
                                 use_container_width=True, type="secondary"):
                        st.session_state[f"confirmar_{clave_quitar}"] = True
                    if st.session_state.get(f"confirmar_{clave_quitar}", False):
                        st.warning("Se quitará el enlace de la aplicación. La minuta y el archivo en Drive se conservarán.")
                        col_si, col_no = st.columns(2)
                        with col_si:
                            if st.button("Confirmar eliminación del enlace", key=f"si_{clave_quitar}",
                                         use_container_width=True):
                                try:
                                    _eliminar_vinculo_minuta_setf(r)
                                    st.session_state.pop(f"confirmar_{clave_quitar}", None)
                                    st.success("Enlace desvinculado correctamente.")
                                    st.rerun()
                                except Exception as error:
                                    st.error(f"No se pudo desvincular: {error}")
                        with col_no:
                            if st.button("Cancelar", key=f"no_{clave_quitar}", use_container_width=True):
                                st.session_state.pop(f"confirmar_{clave_quitar}", None)
                                st.rerun()
                if enviar_vinculo:
                    try:
                        _guardar_vinculo_minuta_setf(r, link_nuevo)
                        st.success("Enlace guardado. La minuta se abrirá directamente desde su tarjeta.")
                        st.rerun()
                    except Exception as error:
                        st.error(f"No se pudo guardar el enlace: {error}")
    if not registros:
        st.info("No hay minutas que coincidan con los filtros seleccionados.")
    elif len(registros) > 100:
        st.caption("Se muestran las primeras 100 minutas. Utiliza los filtros para encontrar otras.")


def _modulo_minutas_diseno_pendiente():
    st.markdown("""<div style="background:linear-gradient(115deg,#162D40,#365464);
        color:white;border-left:5px solid #C4A16A;border-radius:15px;padding:26px 30px">
        <div style="font-size:11px;letter-spacing:2px;color:#D7B57A">MESA DE DISEÑO GEOMÉTRICO</div>
        <h2 style="color:#fff;margin:9px 0">Minutas de reunión</h2>
        <p style="color:#DFE7EA;margin:0">Módulo reservado para la próxima etapa.</p></div>""",
        unsafe_allow_html=True)
    st.info("La gestión de minutas de Diseño Geométrico se implementará más adelante. "
            "Este apartado no consulta ni modifica la información de SETF.")


# =========================================================
# SETF · GESTIÓN DE REUNIONES (ACCESO COMPARTIDO)
# =========================================================
# Datos maestros: pestaña nueva REUNIONES_APP del mismo libro de Minutas SETF.
# La pestaña histórica Reuniones permanece completamente intacta.
# Metadatos y auditoría nuevos: hojas independientes en el MISMO libro.
# No compartir con Diseño Geométrico ni exigir rol de administrador.

_REUNIONES_COLUMNAS = (
    "dia", "fecha", "hora", "asunto", "ubicacion", "asignacion", "antecedente",
)
_REUNIONES_ESTADOS = ("Programada", "Realizada", "Cancelada", "Por confirmar")
_REUNIONES_META_TITULO = "REUNIONES_APP_ESTADOS"
_REUNIONES_META_CABECERAS = [
    "Fila origen", "Huella", "Estado", "ID", "Creado por", "Creado el",
    "Modificado por", "Modificado el",
]
_REUNIONES_HIST_TITULO = "REUNIONES_APP_HISTORIAL"
_REUNIONES_HIST_CABECERAS = [
    "ID", "Fila origen", "Acción", "Usuario", "Fecha y hora", "Antes (JSON)", "Después (JSON)",
]
_REUNIONES_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_REUNIONES_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _exigir_reuniones_setf():
    exigir_sesion()
    if _area_activa_validada() != "setf":
        raise PermissionError("Gestión de reuniones está disponible únicamente para SETF.")
    # Todos los usuarios autenticados y adscritos a SETF pueden leer y escribir.


def _reuniones_ahora():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Mexico_City"))


def _reuniones_fecha(valor):
    from datetime import date as _date
    if isinstance(valor, _date):
        return valor
    cadena = str(valor or "").strip()
    if not cadena:
        return None
    for patron in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(cadena, patron).date()
        except ValueError:
            pass
    return None


def _reuniones_hora(valor):
    from datetime import time as _time
    if isinstance(valor, _time):
        return valor.replace(second=0, microsecond=0)
    cadena = str(valor or "").strip()
    if not cadena:
        return None
    # El archivo histórico almacena la hora como 1000, 1600, 900, etc.
    if re.fullmatch(r"\d{1,4}(?:\.0+)?", cadena):
        digitos = cadena.split(".")[0].zfill(4)
        hh, mm = int(digitos[:-2]), int(digitos[-2:])
    else:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::00)?", cadena)
        if not m:
            return None
        hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return _time(hh, mm)
    return None


def _reuniones_hoja():
    """Agenda NUEVA. No lee ni modifica la pestaña histórica Reuniones."""
    _exigir_reuniones_setf()
    libro = _cliente_gspread_minutas_setf().open_by_key(_id_libro_minutas_setf())
    titulo = "REUNIONES_APP"
    cabeceras = ["Día", "Fecha", "Hora", "Asunto", "Ubicación", "Asignación", "Antecedente"]
    try:
        ws = libro.worksheet(titulo)
    except gspread.WorksheetNotFound:
        # La cuenta de servicio necesita permisos de edición en Control Interno SETF.
        ws = libro.add_worksheet(title=titulo, rows=1000, cols=7)
        ws.update(range_name="A1:G1", values=[cabeceras], value_input_option="RAW")
        libro.batch_update({"requests": [{"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": .09, "green": .17, "blue": .27},
                                              "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"}}]})
        ws.freeze(rows=1)
    encabezados = (ws.get("A1:G1", value_render_option="FORMATTED_VALUE") or [[]])[0]
    canonico = lambda v: "".join(ch for ch in unicodedata.normalize("NFKD", str(v or "").strip().lower()) if ch.isascii() and ch.isalpha())
    if tuple(canonico(x) for x in (list(encabezados)+[""]*7)[:7]) != _REUNIONES_COLUMNAS:
        raise ValueError("La pestaña REUNIONES_APP ya existe con columnas distintas. No se sobrescribió ningún dato.")
    return libro, ws


def _reuniones_hoja_auxiliar(libro, titulo, cabeceras, crear=False):
    try:
        ws = libro.worksheet(titulo)
    except gspread.WorksheetNotFound:
        if not crear:
            return None
        ws = libro.add_worksheet(title=titulo, rows=1000, cols=max(len(cabeceras), 8))
        ws.append_row(cabeceras, value_input_option="RAW")
    if ws.row_values(1)[:len(cabeceras)] != cabeceras:
        raise ValueError(f"La pestaña {titulo} tiene encabezados incompatibles; no se realizó el registro.")
    return ws


def _reuniones_huella(fila):
    # No usa número de fila para detectar registros movidos en la hoja original.
    datos = (list(fila) + [""] * 7)[:7]
    origen = "|".join(str(datos[i] or "").strip().casefold() for i in (1, 2, 3))
    return hashlib.sha256(origen.encode("utf-8")).hexdigest()


def _reuniones_colores_heredados(libro, ws, ultimo_renglon):
    """Detecta verde=realizada, amarillo=programada sin reescribir colores antiguos.

    Si la API no permite consultar formatos, se muestra estado 'Por confirmar'
    para reuniones vencidas, nunca se afirma que ocurrieron solo por la fecha.
    """
    if ultimo_renglon < 5:
        return {}
    try:
        datos = libro.fetch_sheet_metadata(params={
            "ranges": f"'{ws.title}'!A5:D{ultimo_renglon}",
            "includeGridData": True,
            "fields": "sheets(data(startRow,rowData(values(effectiveFormat(backgroundColor)))))",
        })
        colores = {}
        for sheet_data in datos.get("sheets", []):
            for grid in sheet_data.get("data", []):
                inicio = int(grid.get("startRow", 4)) + 1
                for idx, fila in enumerate(grid.get("rowData", [])):
                    for celda in fila.get("values", []):
                        rgb = celda.get("effectiveFormat", {}).get("backgroundColor", {})
                        if not rgb:
                            continue
                        r, g, b = (float(rgb.get(x, 0)) for x in ("red", "green", "blue"))
                        if .26 < g and g > r * 1.25 and g > b * 1.20:
                            colores[inicio + idx] = "Realizada"
                            break
                        if r > .70 and g > .63 and b < .45:
                            colores[inicio + idx] = "Programada"
                            break
        return colores
    except (ValueError, KeyError, TypeError, AttributeError, requests.RequestException, gspread.exceptions.APIError):
        return {}


def _reuniones_leer(libro, ws):
    _exigir_reuniones_setf()
    # A2:G contiene los registros de la pestaña nueva; A1:G1 son encabezados.
    registros_originales = ws.get("A2:G", value_render_option="FORMATTED_VALUE")
    auxiliares = _reuniones_hoja_auxiliar(
        libro, _REUNIONES_META_TITULO, _REUNIONES_META_CABECERAS,
    )
    metadatos = {}
    if auxiliares is not None:
        for idx, fila in enumerate(auxiliares.get_all_values()[1:], start=2):
            valores = (list(fila) + [""] * 8)[:8]
            try:
                num_fila = int(valores[0])
            except ValueError:
                continue
            if num_fila in metadatos:
                raise ValueError("Hay filas duplicadas en REUNIONES_APP_ESTADOS. Corrígelas antes de editar.")
            metadatos[num_fila] = {"meta_fila": idx, "huella": valores[1],
                                    "estado": valores[2], "id": valores[3],
                                    "creado_por": valores[4], "creado_el": valores[5]}
    colores = {}  # La pestaña nueva no depende del coloreado de la histórica.
    hoy = _reuniones_ahora().date()
    salida = []
    for numero_fila, fila in enumerate(registros_originales, start=2):
        valores = (list(fila) + [""] * 7)[:7]
        fecha = _reuniones_fecha(valores[1])
        if not fecha or not any(str(c).strip() for c in valores[2:7]):
            continue  # Ignorar títulos, leyenda de colores y filas vacías.
        hora = _reuniones_hora(valores[2])
        huella = _reuniones_huella(valores)
        meta = metadatos.get(numero_fila)
        # Si se reordenó la hoja o se cambió desde Sheets, no asociar un
        # estado ajeno solo porque sigue ocupando la misma posición.
        meta = meta if meta and meta["huella"] == huella else None
        estado = (meta["estado"] if meta and meta["estado"] in _REUNIONES_ESTADOS
                  else colores.get(numero_fila, "Programada" if fecha >= hoy else "Por confirmar"))
        salida.append({
            "fila": numero_fila, "datos": valores, "huella": huella,
            "fecha": fecha, "hora": hora, "asunto": valores[3],
            "ubicacion": valores[4], "responsable": valores[5],
            "antecedente": valores[6], "estado": estado,
            "id": meta["id"] if meta else "", "meta": meta,
        })
    return salida


def _reuniones_revisar_datos(fecha, hora, asunto, ubicacion, responsable, antecedente, estado):
    if not fecha or not hora or not str(asunto or "").strip():
        raise ValueError("La fecha, la hora y el asunto son obligatorios.")
    if not str(ubicacion or "").strip() or not str(responsable or "").strip():
        raise ValueError("Indica ubicación y responsable.")
    if estado not in _REUNIONES_ESTADOS:
        raise ValueError("Selecciona un estado válido.")
    datos = [str(x or "").strip() for x in (asunto, ubicacion, responsable, antecedente)]
    if any(len(dato) > 1300 for dato in datos):
        raise ValueError("Alguno de los campos de texto supera el límite permitido.")
    if datos[3].lower().startswith(("http:", "https:")):
        _url_minuta_segura(datos[3])
    return [
        _REUNIONES_DIAS[fecha.weekday()].capitalize(),
        fecha.strftime("%d/%m/%Y"),
        hora.strftime("%H%M"),
        *(_texto_sheets_seguro(x) for x in datos),
    ]


def _reuniones_fila_libre(ws):
    """Escribe en una fila VACÍA después del último evento, sin mover la leyenda.

    No usa append_row: esa operación podría colocar datos después de la
    leyenda de estados u otras notas presentes al final de la pestaña.
    """
    todas = ws.get("A2:G", value_render_option="FORMATTED_VALUE")
    ultima_reunion = 1
    for idx, datos in enumerate(todas, start=2):
        fila = (list(datos) + [""] * 7)[:7]
        if _reuniones_fecha(fila[1]) and any(str(x).strip() for x in fila[2:7]):
            ultima_reunion = idx
    fila_destino = ultima_reunion + 1
    while True:
        fila = list(todas[fila_destino - 2]) if fila_destino - 2 < len(todas) else []
        if not any(str(x).strip() for x in fila[:7]):
            # Una fórmula que muestra "" también parece vacía. No sobrescribirla.
            valores_formula = ws.get(f"A{fila_destino}:G{fila_destino}",
                                    value_render_option="FORMULA")
            if not any(str(x).strip() for x in (valores_formula[0] if valores_formula else [])):
                break
        fila_destino += 1
        if fila_destino > max(10000, ws.row_count + 100):
            raise ValueError("No se encontró una fila disponible para registrar la reunión.")
    if fila_destino > ws.row_count:
        ws.add_rows(max(20, fila_destino - ws.row_count))
    return fila_destino


def _reuniones_color_fila(libro, ws, fila, estado):
    # Solo cambia el fondo: respeta validación de responsables, fuentes y enlaces.
    paleta = {
        "Programada": {"red": 1, "green": .94, "blue": .47},
        "Realizada": {"red": .35, "green": .73, "blue": .47},
        "Cancelada": {"red": .96, "green": .79, "blue": .76},
        "Por confirmar": {"red": .89, "green": .91, "blue": .94},
    }
    libro.batch_update({"requests": [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": fila - 1, "endRowIndex": fila,
                  "startColumnIndex": 0, "endColumnIndex": 7},
        "cell": {"userEnteredFormat": {"backgroundColor": paleta[estado]}},
        "fields": "userEnteredFormat.backgroundColor",
    }}]})


def _reuniones_guardar_metadata(libro, fila, huella, estado, id_registro, anterior):
    ws = _reuniones_hoja_auxiliar(
        libro, _REUNIONES_META_TITULO, _REUNIONES_META_CABECERAS, crear=True,
    )
    usuario = identificador_sesion()
    momento = _reuniones_ahora().isoformat(timespec="seconds")
    datos = [str(fila), huella, estado, id_registro,
             anterior.get("creado_por", usuario) if anterior else usuario,
             anterior.get("creado_el", momento) if anterior else momento,
             usuario, momento]
    if anterior and anterior.get("meta_fila"):
        indice = anterior["meta_fila"]
        ws.update(range_name=f"A{indice}:H{indice}", values=[datos], value_input_option="RAW")
    else:
        # Detectar metadatos agregados desde la lectura, antes de anexar.
        ocupadas = {str(f[0]).strip() for f in ws.get_all_values()[1:] if f and str(f[0]).strip()}
        if str(fila) in ocupadas:
            raise ValueError("La reunión ya tiene metadatos nuevos. Actualiza la página para evitar duplicados.")
        ws.append_row(datos, value_input_option="RAW")


def _reuniones_registrar_historial(libro, id_registro, fila, accion, antes, despues):
    ws = _reuniones_hoja_auxiliar(
        libro, _REUNIONES_HIST_TITULO, _REUNIONES_HIST_CABECERAS, crear=True,
    )
    ws.append_row([
        id_registro, str(fila), accion, identificador_sesion(),
        _reuniones_ahora().isoformat(timespec="seconds"),
        json.dumps(antes, ensure_ascii=False),
        json.dumps(despues, ensure_ascii=False),
    ], value_input_option="RAW")


def _reuniones_guardar_nueva(fecha, hora, asunto, ubicacion, responsable, antecedente, estado):
    _exigir_reuniones_setf()
    datos = _reuniones_revisar_datos(fecha, hora, asunto, ubicacion, responsable, antecedente, estado)
    with _candado_minutas_instancia():
        libro, ws = _reuniones_hoja()
        fila = _reuniones_fila_libre(ws)
        # Google Sheets puede devolver una lista con celdas vacías: [["", ...]].
        # La presencia de una fila en la respuesta no significa que esté ocupada.
        comprobacion = ws.get(f"A{fila}:G{fila}", value_render_option="FORMULA")
        if any(str(celda).strip() for renglon in comprobacion for celda in renglon):
            raise RuntimeError("La fila de destino ya fue utilizada. Actualiza y vuelve a intentarlo.")
        ws.update(range_name=f"A{fila}:G{fila}", values=[datos], value_input_option="USER_ENTERED")
        # Sheets puede mostrar 0900 como 900 o convertir la fecha a número
        # de serie. Usar el valor FORMATEADO real para enlazar metadatos.
        fila_guardada = ws.get(f"A{fila}:G{fila}", value_render_option="FORMATTED_VALUE")
        valores_guardados = (list(fila_guardada[0]) + [""] * 7)[:7] if fila_guardada else datos
        id_registro = secrets.token_hex(12)
        try:
            _reuniones_guardar_metadata(libro, fila, _reuniones_huella(valores_guardados), estado, id_registro, None)
        except Exception as exc:
            raise RuntimeError(f"La reunión SÍ se guardó en la fila {fila} de REUNIONES_APP, pero "
                               "falló el estado auxiliar. No vuelvas a dar de alta el mismo evento. "
                               f"Detalle: {exc}") from exc
        try:
            _reuniones_registrar_historial(libro, id_registro, fila, "Creación", {},
                                           {"datos": valores_guardados, "estado": estado})
        except Exception as exc:
            raise RuntimeError(f"La reunión SÍ se guardó en la fila {fila}; no repitas el alta. "
                               f"Falló únicamente la bitácora: {exc}") from exc
        try:
            _reuniones_color_fila(libro, ws, fila, estado)
        except Exception:
            pass  # El color es decorativo: no invalida el registro ni su historial.
    return fila


def _reuniones_guardar_edicion(original, fecha, hora, asunto, ubicacion, responsable, antecedente, estado):
    _exigir_reuniones_setf()
    datos = _reuniones_revisar_datos(fecha, hora, asunto, ubicacion, responsable, antecedente, estado)
    with _candado_minutas_instancia():
        libro, ws = _reuniones_hoja()
        fila = int(original["fila"])
        valores_actuales = ws.get(f"A{fila}:G{fila}", value_render_option="FORMATTED_VALUE")
        valores_actuales = (list(valores_actuales[0]) + [""] * 7)[:7] if valores_actuales else [""] * 7
        if valores_actuales != original["datos"]:
            raise ValueError("Otra persona cambió esta reunión desde que la abriste. "
                             "Actualiza la página antes de editar para evitar sobrescribir cambios.")
        # No volver a escribir la columna G si mantiene su texto: una celda con
        # hipervínculo enriquecido perdería la URL al reemplazarla por su etiqueta.
        actualizaciones = []
        for indice, (antes, despues) in enumerate(zip(original["datos"], datos)):
            if indice == 0:
                # Nombre del día se calcula desde la fecha, no depende de idiomas del servidor.
                cambiado = antes.strip().casefold() != despues.strip().casefold()
            elif indice == 1:
                cambiado = _reuniones_fecha(antes) != fecha
            elif indice == 2:
                cambiado = _reuniones_hora(antes) != hora
            else:
                cambiado = antes.strip() != despues.strip()
            if cambiado:
                columna = "ABCDEFG"[indice]
                actualizaciones.append({"range": f"{columna}{fila}", "values": [[despues]]})
        if not actualizaciones and original["estado"] == estado:
            return False
        if actualizaciones:
            originales_formula = ws.get(f"A{fila}:G{fila}", value_render_option="FORMULA")
            originales_formula = (list(originales_formula[0]) + [""] * 7)[:7] if originales_formula else [""] * 7
            for cambio in actualizaciones:
                indice = "ABCDEFG".index(cambio["range"][0])
                if str(originales_formula[indice]).lstrip().startswith("="):
                    raise ValueError("El campo que intentas editar está calculado con una fórmula "
                                     "en Google Sheets. No se reemplazará esa fórmula.")
            ws.batch_update(actualizaciones, value_input_option="USER_ENTERED")
        # Volver a consultar datos reales para calcular una huella consistente
        # aun si Sheets reformateó la fecha o el texto al guardarlo.
        valores_guardados = ws.get(f"A{fila}:G{fila}", value_render_option="FORMATTED_VALUE")
        valores_guardados = (list(valores_guardados[0]) + [""] * 7)[:7] if valores_guardados else [""] * 7
        id_registro = original.get("id") or secrets.token_hex(12)
        try:
            _reuniones_guardar_metadata(libro, fila, _reuniones_huella(valores_guardados),
                                        estado, id_registro, original.get("meta"))
        except Exception as exc:
            raise RuntimeError("El contenido de la reunión pudo quedar actualizado, pero "
                               f"no se guardó su estado auxiliar. Revisa la fila {fila} antes de reintentar: {exc}") from exc
        try:
            _reuniones_registrar_historial(libro, id_registro, fila, "Edición",
                {"datos": original["datos"], "estado": original["estado"]},
                {"datos": valores_guardados, "estado": estado})
        except Exception as exc:
            raise RuntimeError("La reunión SÍ se actualizó; no repitas el cambio sin revisar. "
                               f"Falló únicamente el historial: {exc}") from exc
        try:
            _reuniones_color_fila(libro, ws, fila, estado)
        except Exception:
            pass
    return True


def _reuniones_historial(libro, limite=40):
    ws = _reuniones_hoja_auxiliar(libro, _REUNIONES_HIST_TITULO, _REUNIONES_HIST_CABECERAS)
    if ws is None:
        return []
    historial = []
    for f in reversed(ws.get_all_values()[1:]):
        datos = (list(f) + [""] * 7)[:7]
        if not datos[0]:
            continue
        try:
            antes = json.loads(datos[5]) if datos[5] else {}
            despues = json.loads(datos[6]) if datos[6] else {}
            anteriores = antes.get("datos") or []
            posteriores = despues.get("datos") or []
            asunto = posteriores[3] if len(posteriores) > 3 else ""
            if datos[2] == "Creación":
                cambios = "Registro inicial"
            else:
                campos = ["Día", "Fecha", "Hora", "Asunto", "Ubicación", "Asignación", "Antecedente"]
                modificados = [nombre for i, nombre in enumerate(campos)
                               if i < len(anteriores) and i < len(posteriores)
                               and anteriores[i] != posteriores[i]]
                if antes.get("estado") != despues.get("estado"):
                    modificados.append("Estado")
                cambios = ", ".join(modificados) or "Actualización"
        except (ValueError, TypeError, KeyError, IndexError):
            asunto, cambios = "", "Sin detalle"
        historial.append({"Fecha": datos[4], "Usuario": datos[3], "Acción": datos[2],
                          "Reunión": asunto, "Campos modificados": cambios, "Fila": datos[1]})
        if len(historial) >= limite:
            break
    return historial


def _reuniones_calendario_html(reuniones, anio, mes):
    import calendar
    calendario = calendar.Calendar(firstweekday=0)
    dias = calendario.monthdatescalendar(anio, mes)
    por_fecha = {}
    for reunion in reuniones:
        por_fecha.setdefault(reunion["fecha"], []).append(reunion)
    partes = ['<div class="setf-reun-cal">']
    for nombre in ("LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM"):
        partes.append(f'<div class="setf-reun-cal-head">{nombre}</div>')
    hoy = _reuniones_ahora().date()
    for semana in dias:
        for dia in semana:
            clase = " fuera" if dia.month != mes else ""
            clase += " actual" if dia == hoy else ""
            partes.append(f'<div class="setf-reun-cal-dia{clase}"><div class="numero">{dia.day}</div>')
            eventos = sorted(por_fecha.get(dia, []), key=lambda r: r["hora"] or datetime.min.time())
            for reunion in eventos[:3]:
                estado_css = {"Programada": "programada", "Realizada": "realizada",
                              "Cancelada": "cancelada", "Por confirmar": "pendiente"}[reunion["estado"]]
                titulo = html.escape(str(reunion["asunto"] or "Sin asunto"), quote=True)
                etiqueta = html.escape((reunion["hora"].strftime("%H:%M") + " · " if reunion["hora"] else "") +
                                      (reunion["asunto"] or "Sin asunto")[:48])
                partes.append(f'<div class="setf-reun-cal-event {estado_css}" title="{titulo}">{etiqueta}</div>')
            if len(eventos) > 3:
                partes.append(f'<div class="setf-reun-cal-mas">+{len(eventos) - 3} más</div>')
            partes.append('</div>')
    partes.append('</div>')
    return "".join(partes)


def _reuniones_ficha_calendario(reunion):
    """Ficha existente de la reunión; sirve para un evento y para la agenda del día."""
    titulo = str(reunion.get("asunto") or "Reunión sin asunto")
    hora = reunion["hora"].strftime("%H:%M") if reunion.get("hora") else "S/H"
    st.markdown("#### Información de la reunión")
    st.markdown(f"**{html.escape(titulo)}**")
    st.caption(f"{_REUNIONES_DIAS[reunion['fecha'].weekday()].capitalize()} "
               f"{reunion['fecha']:%d/%m/%Y} · {hora}")
    st.markdown(f"**Estado:** {html.escape(str(reunion['estado']))}")
    st.markdown(f"**Ubicación:** {html.escape(str(reunion.get('ubicacion') or 'Por definir'))}")
    st.markdown(f"**Responsable:** {html.escape(str(reunion.get('responsable') or 'Sin asignar'))}")
    antecedente = str(reunion.get("antecedente") or "Sin antecedente")
    st.markdown(f"**Antecedente:** {html.escape(antecedente)}")
    if antecedente.lower().startswith("https://"):
        st.link_button("Abrir antecedente ↗", antecedente, use_container_width=True)
    st.caption("Para modificarla, utiliza la pestaña Editar reuniones.")


def _reuniones_calendario_interactivo(reuniones, anio, mes):
    """Calendario compacto: hasta 3 juntas por día, con agenda completa sin recargar Sheets."""
    import calendar
    meses = calendar.Calendar(firstweekday=0).monthdatescalendar(anio, mes)
    por_fecha = {}
    for reunion in reuniones:
        por_fecha.setdefault(reunion["fecha"], []).append(reunion)

    encabezados = st.columns(7, gap="small")
    for columna, rotulo in zip(encabezados, ("LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM")):
        with columna:
            st.markdown(
                f'<div class="reun-cal-encabezado notranslate" translate="no" lang="es-MX">{rotulo}</div>',
                unsafe_allow_html=True,
            )

    hoy = _reuniones_ahora().date()
    for semana in meses:
        columnas = st.columns(7, gap="small")
        for columna, dia in zip(columnas, semana):
            clase = "fuera" if dia.month != mes else "actual" if dia == hoy else "normal"
            with columna:
                with st.container(key=f"reun_dia_{dia:%Y%m%d}_{clase}"):
                    st.markdown(
                        f'<div class="reun-cal-numero notranslate" translate="no" lang="es-MX">{dia.day}</div>',
                        unsafe_allow_html=True,
                    )
                    eventos = sorted(
                        por_fecha.get(dia, []),
                        key=lambda r: (r["hora"] or datetime.min.time(), r.get("fila", 0)),
                    )
                    # No permitir que cuatro o más juntas estiren toda la semana.
                    for reunion in eventos[:3]:
                        estado = {"Programada": "programada", "Realizada": "realizada",
                                  "Cancelada": "cancelada", "Por confirmar": "pendiente"}.get(
                                      reunion["estado"], "pendiente")
                        hora = reunion["hora"].strftime("%H:%M") if reunion["hora"] else "S/H"
                        titulo = str(reunion["asunto"] or "Reunión sin asunto")
                        etiqueta = f"{hora} · {titulo[:26]}{'…' if len(titulo) > 26 else ''}"
                        with st.container(key=f"reun_evento_{estado}_{reunion['fila']}"):
                            with st.popover(etiqueta, use_container_width=True):
                                _reuniones_ficha_calendario(reunion)

                    if len(eventos) > 3:
                        adicionales = len(eventos) - 3
                        # Popover nativo: abre en el navegador sin forzar un rerun
                        # ni una nueva lectura de Google Sheets.
                        with st.container(key=f"reun_eventos_mas_{dia:%Y%m%d}"):
                            with st.popover(f"+{adicionales} {'reuniones' if adicionales != 1 else 'reunión'} más",
                                            use_container_width=True):
                                st.markdown(f"### Agenda del día · {dia:%d/%m/%Y}")
                                st.caption(f"{len(eventos)} reuniones ordenadas por hora")
                                for reunion in eventos:
                                    hora = reunion["hora"].strftime("%H:%M") if reunion["hora"] else "S/H"
                                    titulo = str(reunion["asunto"] or "Reunión sin asunto")
                                    with st.expander(f"{hora} · {titulo}", expanded=False):
                                        _reuniones_ficha_calendario(reunion)

def _responsables_reuniones_setf():
    """Integrantes SETF configurados en Secrets, incluidas cuentas de dos áreas."""
    _exigir_reuniones_setf()
    nombres = []
    usuarios = st.secrets["usuarios"]
    for identificador, datos in usuarios.items():
        if not hasattr(datos, "get"):
            continue
        es_setf = str(datos.get("area", "diseno")).strip().lower() == "setf"
        es_doble_area = str(identificador).strip().lower() in USUARIOS_DOS_AREAS
        if not (es_setf or es_doble_area):
            continue
        # Los tratamientos institucionales son etiquetas explícitas: no dependen
        # del traductor del navegador ni cambian el nombre guardado en Sheets.
        uid = str(identificador).strip().lower()
        tratamientos = {"alondra": "Tte. Alondra", "francely": "Lic. Francely",
                        "arturo": "Ing. Arturo"}
        nombre = tratamientos.get(uid) or str(datos.get("responsable") or datos.get("nombre") or "").strip()
        if nombre and nombre.casefold() not in [x.casefold() for x in nombres]:
            nombres.append(nombre)
    # Catálogo adicional configurable sin editar Python.
    for nombre in st.secrets.get("setf", {}).get("responsables", []):
        nombre = str(nombre).strip()
        if nombre and nombre.casefold() not in [x.casefold() for x in nombres]:
            nombres.append(nombre)
    # También normaliza nombres configurados en la lista adicional [setf].responsables.
    def etiqueta_institucional(valor):
        normal = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode().casefold()
        if "alondra" in normal:
            return "Tte. Alondra"
        if "francely" in normal:
            return "Lic. Francely"
        if "arturo" in normal:
            return "Ing. Arturo"
        return valor
    salida = []
    for nombre in sorted(nombres, key=str.casefold):
        etiqueta = etiqueta_institucional(nombre)
        if etiqueta.casefold() not in [x.casefold() for x in salida]:
            salida.append(etiqueta)
    return sorted(salida, key=str.casefold)


def _modulo_reuniones_setf():
    _exigir_reuniones_setf()
    # El fondo de Reuniones se instala para TODO el módulo, no solo la tabla.
    # El CSS se emite únicamente cuando esta sección está activa: no afecta a DG,
    # Minutas, Oficios ni la pantalla de selección de áreas.
    ruta_fondo_reuniones = ASSETS_DIR / "fondo_consulta_reuniones_setf.png"
    if ruta_fondo_reuniones.is_file():
        imagen_fondo_reuniones = base64.b64encode(
            ruta_fondo_reuniones.read_bytes()
        ).decode("ascii")
        st.markdown(
            '<style>'
            '[data-testid="stAppViewContainer"] {'
            'background-image:linear-gradient(rgba(250,249,246,.10),rgba(250,249,246,.10)),'
            f'url("data:image/png;base64,{imagen_fondo_reuniones}") !important;'
            'background-color:#FAF9F6!important;'
            'background-size:cover!important;'
            'background-position:center center!important;'
            'background-repeat:no-repeat!important;'
            'background-attachment:fixed!important;'
            '}'
            '[data-testid="stHeader"] {background:transparent!important;}'
            '</style>',
            unsafe_allow_html=True,
        )
    else:
        st.warning(
            "No se encontró el fondo del módulo Reuniones en: "
            + str(ruta_fondo_reuniones)
            + ". Comprueba el nombre y que el archivo esté en la carpeta assets del proyecto."
        )
    st.markdown("""
    <style>
    .setf-reun-hero {background:linear-gradient(110deg,#142b3e,#23495a);border-radius:16px;
        border:1px solid rgba(197,157,91,.65);border-left:5px solid #d2a866;
        color:white;padding:24px 31px;margin:12px 0 22px;box-shadow:0 7px 24px rgba(17,43,58,.13)}
    .setf-reun-hero small {font-size:11px;color:#dcc49d!important;letter-spacing:2.2px;font-weight:700}
    .setf-reun-hero h2 {color:#ffffff!important;font-size:30px;font-weight:800;margin:8px 0 4px}
    .setf-reun-hero p {color:#d1dde4!important;font-size:14px;margin:0}
    .setf-reun-kpi {background:rgba(255,255,255,.94);border:1px solid #dfe4e6;border-top:3px solid #c6a16c;
        border-radius:13px;padding:15px 19px;box-shadow:0 5px 16px rgba(19,44,60,.06);margin-bottom:12px}
    .setf-reun-kpi strong {font-size:29px;display:block;color:#173347;line-height:1.2}
    .setf-reun-kpi span {color:#647788;font-size:12px}
    .setf-reun-seccion {font-size:21px;font-weight:800;color:#183348;margin:13px 0}
    .setf-reun-sub {color:#607485;font-size:13px;margin:-8px 0 14px}
    .setf-reun-cal {display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border:1px solid #d9e1e5;
        border-radius:14px;overflow:hidden;background:#fff;box-shadow:0 5px 18px rgba(18,40,55,.07)}
    .setf-reun-cal-head {padding:12px 4px;background:#1b3b50;color:#f6f6f4;text-align:center;
        font-size:11px;font-weight:750;letter-spacing:.8px}
    .setf-reun-cal-dia {min-height:105px;padding:8px;border-right:1px solid #e5e9ec;
        border-bottom:1px solid #e5e9ec;overflow:hidden}
    .setf-reun-cal-dia.fuera {background:#f4f6f7;color:#9aa6ad}
    .setf-reun-cal-dia.actual {box-shadow:inset 0 0 0 2px #c8a369}
    .setf-reun-cal-dia .numero {font-size:12px;font-weight:800;color:#3d5361;margin:0 0 6px}
    .setf-reun-cal-event {font-size:10px;line-height:1.3;border-radius:5px;padding:5px;
        margin-bottom:4px;white-space:normal;overflow-wrap:anywhere}
    .setf-reun-cal-event.programada {background:#fbf0ba;color:#6d5825}
    .setf-reun-cal-event.realizada {background:#d8f0dd;color:#275c38}
    .setf-reun-cal-event.cancelada {background:#fce3e0;color:#953e39}
    .setf-reun-cal-event.pendiente {background:#ebedf2;color:#4f5b6b}
    .setf-reun-cal-mas {font-size:10px;color:#536c81;font-weight:750}
    .setf-reun-prox {background:#fff;border:1px solid #e1e4e5;border-left:4px solid #bc995e;
        border-radius:11px;padding:14px 17px;margin:7px 0;box-shadow:0 3px 13px rgba(15,39,55,.05)}
    .setf-reun-prox strong {color:#173349;font-size:15px}
    .setf-reun-prox p {color:#637687;font-size:12px;margin:4px 0 0}
    @media(max-width:700px) {.setf-reun-cal-dia {min-height:76px;padding:3px}
        .setf-reun-cal-event {font-size:9px;padding:3px}.setf-reun-hero h2 {font-size:24px}}
    /* Identidad propia: agenda editorial clara en lugar de la cabecera azul de oficios. */
    .setf-reun-hero {position:relative;isolation:isolate;overflow:hidden;display:flex;min-height:236px;
      align-items:center;justify-content:space-between;background:linear-gradient(112deg,#fcfaf5 0%,#f3eee3 60%,#e9e6de 100%)!important;
      border:1px solid #dcd5c6!important;border-radius:20px!important;border-left:0!important;
      padding:28px 42px!important;box-shadow:0 15px 35px rgba(29,48,59,.10)!important;
      margin:12px 0 24px!important;color:#1b3849!important}
    .setf-reun-hero:before {content:"";position:absolute;top:0;left:0;bottom:0;width:5px;background:linear-gradient(#c39347,#e2c68e,#af813d)}
    .setf-reun-hero:after {content:"";position:absolute;inset:0;opacity:.5;pointer-events:none;
      background:repeating-linear-gradient(90deg,transparent 0 95px,rgba(170,143,99,.09) 96px,transparent 97px)}
    .setf-reun-hero-copy {position:relative;z-index:2;max-width:650px}
    .setf-reun-eyebrow {display:flex;align-items:center;gap:10px;color:#9b723d!important;font-size:10px;
      font-weight:850;letter-spacing:2px;margin-bottom:21px}
    .setf-reun-eyebrow-line {width:28px;height:2px;background:#b68a4b}
    .setf-reun-hero-topline {font-size:11px;font-weight:800;letter-spacing:3px;color:#607484;margin-bottom:1px}
    .setf-reun-hero h2 {font-size:clamp(36px,4vw,55px)!important;letter-spacing:-2px!important;
      line-height:1.12!important;color:#19394b!important;margin:5px 0 12px!important;font-weight:850!important}
    .setf-reun-hero-dot {color:#bb914b}
    .setf-reun-hero p {color:#556a76!important;font-size:14px!important;line-height:1.65!important;
      max-width:540px;margin:0!important}
    .setf-reun-hero-foot {font-weight:800;color:#456171;font-size:10px;letter-spacing:1.4px;margin-top:24px}
    .setf-reun-live-dot {display:inline-block;width:7px;height:7px;border-radius:50%;background:#74a498;margin-right:7px}
    .setf-reun-foot-sep {color:#b68b4b;margin:0 13px}
    .setf-reun-art {width:240px;min-width:190px;height:210px;position:relative;z-index:1;margin-left:20px}
    .setf-reun-art-ring {position:absolute;border:1px solid rgba(163,130,78,.24);border-radius:50%;top:50%;left:50%;transform:translate(-50%,-50%)}
    .setf-reun-art-ring.ring-a {width:245px;height:245px}.setf-reun-art-ring.ring-b {width:195px;height:195px}
    .setf-reun-art-tile {position:absolute;top:17px;left:57px;width:131px;height:147px;border:1px solid #ccba98;
      border-radius:14px;background:linear-gradient(155deg,#fffefa,#f3ecdc);box-shadow:0 15px 32px rgba(48,63,66,.16);
      display:flex;flex-direction:column;align-items:center;padding-top:23px;transform:rotate(7deg)}
    .setf-reun-art-tile .tile-bind {position:absolute;left:22px;right:22px;top:-6px;height:12px;
      background:repeating-linear-gradient(90deg,#b58e50 0 6px,transparent 6px 21px)}
    .setf-reun-art-tile span {font-size:11px;color:#ac874d;font-weight:800;letter-spacing:3px}
    .setf-reun-art-tile strong {font-size:65px;color:#19394b;line-height:1.13;font-weight:850}
    .setf-reun-art-tile em {font-style:normal;font-size:9px;letter-spacing:2px;color:#8d806b;font-weight:800}
    .setf-reun-art-tag {position:absolute;bottom:2px;right:0;color:#977a56;letter-spacing:1px;font-size:8px;line-height:1.8;font-weight:750}
    /* Barra de opciones: controles segmentados con selección contrastante y sin subrayado básico. */
    div[data-testid="stTabs"] > div[data-baseweb="tab-list"] {background:rgba(248,247,242,.94)!important;
      padding:6px!important;border:1px solid #deddd5;border-radius:13px!important;gap:5px!important;
      box-shadow:0 5px 18px rgba(22,45,58,.06);margin:12px 0 18px}
    div[data-testid="stTabs"] button[data-baseweb="tab"] {border-radius:9px!important;border:1px solid transparent!important;
      background:transparent!important;color:#526a79!important;padding:12px 18px!important;font-weight:750!important;
      transition:background .18s ease,color .18s ease,box-shadow .18s ease!important}
    div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {background:#ece8de!important;color:#203c4e!important}
    div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {background:#1b3c50!important;
      color:#fff!important;box-shadow:0 5px 12px rgba(18,49,68,.2)!important;border-color:#274c60!important}
    div[data-testid="stTabs"] [data-baseweb="tab-highlight"],div[data-testid="stTabs"] [data-baseweb="tab-border"] {display:none!important}
    /* Botones secundarios del calendario, sin alterar el menú lateral ni botones de otros módulos. */
    [class*="st-key-reun_mes_anterior"] button,[class*="st-key-reun_mes_siguiente"] button {
      border:1px solid #c9c4b9!important;background:linear-gradient(180deg,#fffefa,#f2efe8)!important;
      color:#294558!important;box-shadow:0 4px 11px rgba(24,47,58,.09)!important;border-radius:11px!important;
      min-height:42px!important;font-size:13px!important;font-weight:750!important;
      transition:transform .15s ease,background .15s ease,box-shadow .15s ease!important}
    [class*="st-key-reun_mes_anterior"] button:hover,[class*="st-key-reun_mes_siguiente"] button:hover {
      color:#fff!important;background:#21495e!important;transform:translateY(-2px)!important;
      border-color:#21495e!important;box-shadow:0 7px 15px rgba(24,47,58,.17)!important}
    [class*="st-key-reun_mes_anterior"] button:focus-visible,[class*="st-key-reun_mes_siguiente"] button:focus-visible {
      outline:2px solid #bd9657!important;outline-offset:2px!important}
    .setf-reun-kpi {border-top:0!important;border-left:3px solid #bb9255!important;
      border-radius:14px!important;box-shadow:0 8px 20px rgba(23,45,58,.08)!important}
    .setf-reun-seccion {letter-spacing:-.4px}
    @media(max-width:850px){.setf-reun-art {display:none}.setf-reun-hero {padding:25px!important;min-height:195px}
      div[data-testid="stTabs"] button[data-baseweb="tab"]{padding:10px!important;font-size:12px!important}}

    /* REUNIONES v3: densidad visual y acabado institucional, únicamente en esta vista. */
    .setf-reun-hero {min-height:0!important;height:auto!important;padding:19px 30px!important;
      margin:2px 0 11px!important;border-radius:16px!important;box-shadow:0 9px 25px rgba(24,49,63,.09)!important}
    .setf-reun-eyebrow {margin-bottom:8px!important;font-size:9px!important}
    .setf-reun-hero-topline {font-size:9px!important;letter-spacing:2.1px!important}
    .setf-reun-hero h2 {font-size:clamp(29px,3.2vw,40px)!important;letter-spacing:-1px!important;
      margin:2px 0 7px!important}
    .setf-reun-hero p {font-size:12px!important;line-height:1.45!important;max-width:630px}
    .setf-reun-hero-foot {margin-top:12px!important;font-size:9px!important}
    .setf-reun-art {height:132px!important;width:164px!important;min-width:164px!important;margin-left:10px!important}
    .setf-reun-art-ring.ring-a {width:164px!important;height:164px!important}
    .setf-reun-art-ring.ring-b {width:129px!important;height:129px!important}
    .setf-reun-art-tile {left:45px!important;top:8px!important;width:84px!important;height:104px!important;
      padding-top:15px!important;border-radius:10px!important}
    .setf-reun-art-tile strong {font-size:42px!important}
    .setf-reun-art-tile span {font-size:8px!important}
    .setf-reun-art-tile em {font-size:7px!important}
    .setf-reun-art-tag {font-size:6px!important;bottom:0!important}
    .setf-reun-kpi {padding:11px 15px!important;margin:0 0 3px!important;min-height:74px;
      border-left:3px solid #b68c46!important;box-shadow:0 3px 13px rgba(24,47,58,.07)!important}
    .setf-reun-kpi strong {font-size:24px!important}
    .setf-reun-kpi span {font-size:11px!important}
    /* Streamlit usa role=tab en versiones recientes; incluir ambos selectores. */
    [data-testid="stTabs"] [data-baseweb="tab-list"],
    [data-testid="stTabs"] [role="tablist"] {
      display:flex!important;flex-wrap:wrap!important;gap:9px!important;
      padding:10px!important;background:rgba(247,244,237,.92)!important;
      border:1px solid #ded3bd!important;border-radius:15px!important;
      box-shadow:0 7px 22px rgba(20,40,53,.08)!important;
      margin:7px 0 15px!important;height:auto!important}
    [data-testid="stTabs"] [data-baseweb="tab"],
    [data-testid="stTabs"] [role="tab"] {
      min-height:43px!important;padding:10px 17px!important;border-radius:10px!important;
      border:1px solid #cfb784!important;background:linear-gradient(180deg,#fffdf8,#f5eddf)!important;
      color:#294252!important;font-size:13px!important;font-weight:750!important;
      box-shadow:0 3px 8px rgba(27,49,60,.07)!important;
      transition:transform .17s ease,box-shadow .17s ease,background .17s ease!important;
      text-decoration:none!important;white-space:nowrap!important}
    [data-testid="stTabs"] [data-baseweb="tab"]:hover,
    [data-testid="stTabs"] [role="tab"]:hover {
      background:linear-gradient(180deg,#e6bb70,#d49a3e)!important;color:#172d3c!important;
      box-shadow:0 6px 14px rgba(98,75,40,.18)!important;transform:translateY(-1px)}
    [data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"],
    [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
      background:linear-gradient(125deg,#183448,#29576c)!important;color:#fff!important;
      border-color:#ba944f!important;box-shadow:0 6px 16px rgba(25,53,70,.22)!important}
    [data-testid="stTabs"] [data-baseweb="tab-highlight"],
    [data-testid="stTabs"] [data-baseweb="tab-border"] {display:none!important}
    /* Formularios: panel, etiquetas y controles con acabado discreto y premium. */
    [data-testid="stForm"] {background:linear-gradient(145deg,rgba(255,253,247,.98),rgba(247,245,239,.96))!important;
      border:1px solid #dbcbae!important;border-left:4px solid #c79c55!important;
      border-radius:17px!important;padding:24px!important;
      box-shadow:0 10px 28px rgba(25,47,60,.10)!important}
    [data-testid="stForm"] label p {color:#294455!important;font-size:13px!important;font-weight:700!important}
    [data-testid="stForm"] [data-baseweb="input"]>div,
    [data-testid="stForm"] [data-baseweb="select"]>div,
    [data-testid="stForm"] [data-baseweb="base-input"] {
      background:#fffefa!important;border-color:#d4d0c5!important;border-radius:10px!important}
    [data-testid="stForm"] input:focus-visible {outline-color:#c29a5c!important}
    [data-testid="stForm"] [data-testid="stFormSubmitButton"] button,
    [data-testid="stForm"] button[kind="primary"] {
      background:linear-gradient(135deg,#d69d3f,#c58a2e)!important;color:#fff!important;
      border:1px solid #bd852b!important;border-radius:11px!important;
      min-height:47px!important;font-weight:800!important;font-size:14px!important;
      box-shadow:0 6px 15px rgba(145,103,39,.20)!important;
      transition:transform .18s ease,box-shadow .18s ease!important}
    [data-testid="stForm"] [data-testid="stFormSubmitButton"] button:hover,
    [data-testid="stForm"] button[kind="primary"]:hover {
      background:linear-gradient(135deg,#bd8432,#aa7223)!important;
      box-shadow:0 9px 19px rgba(145,103,39,.27)!important;transform:translateY(-1px)}
    [class*="st-key-reun_mes_anterior"] button,[class*="st-key-reun_mes_siguiente"] button {
      background:linear-gradient(135deg,#fff9ed,#eddbb9)!important;
      border:1px solid #c79c55!important;color:#294354!important;border-radius:10px!important;
      min-height:41px!important;box-shadow:0 4px 10px rgba(27,49,60,.09)!important}
    @media(max-width:850px) {.setf-reun-hero {padding:19px 20px!important}
      .setf-reun-art {display:none!important}
      [data-testid="stTabs"] [role="tab"] {padding:9px 11px!important;font-size:11px!important}}

    /* Ajustes finos de jerarquía y tarjetas, limitados a Reuniones SETF. */
    .setf-reun-kpi {background:linear-gradient(150deg,#ffffff,#fcfaf6)!important;
       border:1px solid #dce3e6!important;border-left:3px solid #b78a49!important;
       box-shadow:0 7px 18px rgba(25,48,64,.09)!important;
       border-radius:15px!important;transition:transform .18s ease,box-shadow .18s ease!important}
    .setf-reun-kpi:hover {transform:translateY(-2px);box-shadow:0 12px 24px rgba(25,48,64,.12)!important}
    .setf-reun-kpi strong {font-size:27px!important;letter-spacing:-.4px}
    .setf-reun-prox {padding:17px 21px!important;border-left:5px solid #c59b53!important;
       border-radius:15px!important;background:linear-gradient(110deg,#fff,#fcfaf5)!important;
       box-shadow:0 9px 24px rgba(20,44,61,.10)!important;margin:11px 0!important}
    .setf-reun-prox strong {display:block;font-size:17px!important;letter-spacing:-.25px;margin:6px 0!important}
    .setf-reun-prox p {font-size:13px!important;line-height:1.6!important;color:#4e6374!important}
    .reun-prox-superior {display:flex;align-items:center;justify-content:space-between;gap:8px}
    .reun-prox-etiqueta {font-size:10px;font-weight:800;letter-spacing:1.5px;color:#a78046}
    .reun-prox-estado {background:#f7ecd5;border:1px solid #e7ce9d;border-radius:20px;
       padding:3px 9px;font-size:11px;font-weight:750;color:#785c2e}
    .reun-cal-encabezado {background:#1b3b50;color:#fff;border-radius:8px 8px 4px 4px;
       padding:12px 3px;text-align:center;font-size:11px;font-weight:800;letter-spacing:.9px}
    [class*="st-key-reun_dia_"] {background:#fff;border:1px solid #dfe5e9;border-radius:10px;
       padding:8px!important;min-height:104px;margin:0 0 5px!important;
       box-shadow:0 3px 11px rgba(30,54,70,.04)}
    [class*="st-key-reun_dia_"] [data-testid="stVerticalBlock"] {gap:5px!important}
    [class*="st-key-reun_dia_"] .reun-cal-numero {color:#284356;font-size:12px;font-weight:800}
    [class*="st-key-reun_dia_"][class*="_fuera"] {background:#f1f4f6;opacity:.72}
    [class*="st-key-reun_dia_"][class*="_actual"] {border:2px solid #c69a52;box-shadow:0 0 0 2px rgba(198,154,82,.10)}
    [class*="st-key-reun_evento_"] button {width:100%!important;white-space:normal!important;
       height:auto!important;min-height:30px!important;text-align:left!important;justify-content:flex-start!important;
       line-height:1.3!important;font-size:10px!important;border:1px solid transparent!important;
       padding:6px!important;border-radius:7px!important;font-weight:700!important;
       box-shadow:none!important;overflow-wrap:anywhere!important}
    [class*="st-key-reun_evento_programada"] button {background:#fff0b8!important;color:#745617!important}
    [class*="st-key-reun_evento_realizada"] button {background:#d9f0dd!important;color:#245f3b!important}
    [class*="st-key-reun_evento_cancelada"] button {background:#fce3e0!important;color:#993f39!important}
    [class*="st-key-reun_evento_pendiente"] button {background:#e8edf1!important;color:#516170!important}
    [class*="st-key-reun_evento_"] button:hover {filter:brightness(.96);border-color:#bd9a60!important;
       transform:translateY(-1px);box-shadow:0 3px 8px rgba(20,42,56,.12)!important}
    @media(max-width:850px){[class*="st-key-reun_dia_"]{padding:4px!important;min-height:78px}
       [class*="st-key-reun_evento_"] button{font-size:9px!important;padding:3px!important}}
    /* Indicadores compactos, con jerarquía institucional e iconografía discreta. */
    .setf-reun-kpi {position:relative!important;overflow:hidden!important;min-height:112px!important;
      padding:16px 20px 13px!important;background:linear-gradient(138deg,#ffffff 0%,#fbfaf6 100%)!important;
      border:1px solid #e0e4e6!important;border-top:3px solid #c69a54!important;border-left:1px solid #e0e4e6!important;
      box-shadow:0 8px 22px rgba(24,46,60,.10)!important;border-radius:17px!important}
    .setf-reun-kpi:after {content:"";position:absolute;right:-24px;bottom:-38px;width:105px;height:105px;
      border:1px solid rgba(185,145,79,.14);border-radius:50%;pointer-events:none}
    .setf-reun-kpi-top {display:flex;align-items:center;justify-content:space-between;gap:5px}
    .setf-reun-kpi .setf-reun-kpi-label {color:#647987!important;font-size:11px!important;
      font-weight:800!important;letter-spacing:.45px!important;text-transform:uppercase}
    .setf-reun-kpi .setf-reun-kpi-icon {width:30px;height:30px;border-radius:9px;
      display:flex;align-items:center;justify-content:center;background:#f6eddc!important;
      color:#a47b3d!important;font-size:18px!important;font-weight:800}
    .setf-reun-kpi strong {display:block;font-size:32px!important;line-height:1.15!important;
      color:#17364a!important;margin-top:1px!important;font-weight:850!important}
    .setf-reun-kpi-foot {width:33px;height:2px;border-radius:3px;background:#cca365;margin-top:6px}
    .setf-reun-kpi-realizadas {border-top-color:#6b9e8d!important}
    .setf-reun-kpi-realizadas .setf-reun-kpi-icon {background:#e7f2ed!important;color:#488b71!important}
    .setf-reun-kpi-pendientes {border-top-color:#8297a6!important}
    .setf-reun-kpi-pendientes .setf-reun-kpi-icon {background:#e8eff3!important;color:#587588!important}
    .setf-reun-hero h2 {font-size:clamp(27px,3vw,38px)!important}
    /* Ficha de próxima reunión: datos operativos como elementos principales. */
    .setf-reun-prox {padding:19px 24px 20px!important;background:linear-gradient(120deg,#fff,#faf8f2)!important;
      box-shadow:0 10px 26px rgba(22,45,62,.12)!important;border:1px solid #e0e5e6!important;
      border-left:5px solid #c39850!important}
    .setf-reun-prox strong {font-size:19px!important;color:#16344a!important;margin:12px 0 14px!important}
    .reun-prox-detalles {display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
    .reun-prox-detalle {background:#f3f6f7;border:1px solid #dde5e9;border-radius:10px;
       padding:9px 12px;min-width:118px;display:flex;align-items:center;gap:8px;color:#1c4054}
    .reun-prox-detalle.lugar {flex:1;min-width:230px}
    .reun-prox-ico {font-size:17px;color:#ad8140;font-weight:900}
    .reun-prox-micro {display:block;font-size:9px;color:#697d8a;font-weight:850;
       letter-spacing:1.1px;text-transform:uppercase}
    .reun-prox-valor {display:block;font-size:13px;color:#16364b;font-weight:800;line-height:1.35}
    .reun-prox-responsable {color:#657987;font-size:12px;margin-top:13px;font-weight:650}
    /* Contenedor de la tabla sin afectar tablas de los otros módulos. */
    .st-key-reun_tabla_institucional {background:rgba(255,255,255,.98);padding:9px!important;
       border:1px solid #d9e2e7;border-top:3px solid #bb914d;border-radius:15px;
       box-shadow:0 9px 24px rgba(20,44,62,.09);overflow:hidden}
    .st-key-reun_tabla_institucional [data-testid="stDataFrame"] {border-radius:10px!important;overflow:hidden}
    .reun-tabla-intro {display:flex;align-items:center;justify-content:space-between;
       padding:10px 11px 13px;color:#153649;font-weight:800;font-size:13px}
    .reun-tabla-intro span {font-size:10px;color:#987445;letter-spacing:1.3px;font-weight:850}
    @media(max-width:780px){.reun-prox-detalle{flex:1}.reun-prox-detalle.lugar{min-width:190px}}

    /* Consulta de reuniones SETF: scope aislado del resto de los módulos. */
    .st-key-reun_consulta_area {border-radius:20px!important;padding:26px 26px 22px!important;
      border:1px solid rgba(217,205,183,.76)!important;box-shadow:0 15px 40px rgba(22,46,59,.075)!important;
      background-color:#FAF9F6!important;background-position:center center!important;
      background-repeat:no-repeat!important;background-size:cover!important;}
    .st-key-reun_consulta_area .reun-consulta-encabezado {padding:2px 0 17px;border-bottom:1px solid #e8e0d5;margin-bottom:14px}
    .st-key-reun_consulta_area .reun-consulta-encabezado>span {color:#a17d43;font-size:10px;font-weight:850;letter-spacing:2px}
    .st-key-reun_consulta_area .reun-consulta-encabezado h3 {font-size:24px!important;letter-spacing:-.65px;
      color:#193b51!important;margin:8px 0 4px;font-weight:850}
    .st-key-reun_consulta_area .reun-consulta-encabezado p {font-size:13px;color:#647785;margin:0}
    .st-key-reun_consulta_area .reun-filtros-titulo {font-weight:800;font-size:13px;color:#223f53;margin:5px 0 6px}
    .st-key-reun_consulta_area [data-testid="stTextInput"] input,
    .st-key-reun_consulta_area [data-testid="stSelectbox"] [data-baseweb="select"] > div {background:#fff!important;
       border:1px solid #dce3e8!important;border-radius:11px!important;min-height:46px!important;
       box-shadow:0 2px 6px rgba(27,52,68,.035)!important;color:#18374a!important;}
    .st-key-reun_consulta_area [data-testid="stTextInput"] input:focus {border-color:#be9655!important;box-shadow:0 0 0 3px rgba(190,150,85,.12)!important}
    .st-key-reun_consulta_area [class*="st-key-reun_limpiar_filtros"] button {background:#fff!important;
       color:#264456!important;border:1px solid #d5c19e!important;border-radius:10px!important;
       font-weight:750!important;min-height:42px!important;box-shadow:0 3px 9px rgba(40,54,62,.06)!important;}
    .st-key-reun_consulta_area [class*="st-key-reun_limpiar_filtros"] button:hover {background:#f7f1e7!important;
       border-color:#bf9250!important;color:#153246!important}
    .st-key-reun_consulta_area .st-key-reun_tabla_institucional {background:rgba(255,255,255,.98)!important;
       border:1px solid #dde4e8!important;border-top:4px solid #b89150!important;border-radius:16px!important;
       box-shadow:0 9px 27px rgba(22,45,60,.085)!important;padding:14px!important;margin-top:14px!important}
    .st-key-reun_consulta_area .reun-tabla-intro {padding:8px 7px 15px!important;font-size:16px!important}
    .st-key-reun_consulta_area [data-testid="stDataFrame"] {border:1px solid #e2e8eb!important;border-radius:10px!important;overflow:hidden!important}
    .st-key-reun_consulta_area [data-testid="stDataFrame"] canvas {border-radius:10px!important}
    @media(max-width:720px){.st-key-reun_consulta_area {padding:15px 12px!important}
      .st-key-reun_consulta_area .reun-consulta-encabezado h3 {font-size:20px!important}}
    </style>
    <section class="setf-reun-hero notranslate" translate="no" lang="es-MX" aria-label="Agenda institucional SETF">
      <div class="setf-reun-hero-copy">
        <div class="setf-reun-eyebrow"><span class="setf-reun-eyebrow-line"></span> SETF / COORDINACIÓN INSTITUCIONAL</div>
        <div class="setf-reun-hero-topline">AGENDA Y COORDINACIÓN</div>
        <h2 class="notranslate" translate="no" lang="es-MX">Gestión de reuniones<span class="setf-reun-hero-dot">.</span></h2>
        <p>Planeación, acuerdos y seguimiento de la Mesa de Sistemas Eléctricos de Tracción Ferroviaria.</p>
        <div class="setf-reun-hero-foot"><span class="setf-reun-live-dot"></span> ESPACIO COMPARTIDO <span class="setf-reun-foot-sep">/</span> CONTROL DOCUMENTAL</div>
      </div>
      <div class="setf-reun-art" aria-hidden="true">
        <div class="setf-reun-art-ring ring-a"></div>
        <div class="setf-reun-art-ring ring-b"></div>
        <div class="setf-reun-art-tile"><div class="tile-bind"></div><span>SETF</span><strong>✦</strong><em>AGENDA</em></div>
        <div class="setf-reun-art-tag">01 — PLANIFICACIÓN<br>02 — SEGUIMIENTO</div>
      </div>
    </section>""", unsafe_allow_html=True)
    # Ajustes dedicados al calendario; no tocan formularios ni otros módulos SETF.
    st.markdown("""
    <style>
    .st-key-reun_cal_panel {
        background:rgba(255,255,255,.97)!important;
        border:1px solid #e0e5e3!important;
        border-radius:18px!important;
        padding:16px 18px 13px!important;
        box-shadow:0 9px 26px rgba(25,49,61,.09)!important;
        margin:6px 0 16px!important;
    }
    .st-key-reun_cal_panel .reun-cal-mes {
        text-align:center!important;color:#19384A!important;
        font-size:clamp(18px,1.7vw,23px)!important;
        font-weight:800!important;margin:1px 0 7px!important;
    }
    .st-key-reun_cal_panel [data-testid="stHorizontalBlock"] {
        gap:6px!important;
    }
    .st-key-reun_cal_panel .reun-cal-encabezado {
        border-radius:6px!important;padding:7px 2px!important;
        background:#1b3b50!important;font-size:10px!important;
        letter-spacing:.5px!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_dia_"] {
        background:#fff!important;border:1px solid #e2e8e8!important;
        border-radius:9px!important;
        height:129px!important;min-height:129px!important;
        max-height:129px!important;padding:6px!important;
        margin:0 0 6px!important;
        box-shadow:0 2px 5px rgba(24,50,62,.025)!important;
        overflow:visible!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_dia_"][class*="_fuera"] {
        background:#f4f6f6!important;opacity:.7;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_dia_"][class*="_actual"] {
        border:2px solid #c99b53!important;
        background:#fffaf0!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_dia_"] [data-testid="stVerticalBlock"] {
        gap:3px!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_dia_"] .reun-cal-numero {
        font-size:11px!important;font-weight:850!important;
        margin:0 0 1px!important;line-height:1.15!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_evento_"] button,
    .st-key-reun_cal_panel [class*="st-key-reun_eventos_mas_"] button {
        min-height:22px!important;max-height:24px!important;height:24px!important;
        width:100%!important;padding:3px 5px!important;
        line-height:1.05!important;font-size:10px!important;
        border-radius:5px!important;white-space:nowrap!important;
        overflow:hidden!important;text-overflow:ellipsis!important;
        text-align:left!important;justify-content:flex-start!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_evento_"] button p,
    .st-key-reun_cal_panel [class*="st-key-reun_eventos_mas_"] button p {
        white-space:nowrap!important;overflow:hidden!important;
        text-overflow:ellipsis!important;font-size:10px!important;
    }
    .st-key-reun_cal_panel [class*="st-key-reun_eventos_mas_"] button {
        background:#edf1f2!important;border:1px dashed #a3b3b9!important;
        color:#36576a!important;font-weight:750!important;
    }
    .st-key-reun_cal_panel .reun-cal-leyenda {
        display:flex;flex-wrap:wrap;gap:9px 16px;
        padding:9px 4px 0;color:#687b84;font-size:11px;font-weight:650;
        align-items:center;
    }
    .st-key-reun_cal_panel .reun-cal-leyenda span {display:flex;align-items:center;gap:6px}
    .st-key-reun_cal_panel .reun-cal-leyenda i {
        width:9px;height:9px;display:inline-block;border-radius:3px;
    }
    .st-key-reun_cal_panel .reun-cal-leyenda i.programada {background:#e6c86f}
    .st-key-reun_cal_panel .reun-cal-leyenda i.realizada {background:#90cda3}
    .st-key-reun_cal_panel .reun-cal-leyenda i.cancelada {background:#e19b91}
    .st-key-reun_cal_panel .reun-cal-leyenda i.pendiente {background:#a9b7c7}
    @media(max-width:800px) {
       .st-key-reun_cal_panel {padding:9px 7px!important}
       .st-key-reun_cal_panel [data-testid="stHorizontalBlock"] {gap:3px!important}
       .st-key-reun_cal_panel [class*="st-key-reun_dia_"] {
           padding:3px!important;height:115px!important;
           min-height:115px!important;max-height:115px!important;
       }
       .st-key-reun_cal_panel [class*="st-key-reun_evento_"] button,
       .st-key-reun_cal_panel [class*="st-key-reun_eventos_mas_"] button {
           font-size:9px!important;padding:2px!important;
       }
    }
    </style>
    """, unsafe_allow_html=True)

    # La consulta comparte ahora el mismo fondo del módulo completo.
    try:
        libro, ws = _reuniones_hoja()
        reuniones = _reuniones_leer(libro, ws)
    except Exception as error:
        st.error(f"No se pudo consultar la agenda SETF: {error}")
        st.info("Comprueba que [setf].minutas_spreadsheet_id apunte a Control Interno SETF, "
                "que la cuenta de servicio pueda editarlo y que pueda crear la pestaña REUNIONES_APP. La pestaña Reuniones original no se modifica.")
        return
    if st.session_state.pop("reun_exito", ""):
        st.success("La reunión se guardó en Control Interno SETF y se registró su historial.")

    hoy = _reuniones_ahora().date()
    from datetime import timedelta as _timedelta_reun
    lunes = hoy - _timedelta_reun(days=hoy.weekday())
    domingo = lunes + _timedelta_reun(days=6)
    programadas = sum(r["estado"] == "Programada" for r in reuniones)
    en_semana = sum(lunes <= r["fecha"] <= domingo and r["estado"] != "Cancelada" for r in reuniones)
    realizadas = sum(r["estado"] == "Realizada" for r in reuniones)
    pendientes = sum(r["estado"] == "Por confirmar" for r in reuniones)
    for columna, (cantidad, texto, simbolo, clase) in zip(st.columns(4, gap="small"), [
        (programadas, "Programadas", "◷", "programadas"),
        (en_semana, "Esta semana", "▦", "semana"),
        (realizadas, "Realizadas", "✓", "realizadas"),
        (pendientes, "Por confirmar", "◇", "pendientes"),
    ]):
        with columna:
            st.markdown(
                f'<div class="setf-reun-kpi setf-reun-kpi-{clase}">' 
                f'<div class="setf-reun-kpi-top"><span class="setf-reun-kpi-label">{texto}</span>'
                f'<span class="setf-reun-kpi-icon" aria-hidden="true">{simbolo}</span></div>'
                f'<strong>{cantidad}</strong><div class="setf-reun-kpi-foot"></div></div>',
                unsafe_allow_html=True)

    st.caption("Acceso compartido: cualquier integrante autenticado de SETF puede crear, "
               "consultar y modificar todas las reuniones. Los cambios quedan registrados por usuario.")

    pestana_agenda, pestana_registro, pestana_editar, pestana_historial = st.tabs([
        "▦ Agenda y calendario", "＋ Nueva reunión", "✎ Editar reuniones", "◷ Historial",
    ])
    # El catálogo se obtiene de las cuentas SETF, NO de las reuniones registradas.
    # Incluye a usuarios con acceso a ambas áreas y responsables adicionales de Secrets.
    responsables = _responsables_reuniones_setf()
    # Conservar responsables de reuniones históricas para consultar/editar sin perder datos.
    responsables_historicos = sorted({str(r["responsable"]).strip() for r in reuniones
                                     if str(r["responsable"]).strip()}, key=str.casefold)
    opciones_responsables = ["— Selecciona responsable —", *responsables,
                             "Otro responsable (externo)…"]
    opciones_filtro_responsables = sorted(set(responsables + responsables_historicos), key=str.casefold)

    with pestana_agenda:
        st.markdown('<div class="setf-reun-seccion">Agenda mensual</div>', unsafe_allow_html=True)
        if "reun_cal_ano" not in st.session_state:
            st.session_state.reun_cal_ano = hoy.year
            st.session_state.reun_cal_mes = hoy.month
        with st.container(key="reun_cal_panel"):
            col_prev, col_mes, col_next = st.columns([1, 5, 1], vertical_alignment="center")
            with col_prev:
                if st.button("← Anterior", key="reun_mes_anterior", use_container_width=True):
                    st.session_state.reun_cal_mes -= 1
                    if st.session_state.reun_cal_mes == 0:
                        st.session_state.reun_cal_mes = 12
                        st.session_state.reun_cal_ano -= 1
                    st.rerun()
            with col_mes:
                st.markdown(f'<h3 class="reun-cal-mes notranslate" translate="no" lang="es-MX">'
                            f'{_REUNIONES_MESES[st.session_state.reun_cal_mes - 1].capitalize()} '
                            f'{st.session_state.reun_cal_ano}</h3>', unsafe_allow_html=True)
            with col_next:
                if st.button("Siguiente →", key="reun_mes_siguiente", use_container_width=True):
                    st.session_state.reun_cal_mes += 1
                    if st.session_state.reun_cal_mes == 13:
                        st.session_state.reun_cal_mes = 1
                        st.session_state.reun_cal_ano += 1
                    st.rerun()
            _reuniones_calendario_interactivo(
                reuniones, st.session_state.reun_cal_ano, st.session_state.reun_cal_mes)
            st.markdown(
                '<div class="reun-cal-leyenda notranslate" translate="no" lang="es-MX">'
                '<span><i class="programada"></i>Programada</span>'
                '<span><i class="realizada"></i>Realizada</span>'
                '<span><i class="cancelada"></i>Cancelada</span>'
                '<span><i class="pendiente"></i>Por confirmar</span>'
                '</div>', unsafe_allow_html=True)

        st.markdown('<div class="setf-reun-seccion">Próximas reuniones</div>', unsafe_allow_html=True)
        proximas = sorted((r for r in reuniones if r["fecha"] >= hoy and r["estado"] in
                          ("Programada", "Por confirmar")),
                          key=lambda r: (r["fecha"], r["hora"] or datetime.min.time()))[:6]
        if not proximas:
            st.info("No hay próximas reuniones pendientes en el calendario.")
        for r in proximas:
            fecha_texto = f'{_REUNIONES_DIAS[r["fecha"].weekday()].capitalize()}, {r["fecha"].strftime("%d/%m/%Y")}'
            hora_texto = r["hora"].strftime("%H:%M") if r["hora"] else "Horario por confirmar"
            # Presentación reforzada con datos escapados para evitar HTML de usuario.
            fecha_destacada = html.escape(fecha_texto)
            hora_destacada = html.escape(hora_texto)
            lugar_destacado = html.escape(r["ubicacion"] or "Por definir")
            responsable_destacado = html.escape(r["responsable"] or "Por asignar")
            st.markdown(
                '<div class="setf-reun-prox notranslate" translate="no" lang="es-MX">'
                '<div class="reun-prox-superior">'
                '<span class="reun-prox-etiqueta">PRÓXIMA REUNIÓN</span>'
                '<span class="reun-prox-estado">' + html.escape(r["estado"]) + '</span></div>'
                '<strong>' + html.escape(r["asunto"] or "Reunión sin asunto") + '</strong>'
                '<div class="reun-prox-detalles">'
                '<div class="reun-prox-detalle"><span class="reun-prox-ico">▦</span><div>'
                '<span class="reun-prox-micro">Fecha</span><span class="reun-prox-valor">' + fecha_destacada + '</span></div></div>'
                '<div class="reun-prox-detalle"><span class="reun-prox-ico">◷</span><div>'
                '<span class="reun-prox-micro">Hora</span><span class="reun-prox-valor">' + hora_destacada + '</span></div></div>'
                '<div class="reun-prox-detalle lugar"><span class="reun-prox-ico">⌖</span><div>'
                '<span class="reun-prox-micro">Lugar o enlace</span><span class="reun-prox-valor">' + lugar_destacado + '</span></div></div>'
                '</div><div class="reun-prox-responsable">Responsable: ' + responsable_destacado +
                '</div></div>', unsafe_allow_html=True)
        with st.container(key="reun_consulta_area"):
            st.markdown('<div class="setf-reun-seccion">Consultar todas las reuniones</div>', unsafe_allow_html=True)
            st.markdown('<div class="reun-consulta-encabezado"><span>CONSULTA DOCUMENTAL · SETF</span><h3>Directorio institucional</h3><p>Localiza reuniones por asunto, ubicación, antecedentes o responsable.</p></div>', unsafe_allow_html=True)
            st.markdown('<div class="reun-filtros-titulo">⌕ &nbsp; Búsqueda y filtros</div>', unsafe_allow_html=True)
            consulta, filtro_estado, filtro_resp = st.columns([3, 1.5, 2], gap="medium")
            with consulta:
                busqueda = st.text_input("Buscar reunión", placeholder="Buscar por asunto, lugar, antecedente o responsable...", key="reun_buscar")
            with filtro_estado:
                filtra_estado = st.selectbox("Estado", ["Todos", *_REUNIONES_ESTADOS], key="reun_filtro_estado")
            with filtro_resp:
                filtra_resp = st.selectbox("Responsable", ["Todos", *opciones_filtro_responsables], key="reun_filtro_responsable")
            limpiar_col, explicacion_col = st.columns([1.25, 4.75], vertical_alignment="center")
            with limpiar_col:
                if st.button("↺ Limpiar filtros", key="reun_limpiar_filtros", use_container_width=True):
                    for clave in ("reun_buscar", "reun_filtro_estado", "reun_filtro_responsable"):
                        st.session_state.pop(clave, None)
                    st.rerun()
            with explicacion_col:
                st.caption("Los filtros se aplican al instante, sin alterar la información registrada.")
            filtradas = []
            for r in reuniones:
                texto = " ".join((r["asunto"], r["ubicacion"], r["antecedente"], r["responsable"])).casefold()
                if busqueda.strip().casefold() not in texto:
                    continue
                if filtra_estado != "Todos" and r["estado"] != filtra_estado:
                    continue
                if filtra_resp != "Todos" and r["responsable"] != filtra_resp:
                    continue
                filtradas.append(r)
            filas_tabla = [{"Fecha": r["fecha"].strftime("%d/%m/%Y"),
                            "Hora": r["hora"].strftime("%H:%M") if r["hora"] else r["datos"][2],
                            "Asunto": r["asunto"], "Ubicación": r["ubicacion"],
                            "Asignación": r["responsable"], "Estado": r["estado"],
                            "Antecedente": r["antecedente"], "Fila": r["fila"]}
                           for r in sorted(filtradas, key=lambda r: (r["fecha"], r["hora"] or datetime.min.time()), reverse=True)]
            with st.container(key="reun_tabla_institucional"):
                st.markdown(
                    f'<div class="reun-tabla-intro"><div>Directorio de reuniones</div>'
                    f'<span>{len(filas_tabla)} REGISTROS</span></div>',
                    unsafe_allow_html=True)
                tabla_df = pd.DataFrame(filas_tabla, columns=[
                    "Fecha", "Hora", "Asunto", "Ubicación", "Asignación", "Estado", "Antecedente", "Fila"])
                # Los valores originales permanecen intactos; el formato solo cambia la vista.
                def estilo_fila_reuniones(row):
                    tonos = {"Programada": "#fff9eb", "Realizada": "#edf7f1",
                             "Cancelada": "#fff0ef", "Por confirmar": "#f1f5f7"}
                    color = tonos.get(str(row.get("Estado", "")), "#ffffff")
                    return [f"background-color: {color}; color: #193b51;" if col == "Estado" else "color: #193b51;" for col in row.index]
                tabla_estilizada = tabla_df.style.apply(estilo_fila_reuniones, axis=1)
                st.dataframe(
                    tabla_estilizada, use_container_width=True, hide_index=True, row_height=47,
                    column_config={
                        "Fecha": st.column_config.TextColumn("FECHA", width="small"),
                        "Hora": st.column_config.TextColumn("HORA", width="small"),
                        "Asunto": st.column_config.TextColumn("ASUNTO", width="large"),
                        "Ubicación": st.column_config.TextColumn("UBICACIÓN", width="medium"),
                        "Asignación": st.column_config.TextColumn("RESPONSABLE", width="medium"),
                        "Estado": st.column_config.TextColumn("ESTADO", width="small"),
                        "Antecedente": st.column_config.TextColumn("ANTECEDENTE", width="medium"),
                        "Fila": None,
                    },
                    key="reun_tabla_consulta",
                )
            st.caption("Los antecedentes se registran en la nueva pestaña REUNIONES_APP. "
                       "La hoja histórica Reuniones permanece independiente.")

    with pestana_registro:
        st.markdown('<div class="setf-reun-seccion">Programar una reunión</div>', unsafe_allow_html=True)
        st.markdown('<div class="setf-reun-sub">Cada alta se guarda en la pestaña REUNIONES_APP, '
                    'con usuario, fecha y estado registrados en las hojas auxiliares.</div>', unsafe_allow_html=True)
        with st.form("reun_nueva_form", clear_on_submit=True):
            asunto_n = st.text_input("Asunto de la reunión *", key="reun_n_asunto")
            fn1, fn2 = st.columns(2)
            with fn1:
                fecha_n = st.date_input("Fecha *", value=hoy, format="DD/MM/YYYY", key="reun_n_fecha")
            with fn2:
                from datetime import time as _time_reun
                hora_n = st.time_input("Hora *", value=_time_reun(9, 0), step=1800, key="reun_n_hora")
            ubicacion_n = st.text_input("Ubicación o enlace de reunión *",
                                        placeholder="Campamento, sala o URL de Meet", key="reun_n_ubicacion")
            asignacion_n = st.selectbox("Responsable asignado *", opciones_responsables,
                                        key="reun_n_responsable")
            manual_n = st.text_input("Nombre del responsable externo",
                                     key="reun_n_responsable_otro")
            antecedente_n = st.text_input("Antecedente (oficio, referencia o enlace)",
                                           key="reun_n_antecedente")
            estado_n = st.selectbox("Estado", _REUNIONES_ESTADOS, key="reun_n_estado")
            registrar = st.form_submit_button("Guardar reunión", type="primary", use_container_width=True)
        if registrar:
            try:
                responsable_n = manual_n.strip() if asignacion_n == "Otro responsable (externo)…" else asignacion_n
                if responsable_n == "— Selecciona responsable —":
                    raise ValueError("Selecciona a la persona responsable de la reunión.")
                _reuniones_guardar_nueva(fecha_n, hora_n, asunto_n, ubicacion_n,
                                         responsable_n, antecedente_n, estado_n)
                st.session_state.reun_exito = "Registro correcto"
                st.rerun()
            except Exception as error:
                st.error(f"No se pudo completar el registro: {error}")

    with pestana_editar:
        st.markdown('<div class="setf-reun-seccion">Edición compartida</div>', unsafe_allow_html=True)
        st.markdown('<div class="setf-reun-sub">Todos los integrantes de SETF pueden modificar cualquier '
                    'reunión. Para evitar sobrescrituras, se comprueba que el registro no haya cambiado.</div>',
                    unsafe_allow_html=True)
        if not reuniones:
            st.info("No hay reuniones con una fecha reconocible para editar.")
        else:
            ordenadas = sorted(reuniones, key=lambda r: (r["fecha"], r["fila"]), reverse=True)
            por_fila = {r["fila"]: r for r in ordenadas}
            seleccion = st.selectbox("Selecciona la reunión", list(por_fila),
                                     format_func=lambda fila: f'{por_fila[fila]["fecha"].strftime("%d/%m/%Y")} · '
                                         f'{por_fila[fila]["asunto"] or "Sin asunto"} · fila {fila}',
                                     key="reun_editar_seleccion")
            r = por_fila[seleccion]
            if not r["hora"]:
                st.warning("El registro tiene una hora antigua no reconocible. Indica la hora correcta antes de guardar.")
            with st.form(key=f"reun_editar_form_{seleccion}"):
                asunto_e = st.text_input("Asunto *", value=r["asunto"])
                c1, c2 = st.columns(2)
                with c1:
                    fecha_e = st.date_input("Fecha *", value=r["fecha"], format="DD/MM/YYYY")
                with c2:
                    from datetime import time as _time_reun
                    hora_e = st.time_input("Hora *", value=r["hora"] or _time_reun(9, 0), step=1800)
                ubicacion_e = st.text_input("Ubicación o enlace *", value=r["ubicacion"])
                opciones_edicion = ["— Selecciona responsable —", *opciones_filtro_responsables]
                if r["responsable"] and r["responsable"] not in opciones_edicion:
                    opciones_edicion.append(r["responsable"])
                if "Otro responsable (externo)…" not in opciones_edicion:
                    opciones_edicion.append("Otro responsable (externo)…")
                responsable_e = st.selectbox("Responsable *", opciones_edicion,
                    index=opciones_edicion.index(r["responsable"]) if r["responsable"] in opciones_edicion else 0)
                manual_e = st.text_input("Nombre del responsable externo")
                antecedente_e = st.text_input("Antecedente", value=r["antecedente"],
                    help="Si ya existe un hipervínculo con texto en Google Sheets, déjalo igual para conservar su URL.")
                estado_e = st.selectbox("Estado", _REUNIONES_ESTADOS,
                                        index=_REUNIONES_ESTADOS.index(r["estado"]))
                guardar_e = st.form_submit_button("Guardar cambios", type="primary", use_container_width=True)
            if guardar_e:
                try:
                    asignado_e = manual_e.strip() if responsable_e == "Otro responsable (externo)…" else responsable_e
                    if asignado_e == "— Selecciona responsable —":
                        raise ValueError("Selecciona a la persona responsable de la reunión.")
                    _reuniones_guardar_edicion(r, fecha_e, hora_e, asunto_e, ubicacion_e,
                                               asignado_e, antecedente_e, estado_e)
                    st.session_state.reun_exito = "Actualización correcta"
                    st.rerun()
                except Exception as error:
                    st.error(f"No se pudo completar la edición: {error}")
            st.caption("Antecedente original: " + (r["antecedente"] or "Sin antecedente") +
                       f" · Consulta en la hoja Reuniones, fila {seleccion}.")

    with pestana_historial:
        st.markdown('<div class="setf-reun-seccion">Historial de modificaciones</div>', unsafe_allow_html=True)
        st.caption("Se registra quién creó o editó cada reunión y la fecha exacta. "
                   "Las modificaciones realizadas antes de habilitar este módulo no disponen de historial.")
        try:
            historial = _reuniones_historial(libro)
            if historial:
                st.dataframe(pd.DataFrame(historial), use_container_width=True, hide_index=True)
            else:
                st.info("Todavía no hay cambios registrados desde la aplicación.")
        except Exception as error:
            st.error(f"No se pudo cargar la bitácora: {error}")


# La portada SETF conserva íntegra su interfaz original.
# Solo las subáreas utilizan el encabezado, la navegación horizontal y la campana.
if (
    _area_activa_validada() == "setf"
    and st.session_state.menu_activo in (
        "oficios", "pendientes", "urgentes", "firmados", "minutas", "reuniones"
    )
):
    _setf_barra_superior()

if st.session_state.menu_activo == "home":
    # Portada de Diseño Geométrico con el mismo lenguaje visual del selector de áreas.
    # Los módulos reales y sus permisos permanecen intactos.
    fondo_home = (_imagen_selector("fondo_setf.png") or _imagen_selector("fondo_ferroviario.png")) if _area_activa_validada() == "setf" else _imagen_selector("fondo_ferroviario.png")
    logo_home = (_imagen_selector("logo_setf.png") or _imagen_selector("logo_selector.png")) if _area_activa_validada() == "setf" else _imagen_selector("logo_selector.png")
    foto_oficios = (_imagen_selector("menu_setf_oficios.png") or _imagen_selector("tarjeta_setf.png")) if _area_activa_validada() == "setf" else (_imagen_selector("menu_oficios.png") or _imagen_selector("tarjeta_diseno.png"))
    foto_ingenieria = (_imagen_selector("menu_setf_planos.png") or _imagen_selector("tarjeta_setf.png")) if _area_activa_validada() == "setf" else (_imagen_selector("menu_ingenieria.png") or _imagen_selector("tarjeta_diseno.png"))
    fondo_home_css = (
        f'url("{fondo_home}")'
        if fondo_home else 'linear-gradient(130deg,#eef3f6,#fafafa)'
    )
    st.markdown('<div class="home-institutional-marker"></div>', unsafe_allow_html=True)
    st.markdown(f"""
    <style>
    [data-testid="stAppViewContainer"] {{
       background-image:{fondo_home_css} !important;
       background-size:cover !important;background-position:center center !important;
       background-attachment:fixed !important;
    }}
    [data-testid="stHeader"] {{height:0!important;min-height:0!important;background:transparent!important;}}
    #MainMenu, footer {{visibility:hidden;}}
    [data-testid="stToolbar"] {{display:none!important;}}
    .block-container:has(.home-institutional-marker) {{
       max-width:1480px!important;padding-top:0!important;padding-bottom:0!important;
       margin-top:0!important;
    }}

    /* La fila COMPLETA del encabezado es fija, no un fondo separado. */
    .home-institutional-marker {{height:104px;}}
    [data-testid="stHorizontalBlock"]:has(.home-brand) {{
        position:fixed!important;top:0!important;left:0!important;right:0!important;
        width:100%!important;height:104px!important;
        padding:0 clamp(26px,5.4vw,108px)!important;
        box-sizing:border-box!important;
        background:linear-gradient(110deg,#102235,#20374a)!important;
        border-bottom:1px solid rgba(200,164,96,.35)!important;
        z-index:999!important;align-items:center!important;gap:16px!important;
    }}
    [data-testid="stHorizontalBlock"]:has(.home-brand) > [data-testid="stColumn"] {{
        min-width:0!important;align-self:center!important;
    }}
    [data-testid="stHorizontalBlock"]:has(.home-brand) [data-testid="stVerticalBlock"] {{
        justify-content:center!important;gap:0!important;
    }}
    [data-testid="stHorizontalBlock"]:has(.home-brand) [data-testid="stMarkdownContainer"] p {{margin:0!important;}}
    
    .home-brand {{height:90px;display:flex;align-items:center;gap:17px;position:relative;z-index:1;color:white;}}
    .home-brand img {{height:58px;width:65px;object-fit:contain;}}
    .home-brand-setf img {{height:70px;width:70px;object-fit:contain;
      filter:drop-shadow(0 2px 4px rgba(0,0,0,.2));}}
    .home-brand-name {{border-left:1px solid #caa45e;padding-left:19px;}}
    .home-brand-name strong {{font-size:23px;letter-spacing:1.3px;color:#fff;display:block;line-height:1.1;}}
    .home-brand-name span {{font-size:10px;letter-spacing:3.3px;color:#d5dce3;display:block;margin-top:8px;}}
    .home-user {{color:#fff;text-align:right;font-size:12px;height:90px;display:flex;flex-direction:column;
       justify-content:center;position:relative;z-index:1;}}
    .home-user strong {{display:block;font-size:14px;margin-top:5px;color:white;}}
    [class*="st-key-home_cambiar_area"], [class*="st-key-home_cerrar_sesion"] {{
       display:flex;align-items:center;height:90px;position:relative;z-index:1;
    }}
    [class*="st-key-home_cambiar_area"] button, [class*="st-key-home_cerrar_sesion"] button {{
       min-height:43px!important;border:1px solid #caa45e!important;
       border-radius:9px!important;color:white!important;background:transparent!important;
       font-weight:650!important;font-size:12px!important;
    }}
    [class*="st-key-home_cambiar_area"] button:hover, [class*="st-key-home_cerrar_sesion"] button:hover {{
       background:#344a5c!important;color:white!important;
    }}
    .home-institutional-hero {{text-align:center;margin:4px auto 28px;}}
    .home-institutional-kicker {{font-size:11px;letter-spacing:3px;font-weight:800;color:#a87b34;}}
    .home-institutional-rule {{width:48px;height:2px;background:#b8893e;margin:12px auto;}}
    .home-institutional-title {{color:#10283b;font-size:clamp(30px,3vw,41px);font-weight:850;
       line-height:1.16;letter-spacing:-.8px;}}
    .home-institutional-subtitle {{color:#657688;font-size:15px;margin-top:11px;}}
    .home-institutional-card {{background:linear-gradient(150deg,#214a55,#173340);border:1px solid #62858e;
       border-radius:19px;box-shadow:0 10px 28px rgba(13,33,49,.14);overflow:hidden;
       height:257px;transition:transform .2s,box-shadow .2s;}}
    .home-institutional-card.featured {{background:linear-gradient(145deg,#1d4057,#102c41);border-color:#6b99ab;}}
    .home-institutional-card:hover {{transform:translateY(-3px);box-shadow:0 17px 34px rgba(13,33,49,.19);}}
    .home-institutional-photo {{height:112px;background-size:cover;background-position:center;
       position:relative;background-color:#243c53;}}
    .home-institutional-photo::after {{content:"";position:absolute;inset:0;
       background:linear-gradient(90deg,rgba(13,35,54,.56),rgba(18,38,57,.12));}}
    .home-institutional-status {{position:absolute;z-index:1;top:16px;left:18px;
       background:#d3a75d;color:#10283b;border-radius:7px;padding:9px 13px;
       letter-spacing:1px;font-size:11px;font-weight:800;}}
    .home-institutional-card:not(.featured) .home-institutional-status {{background:#a9c4bd;color:#13303c;}}
    .home-institutional-content {{padding:20px 25px 12px;position:relative;}}
    .home-institutional-icon {{position:absolute;top:-27px;left:24px;width:54px;height:54px;
       display:flex;align-items:center;justify-content:center;color:#d3a75d;
       background:#172f46;border:2px solid #d3a75d;border-radius:13px;
       font-size:24px;box-shadow:0 5px 14px rgba(9,28,44,.23);}}
    .home-institutional-name {{font-size:21px;font-weight:800;color:#ffffff;margin-top:12px;line-height:1.2;}}
    .home-institutional-description {{font-size:14px;color:#d2e0e6;line-height:1.4;margin-top:11px;}}
    [class*="st-key-home_oficios"] button,[class*="st-key-home_diseno"] button,[class*="st-key-home_reuniones"] button {{
       min-height:43px!important;border-radius:9px!important;font-size:13px!important;
       font-weight:750!important;margin-top:3px!important;
    }}
    [class*="st-key-home_oficios"] button {{background:#24546a!important;color:white!important;border:1px solid #608da0!important;}}
    [class*="st-key-home_oficios"] button:hover {{background:#306b83!important;color:white!important;}}
    [class*="st-key-home_diseno"] button {{background:#2d5b60!important;color:white!important;border:1px solid #79a39e!important;}}
    [class*="st-key-home_diseno"] button:hover {{background:#397379!important;color:#fff!important;}}
    [class*="st-key-home_reuniones"] button {{background:#23566b!important;color:white!important;border:1px solid #79a3aa!important;}}
    [class*="st-key-home_reuniones"] button:hover {{background:#32758a!important;color:#fff!important;}}
    .home-institutional-footer {{text-align:center;font-size:11px;color:#637585;margin-top:5px;}}
    /* Encabezado y tarjetas visibles sin desplazamiento a tamaño de escritorio. */
    [data-testid="stAppViewContainer"]:has(.home-institutional-marker) {{overflow-y:auto!important;}}
    [data-testid="stMainBlockContainer"]:has(.home-institutional-marker) {{padding-top:0!important;margin-top:0!important;}}
    [data-testid="stMainBlockContainer"]:has(.home-institutional-marker) > div {{gap:.55rem!important;}}
    .block-container:has(.home-institutional-marker) [data-testid="stVerticalBlock"] {{gap:.55rem!important;}}
    .block-container:has(.home-institutional-marker) [data-testid="stHorizontalBlock"] {{align-items:center;}}
    .block-container:has(.home-institutional-marker) .selector-pie {{display:none!important;}}
    /* El aviso de acceso exclusivo pertenece solamente al selector de áreas. */
    @media (min-width:1000px) and (min-height:680px) {{
       [data-testid="stAppViewContainer"]:has(.home-institutional-marker) {{overflow-y:hidden!important;}}
    }}
    @media(max-height:780px) and (min-width:901px) {{
       .home-institutional-hero {{margin:8px auto 21px;}}
       .home-institutional-card {{height:238px;}}
       .home-institutional-photo {{height:102px;}}
       .home-institutional-description {{margin-top:8px;}}
    }}
    @media(max-width:900px) {{
       .home-brand-name strong {{font-size:17px;}}
       .home-brand-name span {{font-size:9px;letter-spacing:1px;}}
       .home-brand img {{width:48px;}}
       .home-brand-setf img {{width:52px;height:52px;}}
       .home-institutional-card {{height:280px;}}
       .home-institutional-photo {{height:115px;}}
    }}
    @media(max-width:760px) {{
       [data-testid="stHorizontalBlock"]:has(.home-brand) {{height:auto!important;min-height:100px!important;padding:8px 16px!important;flex-wrap:wrap!important;}}
       .home-institutional-marker {{height:124px;}}
    }}
    @media(max-width:640px) {{
       .home-brand-name strong {{font-size:13px;}}
       .home-brand-name span {{font-size:8px;}}
       .home-institutional-hero {{margin:15px auto 25px;}}
       .home-institutional-card {{height:260px;}}
       [data-testid="stAppViewContainer"]:has(.home-institutional-marker) {{overflow-y:auto!important;}}
    }}
    </style>
    """, unsafe_allow_html=True)

    # El encabezado muestra el nombre exacto de la cuenta y evita la traducción
    # automática que convierte la abreviatura «Tte.» en «Señora».
    if identificador_sesion() == "alondra":
        nombre_home = "Tte. Alondra Barajas"
    else:
        nombre_home = str(st.session_state.get("usuario_actual") or "Usuario")
    nombre_home = html.escape(nombre_home)
    cab_logo = f'<img src="{logo_home}" alt="Logotipo institucional">' if logo_home else '<span style="font-size:35px;color:#d3a75d">▰</span>'
    clase_marca = "home-brand home-brand-setf" if _area_activa_validada() == "setf" else "home-brand"
    col_marca, col_usuario, col_cambiar, col_cerrar = st.columns([4.4,1.65,1.25,1.2], gap="medium", vertical_alignment="center")
    with col_marca:
        st.markdown(f"""<div class="{clase_marca}">{cab_logo}<div class="home-brand-name">
           <strong>CONTROL DOCUMENTAL</strong><span>INFRAESTRUCTURA FERROVIARIA</span>
           </div> </div>""", unsafe_allow_html=True)
    with col_usuario:
        st.markdown(f'<div class="home-user">Sesión:<strong class="notranslate" translate="no" lang="es-MX">{nombre_home}</strong></div>', unsafe_allow_html=True)
    with col_cambiar:
        if len(autorizadas) == 2:
            if st.button("⇄ Cambiar de área", key="home_cambiar_area", use_container_width=True):
                st.session_state.area_actual = None
                st.session_state.menu_activo = "home"
                st.session_state.pop("pend_bandeja", None)
                st.rerun()
    with col_cerrar:
        if st.button("↪ Cerrar sesión", key="home_cerrar_sesion", use_container_width=True):
            cerrar_sesion_selector()

    st.markdown(_rotulos_area("""<div class="home-institutional-hero">
      <div class="home-institutional-kicker">MESA DE DISEÑO GEOMÉTRICO DE VÍA FÉRREA</div>
      <div class="home-institutional-rule"></div>
      <div class="home-institutional-title">Selecciona tu módulo de trabajo</div>
      <div class="home-institutional-subtitle">Elige el módulo que deseas consultar.</div>
     </div>"""), unsafe_allow_html=True)

    # SETF tiene tres entradas reales: Oficios, Reuniones y Módulo de planos.
    # El home de Diseño Geométrico conserva su diseño de dos tarjetas.
    if _area_activa_validada() == "setf":
        _, col_oficios, col_reuniones, col_ingenieria, _ = st.columns(
            [.3, 4.8, 4.8, 4.8, .3], gap="medium")
    else:
        _, col_oficios, col_ingenieria, _ = st.columns([.4, 4.8, 4.8, .4], gap="large")
    with col_oficios:
        imagen = f'background-image:url(\'{foto_oficios}\')' if foto_oficios else 'background:linear-gradient(120deg,#1d3d56,#547a8d)'
        st.markdown(f"""<div class="home-institutional-card featured">
          <div class="home-institutional-photo" style="{imagen}"><div class="home-institutional-status">✓ &nbsp; GESTIÓN DOCUMENTAL</div></div>
          <div class="home-institutional-content"><div class="home-institutional-icon">▤</div>
            <div class="home-institutional-name">Oficios en trámite</div>
            <div class="home-institutional-description">Consulta, registro y seguimiento de oficios, revisiones y firmas.</div>
          </div> </div>""", unsafe_allow_html=True)
        if st.button("Ingresar a Oficios en trámite  →", key="home_oficios", use_container_width=True):
            navegar_a("oficios")
    if _area_activa_validada() == "setf":
        with col_reuniones:
            imagen = 'background:linear-gradient(120deg,#21445a,#55717a)'
            st.markdown(f"""<div class="home-institutional-card">
              <div class="home-institutional-photo" style="{imagen}">
                <div class="home-institutional-status">▦ &nbsp; AGENDA COMPARTIDA</div>
              </div>
              <div class="home-institutional-content">
                <div class="home-institutional-icon">▦</div>
                <div class="home-institutional-name">Gestión de reuniones</div>
                <div class="home-institutional-description">Programa, consulta y actualiza reuniones de SETF.</div>
              </div></div>""", unsafe_allow_html=True)
            if st.button("Ingresar a Gestión de reuniones  →", key="home_reuniones", use_container_width=True):
                navegar_a("reuniones")
    with col_ingenieria:
        # SETF tiene un Módulo de planos visual; su gestión funcional queda para otra etapa.
        # En Diseño Geométrico se conserva Ingeniería de detalle sin modificaciones.
        es_setf_home = _area_activa_validada() == "setf"
        titulo_segunda_tarjeta = "Módulo de planos" if es_setf_home else "Ingeniería de detalle"
        estado_segunda_tarjeta = "◩ &nbsp; PRÓXIMAMENTE" if es_setf_home else "⌁ &nbsp; INGENIERÍA"
        descripcion_segunda_tarjeta = (
            "Espacio reservado para la consulta y gestión de planos eléctricos ferroviarios."
            if es_setf_home else "Fase I · Documentación técnica y desarrollo del proyecto."
        )
        boton_segunda_tarjeta = (
            "Ingresar al Módulo de planos  →" if es_setf_home
            else "Ingresar a Ingeniería de detalle  →"
        )
        imagen = f'background-image:url(\'{foto_ingenieria}\')' if foto_ingenieria else 'background:linear-gradient(120deg,#284657,#567688)'
        st.markdown(f"""<div class="home-institutional-card">
          <div class="home-institutional-photo" style="{imagen}"><div class="home-institutional-status">{estado_segunda_tarjeta}</div></div>
          <div class="home-institutional-content"><div class="home-institutional-icon">◩</div>
            <div class="home-institutional-name">{titulo_segunda_tarjeta}</div>
            <div class="home-institutional-description">{descripcion_segunda_tarjeta}</div>
          </div> </div>""", unsafe_allow_html=True)
        if st.button(boton_segunda_tarjeta, key="home_diseno", use_container_width=True):
            # Conserva la ruta interna 'diseno' para no alterar navegación ni permisos.
            navegar_a("diseno")
    st.markdown(_rotulos_area('<div class="home-institutional-footer">TRAZO · ALINEAMIENTO · RASANTE</div>'), unsafe_allow_html=True)

# =========================================================
# MÓDULO OFICIOS EN TRÁMITE
# =========================================================

elif st.session_state.menu_activo == "pendientes":
    # Fondo independiente del menú principal y de Oficios en trámite.
    fondo_pendientes = (obtener_imagen_base64("fondo_pendientes.png")
                        or obtener_imagen_base64("fondo_ferroviario.png"))
    if fondo_pendientes:
        st.markdown(
            f"""<style>
            [data-testid="stAppViewContainer"] {{
                background-image: url("{fondo_pendientes}") !important;
                background-size: cover !important;
                background-position: center center !important;
                background-repeat: no-repeat !important;
                background-attachment: fixed !important;
            }}
            [data-testid="stHeader"] {{ background: transparent !important; }}
            </style>""",
            unsafe_allow_html=True,
        )
    else:
        st.caption("Fondo opcional: guarda fondo_pendientes.png o fondo_ferroviario.png en assets.")

    # El menú lateral se dibuja una sola vez para ambos módulos.
    # No volver a crear sus widgets al cambiar de pantalla.

    # CSS final único: se aplica antes de renderizar tarjetas y evita la
    # aparición momentánea de estilos antiguos de Mis pendientes.
    st.markdown("""<style>
/* Solo Mis pendientes: conserva la identidad visual institucional. */
    .stMainBlockContainer:has(.pend-hero) {
        max-width: 1240px !important;
        padding-top: 2rem !important;
        padding-bottom: 4rem !important;
    }
    /* En SETF la barra superior ya reserva espacio; DG no se altera. */
    .stMainBlockContainer:has(.pend-hero):has(.setf-pend-switch-title) {
        max-width: none !important;
        padding-top: 0 !important;
    }
    .pend-hero {
        position:relative; overflow:hidden; border-radius:14px;
        padding:36px 42px 31px; min-height:190px;
        background:linear-gradient(110deg,#142c3e 0%,#213e50 70%,#2c4a58 100%);
        box-shadow:0 10px 28px rgba(17,39,53,.18);
        border-left:4px solid #bd995b; color:#fff;
    }
    .pend-hero:after {
        content:""; position:absolute; right:32px; top:24px;
        width:32%; height:120px; opacity:.20;
        background:repeating-linear-gradient(0deg,transparent 0 22px,#8cabbc 23px 24px),
                   repeating-linear-gradient(90deg,transparent 0 54px,#8cabbc 55px 56px);
        transform:skewX(-12deg); pointer-events:none;
    }
    .pend-hero-kicker {color:#d7b377;font-size:11px;font-weight:700;letter-spacing:2px}
    .pend-hero-title {font-size:34px;font-weight:800;margin-top:14px;letter-spacing:-.6px}
    .pend-hero-desc {font-size:14px;color:#cbd9e2;margin-top:5px}
    .pend-hero-line {width:48px;height:3px;background:#d5ae69;margin-top:20px}
    .pend-hero-track {position:absolute;right:30px;bottom:22px;font-size:10px;
                      color:#b4c8d3;letter-spacing:2px}
    .pend-section-label {text-align:center;font-size:10px;font-weight:800;
                         color:#a27a3e;letter-spacing:2.6px;margin-top:27px}
    .pend-list-title {text-align:center;font-size:22px;font-weight:750;
                      color:#173246;margin:9px 0 12px}
    .pend-kpi {background:rgba(255,255,255,.97);border-radius:12px;
               border:1px solid #dce5e9;border-top:4px solid #b99a65;
               padding:20px 24px;min-height:133px;
               box-shadow:0 8px 20px rgba(15,42,59,.08)}
    .pend-kpi-number {font-size:36px;font-weight:800;color:#173246;line-height:1.1}
    .pend-kpi-title {font-size:14px;font-weight:750;color:#173246;margin-top:7px}
    .pend-kpi-desc {font-size:11px;color:#6c8190;margin-top:3px}
    .stMainBlockContainer:has(.pend-hero) [data-testid="stVerticalBlockBorderWrapper"] {
        background:rgba(255,255,255,.97);border-radius:13px;
        border-color:#dce5e9;box-shadow:0 6px 16px rgba(15,42,59,.05);
    }
    .pend-oficio {padding:3px 0}
    .pend-oficio-top {display:flex;align-items:center;gap:14px;flex-wrap:wrap;
                     color:#173246;font-size:15px}
    .pend-numero {font-size:12px;color:#708696;margin-top:3px}
    .pend-asunto {font-size:14px;color:#263f50;margin-top:10px;
                  font-weight:600;line-height:1.45}
    .pend-meta {display:flex;gap:22px;flex-wrap:wrap;margin-top:12px;
                font-size:12px;color:#728491}
    .pend-estado {border-radius:20px;padding:4px 11px;font-size:11px;font-weight:700;
                  background:#edf2f5;color:#365568}
    .pend-estado.revision {background:#e9f1f8;color:#326281}
    .pend-estado.correcciones {background:#fff0e7;color:#a15a34}
    .pend-estado.elaboracion {background:#f4f0e6;color:#896d35}
    .pend-estado.firma {background:#e7f4ed;color:#2d7759}
    .pend-estado.recaido {background:#f8e8e9;color:#9e4650}
    /* EDICIÓN RÁPIDA: solo los campos del formulario de Mis pendientes. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] input:not([type="checkbox"]),
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] textarea,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="select"] > div {
        background-color: #D8E5ED !important;
        color: #19384B !important;
        border-color: #A8BDCA !important;
        border-radius: 7px !important;
    }
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] input:focus,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] textarea:focus,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="select"]:focus-within > div {
        border-color: #C69A4C !important;
        box-shadow: 0 0 0 1px #C69A4C !important;
    }
    /* Streamlit puede usar una capa interior para el campo de fecha. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="input"] > div,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="textarea"] > div {
        background-color: #D8E5ED !important;
        border-color: #A8BDCA !important;
        border-radius: 7px !important;
    }
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="input"]:focus-within > div,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] [data-baseweb="textarea"]:focus-within > div {
        border-color: #C69A4C !important;
        box-shadow: 0 0 0 1px #C69A4C !important;
    }
    /* Conservar el aspecto de campo inactivo de fecha. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] input:disabled {
        opacity: .65 !important;
        -webkit-text-fill-color: #607887 !important;
    }

    /* EDICIÓN DE OFICIO: estilo del formulario Registrar nuevo oficio. */
    .st-key-pend_editor {
        background:#26333F !important;
        border:1px solid #587081 !important;
        border-radius:13px !important;
        padding:24px 24px 28px !important;
        box-shadow:0 12px 28px rgba(19,40,53,.18) !important;
    }
    .st-key-pend_editor [data-testid="stVerticalBlockBorderWrapper"] {
        background:transparent !important;
        border-color:transparent !important;
        box-shadow:none !important;
    }
    .pend-editor-kicker {color:#D4A65A;font-size:10px;font-weight:800;letter-spacing:2px}
    .pend-editor-title {color:#FFFFFF;font-size:23px;font-weight:800;margin-top:5px}
    .pend-editor-sub {color:#BFD0DB;font-size:12px;margin-top:3px}


/* Centro de autorizaciones: estilos exclusivos de la bandeja de la Tte. */
.tte-queue-header {background:linear-gradient(110deg,#152d3f,#294b5d);color:white;
    border-left:4px solid #c39b55;border-radius:12px;padding:24px 28px;margin:14px 0 18px;
    box-shadow:0 8px 24px rgba(24,48,65,.12)}
.tte-queue-eyebrow {font-size:10px;letter-spacing:2px;font-weight:800;color:#e0bb7b}
.tte-queue-heading {font-size:26px;font-weight:800;margin:7px 0 4px;color:white}
.tte-queue-description {font-size:13px;color:#d8e5ed}
.tte-file-top {display:flex;justify-content:space-between;gap:10px;align-items:center;margin:0 0 8px}
.tte-file-label {font-size:10px;font-weight:800;letter-spacing:1.6px;color:#9a7842}
.tte-file-chip {font-size:11px;background:#fff3dc;color:#805a22;border:1px solid #eed8a7;
    border-radius:99px;padding:5px 11px;font-weight:700}
.tte-file-title {font-size:19px;font-weight:800;color:#18374b;overflow-wrap:anywhere}
.tte-file-meta {font-size:12px;color:#647b8b;margin-top:4px}
.tte-step-badge {font-size:10px;color:#58758a;text-align:right;letter-spacing:.7px}
.tte-step-badge strong {color:#18374b;font-size:12px;letter-spacing:0}
.tte-timeline {display:flex;gap:8px;align-items:center;margin:16px 0 19px}
.tte-step {flex:1;display:flex;gap:7px;align-items:center;justify-content:center;
    font-size:11px;font-weight:650;color:#7b8d9b;background:#f2f5f7;border-radius:8px;
    padding:10px 6px;min-width:0;text-align:center}
.tte-step span {width:22px;height:22px;border-radius:50%;display:inline-flex;
    justify-content:center;align-items:center;background:#dce4e9;color:#476273;flex-shrink:0}
.tte-step.done {background:#eaf3ed;color:#31664e}
.tte-step.done span {background:#398260;color:white}
.tte-step.current {background:#e8f0f5;color:#183f58;border:1px solid #a9c5d5}
.tte-step.current span {background:#24475d;color:white}
.tte-document-line {display:flex;gap:12px;align-items:center;padding:13px 15px;
    background:#f7fafb;border:1px solid #dce6ec;border-radius:9px;margin:10px 0 12px;
    color:#1e3d50;overflow-wrap:anywhere}
.tte-document-line>span {font-size:25px}.tte-document-line strong {display:block;font-size:12px}
.tte-document-line small {display:block;color:#738898;font-size:11px;margin-top:4px}
.tte-decision-title {font-size:16px;font-weight:800;color:#17374b;margin:20px 0 3px}
.tte-decision-sub {font-size:12px;color:#667d8b;margin-bottom:11px}
@media(max-width:700px){.tte-timeline {flex-direction:column;align-items:stretch}
.tte-file-top {flex-wrap:wrap}.tte-queue-heading {font-size:21px}}
    .doc-center-head {background:#223443;border-left:4px solid #c59b4d;border-radius:9px;
      padding:18px 22px;margin:14px 0 18px;color:#fff;}
    .doc-center-kicker {font-size:10px;letter-spacing:2px;color:#e0bb7b;font-weight:700;}
    .doc-center-title {font-size:22px;font-weight:750;line-height:1.3;margin-top:6px;}
    .doc-center-sub {font-size:12px;color:#c6d4dc;margin-top:5px;}
    .pend-editor-section {color:#FFFFFF;text-align:center;font-size:12px;font-weight:800;
                          padding:20px 0 12px;margin-top:6px;border-bottom:1px solid #587081}
    .pend-editor-sign {color:#EAF2F7;font-size:12px;font-weight:800;margin:10px 0 2px}
    .pend-editor-divider {height:1px;background:#607789;margin:15px 0}
    .st-key-pend_editor label, .st-key-pend_editor [data-testid="stWidgetLabel"] p,
    .st-key-pend_editor [data-testid="stForm"] label p {
        color:#EDF4F8 !important;font-weight:650 !important;
    }
    .st-key-pend_editor [data-testid="stForm"] {background:transparent !important;
        border:none !important;padding:0 !important}
    .st-key-pend_editor input:not([type="checkbox"]),
    .st-key-pend_editor textarea,
    .st-key-pend_editor [data-baseweb="select"] > div,
    .st-key-pend_editor [data-baseweb="input"] > div,
    .st-key-pend_editor [data-baseweb="textarea"] > div {
        background:#394F5E !important;color:#F6FBFE !important;
        border-color:#AFC2CD !important;border-radius:7px !important;
    }
    .st-key-pend_editor input::placeholder,.st-key-pend_editor textarea::placeholder {
        color:#C0CED8 !important;
    }
    .st-key-pend_editor input:focus,.st-key-pend_editor textarea:focus,
    .st-key-pend_editor [data-baseweb="select"]:focus-within > div {
        border-color:#D1A35B !important;box-shadow:0 0 0 1px #D1A35B !important;
    }
    .st-key-pend_editor [data-testid="stFormSubmitButton"] button {
        background:#C79D4E !important;color:#FFFFFF !important;
        border:1px solid #C79D4E !important;font-weight:700 !important;
    }
    .st-key-pend_editor [data-testid="stFormSubmitButton"] button:hover {
        background:#B78A39 !important;border-color:#B78A39 !important;
    }
    @media(max-width:760px){
      .pend-hero {padding:25px 23px;min-height:165px}
      .pend-hero-title {font-size:27px}
      .pend-hero-track {display:none}
    }
    
    /* ===============================================================
       REDISEÑO VISUAL DE EXPEDIENTES DE LA TTE. ALONDRA
       Se aplica exclusivamente a los contenedores tte_expediente_.
       No modifica acciones, estados, permisos ni almacenamiento.
       =============================================================== */
    div[class*="st-key-tte_expediente_"] {
        background:linear-gradient(145deg,rgba(225,235,242,.98),rgba(213,226,235,.97)) !important;
        border:1px solid #b8cbd8 !important;
        border-radius:16px !important;
        padding:19px 22px 22px !important;
        margin:13px 0 22px !important;
        box-shadow:0 12px 29px rgba(21,49,69,.12) !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-file-top {
        background:#1d394b !important;
        border-top:3px solid #c39b55 !important;
        border-radius:11px 11px 0 0 !important;
        padding:17px 19px 7px !important;
        margin:0 !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-file-label {
        color:#e4bc79 !important;
        font-size:10px !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-file-title {
        color:#17384d !important;
        font-size:21px !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-file-meta {
        color:#536f82 !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-file-chip {
        background:#fff0d2 !important;
        border-color:#e5c486 !important;
        color:#78531b !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-timeline {
        background:rgba(255,255,255,.58);
        border:1px solid #c7d7e1;
        border-radius:12px;
        padding:10px;
        margin:16px 0 !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-step {
        background:#edf3f7;
        min-height:42px;
    }
    div[class*="st-key-tte_expediente_"] .tte-step.current {
        background:#d4e6f0 !important;
        border-color:#8eafc2 !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-document-line {
        background:rgba(255,255,255,.88) !important;
        border-color:#c7d7e1 !important;
        border-left:3px solid #b99453 !important;
        border-radius:11px !important;
        padding:15px 17px !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-document-line strong {
        color:#19394e !important;
        font-size:13px !important;
    }
    div[class*="st-key-tte_expediente_"] .tte-decision-title {
        border-top:1px solid #bbcedb;
        padding-top:18px;
        color:#19394e !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stForm"] {
        background:rgba(247,250,252,.92) !important;
        border:1px solid #c3d3de !important;
        border-radius:12px !important;
        padding:16px 18px 18px !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stForm"] textarea,
    div[class*="st-key-tte_expediente_"] [data-testid="stForm"] [data-baseweb="textarea"] > div {
        background:#ffffff !important;
        color:#17384d !important;
        border-color:#b8cbd8 !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button {
        background:#b9924e !important;
        border-color:#b9924e !important;
        color:#ffffff !important;
        font-weight:750 !important;
        min-height:44px !important;
        border-radius:9px !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button:hover {
        background:#a47b37 !important;
        border-color:#a47b37 !important;
    }
    div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] {
        background:rgba(247,250,252,.7) !important;
        border:1px solid #c4d4de !important;
        border-radius:9px !important;
    }
    @media(max-width:760px) {
        div[class*="st-key-tte_expediente_"] {padding:12px !important;}
        div[class*="st-key-tte_expediente_"] .tte-file-top {padding:13px !important;}
        div[class*="st-key-tte_expediente_"] .tte-timeline {padding:6px !important;}
    }

    
    /* Bandeja compacta de John y Alejandra: papel institucional, no azul plano. */
    div[class*="st-key-revisor_expediente_"] {
        background:linear-gradient(125deg,rgba(255,255,255,.97),rgba(248,250,251,.96)) !important;
        border:1px solid #d5e0e6 !important;border-left:3px solid #c29a57 !important;
        border-radius:12px !important;padding:14px 18px 12px !important;
        margin:9px 0 !important;box-shadow:0 5px 18px rgba(24,48,65,.075) !important;
    }
    .rev-list-intro {display:flex;justify-content:space-between;align-items:center;
        gap:16px;margin:12px 0 14px;padding:0 3px}
    .rev-list-kicker {font-size:10px;letter-spacing:1.8px;font-weight:800;color:#ad8246}
    .rev-list-intro h4 {font-size:21px;color:#18354a;margin:4px 0 2px;font-weight:800}
    .rev-list-intro p {font-size:12px;color:#64798a;margin:0}
    .rev-list-count {white-space:nowrap;border:1px solid #d8e1e7;background:#fff;
        color:#304c5e;border-radius:30px;padding:7px 12px;font-size:12px;font-weight:750}
    .rev-compact-head {display:flex;justify-content:space-between;align-items:center;gap:16px}
    .rev-compact-left {min-width:0}
    .rev-compact-kicker {font-size:9px;letter-spacing:1.45px;color:#a78149;font-weight:800}
    .rev-compact-title {font-size:18px;font-weight:800;color:#19384b;margin:3px 0;
        overflow-wrap:anywhere;line-height:1.3}
    .rev-compact-meta {font-size:12px;color:#617889}
    .rev-dot {padding:0 4px;color:#b7c5cd}
    .rev-compact-badge {display:inline-flex;align-items:center;flex-shrink:0;
        font-size:10px;font-weight:800;letter-spacing:.35px;border-radius:20px;
        padding:7px 11px;border:1px solid #bcd2dc;background:#eef5f8;color:#315a70}
    .rev-compact-badge.ajustes {background:#fff5e5;border-color:#e7c68b;color:#8a5d1d}
    .rev-compact-badge.firma {background:#edf4ef;border-color:#bdd8c6;color:#3a7052}
    .rev-compact-note {margin-top:11px;display:flex;gap:12px;align-items:baseline;
        padding:9px 12px;background:#fffaf0;border:1px solid #f0ddba;border-radius:8px;
        color:#344b59;font-size:12px;line-height:1.5}
    .rev-compact-note strong {color:#8d622b;white-space:nowrap;font-size:11px}
    .rev-compact-note span {overflow-wrap:anywhere}
    .rev-compact-next {color:#718493;font-size:11px;margin:9px 0 5px}
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] {
        background:rgba(251,252,253,.9) !important;border:1px solid #dce5ea !important;
        border-radius:8px !important;
    }
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] summary {
        color:#24465b !important;font-weight:650 !important;
    }
    @media(max-width:760px) {
        div[class*="st-key-revisor_expediente_"] {padding:12px !important}
        .rev-compact-head {align-items:flex-start;flex-wrap:wrap;gap:8px}
        .rev-compact-note {display:block}
        .rev-compact-note strong {display:block;margin-bottom:4px}
        .rev-list-intro {flex-wrap:wrap}
    }
    /* Tarjetas institucionales: cada oficio es una unidad visual independiente.
       Los selectores se limitan a la bandeja de John y Alejandra. */
    div[class*="st-key-revisor_expediente_"][data-testid="stVerticalBlockBorderWrapper"],
    div[class*="st-key-revisor_expediente_"] {
        background:linear-gradient(150deg,#ffffff 0%,#f7f9fa 100%) !important;
        border:1px solid #c5d1d9 !important;
        border-left:4px solid #b9904d !important;
        border-radius:15px !important;
        box-shadow:0 8px 24px rgba(18,42,59,.12),0 1px 3px rgba(18,42,59,.05) !important;
        padding:15px 19px 13px !important;
        margin:13px 0 19px !important;
        overflow:hidden;
    }
    div[class*="st-key-revisor_expediente_"] > div[data-testid="stVerticalBlock"] {
        gap:.48rem !important;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-head {
        background:linear-gradient(95deg,#edf2f5 0%,#f9fafb 100%);
        border:1px solid #dce5e9;
        border-radius:10px;
        padding:11px 14px;
        margin:0 0 3px;
        min-height:68px;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-title {
        font-size:19px !important;
        color:#18364a !important;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-note {
        margin:5px 0 2px;
        border-left:3px solid #c6984c;
        background:#fff9ed;
        border-radius:7px;
        padding:9px 13px;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-next {
        margin:3px 2px 2px;
        color:#4b6678;
        font-size:11px;
        font-weight:600;
    }
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] {
        background:#f0f4f6 !important;
        border:1px solid #ccd9e1 !important;
        border-radius:9px !important;
        margin-top:4px !important;
    }
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] summary {
        color:#1c4055 !important;
        font-weight:750 !important;
        min-height:37px;
    }
    div[class*="st-key-revisor_expediente_"][class*="_ajustes"] {
        border-left-color:#c4933f !important;
    }
    div[class*="st-key-revisor_expediente_"][class*="_firma"] {
        border-left-color:#568b70 !important;
    }
    div[class*="st-key-revisor_expediente_"][class*="_revision"] {
        border-left-color:#6088a0 !important;
    }
    @media(max-width:760px) {
        div[class*="st-key-revisor_expediente_"] {padding:10px 11px !important;margin:10px 0 14px !important;}
        div[class*="st-key-revisor_expediente_"] .rev-compact-head {padding:10px;}
    }
    .revision-feedback {
        background:#fffaf0;border:1px solid #dfbf83;border-left:5px solid #bd8b3b;
        border-radius:12px;padding:18px 20px;margin:15px 0 19px;
        box-shadow:0 5px 14px rgba(89,63,21,.06);
    }
    .revision-feedback-eyebrow {color:#92682c;font-size:10px;font-weight:800;letter-spacing:1.3px;}
    .revision-feedback-title {color:#233b4b;font-size:17px;font-weight:800;margin-top:7px;}
    .revision-feedback-text {
        background:white;border:1px solid #ead7b5;border-radius:9px;
        padding:14px 16px;margin:14px 0 10px;color:#213c4e;
        font-size:14px;line-height:1.65;overflow-wrap:anywhere;
    }
    .revision-feedback-footer {color:#667987;font-size:11px;}
    .revision-feedback-hint {
        margin-top:13px;border-top:1px solid #e6d3b1;padding-top:12px;
        color:#536c7b;font-size:12px;line-height:1.5;
    }
/* Rediseño 2026: solo se aplica al módulo Mis pendientes. */
    .stMainBlockContainer:has(.pend-hero) {max-width:1500px !important;padding-left:30px !important;padding-right:30px !important}
    .stMainBlockContainer:has(.pend-hero) > div {gap:1.1rem}
    .pend-hero {background:rgba(245,249,252,.89);color:#142e43;box-shadow:none;
        border-left:0;border-radius:16px;padding:22px 22px 18px;min-height:135px;
        border-bottom:3px solid #b99354}
    .pend-hero:after {opacity:.11;right:15px;top:12px}
    .pend-hero-kicker {color:#7c8999;font-size:11px;letter-spacing:2.1px}
    .pend-hero-title {font-size:40px;color:#122d41;margin-top:8px}
    .pend-hero-desc {color:#4c6274;font-size:14px}
    .pend-hero-line {margin-top:12px}
    .pend-hero-track {color:#617a89}
    .pend-nav-label {font-size:10px;color:#9b7b4d;font-weight:800;letter-spacing:2px;margin:9px 0 2px}
    div[class*="st-key-pend_nav_"] {background:rgba(255,255,255,.95);border:1px solid #dbe3ea;
        border-radius:12px;padding:10px 12px 9px;box-shadow:0 5px 17px rgba(22,44,61,.07);min-height:90px}
    div[class*="st-key-pend_nav_"] .pend-nav-caption {font-size:11px;color:#6c8091;margin-bottom:5px}
    div[class*="st-key-pend_nav_"] button {height:42px !important;background:transparent !important;
        border:0 !important;color:#19364c !important;text-align:left !important;
        font-weight:750 !important;font-size:14px !important;box-shadow:none !important}
    div[class*="st-key-pend_nav_"] button:hover {color:#9b722f !important;background:#f4f6f7 !important}
    div[class*="st-key-pend_nav_"][class*="_activo"] {background:#172f42;border-color:#c19a56}
    div[class*="st-key-pend_nav_"][class*="_activo"] button,
    div[class*="st-key-pend_nav_"][class*="_activo"] .pend-nav-caption {color:white !important}
    .pend-nav-disabled {border:1px dashed #c9d4dd;border-radius:10px;padding:20px 8px;color:#8193a0;font-size:12px}
    .pend-section-label {text-align:left;margin-top:14px;margin-bottom:8px;color:#9b7b4d}
    .pend-kpi {display:flex;align-items:center;gap:20px;min-height:116px;padding:18px 23px;
        border:1px solid #e2e8ed;border-top:1px solid #e2e8ed;
        background:rgba(255,255,255,.95);border-radius:13px}
    .pend-kpi-icon {width:54px;height:54px;display:flex;align-items:center;justify-content:center;
        border-radius:12px;background:color-mix(in srgb,var(--accent) 12%,white);color:var(--accent);
        font-size:31px;font-weight:700;flex-shrink:0}
    .pend-kpi-number {font-size:31px}
    .pend-kpi-title {margin-top:2px}
    .pend-list-title {text-align:left;margin:7px 0 4px;font-size:23px}
    .stMainBlockContainer:has(.pend-hero) [data-testid="stVerticalBlockBorderWrapper"] {
        border-color:#d9e3ea;box-shadow:0 4px 14px rgba(22,43,57,.06)}
    .pend-oficio {padding:11px 4px 12px 52px;position:relative}
    .pend-oficio:before {content:'▤';position:absolute;left:0;top:14px;background:#edf2f7;
        border-radius:10px;width:40px;height:45px;display:flex;align-items:center;justify-content:center;
        color:#24445b;font-size:25px}
    .pend-oficio-top {font-size:16px}
    .pend-asunto {font-size:14px}
    .pend-meta {font-size:12px;color:#60798a}
    @media(max-width:800px) {
      .stMainBlockContainer:has(.pend-hero) {padding-left:14px !important;padding-right:14px !important}
      .pend-hero-title {font-size:30px}
      .pend-hero-track {display:none}
      .pend-kpi {padding:14px;gap:10px}
      .pend-kpi-icon {width:36px;height:40px;font-size:23px}
    }
/* Ancho real de trabajo y fondo con menor protagonismo. */
    .stMainBlockContainer:has(.pend-hero) {
        max-width: 1580px !important;
        width: 96% !important;
        padding: 1.1rem 24px 4rem !important;
    }
    .stMainBlockContainer:has(.pend-hero) > div {gap: .9rem !important;}
    [data-testid="stAppViewContainer"]:has(.pend-hero) {
        background-color: #edf2f6 !important;
        background-blend-mode: normal !important;
    }
    /* Cabecera institucional compacta. */
    .pend-hero {
        min-height: 125px !important;
        padding: 23px 32px !important;
        background: linear-gradient(112deg,rgba(255,255,255,.97),rgba(242,247,251,.94)) !important;
        border: 1px solid #dce5ec !important;
        border-left: 5px solid #ba9554 !important;
        border-bottom: 1px solid #dce5ec !important;
        border-radius: 16px !important;
        box-shadow: 0 9px 25px rgba(20,45,63,.07) !important;
    }
    .pend-hero-title {font-size: 33px !important; margin-top: 6px !important;}
    .pend-hero-kicker {color:#987642 !important;font-size:10px !important;}
    .pend-hero-line {display:none !important;}
    /* Bandejas: cuatro módulos uniformes con selección visible. */
    div[class*="st-key-pend_nav_"] {
        min-height: 95px !important;
        background: rgba(255,255,255,.97) !important;
        border: 1px solid #dbe5ec !important;
        border-radius: 13px !important;
        padding: 15px 17px !important;
        box-shadow: 0 5px 15px rgba(25,50,67,.045) !important;
    }
    div[class*="st-key-pend_nav_"][class*="_activo"] {
        background: #1e3a4e !important;
        border-color: #c4a36b !important;
        box-shadow: 0 8px 20px rgba(19,44,61,.16) !important;
    }
    div[class*="st-key-pend_nav_"] button {font-size:15px !important;}
    div[class*="st-key-pend_nav_"] .pend-nav-caption {font-size:12px !important;}
    /* Resumen: cifras proporcionadas y sin líneas de color superiores. */
    .pend-kpi {
        min-height: 100px !important;
        padding: 15px 20px !important;
        border: 1px solid #dfe7ed !important;
        box-shadow: 0 5px 17px rgba(18,45,62,.05) !important;
    }
    .pend-kpi-number {font-size:29px !important;}
    .pend-kpi-icon {width:47px !important;height:47px !important;font-size:25px !important;}
    .pend-list-title {font-size:23px !important;color:#1b394c !important;}
    /* Fichas de expediente: tarjeta ligera, separada del contenido editable. */
    div[class*="st-key-pend_ficha_"] {
        background: rgba(255,255,255,.97) !important;
        border: 1px solid #dbe4eb !important;
        border-radius: 15px !important;
        padding: 12px 18px !important;
        box-shadow: 0 7px 20px rgba(18,44,63,.055) !important;
        margin-bottom: 8px !important;
    }
    div[class*="st-key-pend_ficha_"] .pend-oficio {padding-top: 8px !important;padding-bottom:8px !important;}
    div[class*="st-key-pend_ficha_"] button[kind="primary"] {
        border-radius:9px !important;background:#24445a !important;
        border-color:#24445a !important;min-height:42px !important;
    }
    /* El editor deja de ser un bloque oscuro dentro del expediente. */
    .st-key-pend_editor {
        background:#f8fafc !important;
        border:1px solid #d9e4eb !important;
        border-radius:14px !important;
        padding:23px 27px 26px !important;
        box-shadow:none !important;
        margin-top: 12px !important;
    }
    .st-key-pend_editor [data-testid="stVerticalBlockBorderWrapper"] {
        background:transparent !important;
        border-color:transparent !important;
        box-shadow:none !important;
    }
    .pend-editor-header {padding-bottom:12px !important;border-bottom:1px solid #dce5ec !important;}
    .pend-editor-kicker {color:#a17c42 !important;}
    .pend-editor-title {color:#1d3a4e !important;font-size:22px !important;}
    .pend-editor-sub {color:#62798a !important;}
    .pend-editor-section {
        color:#28465b !important;
        text-align:left !important;
        font-size:12px !important;
        border-bottom:1px solid #dce5eb !important;
        padding:16px 0 10px !important;
    }
    .pend-editor-sign {color:#29465a !important;}
    .pend-editor-divider {background:#dce5eb !important;}
    .st-key-pend_editor label,
    .st-key-pend_editor [data-testid="stWidgetLabel"] p,
    .st-key-pend_editor [data-testid="stForm"] label p {
        color:#244358 !important;font-weight:650 !important;
    }
    .st-key-pend_editor input:not([type="checkbox"]),
    .st-key-pend_editor textarea,
    .st-key-pend_editor [data-baseweb="select"] > div,
    .st-key-pend_editor [data-baseweb="input"] > div,
    .st-key-pend_editor [data-baseweb="textarea"] > div {
        background:#fff !important;color:#18374b !important;
        border:1px solid #cbd9e3 !important;border-radius:8px !important;
    }
    .st-key-pend_editor input::placeholder,
    .st-key-pend_editor textarea::placeholder {color:#7a8e9b !important;}
    .st-key-pend_editor [data-baseweb="select"] span,
    .st-key-pend_editor [data-baseweb="select"] input {color:#18374b !important;}
    .st-key-pend_editor [data-testid="stFormSubmitButton"] button {
        background:#25465b !important;color:#fff !important;
        border:1px solid #25465b !important;border-radius:9px !important;
        min-height:44px !important;
    }
    .st-key-pend_editor [data-testid="stFormSubmitButton"] button:hover {
        background:#163448 !important;border-color:#163448 !important;
    }
    /* Cabecera del centro de revisión: acorde al editor claro. */
    .doc-center-head {
        background:linear-gradient(110deg,#213e52,#2d5164) !important;
        border-radius:11px !important;
        padding:15px 19px !important;
        margin:12px 0 !important;
    }
    .doc-center-title {font-size:19px !important;}
    /* Evita bordes y sombras globales aplicados a cada contenedor anidado. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stVerticalBlockBorderWrapper"] {
        box-shadow:none;
    }
    @media(max-width:900px) {
        .stMainBlockContainer:has(.pend-hero) {width:100% !important;padding:12px 13px 3rem !important;}
        .pend-hero {padding:20px !important;}
        .pend-hero-title {font-size:28px !important;}
        .st-key-pend_editor {padding:16px !important;}
    }
div[class*="st-key-revisor_expediente_"] {
        background:linear-gradient(145deg,#e5e7e8 0%,#d9dcde 100%) !important;
        border:1px solid #b8bdc0 !important;
        border-left:4px solid #bd9553 !important;
        border-radius:13px !important;
        padding:12px 14px !important;
        box-shadow:0 5px 17px rgba(29,34,37,.12) !important;
        margin:10px 0 14px !important;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-head {
        background:linear-gradient(110deg,#3c4043,#515558) !important;
        border:1px solid #64696b !important;
        border-left:3px solid #d2ac6a !important;
        border-radius:9px !important;
        padding:12px 14px !important;
        min-height:65px !important;
        margin-bottom:6px !important;
    }
    div[class*="st-key-revisor_expediente_"] .rev-compact-kicker {color:#e5bf7d !important;font-size:9px !important;letter-spacing:1.2px !important;}
    div[class*="st-key-revisor_expediente_"] .rev-compact-title {color:#fff !important;font-size:17px !important;line-height:1.3 !important;overflow-wrap:anywhere !important;}
    div[class*="st-key-revisor_expediente_"] .rev-compact-meta {color:#e0e2e3 !important;font-size:11px !important;}
    div[class*="st-key-revisor_expediente_"] .rev-compact-badge {font-size:9px !important;letter-spacing:.3px !important;max-width:190px !important;white-space:normal !important;text-align:center !important;}
    div[class*="st-key-revisor_expediente_"] .rev-compact-next {color:#51565a !important;font-size:11px !important;}
    div[class*="st-key-revisor_expediente_"] .rev-compact-note {background:#faf3e6 !important;border:1px solid #dfc69a !important;border-left:3px solid #c39751 !important;}
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] {background:#f1f2f2 !important;border:1px solid #b9c0c4 !important;}
    div[class*="st-key-revisor_expediente_"] [data-testid="stExpander"] summary {color:#333b40 !important;}
    .doc-center-head {background:linear-gradient(110deg,#373b3e,#515558) !important;border-left:4px solid #c69c58 !important;border-radius:10px !important;padding:13px 17px !important;margin:10px 0 12px !important;}
    .doc-center-kicker {color:#e3bd7e !important;font-size:9px !important;}
    .doc-center-title {font-size:17px !important;line-height:1.25 !important;margin-top:5px !important;color:#fff !important;}
    .doc-center-sub {font-size:11px !important;color:#e0e2e2 !important;}
    .doc-summary-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:11px 0 13px;}
    .doc-summary-item {background:#e7e9e9;border:1px solid #c3c8ca;border-radius:9px;padding:10px 12px;min-width:0;display:flex;flex-direction:column;gap:5px;}
    .doc-summary-item span {color:#666c70;font-size:9px;letter-spacing:1px;font-weight:750;text-align:center;}
    .doc-summary-item strong {color:#30383d;font-size:17px;font-weight:750;line-height:1.25;overflow-wrap:anywhere;text-align:center;}
    .doc-summary-status strong {font-size:12px;color:#6b542f;line-height:1.35;}
    div[class*="st-key-revisor_expediente_"] .revision-feedback {padding:12px 14px !important;margin:10px 0 !important;background:#f9f4ea !important;}
    div[class*="st-key-revisor_expediente_"] .revision-feedback-title {font-size:14px !important;}
    div[class*="st-key-revisor_expediente_"] .revision-feedback-text {font-size:12px !important;padding:10px 12px !important;margin:9px 0 !important;}
    div[class*="st-key-revisor_expediente_"] .revision-feedback-hint {font-size:11px !important;margin-top:9px !important;padding-top:8px !important;}
    @media(max-width:680px) {
        .doc-summary-grid {grid-template-columns:repeat(2,minmax(0,1fr));}
        .doc-summary-status {grid-column:1/-1;}
        div[class*="st-key-revisor_expediente_"] .rev-compact-head {flex-wrap:wrap !important;}
    }
/* No sustituir la imagen ferroviaria del módulo: solo quitar el velo que la borraba. */
    [data-testid="stAppViewContainer"]:has(.pend-hero) {
        background-color:transparent !important;
        background-blend-mode:normal !important;
    }
    .stMainBlockContainer:has(.pend-hero) {
        max-width:1240px !important;
        width:calc(100% - 48px) !important;
        padding:2rem 12px 4rem !important;
    }
    /* Cabecera azul marino idéntica en carácter al módulo principal. */
    .pend-hero {
        min-height:174px !important;
        padding:33px 38px !important;
        background:linear-gradient(112deg,#142c3e 0%,#1e394b 68%,#304f5b 100%) !important;
        border:0 !important;
        border-left:4px solid #c4a063 !important;
        border-radius:12px !important;
        box-shadow:0 8px 22px rgba(16,39,55,.13) !important;
        color:white !important;
    }
    .pend-hero-kicker {color:#e2b878 !important;font-size:10px !important;letter-spacing:2px !important;}
    .pend-hero-title {color:#fff !important;font-size:31px !important;margin-top:12px !important;}
    .pend-hero-desc {color:#d6e3ec !important;font-size:13px !important;}
    .pend-hero-line {display:block !important;width:39px !important;height:2px !important;
        margin-top:19px !important;background:#d7ae69 !important;}
    .pend-hero-track {color:#b7cbd5 !important;}
    .pend-hero:after {opacity:.18 !important;}
    /* Bandejas compactas: sin enormes botones incrustados en cajas. */
    .pend-nav-label,.pend-section-label {text-align:left !important;margin-top:21px !important;}
    div[class*="st-key-pend_nav_"] {
        min-height:0 !important;
        padding:9px 11px !important;
        background:rgba(255,255,255,.97) !important;
        border:1px solid #dbe3e9 !important;
        border-radius:11px !important;
        box-shadow:0 4px 13px rgba(25,50,67,.05) !important;
    }
    div[class*="st-key-pend_nav_"] .pend-nav-caption {
        color:#6b7f8e !important;font-size:11px !important;margin:1px 3px 5px !important;
    }
    div[class*="st-key-pend_nav_"] button {
        min-height:42px !important;height:auto !important;
        background:#f3f6f8 !important;color:#244358 !important;
        border:1px solid #e0e7ec !important;border-radius:7px !important;
        font-size:13px !important;font-weight:650 !important;
    }
    div[class*="st-key-pend_nav_"] button p {color:inherit !important;}
    div[class*="st-key-pend_nav_"][class*="_activo"] {
        background:#fff !important;border-color:#c6a66b !important;
        box-shadow:0 4px 15px rgba(27,56,73,.08) !important;
    }
    div[class*="st-key-pend_nav_"][class*="_activo"] .pend-nav-caption {color:#5b6e7c !important;}
    div[class*="st-key-pend_nav_"][class*="_activo"] button {
        background:#253f51 !important;color:#fff !important;border-color:#253f51 !important;
    }
    .pend-nav-disabled {padding:10px 8px !important;min-height:42px !important;}
    /* Panorama documental: tarjetas pequeñas, proporcionales al menú principal. */
    .pend-kpi {
        min-height:102px !important;padding:16px 19px !important;gap:14px !important;
        border:1px solid #dfe6eb !important;border-radius:11px !important;
        box-shadow:0 5px 16px rgba(20,44,60,.055) !important;
        background:rgba(255,255,255,.98) !important;
    }
    .pend-kpi-number {font-size:29px !important;}
    .pend-kpi-icon {width:43px !important;height:43px !important;font-size:21px !important;}
    .pend-list-title {text-align:left !important;font-size:20px !important;margin:8px 0 !important;}
    /* Fichas y formularios: superficie blanca, borde dorado sutil en zona de trabajo. */
    div[class*="st-key-pend_ficha_"] {
        border:1px solid #dce4e9 !important;
        background:rgba(255,255,255,.98) !important;
        border-radius:11px !important;
        box-shadow:0 5px 15px rgba(25,50,67,.05) !important;
    }
    .st-key-pend_editor {background:#fff !important;border-radius:10px !important;}
    @media(max-width:900px) {
        .stMainBlockContainer:has(.pend-hero) {width:100% !important;padding:1rem 12px 3rem !important;}
        .pend-hero {min-height:150px !important;padding:24px !important;}
        .pend-hero-title {font-size:27px !important;}
    }
/* Expedientes de revisión final: quitar el azul en todos los estados. */
    div[class*="st-key-tte_expediente_"] .tte-file-top {background:#44423d !important;border-top-color:#c69a55 !important;}
    div[class*="st-key-tte_expediente_"] .tte-file-label {color:#f4c781 !important;}
    div[class*="st-key-tte_expediente_"] .tte-file-title,
    div[class*="st-key-tte_expediente_"] .tte-file-meta,
    div[class*="st-key-tte_expediente_"] .tte-step-badge,
    div[class*="st-key-tte_expediente_"] .tte-step-badge strong,
    div[class*="st-key-tte_expediente_"] .tte-document-line strong,
    div[class*="st-key-tte_expediente_"] .tte-decision-title {color:#302d27 !important;}
    div[class*="st-key-tte_expediente_"] .tte-timeline {background:#f2eee6 !important;border-color:#c9bba6 !important;}
    div[class*="st-key-tte_expediente_"] .tte-step {background:#e9e3d9 !important;border:1px solid #c8bca9 !important;color:#3e3a33 !important;}
    div[class*="st-key-tte_expediente_"] .tte-step.done {background:#e4eee5 !important;color:#22543d !important;border-color:#b4c9b8 !important;}
    div[class*="st-key-tte_expediente_"] .tte-step.current {background:#e9d6b6 !important;color:#4b3219 !important;border-color:#ad8247 !important;}
    div[class*="st-key-tte_expediente_"] .tte-step.current span {background:#765329 !important;color:#fffaf1 !important;}
    div[class*="st-key-tte_expediente_"] .tte-step:not(.current):not(.done) span {background:#d0c5b4 !important;color:#423a30 !important;}
    div[class*="st-key-tte_expediente_"] .tte-document-line {background:#fffdf9 !important;border-color:#c9bba6 !important;border-left-color:#b88d50 !important;}
    div[class*="st-key-tte_expediente_"] [data-testid="stForm"] {background:#f5f1e9 !important;border-color:#c9bba6 !important;}
    div[class*="st-key-tte_expediente_"] textarea,
    div[class*="st-key-tte_expediente_"] [data-baseweb="textarea"] > div {background:#fffdf9 !important;color:#292923 !important;border-color:#bcae98 !important;}
    div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] {background:#f6f2ea !important;border-color:#c9bba6 !important;}
    div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] summary,
    div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] summary * {color:#302e29 !important;}
    /* Texto oscuro en tarjetas claras; blanco sobre las oscuras. */
    .rev-compact-head .rev-compact-title,
    .rev-compact-head .rev-compact-meta,
    .rev-compact-head .rev-compact-badge.ajustes {color:#fff9ed !important;}
    .rev-compact-head .rev-compact-badge.ajustes {background:#785528 !important;border-color:#e2c48d !important;}
    .rev-compact-note,.rev-compact-note span,.rev-compact-next {color:#38352e !important;}
    .rev-compact-head .rev-compact-kicker {color:#efc17d !important;}
    /* El botón activo se había quedado con texto oscuro sobre grafito. */
    div[class*="st-key-pend_nav_"][class*="_activo"] button,
    div[class*="st-key-pend_nav_"][class*="_activo"] button * {background:#504c43 !important;background-color:#504c43 !important;color:#fffaf0 !important;-webkit-text-fill-color:#fffaf0 !important;opacity:1 !important;}
    div[class*="st-key-pend_nav_"][class*="_activo"] button:disabled,
    div[class*="st-key-pend_nav_"][class*="_activo"] button:disabled * {background:#504c43 !important;color:#fffaf0 !important;-webkit-text-fill-color:#fffaf0 !important;opacity:1 !important;}
    div[class*="st-key-pend_nav_"] .pend-nav-caption {color:#62594b !important;}
    /* Etiqueta de correcciones: contraste alto en vez de blanco sobre marfil. */
    .rev-compact-badge.ajustes {background:#775329 !important;border-color:#b88c4c !important;color:#fff9ed !important;}
    .rev-compact-badge.ajustes * {color:#fff9ed !important;}
    /* Formularios y etiquetas legibles en ambas bandejas. */
    div[class*="st-key-tte_expediente_"] [data-testid="stWidgetLabel"],
    div[class*="st-key-tte_expediente_"] [data-testid="stWidgetLabel"] *,
    div[class*="st-key-tte_expediente_"] [data-testid="stCaptionContainer"],
    div[class*="st-key-tte_expediente_"] [data-testid="stCaptionContainer"] * {color:#4d453a !important;}
    div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button {background:#4c4840 !important;color:#fffaf0 !important;border:1px solid #b98c4f !important;}
/* Encabezado principal: grafito con acento cobre, sin azul ni morado. */
    .pend-hero {
        background:linear-gradient(112deg,#30312f 0%,#484843 70%,#625a4e 100%) !important;
        border-left:4px solid #c69a53 !important;
        box-shadow:0 9px 24px rgba(38,35,29,.17) !important;
    }
    .pend-hero:after {opacity:.10 !important;}
    .pend-hero-kicker {color:#edc88c !important;}
    .pend-hero-title {color:#fffdf8 !important;}
    .pend-hero-desc,.pend-hero-track {color:#e2ddd3 !important;}
    .tte-queue-header {
        background:linear-gradient(110deg,#534c3e,#75634b) !important;
        border-left:4px solid #d7b67b !important;
        box-shadow:0 7px 22px rgba(59,50,36,.16) !important;
    }
    .tte-queue-eyebrow {color:#f0d49e !important;}
    .tte-queue-description {color:#f4ece0 !important;}
    /* Métricas del revisor: gris piedra claro y alto contraste. */
    .tte-summary-card {
        background:linear-gradient(145deg,#e5e0d7,#d6d0c5) !important;
        border:1px solid #bdb4a5 !important;
        border-left:4px solid #ad8150 !important;
        box-shadow:0 5px 14px rgba(50,46,39,.10) !important;
    }
    .tte-summary-label {color:#534a3e !important;}
    .tte-summary-value,.tte-summary-card--final .tte-summary-value {color:#2f302d !important;}
    /* Navegación y fichas de expedientes. */
    div[class*="st-key-pend_nav_"] {background:#eeece8 !important;border-color:#d1cbc0 !important;}
    div[class*="st-key-pend_nav_"] button {
        background:#e3e0da !important;color:#383b39 !important;border-color:#cfc9bf !important;
    }
    div[class*="st-key-pend_nav_"][class*="_activo"] {
        background:#e6e0d5 !important;border-color:#bc965e !important;
    }
    div[class*="st-key-pend_nav_"][class*="_activo"] button {
        background:#56554d !important;color:#fff !important;border-color:#56554d !important;
    }
    div[class*="st-key-revisor_expediente_"],div[class*="st-key-pend_ficha_"] {
        background:linear-gradient(140deg,#eeece8,#e4e2de) !important;
        border:1px solid #bcbab3 !important;
        border-left:4px solid #c79a53 !important;
        box-shadow:0 6px 17px rgba(48,44,38,.13) !important;
        border-radius:12px !important;
    }
    .rev-compact-head {background:linear-gradient(110deg,#3c3d3a,#55544f) !important;}
    .doc-center-head {background:linear-gradient(110deg,#454641,#605e55) !important;}
    .doc-summary-item {background:#e9e5df !important;border-color:#c9c1b5 !important;}
    .doc-summary-item strong {color:#343531 !important;}
    .doc-summary-status strong {color:#74572f !important;}
    .rev-list-intro h4 {color:#343631 !important;}
    .rev-list-count {background:#eee4d3 !important;color:#70502c !important;}
    /* Acciones de envío y reenvío. */
    div[class*="st-key-doc_reenviar_"] button,div[class*="st-key-doc_tte_"] button {
        background:#514e47 !important;color:#fff !important;
        border:1px solid #bd955b !important;border-left:4px solid #d2aa6a !important;
    }
    div[class*="st-key-doc_reenviar_"] button:hover,div[class*="st-key-doc_tte_"] button:hover {
        background:#696153 !important;color:#fff !important;
    }
    @media(max-width:760px) {
        .tte-summary-card {min-height:88px !important;padding:12px 6px !important;}
        .tte-summary-value {font-size:20px !important;}
    }
/* Solo el módulo de pendientes; los otros módulos quedan intactos. */
    [data-testid="stAppViewContainer"]:has(.pend-hero) {
        --pend-carbon:#41413d; --pend-carbon-hover:#5c574e;
        --pend-stone:#e9e5de; --pend-ivory:#f7f4ee;
        --pend-copper:#bb9255; --pend-border:#c9c0b3;
        --pend-ink:#343630;
    }
    /* Encabezados y barras internas de expedientes. */
    .stMainBlockContainer:has(.pend-hero) .rev-compact-head,
    .stMainBlockContainer:has(.pend-hero) .tte-file-header,
    .stMainBlockContainer:has(.pend-hero) .doc-center-head,
    .stMainBlockContainer:has(.pend-hero) .doc-center-title-bar {
        background:linear-gradient(110deg,#41413d,#625c51) !important;
        border-color:#bd955a !important;
    }
    .stMainBlockContainer:has(.pend-hero) .rev-compact-head *,
    .stMainBlockContainer:has(.pend-hero) .doc-center-head * {color:#faf8f3 !important;}
    /* Indicadores, etapas, etiquetas y elementos de historial. */
    .stMainBlockContainer:has(.pend-hero) .rev-compact-step,
    .stMainBlockContainer:has(.pend-hero) .rev-compact-badge,
    .stMainBlockContainer:has(.pend-hero) .tte-step-badge,
    .stMainBlockContainer:has(.pend-hero) .tte-file-badge,
    .stMainBlockContainer:has(.pend-hero) .doc-summary-item,
    .stMainBlockContainer:has(.pend-hero) .rev-list-count,
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card {
        background:#e7e1d6 !important;border-color:#c7b9a4 !important;
        color:#3f3c35 !important;
    }
    .stMainBlockContainer:has(.pend-hero) .rev-compact-step *,
    .stMainBlockContainer:has(.pend-hero) .tte-step-badge *,
    .stMainBlockContainer:has(.pend-hero) .doc-summary-item *,
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card * {color:#3f3c35 !important;}
    /* Progreso: se eliminan fondos celestes, manteniendo el verde de completado. */
    .stMainBlockContainer:has(.pend-hero) .rev-step-active,
    .stMainBlockContainer:has(.pend-hero) .rev-compact-step--active,
    .stMainBlockContainer:has(.pend-hero) .tte-progress-active,
    .stMainBlockContainer:has(.pend-hero) .tte-progress-current,
    .stMainBlockContainer:has(.pend-hero) [class*="step-current"],
    .stMainBlockContainer:has(.pend-hero) [class*="step-active"] {
        background:#e9dcc5 !important;color:#604725 !important;
        border-color:#bd9458 !important;
    }
    /* Tarjetas de expedientes, archivos y zonas de observaciones. */
    .stMainBlockContainer:has(.pend-hero) .rev-compact-file,
    .stMainBlockContainer:has(.pend-hero) .rev-compact-note,
    .stMainBlockContainer:has(.pend-hero) .tte-file-card,
    .stMainBlockContainer:has(.pend-hero) .revision-feedback,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stExpander"] {
        background:#f5f2ec !important;border-color:#c8beb0 !important;
    }
    /* Todos los botones Streamlit dentro de Mis pendientes. */
    .stMainBlockContainer:has(.pend-hero) button[kind="primary"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stBaseButton-primary"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stFormSubmitButton"] button {
        background:#494943 !important;background-color:#494943 !important;
        color:#fffdf8 !important;border:1px solid #b99159 !important;
        box-shadow:none !important;
    }
    .stMainBlockContainer:has(.pend-hero) button[kind="primary"] *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stBaseButton-primary"] *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stFormSubmitButton"] button * {color:#fffdf8 !important;}
    .stMainBlockContainer:has(.pend-hero) button[kind="secondary"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stBaseButton-secondary"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stLinkButton"] a,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stDownloadButton"] button {
        background:#f0ece5 !important;background-color:#f0ece5 !important;
        color:#42413a !important;border:1px solid #c5b9a8 !important;
        box-shadow:none !important;
    }
    .stMainBlockContainer:has(.pend-hero) button[kind="secondary"] *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stBaseButton-secondary"] *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stLinkButton"] a *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stDownloadButton"] button * {color:#42413a !important;}
    .stMainBlockContainer:has(.pend-hero) button:hover,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stLinkButton"] a:hover {
        border-color:#aa7d43 !important;filter:brightness(.95);
    }
    /* Controles de formulario: búsqueda, selección, radio y observaciones. */
    .stMainBlockContainer:has(.pend-hero) [data-baseweb="input"] > div,
    .stMainBlockContainer:has(.pend-hero) [data-baseweb="textarea"] > div,
    .stMainBlockContainer:has(.pend-hero) [data-baseweb="select"] > div {
        background:#f7f4ee !important;border-color:#c8beb0 !important;
        box-shadow:none !important;
    }
    .stMainBlockContainer:has(.pend-hero) input,
    .stMainBlockContainer:has(.pend-hero) textarea,
    .stMainBlockContainer:has(.pend-hero) [data-baseweb="select"] span {
        color:#383a34 !important;background-color:transparent !important;
    }
    .stMainBlockContainer:has(.pend-hero) input:focus,
    .stMainBlockContainer:has(.pend-hero) textarea:focus {outline-color:#b99159 !important;}
    .stMainBlockContainer:has(.pend-hero) [role="radio"][aria-checked="true"],
    .stMainBlockContainer:has(.pend-hero) [role="checkbox"][aria-checked="true"] {
        background-color:#a67c46 !important;border-color:#a67c46 !important;
    }
    /* Mensajes informativos sin paneles azulados. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stAlert"]:has([data-testid="stNotificationContentInfo"]),
    .stMainBlockContainer:has(.pend-hero) [data-testid="stAlert"]:has([data-testid="stNotificationContentWarning"]) {
        background:#eee7da !important;color:#514632 !important;
        border-color:#c9b58e !important;
    }
    /* Prioridad a los botones de navegación activos. */
    div[class*="st-key-pend_nav_"][class*="_activo"] button,
    div[class*="st-key-pend_nav_"][class*="_activo"] button * {
        background:#56534b !important;color:#fff !important;
    }

    /* Ajuste final: contraste y eliminación completa de celestes en pendientes. */
    .stMainBlockContainer:has(.pend-hero) .tte-file-title,
    .stMainBlockContainer:has(.pend-hero) .tte-file-meta,
    .stMainBlockContainer:has(.pend-hero) .tte-step-badge,
    .stMainBlockContainer:has(.pend-hero) .tte-step-badge strong,
    .stMainBlockContainer:has(.pend-hero) .tte-document-line strong,
    .stMainBlockContainer:has(.pend-hero) .tte-decision-title,
    .stMainBlockContainer:has(.pend-hero) .rev-list-intro h4,
    .stMainBlockContainer:has(.pend-hero) .rev-compact-note,
    .stMainBlockContainer:has(.pend-hero) .rev-compact-file {color:#292b27 !important;}
    .stMainBlockContainer:has(.pend-hero) .tte-file-top {
        background:#45443f !important;border-top:3px solid #c59a59 !important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-file-top .tte-file-label {color:#f0cb8c !important;}
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] {
        background:linear-gradient(145deg,#e7e2da,#d9d4cb) !important;
        border:1px solid #bcb2a3 !important;border-left:4px solid #bd9050 !important;
        box-shadow:0 9px 24px rgba(52,46,37,.13) !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-timeline {
        background:#f3efe8 !important;border:1px solid #c8bfb0 !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step {
        background:#e7e1d7 !important;border:1px solid #cfc4b4 !important;
        color:#393a34 !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.current {
        background:#e7d4b3 !important;border-color:#aa824b !important;color:#49341d !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.current span {
        background:#8b6535 !important;color:#fffaf1 !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-document-line {
        background:#faf8f3 !important;border:1px solid #c8bdac !important;
        border-left:3px solid #b58b4f !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] {
        background:#f4f0e9 !important;border:1px solid #c8bcac !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] textarea,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] [data-baseweb="textarea"] > div {
        background:#fffdfa !important;color:#282a26 !important;border-color:#bfb19e !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] {
        background:#f5f1ea !important;border-color:#c8bcac !important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button {
        background:#47443e !important;color:#fffaf1 !important;border:1px solid #c39a5b !important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-step:not(.current):not(.done) span {
        background:#d7cfc1 !important;color:#4b453b !important;
    }
    /* Legibilidad de texto general sobre fondos claros. */
    .stMainBlockContainer:has(.pend-hero) [data-testid="stExpander"] summary,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stExpander"] summary *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] label,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stForm"] label *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stCaptionContainer"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stCaptionContainer"] *,
    .stMainBlockContainer:has(.pend-hero) [data-testid="stWidgetLabel"],
    .stMainBlockContainer:has(.pend-hero) [data-testid="stWidgetLabel"] * {
        color:#41413b !important;
    }
    .stMainBlockContainer:has(.pend-hero) input::placeholder,
    .stMainBlockContainer:has(.pend-hero) textarea::placeholder {color:#746e64 !important;opacity:1 !important;}
    .stMainBlockContainer:has(.pend-hero) button:disabled,
    .stMainBlockContainer:has(.pend-hero) button:disabled * {
        color:#56534b !important;background:#d7d0c5 !important;opacity:1 !important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-file-chip {color:#65451f !important;}
    /* RESUMEN DE ACTIVIDAD — tarjetas marfil con acento institucional discreto. */
    .stMainBlockContainer:has(.pend-hero) .pend-kpi {
        background:rgba(247,244,238,.97) !important;
        border:1px solid #d9d0c3 !important;
        border-left:3px solid var(--accent,#bd9a64) !important;
        border-radius:12px !important;
        box-shadow:0 5px 16px rgba(61,52,40,.085) !important;
        min-height:102px !important;
        padding:16px 19px !important;
        gap:14px !important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-kpi-icon {
        background:color-mix(in srgb,var(--accent,#bd9a64) 12%,#f7f4ee) !important;
        color:var(--accent,#bd9a64) !important;
        border-radius:11px !important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-kpi-number,
    .stMainBlockContainer:has(.pend-hero) .pend-kpi-title {color:#243b48 !important;}
    .stMainBlockContainer:has(.pend-hero) .pend-kpi-desc {color:#6f6b62 !important;}
.stMainBlockContainer:has(.pend-hero) {
        --pg-ink:#493B46; --pg-deep:#332B35; --pg-copper:#BC936B;
        --pg-ivory:#F5F0E8; --pg-cream:#FFFDF9; --pg-muted:#756D72;
        max-width:1320px!important;
    }
    /* Cabecera: imagen transparente a la derecha, texto siempre por encima. */
    .stMainBlockContainer:has(.pend-hero) .pend-hero {
        isolation:isolate!important;
        position:relative!important;
        min-height:177px!important;
        padding:26px 36px 25px 38px!important;
        overflow:hidden!important;
        background:linear-gradient(112deg,#342C36 0%,#493B46 61%,#514149 100%)!important;
        border:1px solid rgba(171,123,91,.55)!important;
        border-left:4px solid #C59269!important;
        border-radius:14px!important;
        box-shadow:0 15px 30px rgba(57,39,46,.18)!important;
        color:#FFFDF9!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero::before {
        content:""!important;
        display:block!important;
        position:absolute!important;
        inset:auto 0 0 auto!important;
        width:75%!important;
        height:100%!important;
        background:url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4NjAgMjQwIiBmaWxsPSJub25lIj48ZyBzdHJva2U9IiNDNzlCNzIiIG9wYWNpdHk9Ii4zMiIgc3Ryb2tlLXdpZHRoPSIuOCI+PHBhdGggZD0iTS0yMCAyMzBDMjA4IDIyNiAyOTkgMTcxIDQyMSAxNTNTNjcwIDY5IDkxMS05Ii8+PHBhdGggZD0iTS0yMCAyNDJDMjEyIDIzNyAzMDQgMTgzIDQyNCAxNjVTNjc3IDgxIDkxMyAzIi8+PHBhdGggZD0iTS0yMCAyNTZDMjE2IDI1MiAzMDkgMTk3IDQyOCAxNzdTNjgyIDk1IDkxOSAxNyIvPjxwYXRoIGQ9Ik0tMjAgMjY4QzIyMCAyNjIgMzE1IDIwOSA0MzQgMTkxUzY4OCAxMTAgOTI1IDMyIi8+PC9nPjxnIHN0cm9rZT0iI0U2QzRBMSIgb3BhY2l0eT0iLjE5IiBzdHJva2Utd2lkdGg9Ii43Ij48cGF0aCBkPSJNMCAyMDVIODYwTTAgMTY1SDg2ME0wIDEyNUg4NjBNMCA4NUg4NjBNMCA0NUg4NjBNMTAwIDBWMjQwTTIyMCAwVjI0ME0zNDAgMFYyNDBNNDYwIDBWMjQwTTU4MCAwVjI0ME03MDAgMFYyNDBNODIwIDBWMjQwIi8+PHBhdGggZD0iTTMwMCAwTDc3MCAyNDBNMzkyIDBMODUwIDI0MCIvPjwvZz48ZyBmaWxsPSIjQ0VBNDdBIj48Y2lyY2xlIGN4PSI0MjAiIGN5PSIxNTMiIHI9IjMiIG9wYWNpdHk9Ii43MiIvPjxjaXJjbGUgY3g9IjU5MCIgY3k9IjEwOSIgcj0iMyIgb3BhY2l0eT0iLjY4Ii8+PGNpcmNsZSBjeD0iNzE4IiBjeT0iNTciIHI9IjMiIG9wYWNpdHk9Ii42OCIvPjwvZz48L3N2Zz4=") right bottom / contain no-repeat!important;
        opacity:.55!important;
        z-index:0!important;
        pointer-events:none!important;
        transform:none!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero::after {
        content:""!important;
        display:block!important;
        position:absolute!important;
        right:0!important;left:auto!important;top:0!important;bottom:0!important;
        width:63%!important;
        height:100%!important;
        background-image:var(--pend-trazo,none)!important;
        background-position:right center!important;
        background-size:contain!important;
        background-repeat:no-repeat!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 32%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 32%)!important;
        opacity:.64!important;
        z-index:1!important;
        transform:none!important;
        filter:none!important;
        pointer-events:none!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {
        position:relative!important;
        z-index:2!important;
        max-width:58%!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-kicker {
        color:#E3B58D!important;
        font-size:10px!important;
        font-weight:800!important;
        letter-spacing:2.05px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-title {
        color:#FFFBF8!important;
        font-family:inherit!important;
        font-size:clamp(28px,2.7vw,40px)!important;
        line-height:1.1!important;
        margin:15px 0 7px!important;
        font-weight:800!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-desc {
        color:#E8DDE0!important;font-size:14px!important;
        line-height:1.45!important;margin:0!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-line {
        display:block!important;
        width:54px!important;height:3px!important;
        margin:16px 0 0!important;
        background:#CB9871!important;
        border-radius:10px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-hero-track {
        position:absolute!important;right:26px!important;bottom:18px!important;
        z-index:2!important;color:#E5BA99!important;
        font-size:9px!important;letter-spacing:1.4px!important;
        text-shadow:0 1px 4px #302630!important;
    }
    /* Bandejas: tarjetas editoriales, sin botón grande dentro de un recuadro. */
    .stMainBlockContainer:has(.pend-hero) .pend-nav-head {
        display:flex!important;justify-content:space-between!important;
        align-items:center!important;margin:7px 1px 2px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-nav-head span {
        font-size:11px!important;letter-spacing:2px!important;
        font-weight:850!important;color:#765247!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-nav-head small {
        font-size:9px!important;letter-spacing:1.45px!important;
        color:#8B777B!important;font-weight:750!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] {
        box-sizing:border-box!important;
        min-height:158px!important;
        padding:17px 19px 13px!important;
        background:linear-gradient(148deg,#FFFEFB,#F5F0EB)!important;
        border:1px solid #D7C9C0!important;
        border-radius:13px!important;
        box-shadow:0 8px 18px rgba(66,49,52,.09)!important;
        transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"]:hover {
        border-color:#BC936B!important;
        transform:translateY(-2px)!important;
        box-shadow:0 11px 22px rgba(66,49,52,.15)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] {
        background:linear-gradient(150deg,#FFFDF9,#EFE6DD)!important;
        border-color:#B98865!important;
        box-shadow:inset 3px 0 0 #BA8C68,0 8px 18px rgba(66,49,52,.12)!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-head {
        display:flex!important;gap:12px!important;align-items:center!important;
        min-height:49px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-icon {
        width:43px!important;height:43px!important;flex-shrink:0!important;
        display:inline-flex!important;align-items:center!important;justify-content:center!important;
        background:#F0DDD0!important;
        color:#775342!important;font-size:25px!important;font-weight:750!important;
        border-radius:50%!important;border:1px solid rgba(185,137,101,.15)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_2"] .pend-tile-icon {
        background:#E1E7DC!important;color:#526456!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_3"] .pend-tile-icon {
        background:#F3E8D8!important;color:#94724E!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-body {
        display:flex!important;flex-direction:column!important;gap:2px!important;
        min-width:0!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-body strong {
        color:#41333C!important;font-size:16px!important;
        font-weight:800!important;line-height:1.2!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-sub {
        color:#A16D50!important;font-size:11px!important;
        line-height:1.2!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {
        color:#746B70!important;font-size:11px!important;
        line-height:1.4!important;margin:11px 0 9px!important;
        min-height:31px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] button[kind="secondary"],
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] [data-testid="stBaseButton-secondary"] {
        background:transparent!important;background-color:transparent!important;
        color:#995C42!important;-webkit-text-fill-color:#995C42!important;
        border:0!important;border-top:1px solid #E3D8CE!important;
        border-radius:0!important;box-shadow:none!important;
        height:28px!important;min-height:28px!important;
        width:100%!important;text-align:right!important;
        justify-content:flex-end!important;
        padding:5px 0 0!important;
        font-weight:800!important;font-size:11px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] button * {
        color:#995C42!important;-webkit-text-fill-color:#995C42!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] button:hover {
        background:#F8EFE8!important;color:#78452F!important;
    }
    /* Centro de autorizaciones: un único panel claro con línea de trazo. */
    .stMainBlockContainer:has(.pend-hero) .tte-queue-header {
        isolation:isolate!important;
        position:relative!important;
        overflow:hidden!important;
        min-height:112px!important;
        margin:16px 0 9px!important;
        padding:21px 26px 18px 27px!important;
        border:1px solid #D4C3B4!important;
        border-left:4px solid #BA8C66!important;
        border-radius:12px!important;
        box-shadow:0 8px 18px rgba(66,49,52,.07)!important;
        background:linear-gradient(115deg,#FFFEFA 4%,#EEE6DD 100%)!important;
        color:#43323D!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-queue-header::after {
        content:""!important;display:block!important;
        position:absolute!important;right:0!important;bottom:0!important;
        width:52%!important;height:100%!important;
        background:url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA4NjAgMjQwIiBmaWxsPSJub25lIj48ZyBzdHJva2U9IiNDNzlCNzIiIG9wYWNpdHk9Ii4zMiIgc3Ryb2tlLXdpZHRoPSIuOCI+PHBhdGggZD0iTS0yMCAyMzBDMjA4IDIyNiAyOTkgMTcxIDQyMSAxNTNTNjcwIDY5IDkxMS05Ii8+PHBhdGggZD0iTS0yMCAyNDJDMjEyIDIzNyAzMDQgMTgzIDQyNCAxNjVTNjc3IDgxIDkxMyAzIi8+PHBhdGggZD0iTS0yMCAyNTZDMjE2IDI1MiAzMDkgMTk3IDQyOCAxNzdTNjgyIDk1IDkxOSAxNyIvPjxwYXRoIGQ9Ik0tMjAgMjY4QzIyMCAyNjIgMzE1IDIwOSA0MzQgMTkxUzY4OCAxMTAgOTI1IDMyIi8+PC9nPjxnIHN0cm9rZT0iI0U2QzRBMSIgb3BhY2l0eT0iLjE5IiBzdHJva2Utd2lkdGg9Ii43Ij48cGF0aCBkPSJNMCAyMDVIODYwTTAgMTY1SDg2ME0wIDEyNUg4NjBNMCA4NUg4NjBNMCA0NUg4NjBNMTAwIDBWMjQwTTIyMCAwVjI0ME0zNDAgMFYyNDBNNDYwIDBWMjQwTTU4MCAwVjI0ME03MDAgMFYyNDBNODIwIDBWMjQwIi8+PHBhdGggZD0iTTMwMCAwTDc3MCAyNDBNMzkyIDBMODUwIDI0MCIvPjwvZz48ZyBmaWxsPSIjQ0VBNDdBIj48Y2lyY2xlIGN4PSI0MjAiIGN5PSIxNTMiIHI9IjMiIG9wYWNpdHk9Ii43MiIvPjxjaXJjbGUgY3g9IjU5MCIgY3k9IjEwOSIgcj0iMyIgb3BhY2l0eT0iLjY4Ii8+PGNpcmNsZSBjeD0iNzE4IiBjeT0iNTciIHI9IjMiIG9wYWNpdHk9Ii42OCIvPjwvZz48L3N2Zz4=") right bottom / cover no-repeat!important;
        opacity:.32!important;pointer-events:none!important;z-index:0!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-queue-eyebrow,
    .stMainBlockContainer:has(.pend-hero) .tte-queue-heading,
    .stMainBlockContainer:has(.pend-hero) .tte-queue-description,
    .stMainBlockContainer:has(.pend-hero) .tte-queue-rail {position:relative!important;z-index:1!important;}
    .stMainBlockContainer:has(.pend-hero) .tte-queue-eyebrow {
        color:#A47457!important;font-size:10px!important;
        font-weight:800!important;letter-spacing:1.65px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-queue-heading {
        color:#40323C!important;font-size:24px!important;font-weight:800!important;
        line-height:1.2!important;margin-top:6px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-queue-description {
        color:#756D71!important;font-size:12px!important;line-height:1.35!important;
        margin-top:5px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-queue-rail {
        position:absolute!important;right:22px!important;bottom:17px!important;
        font-size:9px!important;letter-spacing:1.4px!important;
        color:#97847E!important;
    }
    /* Resumen con iconografía, números y borde cobre. No son botones. */
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card,
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card--final {
        box-sizing:border-box!important;
        min-height:100px!important;
        padding:16px!important;
        display:flex!important;flex-direction:row!important;
        justify-content:flex-start!important;align-items:center!important;
        gap:14px!important;
        border:1px solid #DACCC0!important;
        border-left:3px solid #C09A75!important;
        border-radius:12px!important;
        background:linear-gradient(145deg,#FFFDF9,#F2ECE5)!important;
        box-shadow:0 6px 15px rgba(66,49,52,.085)!important;
        text-align:left!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-glyph {
        display:flex!important;align-items:center!important;justify-content:center!important;
        width:50px!important;height:50px!important;flex-shrink:0!important;
        border-radius:50%!important;background:#F3DECF!important;
        color:#8A644A!important;font-size:25px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card:nth-child(2) .tte-summary-glyph {
        background:#DFE7D9!important;color:#60705C!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-data {
        display:flex!important;flex-direction:column!important;gap:4px!important;min-width:0!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-value,
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card--final .tte-summary-value {
        color:#41333C!important;font-size:28px!important;
        font-weight:850!important;line-height:1.03!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card--final .tte-summary-value {font-size:23px!important;}
    .stMainBlockContainer:has(.pend-hero) .tte-summary-label {
        color:#776A70!important;font-size:12px!important;
        font-weight:700!important;line-height:1.3!important;
    }
    @media(max-width:950px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {max-width:73%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero::after {width:57%!important;opacity:.40!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-rail {display:none!important;}
    }
    @media(max-width:650px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero {padding:21px!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero::after {opacity:.22!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero-track {display:none!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-nav-head small {display:none!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-summary-card {padding:12px!important;gap:9px!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-summary-glyph {width:38px!important;height:38px!important;font-size:19px!important;}
    }

    /* ===========================================================
       CORRECCIÓN PENDIENTES / DISTRIBUCIÓN Y BOTONES
       Solo interviene en esta pantalla; no cambia permisos ni callbacks.
       =========================================================== */
    @media (min-width: 1050px) {
      /* Recupera el espacio superior que ocupan los bloques CSS de Streamlit,
         manteniendo la cabecera institucional en su posición aprobada. */
      .stMainBlockContainer:has(.pend-hero) .pend-hero {
        /* La distancia hasta la cabecera queda en aprox. la mitad. */
        margin-top:16px!important;
      }
    }
    @media (min-width: 720px) and (max-width:1049px) {
      .stMainBlockContainer:has(.pend-hero) .pend-hero {
        margin-top:14px!important;
      }
    }
    @media (max-width:719px) {
      .stMainBlockContainer:has(.pend-hero) .pend-hero {
        margin-top:0!important;
      }
    }
    /* Solamente las tarjetas padre. La clave de los botones ahora es
       pend_action_*, para que NUNCA hereden estilos de tarjetas. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"],
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] {
      min-height:150px!important;
      height:auto!important;
      padding:15px 17px 12px!important;
      border-radius:12px!important;
      box-shadow:0 6px 15px rgba(66,49,52,.075)!important;
      display:flex!important;
      flex-direction:column!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] {
      box-shadow:inset 3px 0 0 #BA8C68,0 6px 15px rgba(66,49,52,.085)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"] > [data-testid="stVerticalBlock"],
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] > [data-testid="stVerticalBlock"] {
      gap:5px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-nav-head {
      margin:8px 1px 6px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-head {
      min-height:40px!important;
      gap:10px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-icon {
      width:39px!important;height:39px!important;font-size:22px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-body strong {
      font-size:15px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {
      min-height:29px!important;
      margin:7px 0 3px!important;
      line-height:1.35!important;
      font-size:11px!important;
    }
    /* El control real ocupa una sola franja compacta al pie de cada tarjeta. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] button,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"] [data-testid="stBaseButton-secondary"],
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] [data-testid="stBaseButton-secondary"] {
      display:flex!important;
      justify-content:center!important;
      align-items:center!important;
      box-sizing:border-box!important;
      width:100%!important;
      min-height:35px!important;
      height:35px!important;
      padding:6px 12px!important;
      background:#F5EEE7!important;
      background-color:#F5EEE7!important;
      border:1px solid #DCC5B4!important;
      border-radius:8px!important;
      box-shadow:none!important;
      color:#81523F!important;
      -webkit-text-fill-color:#81523F!important;
      font-size:12px!important;
      font-weight:750!important;
      line-height:1.1!important;
      text-align:center!important;
      transform:none!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"] button *,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] button * {
      color:inherit!important;
      -webkit-text-fill-color:inherit!important;
      background:transparent!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] button {
      background:#705245!important;
      background-color:#705245!important;
      color:#FFF9F2!important;
      -webkit-text-fill-color:#FFF9F2!important;
      border-color:#705245!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"] button:hover {
      background:#EAD9C9!important;
      background-color:#EAD9C9!important;
      border-color:#BD987D!important;
      color:#6E4436!important;
      -webkit-text-fill-color:#6E4436!important;
    }
    /* Defensivo: un contenedor de botón no puede parecer otra tarjeta. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_action_"] {
      min-height:0!important;height:auto!important;padding:0!important;
      background:transparent!important;border:0!important;
      border-radius:0!important;box-shadow:none!important;transform:none!important;
    }
    @media (max-width:900px) {
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_normal"],
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] {
        min-height:0!important;
      }
    }
/* ALCANCE ESTRICTO: MIS PENDIENTES (NO AFECTA DISEÑO GEOMÉTRICO/SETF). */
    .stMainBlockContainer:has(.pend-hero) {
      --revision-ivory:#F5F1E8;
      --revision-sand:#EFE8DC;
      --revision-inner:#FAF7F2;
      --revision-stroke:#C8B79C;
      --revision-gold:#B68A3A;
      --revision-terra:#A86C4A;
      --revision-olive:#6E7F57;
      --revision-graphite:#5E5A54;
      --revision-text:#34312F;
    }

    /* EXPEDIENTE: contorno más visible sobre el fondo de planos. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] {
      background:linear-gradient(145deg,#F5F1E8 0%,#EFE8DC 100%)!important;
      border:2px solid #B9A789!important;
      border-left:5px solid #B68A3A!important;
      border-radius:14px!important;
      box-shadow:0 12px 30px rgba(67,51,40,.13),0 2px 8px rgba(67,51,40,.055)!important;
      color:#34312F!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-file-top {
      background:linear-gradient(108deg,#4B463F,#5E574D)!important;
      border:1px solid #B68A3A!important;
      border-bottom:0!important;
      border-top:3px solid #C8A066!important;
      border-radius:10px 10px 0 0!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-file-label {
      color:#F3D4A6!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-file-title,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step-badge strong,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-decision-title {
      color:#3E3934!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-file-meta,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step-badge,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-decision-sub {
      color:#786E63!important;
    }
    /* PROCESO DE TRES ETAPAS: distinto relleno y borde por situación. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-timeline {
      background:#FAF7F2!important;
      border:2px solid #C8B79C!important;
      border-radius:12px!important;
      padding:9px!important;
      box-shadow:inset 0 1px 3px rgba(73,59,40,.04)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step {
      box-sizing:border-box!important;
      background:#F0E9DD!important;
      color:#61574A!important;
      border:2px solid #BAADA0!important;
      border-top:3px solid #A86C4A!important;
      border-radius:9px!important;
      min-height:46px!important;
      font-weight:750!important;
      gap:8px!important;
      box-shadow:0 2px 6px rgba(56,47,39,.045)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step span {
      background:#DCCABC!important;
      color:#584736!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.done {
      background:#E9EEDF!important;
      color:#3F5236!important;
      border:2px solid #81916A!important;
      border-top:3px solid #6E7F57!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.done span {
      background:#6E7F57!important;color:#FFFFFF!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.current {
      background:#F2E1C2!important;
      color:#674B21!important;
      border:2px solid #B68A3A!important;
      border-top:3px solid #A57927!important;
      box-shadow:0 3px 9px rgba(182,138,58,.15)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-step.current span {
      background:#A67B32!important;color:#FFFFFF!important;
    }
    /* ÚLTIMA VERSIÓN: verde salvia. Claramente diferente del historial terracota. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-document-line {
      background:linear-gradient(105deg,#E8EFE3 0%,#F3F6ED 100%)!important;
      border:2px solid #889B79!important;
      border-left:5px solid #647B55!important;
      border-radius:10px!important;
      padding:13px 16px!important;
      box-shadow:0 3px 12px rgba(60,80,48,.11)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-document-line strong {
      color:#32472E!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-document-line small {
      font-size:11px!important;font-weight:800!important;letter-spacing:.35px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-document-line small {
      color:#766C60!important;
    }

    /* WORD / PDF: pertenecen al documento VIGENTE, con acentos oliva/cobre. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_word_panel"] {
      background:linear-gradient(145deg,#F9FBF5,#EBF1E5)!important;
      border:2px solid #A5B397!important;
      border-left:5px solid #70865F!important;
      border-radius:11px!important;
      box-shadow:0 5px 14px rgba(69,52,38,.075)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-revision_formatos_"][class*="_pdf_panel"] {
      background:linear-gradient(145deg,#F9FBF5,#EDF2E7)!important;
      border:2px solid #B2BB9B!important;
      border-left:5px solid #81916A!important;
      border-radius:11px!important;
      box-shadow:0 5px 14px rgba(69,52,38,.075)!important;
    }
    .stMainBlockContainer:has(.pend-hero) .revision-file-name,
    .stMainBlockContainer:has(.pend-hero) .revision-file-heading {color:#443D36!important;}
    .stMainBlockContainer:has(.pend-hero) [class*="st-key-revision_formatos_"] .revision-file-symbol {
      background:#DFE9D8!important;border-color:#A2B397!important;color:#516A46!important;
    }
    /* HISTÓRICOS: terracota/gris cálido y filas compactas. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-hist_pdf_version_"] {
      background:linear-gradient(115deg,#F2E6DD,#F8F2ED)!important;
      border:1.5px solid #C7A38E!important;
      border-left:4px solid #A56547!important;
      border-radius:9px!important;
      padding:8px 12px!important;
      margin:5px 0!important;
      box-shadow:0 2px 6px rgba(65,49,40,.055)!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-version-title {
      color:#583C30!important;
      border-bottom:none!important;
      padding-bottom:0!important;
      margin-bottom:2px!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-version-date,
    .stMainBlockContainer:has(.pend-hero) .hist-version-empty {color:#766B60!important;}
    .stMainBlockContainer:has(.pend-hero) .hist-feedback {
      padding:7px 10px!important;margin:5px 0!important;
      background:#F3E3D5!important;
      border:1px solid #D4AB8D!important;
      border-left:4px solid #A86C4A!important;
      border-radius:7px!important;
      box-shadow:0 2px 8px rgba(83,53,38,.045)!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-feedback.aprobado {
      background:#EEF1E8!important;
      border:1px solid #A6B398!important;
      border-left:4px solid #6E7F57!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-feedback-title {color:#664831!important;}
    .stMainBlockContainer:has(.pend-hero) .hist-feedback-body {color:#3C352F!important;}
    .stMainBlockContainer:has(.pend-hero) .hist-feedback-meta {color:#70675D!important;}
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-hist_pdf_version_"] [data-testid="stToggle"] {
      padding:1px 0 0!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-pdf-open {
      background:#FAF1E9!important;border-color:#B8876E!important;
    }
    .stMainBlockContainer:has(.pend-hero) .hist-quick-summary {color:#70594D!important;}

    /* DECISIÓN / CIERRE: marco grafito cálido. */
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] {
      background:#FAF7F2!important;
      border:2px solid #C8B79C!important;
      border-left:5px solid #5E5A54!important;
      border-radius:12px!important;
      box-shadow:0 3px 11px rgba(66,53,39,.05)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stExpander"] {
      background:#F5F1E8!important;
      border:1.5px solid #C8B79C!important;
      border-radius:9px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] textarea,
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stForm"] [data-baseweb="textarea"] > div {
      background:#FFFEFA!important;
      color:#3A352F!important;
      border:1.5px solid #BFAF99!important;
      border-radius:8px!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button {
      background:#5E5A54!important;
      border:1.5px solid #B68A3A!important;
      border-left:4px solid #CDA460!important;
      color:#FFFFFF!important;
      box-shadow:0 3px 10px rgba(54,47,41,.13)!important;
    }
    .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] [data-testid="stFormSubmitButton"] button:hover {
      background:#514A41!important;border-color:#C79B56!important;
    }
    /* CENTRO DE AUTORIZACIONES: contraste claro y coherente con la nueva ficha. */
    .stMainBlockContainer:has(.pend-hero) .tte-queue-header {
      background:linear-gradient(125deg,#FAF7F2,#EFE8DC)!important;
      border:2px solid #C8B79C!important;
      border-left:5px solid #B68A3A!important;
    }
    .stMainBlockContainer:has(.pend-hero) .tte-summary-card {
      background:linear-gradient(145deg,#FAF7F2,#EEE5D8)!important;
      border:2px solid #C8B79C!important;
      border-left:5px solid #B68A3A!important;
    }
    @media(max-width:700px) {
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-tte_expediente_"] .tte-timeline {padding:7px!important;}
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-hist_pdf_version_"] {padding:10px 11px!important;}
    }
</style>""", unsafe_allow_html=True)

    # Estilo final de las bandejas con ilustraciones, SIN modificar los procesos.
    # El CSS se coloca antes de construir la página para evitar el estilo intermedio.
    st.markdown("""
    <style>
      /* Respetar un espacio institucional entre cabecera y módulo. */
      @media (min-width:1050px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero {margin-top:16px!important;}
      }
      @media (min-width:720px) and (max-width:1049px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero {margin-top:14px!important;}
      }
      @media(max-width:719px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero {margin-top:12px!important;}
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero {
        border-radius:14px!important;
        overflow:hidden!important;
        background:linear-gradient(107deg,#372D39 0%,#4A3945 66%,#433443 100%)!important;
        box-shadow:0 8px 22px rgba(49,37,48,.11)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero::after {
        width:57%!important; opacity:.80!important;
        background-position:right center!important;
        background-size:cover!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 42%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 42%)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {max-width:67%!important;}

      /* Bandejas con espacio propio, encabezados alineados y fotografía editorial. */
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"],
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] {
        position:relative!important;isolation:isolate!important;
        overflow:hidden!important;
        min-height:184px!important;
        padding:20px 22px 14px!important;
        border:1.5px solid #D1B8A4!important;
        border-left:4px solid #BB8C68!important;
        border-radius:14px!important;
        background:linear-gradient(112deg,#FFFCF8 0%,#F5EBE1 100%)!important;
        box-shadow:0 7px 18px rgba(51,40,46,.095)!important;
        transition:transform .18s ease,box-shadow .18s ease!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] {
        border-color:#B8C6B7!important;
        border-left-color:#72907A!important;
        background:linear-gradient(112deg,#FFFEFA 0%,#EDF2EB 100%)!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"]:hover,
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"]:hover {
        transform:translateY(-2px)!important;
        box-shadow:0 10px 22px rgba(51,40,46,.14)!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_activo"] {
        box-shadow:inset 3px 0 0 #C69869,0 8px 22px rgba(51,40,46,.11)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-art {
        display:block!important;
        position:absolute!important;
        top:0!important;right:0!important;
        width:47%!important;height:76%!important;
        background-size:cover!important;
        background-position:right center!important;
        background-repeat:no-repeat!important;
        opacity:.45!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 52%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 52%)!important;
        pointer-events:none!important;
        z-index:0!important;
        border-top-right-radius:14px;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-head,
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-copy {
        position:relative!important;z-index:1!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-head {
        display:flex!important;align-items:center!important;gap:13px!important;
        min-height:59px!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-icon {
        width:48px!important;height:48px!important;
        border:1px solid #D8BFAA!important;
        background:#EDD9C9!important;
        color:#7C5742!important;font-size:24px!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="_firma_"] .pend-tile-icon {
        border-color:#BDCFBC!important;
        background:#DCE8D8!important;color:#4A6455!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-body strong {
        font-size:17px!important;line-height:1.2!important;
        font-weight:800!important;color:#3F333B!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-sub {
        font-size:11px!important;font-weight:720!important;
        color:#9A6D50!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {
        max-width:70%!important;
        min-height:25px!important;
        font-size:11px!important;
        line-height:1.4!important;
        margin:5px 0 12px!important;
        color:#635C5B!important;
      }
      /* Franja de acción sin cajas anidadas ni desalineaciones. */
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"] button,
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] button {
        width:100%!important;
        min-height:42px!important;height:42px!important;
        padding:8px 15px!important;
        border:1px solid #A87D5A!important;border-radius:8px!important;
        background:#906B48!important;
        color:#FFFFFF!important;-webkit-text-fill-color:#FFFFFF!important;
        font-size:12px!important;font-weight:730!important;
        display:flex!important;align-items:center!important;
        justify-content:center!important;text-align:center!important;
        box-shadow:0 2px 5px rgba(84,57,36,.12)!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"] button *,
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] button * {
        color:inherit!important;-webkit-text-fill-color:inherit!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] button {
        background:#355C50!important;border-color:#355C50!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"] button:hover {background:#765637!important;}
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] button:hover {background:#294B42!important;}
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] [data-testid="stVerticalBlock"] {gap:4px!important;}

      /* Centro de autorizaciones: ilustración propia, no invade el título. */
      .stMainBlockContainer:has(.pend-hero) .tte-queue-header {
        min-height:126px!important;
        background:linear-gradient(112deg,#FFFCF8 0%,#F1E6DA 100%)!important;
        border:1px solid #C7B19B!important;
        border-left:5px solid #B88B55!important;
        box-shadow:0 6px 18px rgba(60,45,38,.08)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-header::after {
        content:""!important;
        background:var(--pend-queue-art,none) right center / cover no-repeat!important;
        width:55%!important;height:100%!important;right:0!important;
        opacity:.50!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 45%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 45%)!important;
        pointer-events:none!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-description {max-width:57%!important;}

      /* Indicadores: superficie editorial y motivos de plano discretos. */
      .stMainBlockContainer:has(.pend-hero) .tte-summary-card {
        position:relative!important;isolation:isolate!important;overflow:hidden!important;
        min-height:108px!important;padding:18px 20px!important;
        background:linear-gradient(120deg,#FFFCF7 0%,#F1E8DB 100%)!important;
        border:1.5px solid #CBBAA4!important;
        border-left:5px solid #B68A56!important;
        border-radius:12px!important;
        box-shadow:0 6px 14px rgba(51,41,34,.08)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-summary-card--weekly {
        border-left-color:#9AAB92!important;
        background:linear-gradient(120deg,#FFFEFA,#EDF2EB)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-summary-card--total {
        border-left-color:#A77362!important;
        background:linear-gradient(120deg,#FFFEFB,#F5E9E2)!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-summary-card::after {
        content:"";position:absolute;right:-9px;top:10px;
        width:102px;height:83px;opacity:.22;pointer-events:none;
        background:repeating-linear-gradient(0deg,transparent 0 12px,#B39C81 12.5px 13px),
          repeating-linear-gradient(90deg,transparent 0 16px,#B39C81 16.5px 17px);
        -webkit-mask-image:linear-gradient(90deg,transparent,#000);
        mask-image:linear-gradient(90deg,transparent,#000);
      }
      .stMainBlockContainer:has(.pend-hero) .tte-summary-value {
        font-size:30px!important;font-weight:840!important;color:#42363B!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-summary-label {
        font-size:12px!important;font-weight:730!important;line-height:1.35!important;
        color:#52494A!important;
      }
      @media(max-width:930px) {
        .stMainBlockContainer:has(.pend-hero) .pend-tile-art {opacity:.27!important;width:43%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {max-width:85%!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-header::after {width:49%!important;opacity:.34!important;}
      }
      @media(max-width:650px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero::after {opacity:.22!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-description {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-header::after {opacity:.20!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-art {opacity:.17!important;}
      }

      /* ===== AJUSTES FINALES · PENDIENTES Y BANDEJAS · 2026-10-23 ===== */
      /* No mover el panel sobre la cabecera institucional. */
      .stMainBlockContainer:has(.pend-hero) .pend-hero {
        margin-top:18px!important;
        margin-bottom:10px!important;
        min-height:178px!important;
        padding:26px 36px 25px!important;
        border-radius:14px!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero::after {
        display:none!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero-illustration {
        position:absolute!important;
        top:0!important;right:0!important;bottom:0!important;
        width:60%!important;
        display:block!important;
        z-index:1!important;
        background-position:right center!important;
        background-size:cover!important;
        background-repeat:no-repeat!important;
        opacity:.78!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 36%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 36%)!important;
        pointer-events:none!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {
        z-index:2!important;position:relative!important;
        max-width:65%!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-hero-track {
        z-index:3!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-nav-head {
        margin:7px 2px 8px!important;
      }
      /* Two real work cards. Their title/subtitle occupy separate rows. */
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_revision_"],
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"][class*="_firma_"] {
        padding:17px 19px 16px!important;
        min-height:190px!important;
        isolation:isolate!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-head {
        display:grid!important;
        grid-template-columns:50px minmax(0,1fr)!important;
        gap:14px!important;
        align-items:center!important;
        margin-bottom:7px!important;
        min-height:56px!important;
        max-width:75%!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-body {
        display:flex!important;
        flex-direction:column!important;
        justify-content:center!important;
        align-items:flex-start!important;
        gap:5px!important;
        min-width:0!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-body .pend-tile-name {
        display:block!important;
        font-size:17px!important;
        font-weight:800!important;
        line-height:1.15!important;
        letter-spacing:0!important;
        margin:0!important;
        padding:0!important;
        color:#41333C!important;
        text-align:left!important;
        white-space:normal!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-body .pend-tile-sub {
        display:block!important;
        font-size:11px!important;
        font-weight:650!important;
        line-height:1.35!important;
        color:#956D51!important;
        margin:0!important;
        padding:0!important;
        white-space:normal!important;
        text-align:left!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="_firma_"] .pend-tile-sub {
        color:#577460!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-copy {
        display:block!important;
        max-width:69%!important;
        min-height:25px!important;
        font-size:11px!important;
        line-height:1.45!important;
        margin:3px 0 12px!important;
        color:#625A59!important;
      }
      .stMainBlockContainer:has(.pend-hero) .pend-tile-art {
        z-index:0!important;
        width:49%!important;
        height:83%!important;
        opacity:.66!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 45%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 45%)!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] [data-testid="stButton"] {
        position:relative!important;
        z-index:2!important;
        margin-top:auto!important;
      }
      .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] button {
        width:100%!important;
        min-height:40px!important;
      }
      /* Authorization banner illustration on right: actual image layer. */
      .stMainBlockContainer:has(.pend-hero) .tte-queue-header {
        isolation:isolate!important;
        position:relative!important;
        overflow:hidden!important;
        min-height:126px!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-header::after {
        display:none!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-illustration {
        position:absolute!important;
        z-index:1!important;
        inset:0 0 0 auto!important;
        width:59%!important;
        height:100%!important;
        display:block!important;
        background-position:center right!important;
        background-size:cover!important;
        background-repeat:no-repeat!important;
        opacity:.85!important;
        -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 40%)!important;
        mask-image:linear-gradient(90deg,transparent 0%,#000 40%)!important;
        pointer-events:none!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-content {
        position:relative!important;
        z-index:2!important;
        max-width:60%!important;
      }
      .stMainBlockContainer:has(.pend-hero) .tte-queue-rail {
        z-index:3!important;
      }
      @media(max-width:900px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero {margin-top:14px!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-head {max-width:88%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {max-width:86%!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-content {max-width:70%!important;}
      }
      @media(max-width:650px) {
        .stMainBlockContainer:has(.pend-hero) .pend-hero-illustration {opacity:.35!important;width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-hero-copy {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-head {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-copy {max-width:100%!important;}
        .stMainBlockContainer:has(.pend-hero) .pend-tile-art {opacity:.23!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-illustration {opacity:.3!important;}
        .stMainBlockContainer:has(.pend-hero) .tte-queue-content {max-width:100%!important;}
      }
    </style>
    """, unsafe_allow_html=True)

    # Ajustes visuales de Mis pendientes: no modifican el flujo ni las credenciales.
    # Acabado visual del seguimiento: gris grafito, borde dorado y jerarquía compacta.
    # Solo afecta a las tarjetas documentales de este módulo.
    # Acabado final: mismo lenguaje visual que Oficios en trámite.
    # Estas reglas están deliberadamente al final para prevalecer sobre el CSS previo.
    # Corrección puntual de contraste: selectores directos que sí coinciden
    # con los contenedores reales de Streamlit (sin depender de :has()).
    # La interfaz se muestra solo tras inyectar todos sus estilos definitivos.

    # Ajustes visuales finales de Mis pendientes y Centro de autorizaciones.
    # Se mantienen intactos los procesos, permisos, versiones y conexiones.
    # Los fondos se cargan desde assets/ mediante el código existente.


    # Paleta integral exclusiva de Mis pendientes. Se inyecta al final para
    # sustituir estilos azules de componentes y formularios sin tocar lógica.


    # ============================================================
    # DISEÑO MIS PENDIENTES · GRAFITO CIRUELA Y COBRE (V3)
    # Presentación exclusiva; sin cambios a los flujos de gestión.
    # ============================================================

    # Pulido cromático final del área Mis pendientes / revisión de la Tte.
    # Estas reglas solo modifican superficies, bordes y jerarquía visual.
    # No se cambia ningún callback de Streamlit ni el acceso a Drive/Sheets.

    # Ajustes responsivos exclusivos de SETF; se aplican después del tema heredado.
    if _area_activa_validada() == "setf":
        st.markdown("""<style>
        /* Más superficie útil para subáreas SETF, sin alterar la portada. */
        [data-testid="stAppViewContainer"] .block-container:has(.pend-hero) {
            max-width: none !important;
            width: 100% !important;
            padding-left: clamp(12px, 1.2vw, 20px) !important;
            padding-right: clamp(12px, 1.2vw, 20px) !important;
        }
        /* No usar márgenes negativos sobre la tarjeta: comprimir el espaciado real. */
        .stMainBlockContainer:has(.pend-hero) .pend-hero {
            margin-top: 0 !important;
            margin-bottom: 12px !important;
        }
        .stMainBlockContainer:has(.pend-hero) .pend-nav-head {
            margin-top: 6px !important;
            margin-bottom: 8px !important;
        }
        .stMainBlockContainer:has(.pend-hero) div[data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) {
            gap: clamp(10px, 1vw, 16px) !important;
            align-items: stretch !important;
        }
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] {
            height:100% !important;
            min-height: 178px !important;
            display:flex !important;
            flex-direction:column !important;
            padding:15px !important;
        }
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-head {
            max-width:100% !important;
            gap: 9px !important;
            grid-template-columns:42px minmax(0,1fr) !important;
            min-height: 50px !important;
        }
        .stMainBlockContainer:has(.pend-hero) .pend-tile-body .pend-tile-name {
            font-size:clamp(14px,1.15vw,17px) !important;
            overflow-wrap:break-word !important;
        }
        .stMainBlockContainer:has(.pend-hero) div[class*="st-key-pend_nav_"] .pend-tile-copy {
            max-width: 94% !important;
            min-height: 30px !important;
            margin-bottom:8px !important;
        }
        .stMainBlockContainer:has(.pend-hero) .pend-tile-art {opacity:.24 !important;}
        /* Las cuentas con diferentes permisos presentan 2, 3 o 4 tarjetas. */
        @media (min-width:700px) and (max-width:1250px) {
            .stMainBlockContainer:has(.pend-hero) div[data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) {
                flex-wrap: wrap !important;
            }
            .stMainBlockContainer:has(.pend-hero) div[data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) > div[data-testid="stColumn"] {
                flex: 1 1 calc(50% - 12px) !important;
                width: calc(50% - 12px) !important;
                min-width: 0 !important;
            }
        }
        @media (max-width:699px) {
            .stMainBlockContainer:has(.pend-hero) div[data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) {
                flex-wrap:wrap !important;
            }
            .stMainBlockContainer:has(.pend-hero) div[data-testid="stHorizontalBlock"]:has([class*="st-key-pend_nav_"]) > div[data-testid="stColumn"] {
                flex: 1 1 100% !important; width:100% !important; min-width:0 !important;
            }
        }
        </style>""", unsafe_allow_html=True)
    mostrar_mis_pendientes()

elif st.session_state.menu_activo == "oficios":

    # =========================================================
    # ESTILO GRAFITO Y AZUL ACERO — MÓDULO OFICIOS
    # =========================================================
    # -----------------------------------------------------
    # SESIÓN DEL USUARIO
    # -----------------------------------------------------
    mostrar_sesion()

    # Fondo exclusivo del módulo Oficios y del menú lateral
    fondo_oficios = cargar_imagen_base64("assets/fondo_oficios.png")
    if not fondo_oficios:
        fondo_oficios = cargar_imagen_base64("assets/fondo_ferroviario.png")
    if fondo_oficios:
        st.markdown(
            f"""
            <style>
            [data-testid="stAppViewContainer"] {{
                background-image: url("data:image/png;base64,{fondo_oficios}") !important;
                background-size: cover !important;
                background-position: center center !important;
                background-repeat: no-repeat !important;
                background-attachment: fixed !important;
            }}
            [data-testid="stSidebar"] > div {{
                background-color: transparent !important;
            }}

            [data-testid="stSidebar"] [data-testid="stImage"] {{
                display: flex;
                justify-content: center;
            }}

            [data-testid="stSidebar"] .stButton button {{
                background: linear-gradient(120deg,#363B39,#404540) !important;
                color: #F7F5F0 !important;
                border: 1px solid #51544D !important;
                border-left: 3px solid #9D865C !important;
                border-radius: 10px !important;
                min-height: 48px !important;
                font-weight: 650 !important;
            }}

            [data-testid="stSidebar"] .stButton button:hover {{
                background: #4C5148 !important;
                border-color: #B99A65 !important;
                color: #FFFDF7 !important;
            }}
            [data-testid="stSidebar"] [class*="st-key-nav_compartida_oficios"] button {{
                background:linear-gradient(120deg,#4A4C44,#55574D) !important;
                border:1px solid #B99A65 !important;
                border-left:6px solid #E2B66B !important;
                box-shadow:inset 3px 0 0 #E2B66B, 0 5px 15px rgba(48,44,35,.22) !important;
            }}
            </style>
            """,
            unsafe_allow_html=True
        )

    # El menú lateral común ya fue construido antes de elegir el módulo.

    # ============================================================
    # 1. ENCABEZADO INSTITUCIONAL — DISEÑO MODERNO
    # ============================================================

    components.html(
        """
        <html>
        <head>
        <style>
            * {
                box-sizing: border-box;
            }

            body {
                margin: 0;
                padding: 0;
                font-family: Arial, Helvetica, sans-serif;
                background: transparent;
                overflow: hidden;
            }

            .encabezado-oficios {
                position: relative;
                width: 100%;
                min-height: 162px;
                padding: 24px 38px;
                overflow: hidden;

                display: flex;
                align-items: center;
                justify-content: space-between;

                background: linear-gradient(
                    112deg,
                    #142838 0%,
                    #203B4B 58%,
                    #314C58 100%
                );

                border: 1px solid rgba(198, 163, 109, 0.28);
                border-radius: 14px;

                box-shadow: 0 8px 24px rgba(16, 32, 48, 0.12);
            }

            .encabezado-oficios::before {
                content: "";
                position: absolute;
                left: 0;
                top: 26px;
                bottom: 26px;
                width: 4px;
                background: #C6A36D;
                border-radius: 0 4px 4px 0;
            }

            .encabezado-texto {
                position: relative;
                z-index: 2;
                flex: 1;
                min-width: 0;
            }

            .etiqueta {
                display: inline-block;
                color: #D9BF94;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 2.5px;
                text-transform: uppercase;
            }

            .titulo {
                margin: 13px 0 10px;
                color: #FFFFFF;
                font-size: 31px;
                font-weight: 700;
                letter-spacing: -0.6px;
                line-height: 1.15;
            }

            .subtitulo {
                margin: 0;
                color: #C8D3D9;
                font-size: 13px;
                font-weight: 400;
                line-height: 1.5;
            }

            .linea-dorada {
                width: 44px;
                height: 3px;
                margin-top: 18px;
                background: #C6A36D;
                border-radius: 3px;
            }

            .esquema-ferroviario {
                position: relative;
                z-index: 1;
                width: 38%;
                max-width: 380px;
                min-width: 200px;
                opacity: 0.82;
            }

            .esquema-ferroviario svg {
                display: block;
                width: 100%;
                height: auto;
            }

            @media (max-width: 650px) {
                .encabezado-oficios {
                    min-height: 155px;
                    padding: 25px;
                }

                .titulo {
                    font-size: 25px;
                }

                .esquema-ferroviario {
                    display: none;
                }
            }
        </style>
        </head>

        <body>
            <div class="encabezado-oficios">

                <div class="encabezado-texto">

                    <span class="etiqueta">
                        MESA DE DISEÑO GEOMÉTRICO · GESTIÓN DE OFICIOS
                    </span>

                    <h1 class="titulo">
                        Oficios en trámite
                    </h1>

                    <p class="subtitulo">
                        Consulta, registro y seguimiento documental
                    </p>

                    <div class="linea-dorada"></div>

                </div>

                <div class="esquema-ferroviario">

                    <svg viewBox="0 0 330 120"
                         xmlns="http://www.w3.org/2000/svg">

                        <!-- Cuadrícula técnica -->
                        <g stroke="#8197A2"
                           stroke-width="0.5"
                           opacity="0.28">

                            <path d="M15 5 V115"/>
                            <path d="M63 5 V115"/>
                            <path d="M111 5 V115"/>
                            <path d="M159 5 V115"/>
                            <path d="M207 5 V115"/>
                            <path d="M255 5 V115"/>
                            <path d="M303 5 V115"/>

                            <path d="M0 15 H330"/>
                            <path d="M0 45 H330"/>
                            <path d="M0 75 H330"/>
                            <path d="M0 105 H330"/>
                        </g>

                        <!-- Alineamiento principal -->
                        <path
                            d="M-10 100 C80 100 100 25 190 35 S280 85 340 20"
                            stroke="#D6B47C"
                            stroke-width="2.8"
                            fill="none"
                        />

                        <!-- Línea paralela -->
                        <path
                            d="M-10 111 C80 111 100 36 190 46 S280 96 340 31"
                            stroke="#A9BDC6"
                            stroke-width="1.5"
                            fill="none"
                        />

                        <!-- Trazo auxiliar -->
                        <path
                            d="M10 90 L48 80 L86 55 L130 30 L180 26 L230 43 L285 48"
                            stroke="#B4C5CC"
                            stroke-width="0.9"
                            stroke-dasharray="4 5"
                            fill="none"
                            opacity="0.75"
                        />

                        <!-- Puntos de control -->
                        <g fill="#203B4B"
                           stroke="#D6B47C"
                           stroke-width="1.8">

                            <circle cx="48" cy="80" r="3.5"/>
                            <circle cx="130" cy="30" r="3.5"/>
                            <circle cx="230" cy="43" r="3.5"/>
                        </g>

                        <!-- Etiqueta técnica -->
                        <text
                            x="185"
                            y="112"
                            font-size="8"
                            fill="#C2CFD4"
                            letter-spacing="1.5"
                            text-anchor="middle"
                        >
                            TRAZO · ALINEAMIENTO · RASANTE
                        </text>

                    </svg>

                </div>

            </div>
        </body>
        </html>
        """.replace(
            "MESA DE DISEÑO GEOMÉTRICO · GESTIÓN DE OFICIOS",
            "MESA DE SISTEMAS ELÉCTRICOS DE TRACCIÓN FERROVIARIA · GESTIÓN DE OFICIOS"
            if _area_activa_validada() == "setf" else
            "MESA DE DISEÑO GEOMÉTRICO · GESTIÓN DE OFICIOS"
        ).replace(
            "TRAZO · ALINEAMIENTO · RASANTE",
            "ENERGÍA · CATENARIA · TRACCIÓN"
            if _area_activa_validada() == "setf" else
            "TRAZO · ALINEAMIENTO · RASANTE"
        ),
        height=178,
        scrolling=False
    )

    # ============================================================
    # 2. PANEL OPERATIVO DE OFICIOS
    # ============================================================

    df_oficios = obtener_oficios()
    
    n_revision = 0
    n_firma = 0
    n_concluidos = 0
    n_pendientes = 0

    if not df_oficios.empty and "Estatus" in df_oficios.columns:
        estatus_series = df_oficios["Estatus"].astype(str).str.strip().str.lower()
        n_revision = (estatus_series == "en revisión").sum()
        n_firma = (estatus_series.isin(["en firma", "en espera de firma"])).sum()
        n_concluidos = estatus_series.isin(["firmado", "concluido", "finalizado"]).sum()
        n_pendientes = (~estatus_series.isin(["firmado", "concluido", "finalizado"])).sum()

    st.markdown(
        """
        <style>

        /* =====================================================
            CABECERA DE SECCIÓN
        ===================================================== */

        .of-panel-header {
            margin-top: 0;
            margin-bottom: 8px;
        }

        .of-eyebrow {
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 2.2px;
            color: #B08A4A;
            margin-bottom: 5px;
        }

        .of-title {
            font-size: 24px;
            line-height: 1.1;
            font-weight: 750;
            color: #10283B;
            margin: 0;
        }

        .of-subtitle {
            font-size: 12px;
            color: #73838E;
            margin-top: 6px;
        }


        /* =====================================================
            EXPEDIENTE ENCONTRADO
        ===================================================== */

        .of-record {
            position: relative;
            overflow: hidden;

            margin-top: 14px;
            margin-bottom: 16px;

            background: linear-gradient(
                135deg,
                #28586A 0%,
                #347084 100%
            );

            border: none;
            border-left: 4px solid #B08A4A;

            border-radius: 9px;

            padding: 20px 24px;

            box-shadow:
                0 8px 18px rgba(16, 40, 59, 0.10);
        }

        .of-record::after {
            content: "";
            position: absolute;

            width: 230px;
            height: 70px;

            right: -25px;
            bottom: -45px;

            border-top: 2px solid rgba(207, 170, 101, 0.40);
            border-radius: 50%;

            transform: rotate(-5deg);
        }

        .of-record-label {
            position: relative;
            z-index: 2;

            color: #D4B06C;

            font-size: 9px;
            font-weight: 800;

            letter-spacing: 1.8px;

            margin-bottom: 7px;
        }

        .of-record-title {
            position: relative;
            z-index: 2;

            color: #FFFFFF;

            font-size: 20px;
            font-weight: 700;

            margin-top: 0;
        }

        .of-record-project {
            position: relative;
            z-index: 2;

            color: #E1E7EA;

            font-size: 11px;

            margin-top: 5px;
        }

        </style>
        """,
        unsafe_allow_html=True
    )

    # =========================================================
    # TÍTULO DEL PANEL
    # =========================================================

    st.markdown(
        """<div class="of-panel-header">
    <div class="of-eyebrow">CONTROL · SEGUIMIENTO · EXPEDIENTE</div>
    <div class="of-title">Panorama documental</div>
    <div class="of-subtitle">
        Estado general y consulta de los oficios registrados
    </div>
</div>""",
        unsafe_allow_html=True
    )

    # =========================================================
    # INDICADORES — ESTILO INSTITUCIONAL MODERNO
    # =========================================================

    if _area_activa_validada() == "setf":
        # Solo SETF: sustituir KPI informativos de iframe por widgets nativos.
        # Así la navegación y los accesos rápidos funcionan con la sesión real,
        # sin URLs manuales, JavaScript en iframe ni consultas adicionales.
        st.markdown("""
        <style>
          /* PANORAMA DOCUMENTAL · ACCESOS RÁPIDOS SETF */
          div[class*="st-key-setf_of_kpi_"] {
              position:relative !important;
              border:1.5px solid #C6A36D !important;
              border-left:4px solid #C6A36D !important;
              border-radius:13px !important;
              background:#F8F5ED !important;
              padding:12px 13px 11px !important;
              min-height:158px !important;
              box-shadow:0 5px 16px rgba(16,40,59,.07) !important;
              transition:transform .15s ease, box-shadow .15s ease !important;
          }
          div[class*="st-key-setf_of_kpi_"]:hover {
              transform:translateY(-1px);
              box-shadow:0 8px 20px rgba(16,40,59,.11) !important;
          }
          div[class*="st-key-setf_of_kpi_firma"] {background:#F0F6FA !important;border-color:#6887A0 !important;border-left-color:#6887A0 !important;}
          div[class*="st-key-setf_of_kpi_concluidos"] {background:#F0F8F4 !important;border-color:#4F927B !important;border-left-color:#4F927B !important;}
          div[class*="st-key-setf_of_kpi_pendientes"] {background:#FBF3EF !important;border-color:#B77D61 !important;border-left-color:#B77D61 !important;}
          div[class*="st-key-setf_of_kpi_"] [data-testid="stVerticalBlock"] {gap:.24rem !important;}
          .setf-of-kpi-top {display:flex;align-items:center;justify-content:space-between;gap:8px;}
          .setf-of-kpi-number {font-size:32px;line-height:1;letter-spacing:-.8px;font-weight:800;color:#152E3C;font-variant-numeric:tabular-nums;}
          .setf-of-kpi-icon {width:31px;height:31px;border-radius:9px;background:rgba(255,255,255,.65);border:1px solid rgba(110,125,135,.16);color:#7C6135;display:grid;place-items:center;font-size:17px;}
          .setf-of-kpi-title {margin:7px 0 2px;font-weight:800;font-size:13px;line-height:1.2;color:#1B3546;}
          .setf-of-kpi-desc {font-size:10.5px;line-height:1.25;color:#6B7D87;min-height:13px;}
          .st-key-setf_of_kpi_firma .setf-of-kpi-icon {color:#3F698A;}
          .st-key-setf_of_kpi_concluidos .setf-of-kpi-icon {color:#317A60;}
          .st-key-setf_of_kpi_pendientes .setf-of-kpi-icon {color:#A05E45;}
          div[class*="st-key-setf_of_kpi_"] [data-testid="stHorizontalBlock"] {gap:6px !important;align-items:center!important;}
          div[class*="st-key-setf_of_kpi_"] [data-testid="stButton"] button,
          div[class*="st-key-setf_of_kpi_"] [data-testid="stPopover"] button {
              min-height:30px !important;height:30px !important;
              border-radius:8px !important;padding:2px 8px !important;
              font-size:11px !important;font-weight:700 !important;
              border:1px solid #C7D1D5 !important;background:rgba(255,255,255,.75) !important;
              color:#253C49 !important;box-shadow:none!important;
          }
          div[class*="st-key-setf_of_kpi_"] [data-testid="stButton"] button:hover,
          div[class*="st-key-setf_of_kpi_"] [data-testid="stPopover"] button:hover {
              background:#E5EBE9 !important;border-color:#9BADB2!important;color:#1D3D42!important;
          }
          div[class*="st-key-setf_of_kpi_"] [data-testid="stPopover"] button {
              line-height:1!important;padding:0!important;
          }
          div[class*="st-key-setf_of_kpi_"] [data-testid="stPopover"] button p {
              font-size:0!important;line-height:0!important;
          }
          div[class*="st-key-setf_of_kpi_"] [data-testid="stPopover"] button::after {
              content:"▾";font-size:17px;font-weight:800;color:#253C49;
              line-height:1;display:block;
          }
          /* Las cuatro tarjetas tienen la misma altura y el pie a la misma cota. */
          div[class*="st-key-setf_of_kpi_"] > div[data-testid="stVerticalBlock"] {
              min-height:132px!important;display:flex!important;flex-direction:column!important;
              justify-content:space-between!important;
          }
          @media(max-width:1100px) {
            div[class*="st-key-setf_of_kpi_"] {min-height:162px!important;}
            .setf-of-kpi-number {font-size:28px;}
          }
        </style>
        """, unsafe_allow_html=True)

        def _setf_ir_desde_kpi(modulo, *, vista=None, bandeja=None,
                               estado=None, vista_firmados=None):
            """Navegar por controles existentes, sin alterar permisos ni registros."""
            if _area_activa_validada() != "setf":
                return
            if modulo == "pendientes":
                st.session_state["setf_pend_vista"] = vista or "Mis pendientes"
                if bandeja:
                    st.session_state["pend_bandeja"] = bandeja
                st.session_state["pend_expediente_abierto"] = None
            elif modulo == "oficios" and estado is not None:
                # El filtro real del Centro de expedientes usa esta misma clave.
                st.session_state["filtro_tabla_estatus"] = estado
                st.session_state["filtro_tabla_proyecto"] = "Todos"
                st.session_state["filtro_tabla_responsable"] = "Todos"
                st.session_state["filtro_tabla_subproyecto"] = "Todos los subproyectos"
            elif modulo == "firmados":
                st.session_state["firmados_kpi_filtro"] = vista_firmados or "todos"
                st.session_state["buscar_firmados"] = ""
            navegar_a(modulo)

        opciones_kpi = (
            ("revision", int(n_revision), "En revisión", "Documentación en análisis", "◷",
             ("oficios", {"estado": "En revisión"})),
            ("firma", int(n_firma), "En firma", "Pendiente de formalización", "✎",
             ("pendientes", {"vista": "Mis pendientes", "bandeja": "En proceso de firma"})),
            ("concluidos", int(n_concluidos), "Firmados", "Documentos formalizados", "✓",
             ("firmados", {"vista_firmados": "todos"})),
            ("pendientes", int(n_pendientes), "Pendientes", "Requieren seguimiento", "!",
             ("pendientes", {"vista": "Pendientes de la mesa"})),
        )
        for columna, (clave, numero, titulo, descripcion, icono, destino) in zip(
                st.columns(4, gap="small"), opciones_kpi):
            with columna:
                with st.container(key=f"setf_of_kpi_{clave}"):
                    st.markdown(
                        '<div class="setf-of-kpi-content" translate="no">'
                        '<div class="setf-of-kpi-top">'
                        f'<span class="setf-of-kpi-number">{numero:02d}</span>'
                        f'<span class="setf-of-kpi-icon" aria-hidden="true">{icono}</span>'
                        '</div>'
                        f'<div class="setf-of-kpi-title">{html.escape(titulo)}</div>'
                        f'<div class="setf-of-kpi-desc">{html.escape(descripcion)}</div>'
                        '</div>', unsafe_allow_html=True,
                    )
                    accion_col, flecha_col = st.columns([4.6, 1], gap="small",
                                                          vertical_alignment="center")
                    with accion_col:
                        if st.button("Abrir listado", key=f"setf_of_abrir_{clave}",
                                     use_container_width=True,
                                     help=f"Consultar {titulo.lower()}"):
                            _setf_ir_desde_kpi(destino[0], **destino[1])
                    with flecha_col:
                        # Popover real de Streamlit: el menú flota sobre la página
                        # y NO aumenta el alto de ninguna de las cuatro tarjetas.
                        with st.popover(f"Accesos {titulo}", use_container_width=True):
                            st.markdown(f"**{titulo} · Accesos rápidos**")
                            if clave == "revision":
                                if st.button("Ver oficios en revisión", key="setf_kpi_rev_listado",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("oficios", estado="En revisión")
                                if puede_gestionar_borradores() or puede_realizar_revision_final():
                                    if st.button("Abrir bandeja Por revisar", key="setf_kpi_rev_revisor",
                                                 use_container_width=True):
                                        _setf_ir_desde_kpi("pendientes", vista="Mis pendientes", bandeja="Por revisar")
                            elif clave == "firma":
                                if st.button("Ver bandeja en firma", key="setf_kpi_firma_bandeja",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("pendientes", vista="Mis pendientes", bandeja="En proceso de firma")
                                if st.button("Enviados a firma hoy", key="setf_kpi_firma_hoy",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("firmados", vista_firmados="hoy")
                                if st.button("Enviados a firma esta semana", key="setf_kpi_firma_semana",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("firmados", vista_firmados="semana")
                            elif clave == "concluidos":
                                if st.button("Archivo de firmados", key="setf_kpi_firm_archivo",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("firmados", vista_firmados="todos")
                                if st.button("Firmados esta semana", key="setf_kpi_firm_semana",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("firmados", vista_firmados="firmados")
                            elif clave == "pendientes":
                                if st.button("Mis pendientes", key="setf_kpi_pend_mios",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("pendientes", vista="Mis pendientes")
                                if st.button("Pendientes de la mesa", key="setf_kpi_pend_todos",
                                             use_container_width=True):
                                    _setf_ir_desde_kpi("pendientes", vista="Pendientes de la mesa")

    else:
        html_kpis = """
        <!DOCTYPE html>
        <html>
        <head>
        <style>
            * {
                box-sizing: border-box;
            }

            body {
                margin: 0;
                padding: 0;
                background: transparent;
                font-family: Arial, Helvetica, sans-serif;
                overflow: hidden;
            }

            .kpi-grid {
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 13px;
                width: 100%;
                padding: 3px 1px 8px;
            }

            .kpi {
                position: relative;
                height: 120px;
                padding: 16px 19px;
                background: #F2F7FA;
                border: 1.7px solid #C6A36D;
                border-radius: 13px;
                box-shadow: 0 4px 13px rgba(16, 40, 59, 0.07);
                overflow: hidden;
            }

            .kpi::before {
                content: "";
                position: absolute;
                left: 0;
                top: 17px;
                bottom: 17px;
                width: 3px;
                background: #C6A36D;
                border-radius: 0 3px 3px 0;
            }

            .kpi-top {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
            }

            .numero {
                color: #142B3C;
                font-size: 34px;
                font-weight: 750;
                line-height: 1;
                letter-spacing: -1px;
            }

            .icono {
                width: 32px;
                height: 32px;
                display: flex;
                align-items: center;
                justify-content: center;
                background: #F0F4F6;
                border: 1px solid #E2E8EC;
                border-radius: 9px;
                color: #496273;
                font-size: 15px;
                font-weight: 700;
            }

            .nombre {
                margin-top: 12px;
                color: #183348;
                font-size: 12px;
                font-weight: 750;
            }

            .detalle {
                margin-top: 5px;
                color: #7C8B96;
                font-size: 10px;
                line-height: 1.3;
            }

            .revision {border-color:#C6A36D;background:#F8F5ED;}
            .revision::before { background: #C6A36D; }
            .revision .icono {color:#8B642C;background:#FBF3E6;border-color:#EAD5B7;}

            .firma {border-color:#6887A0;background:#F0F6FA;}
            .firma::before { background: #6887A0; }
            .firma .icono {color:#3F698A;background:#EEF4F8;border-color:#CDDFEA;}

            .concluidos {border-color:#4F927B;background:#F0F8F4;}
            .concluidos::before { background: #4F927B; }
            .concluidos .icono {color:#317A60;background:#EDF8F1;border-color:#CDE7D7;}

            .pendientes {border-color:#B77D61;background:#FBF3EF;}
            .pendientes::before { background: #B77D61; }
            .pendientes .icono {color:#A05E45;background:#FFF1EA;border-color:#EBCFBD;}

            @media (max-width: 650px) {
                .kpi-grid {
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }
            }
        </style>
        </head>

        <body>
            <div class="kpi-grid">

                <div class="kpi revision">
                    <div class="kpi-top">
                        <div class="numero">__REVISION__</div>
                        <div class="icono">◷</div>
                    </div>
                    <div class="nombre">En revisión</div>
                    <div class="detalle">Documentación en análisis</div>
                </div>

                <div class="kpi firma">
                    <div class="kpi-top">
                        <div class="numero">__FIRMA__</div>
                        <div class="icono">✎</div>
                    </div>
                    <div class="nombre">En firma</div>
                    <div class="detalle">Pendiente de formalización</div>
                </div>

                <div class="kpi concluidos">
                    <div class="kpi-top">
                        <div class="numero">__CONCLUIDOS__</div>
                        <div class="icono">✓</div>
                    </div>
                    <div class="nombre">Firmados</div>
                    <div class="detalle">Documentos formalizados</div>
                </div>

                <div class="kpi pendientes" translate="no">
                    <div class="kpi-top">
                        <div class="numero">__PENDIENTES__</div>
                        <div class="icono">!</div>
                    </div>
                    <div class="nombre notranslate" translate="no">Pendientes</div>
                    <div class="detalle">Requieren seguimiento</div>
                </div>

            </div>
        </body>
        </html>
        """

        html_kpis = (
            html_kpis
            .replace("__REVISION__", f"{n_revision:02d}")
            .replace("__FIRMA__", f"{n_firma:02d}")
            .replace("__CONCLUIDOS__", f"{n_concluidos:02d}")
            .replace("__PENDIENTES__", f"{n_pendientes:02d}")
        )

        components.html(
            html_kpis,
            height=137,
            scrolling=False
        )

    # =========================================================
    # CONSULTA DE EXPEDIENTES
    # =========================================================

    # Estilos del expediente: una sola inyección antes de la búsqueda.
    # Evita que Streamlit reserve espacios entre los widgets y la ficha.
    st.markdown("""<style>

        .exp-titulo {margin:18px 0 13px}
        .exp-titulo strong {font-size:19px;color:#172F40}
        .exp-titulo p {margin:5px 0 0;font-size:12px;color:#728693}
        .exp-panel {border:1px solid #D7E1E7;border-radius:10px;overflow:hidden;
            background:rgba(255,255,255,.95);box-shadow:0 7px 24px rgba(20,42,58,.07)}
        .exp-cabecera {background:linear-gradient(110deg,#233541,#365B6D);
            border-left:4px solid #C5A16C;padding:23px 26px}
        .exp-cabecera-fila {display:flex;justify-content:space-between;gap:18px;align-items:flex-start}
        .exp-eyebrow {color:#D9BE90;letter-spacing:1.7px;font-size:10px;font-weight:750}
        .exp-numero {color:white;font-size:22px;font-weight:750;margin-top:8px;overflow-wrap:anywhere}
        .exp-proyecto {color:#D3E0E7;font-size:12px;margin-top:7px}
        .exp-estado {color:#F3F7F9;border:1px solid #9CB4C1;border-radius:5px;
            padding:8px 12px;font-size:11px;font-weight:650;white-space:nowrap}
        .exp-datos {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));padding:12px 17px}
        .exp-dato {padding:14px 13px;border-bottom:1px solid #E5ECEF;min-width:0}
        .exp-dato:nth-child(3n+2),.exp-dato:nth-child(3n+3) {border-left:1px solid #E5ECEF}
        .exp-dato:nth-last-child(-n+3) {border-bottom:none}
        .exp-label {font-size:10px;letter-spacing:1px;color:#708694;font-weight:750;text-transform:uppercase}
        .exp-value {font-size:12px;color:#203849;font-weight:650;line-height:1.5;margin-top:5px;overflow-wrap:anywhere;white-space:normal}
        .exp-seguimiento {border-top:1px solid #DFE7EC;padding:19px 24px 23px}
        .exp-seguimiento-label {font-size:11px;font-weight:750;letter-spacing:1px;color:#577283;margin-bottom:19px}
        .exp-etapas {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));position:relative;gap:5px}
        .exp-etapas:before {content:"";position:absolute;top:11px;left:12.5%;right:12.5%;height:1px;background:#CBD8E0}
        .exp-etapa {position:relative;text-align:center}
        .exp-punto {width:23px;height:23px;margin:0 auto 10px;border-radius:50%;display:flex;
            align-items:center;justify-content:center;font-size:11px;font-weight:750;
            color:#8799A4;background:#EDF2F5;border:1px solid #C7D4DC}
        .exp-etapa.completa .exp-punto {background:#466B7D;border-color:#466B7D;color:white}
        .exp-etapa.actual .exp-punto {background:white;border:2px solid #B48E54;color:#8B6A3A}
        .exp-etapa-nombre {font-size:11px;font-weight:650;color:#8497A3}
        .exp-etapa.completa .exp-etapa-nombre,.exp-etapa.actual .exp-etapa-nombre {color:#203849}
        .exp-etapa-nota {font-size:10px;color:#8497A3;margin-top:5px}
        @media(max-width:650px) {
          .exp-datos {grid-template-columns:repeat(2,minmax(0,1fr))}
          .exp-dato {border-left:none!important;border-bottom:1px solid #E5ECEF!important}
          .exp-cabecera-fila {flex-direction:column}
          .exp-numero {font-size:18px}
          .exp-seguimiento {padding:16px 8px}
          .exp-etapa-nombre {font-size:10px}
          .exp-etapa-nota {font-size:9px}
        }
        

        .exp-titulo {margin:13px 0 10px}
        .exp-cabecera {padding:17px 22px!important}
        .exp-numero {font-size:20px!important;margin-top:6px!important}
        .exp-proyecto {margin-top:5px!important}
        .exp-seguimiento {padding:14px 22px 17px!important}
        .exp-seguimiento-label {margin-bottom:13px!important}

        .st-key-expediente_campos {
            background:rgba(255,255,255,.97);
            border:1px solid #D7E1E7;
            border-top:0;
            border-radius:0;
            padding:0 14px 1px;
            margin-top:0px;
            padding-bottom: 12px !important;
            margin-bottom: 0 !important;
        }
        .st-key-expediente_campos [data-testid="stHorizontalBlock"] {
            gap:0!important;
            align-items: stretch !important;
            height: auto !important;
        }
        .st-key-expediente_campos .st-key-exp_celda_folio,
        .st-key-expediente_campos .st-key-exp_celda_responsable,
        .st-key-expediente_campos .st-key-exp_celda_estatus,
        .st-key-expediente_campos .st-key-exp_celda_fecha,
        .st-key-expediente_campos .st-key-exp_celda_proyecto,
        .st-key-expediente_campos .st-key-exp_celda_asunto {
            padding:9px 12px 9px;
            min-height: 76px;
            height: auto !important;
            overflow: visible !important;
            padding-bottom: 16px !important;
            border-bottom:1px solid #E5ECEF;
            transition:background .15s ease,border-color .15s ease;
        }
        .st-key-expediente_campos .st-key-exp_celda_responsable,
        .st-key-expediente_campos .st-key-exp_celda_estatus,
        .st-key-expediente_campos .st-key-exp_celda_proyecto,
        .st-key-expediente_campos .st-key-exp_celda_asunto {
            border-left:1px solid #E5ECEF;
        }
        .st-key-expediente_campos .st-key-exp_celda_fecha,
        .st-key-expediente_campos .st-key-exp_celda_proyecto,
        .st-key-expediente_campos .st-key-exp_celda_asunto {
            border-bottom:0;
        }
        .st-key-expediente_campos .exp-label {
            color:#6D8493!important;
            font-size:9px!important;
            letter-spacing:1px!important;
        }
        .st-key-expediente_campos .exp-value {
            margin-top:5px!important;
            font-size:12px!important;
            line-height:1.5!important;
            white-space: normal !important;
            overflow-wrap: anywhere !important;
            height: auto !important;
            max-height: none !important;
            overflow: visible !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_editar_"] button {
            min-height:23px!important;
            height:23px!important;
            width:24px!important;
            padding:0!important;
            background:#EDF2F5!important;
            border:1px solid #DDE6EB!important;
            border-radius:4px!important;
            box-shadow:none!important;
            color:#36596C!important;
            font-size:12px!important;
            transform:none!important;
        }
        .st-key-expediente_campos [class*="st-key-exp_editar_"] button:hover {
            background:#E2EBF0!important;
            border-color:#B58F54!important;
            color:#243E4D!important;
        }
        .st-key-expediente_campos [data-testid="stMarkdownContainer"] p {
            margin:0!important;
        }
        .st-key-expediente_campos [data-testid="stTextInput"] input {
            min-height:35px!important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"]:has(
            [class*="st-key-exp_guardar_"]
        ) {
            background:#FFFDF8;
            outline:1px solid #C5A16C;
            outline-offset:-1px;
            border-radius:4px;
        }
        @media(max-width:650px) {
            .st-key-expediente_campos [class*="st-key-exp_celda_"] {
                min-height:70px;
            }
        }
        

        .exp-titulo {margin:17px 0 10px!important;border-left:2px solid #B89A63;padding-left:12px}
        .exp-titulo strong {font-size:19px!important;color:#172F40!important;font-weight:780!important}
        .exp-titulo p {font-size:11px!important;color:#526471!important;margin-top:2px!important}
        .exp-panel {background:#FAF9F6!important;border-color:#E0DCD3!important;
            box-shadow:0 3px 12px rgba(25,39,40,.045)!important}
        .exp-cabecera {background:linear-gradient(112deg,#333A39,#414643)!important;
            border-left:3px solid #B89A63!important;padding:15px 20px!important}
        .exp-eyebrow {font-size:9px!important;letter-spacing:1.6px!important;color:#E4C78F!important}
        .exp-numero {font-size:20px!important;letter-spacing:.1px;margin-top:6px!important;color:#FFFFFF!important}
        .exp-proyecto {font-size:11px!important;color:#F0F0EC!important;margin-top:5px!important}
        .exp-estado {border:1px solid #C5A56C!important;background:rgba(255,255,255,.07)!important;
            color:#FFFFFF!important;border-radius:20px!important;padding:5px 12px!important;font-size:10px!important}
        .st-key-expediente_campos {background:#FAF9F6!important;border-color:#E0DCD3!important;
            padding:10px 12px 12px!important}
        .st-key-expediente_campos [data-testid="stHorizontalBlock"] {gap:9px!important;margin-bottom:8px!important}
        .st-key-expediente_campos [class*="st-key-exp_celda_"] {background:#FFFFFF!important;
            border:1px solid #E4E1D9!important;border-radius:7px!important;
            padding:8px 11px 9px!important;min-height:61px!important;
            transition:border-color .18s,background .18s!important}
        .st-key-expediente_campos [class*="st-key-exp_celda_"]:hover {
            border-color:#C4AC80!important;background:#FFFEFC!important}
        .st-key-expediente_campos .exp-label {color:#4D5C60!important;font-size:10px!important;
            letter-spacing:.65px!important;font-weight:780!important}
        .st-key-expediente_campos .exp-value {color:#10283B!important;font-size:13px!important;
            font-weight:720!important;margin-top:3px!important;line-height:1.35!important}
        .st-key-expediente_campos [class*="st-key-exp_editar_"] button {
            background:#F7F4EC!important;border:1px solid #D8C8AA!important;
            border-radius:5px!important;color:#66512C!important;
            min-height:21px!important;height:21px!important;width:22px!important}
        .st-key-expediente_campos [class*="st-key-exp_editar_"] button:hover {
            border-color:#A9874E!important;background:#F3E9D6!important}
        .st-key-expediente_campos [data-testid="stExpander"] {
            border-color:#DAD6CB!important;background:#FFFEFC!important}
        .exp-seguimiento {border-top:0!important;padding:13px 20px 15px!important}
        .exp-seguimiento-label {color:#42575B!important;font-size:10px!important;
            letter-spacing:1.1px!important;margin-bottom:14px!important}
        .exp-etapas:before {background:#CCD3CE!important;height:2px!important;top:12px!important}
        .exp-punto {width:25px!important;height:25px!important;margin-bottom:7px!important;
            background:#F1F0EC!important;border:1px solid #C7CDC6!important;
            color:#53636A!important;font-size:11px!important}
        .exp-etapa.completa .exp-punto {background:#476977!important;border-color:#476977!important;color:#FFFFFF!important}
        .exp-etapa.actual .exp-punto {background:#B69558!important;border-color:#B69558!important;color:#FFFFFF!important}
        .exp-etapa-nombre {color:#263F4A!important;font-size:11px!important;font-weight:750!important}
        .exp-etapa-nota {color:#526874!important;font-size:10px!important;margin-top:3px!important}
        @media(max-width:650px) {
          .exp-cabecera {padding:14px 15px!important}
          .exp-numero {font-size:17px!important}
          .st-key-expediente_campos {padding:9px!important}
          .st-key-expediente_campos [data-testid="stHorizontalBlock"] {gap:7px!important}
          .st-key-expediente_campos [class*="st-key-exp_celda_"] {min-height:57px!important;padding:8px!important}
          .exp-seguimiento {padding:12px 8px!important}
          .exp-etapa-nombre {font-size:10px!important}
          .exp-etapa-nota {font-size:9px!important}
        }
        

        .st-key-expediente_campos {
            padding: 12px 14px 14px !important;
            background: #FBFAF7 !important;
            border: 1px solid #DEDAD1 !important;
            border-top: 0 !important;
            border-radius: 0 0 9px 9px !important;
        }
        .st-key-expediente_campos [data-testid="stHorizontalBlock"] {
            align-items: stretch !important;
            gap: 12px !important;
            margin-bottom: 10px !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] {
            min-height: 0 !important;
            height: auto !important;
            padding: 10px 13px !important;
            border: 1px solid #E0DDD6 !important;
            border-radius: 7px !important;
            background: #FFFFFF !important;
            box-shadow: 0 1px 3px rgba(21,37,43,.035) !important;
            overflow: visible !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] [data-testid="stVerticalBlock"] {
            gap: 3px !important;
        }
        .st-key-expediente_campos .exp-label {
            color: #50636C !important;
            font-size: 10px !important;
            letter-spacing: .7px !important;
            font-weight: 750 !important;
        }
        .st-key-expediente_campos .exp-value {
            color: #142F3E !important;
            font-size: 13px !important;
            font-weight: 650 !important;
            line-height: 1.45 !important;
            margin-top: 4px !important;
            overflow-wrap: anywhere !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_editar_"] button {
            height: 25px !important;
            min-height: 25px !important;
            width: 27px !important;
            background: #F8F5EE !important;
            border: 1px solid #D8C5A2 !important;
            color: #765D36 !important;
        }
        .st-key-expediente_campos [data-testid="stExpander"] {
            background: #FFFFFF !important;
            border: 1px solid #E0DDD6 !important;
        }
        @media (max-width: 720px) {
            .st-key-expediente_campos {padding: 9px !important;}
            .st-key-expediente_campos [data-testid="stHorizontalBlock"] {
                gap: 8px !important;
            }
            .st-key-expediente_campos [class*="st-key-exp_celda_"] {
                padding: 9px 10px !important;
            }
        }
        

        .st-key-expediente_campos [class*="st-key-exp_celda_"] {
            min-height: 0 !important;
            padding: 10px 13px 11px !important;
            align-self: start !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] > div[data-testid="stVerticalBlock"],
        .st-key-expediente_campos [class*="st-key-exp_celda_"] div[data-testid="stVerticalBlock"] {
            gap: 0 !important;
            row-gap: 0 !important;
            justify-content: flex-start !important;
            align-content: start !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] div[data-testid="stHorizontalBlock"] {
            margin: 0 !important;
            padding: 0 !important;
            align-items: center !important;
            min-height: 27px !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] div[data-testid="stElementContainer"],
        .st-key-expediente_campos [class*="st-key-exp_celda_"] div[data-testid="stMarkdownContainer"] {
            margin-top: 0 !important;
            margin-bottom: 0 !important;
            padding-top: 0 !important;
            padding-bottom: 0 !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] .exp-label {
            line-height: 1.25 !important;
            margin: 0 !important;
            padding: 0 !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] .exp-value {
            margin: 2px 0 0 !important;
            padding: 0 !important;
            line-height: 1.35 !important;
            font-size: 13px !important;
            font-weight: 700 !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] [data-testid="stExpander"] {
            margin: 4px 0 0 !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] [data-testid="stExpander"] details summary {
            min-height: 35px !important;
            padding-top: 6px !important;
            padding-bottom: 6px !important;
        }
        .st-key-expediente_campos [class*="st-key-exp_celda_"] [data-testid="stButton"] {
            margin: 0 !important;
        }
        

        /* Únicamente la ficha de expediente de la consulta principal. */
        .st-key-exp_integral {
            border:1px solid #C9D4D9!important;
            border-radius:14px!important;
            overflow:hidden!important;
            background:rgba(251,252,250,.975)!important;
            box-shadow:0 17px 43px rgba(20,42,58,.15)!important;
            padding:0!important;
            margin-top:18px!important;
        }
        .st-key-exp_integral > [data-testid="stVerticalBlock"] {
            gap:0!important;
        }
        .st-key-exp_integral [data-testid="stMarkdownContainer"] p {
            margin-bottom:0!important;
        }
        .exp-int-hero {
            position:relative;isolation:isolate;
            display:grid;grid-template-columns:minmax(0,1fr) minmax(215px,.40fr);
            min-height:175px;
            padding:25px 27px 25px 37px;
            gap:18px;
            background:linear-gradient(108deg,#122C40,#1A4358 70%,#234E5E)!important;
            color:#fff!important;
            border-bottom:3px solid #C49B55;
            overflow:hidden;
        }
        .exp-int-hero:before {
            content:"";position:absolute;left:18px;top:25px;bottom:25px;width:5px;
            border-radius:2px;background:#D7AB63;z-index:2;
        }
        .exp-int-hero:after {
            content:"";position:absolute;right:0;top:0;bottom:0;width:60%;
            opacity:.17;z-index:0;pointer-events:none;
            background-size:cover;background-position:right center;background-repeat:no-repeat;
        }
        .exp-int-hero > div {z-index:1;position:relative;min-width:0}
        .exp-int-overline {
            font-size:10px;letter-spacing:2.05px;color:#E9BC72;
            font-weight:800;text-transform:uppercase;margin-bottom:12px;
        }
        .exp-int-number {
            font-size:clamp(22px,2.15vw,31px);line-height:1.25;
            letter-spacing:.1px;font-weight:800;color:#FFFFFF;
            overflow-wrap:anywhere;
        }
        .exp-int-project {
            margin-top:10px;color:#E1EAF0;font-size:12px;
            line-height:1.55;overflow-wrap:anywhere;
        }
        .exp-int-summary {display:flex;flex-direction:column;align-items:flex-end;gap:19px}
        .exp-int-status {
            display:inline-flex;align-items:center;justify-content:center;
            gap:9px;max-width:100%;padding:9px 16px;
            border-radius:7px;border:1px solid #C6D0CB;
            color:#FAFEFE;background:rgba(255,255,255,.11);
            text-transform:uppercase;letter-spacing:.6px;font-size:13px;
            font-weight:800;text-align:center;
        }
        .exp-int-status:before {content:"●";font-size:11px;color:#DCC188}
        .exp-int-status.exp-int-firmado {
            color:#F5FFFA;background:rgba(77,132,115,.35);
            border-color:#A1C9B8;
        }
        .exp-int-status.exp-int-firmado:before {content:"✓";font-size:15px;color:#D2EEDF}
        .exp-int-status.exp-int-proceso {background:rgba(89,126,146,.35);border-color:#BACED8}
        .exp-int-meta {display:flex;gap:28px;text-align:right;max-width:100%}
        .exp-int-meta span {
            display:block;color:#C5D6DF;font-size:9px;font-weight:800;
            letter-spacing:1.4px;text-transform:uppercase;margin-bottom:5px;
        }
        .exp-int-meta strong {font-size:13px;color:#FFF;font-weight:800;overflow-wrap:anywhere}
        [class*="st-key-exp_int_seccion_"] {
            background:rgba(252,252,250,.98)!important;
            padding:21px 27px 23px!important;
            border-bottom:1px solid #D8D8D1!important;
        }
        .exp-int-section:nth-of-type(even) {background:rgba(248,250,250,.96)!important}
        .exp-int-heading {
            display:flex;align-items:center;gap:17px;
            margin-bottom:18px;position:relative;
        }
        .exp-int-index {
            font-size:34px;font-weight:500;color:#C19C58;
            line-height:1;font-family:Georgia,serif;
            padding-right:17px;border-right:2px solid #C39E5B;
            letter-spacing:-1.7px;
        }
        .exp-int-section-title {font-size:15px;letter-spacing:1.4px;color:#122C40;font-weight:850}
        .exp-int-section-desc {font-size:11px;color:#71838C;margin-top:3px}
        .exp-int-tagline {
            margin-left:auto;letter-spacing:1.4px;font-size:9px;
            color:#6C8792;font-weight:700;white-space:nowrap;
        }
        .exp-int-subline {
            margin:0 0 9px 0;color:#A17D43;font-size:9px;
            letter-spacing:1.5px;font-weight:800;text-transform:uppercase;
        }
        .st-key-exp_integral [class*="st-key-exp_celda_"] {
            background:#F8FAFA!important;
            border:1px solid #D6E1E4!important;
            border-left:2px solid #B6C7CE!important;
            border-radius:6px!important;
            min-height:64px!important;
            padding:9px 12px 10px!important;
            box-shadow:none!important;
            transition:background .18s,border-color .18s!important;
        }
        .st-key-exp_integral [class*="st-key-exp_celda_"]:hover {
            background:#FFFFFF!important;border-color:#BEA16B!important;
        }
        .st-key-exp_integral [class*="st-key-exp_celda_"] .exp-label {
            color:#597182!important;font-size:9px!important;letter-spacing:1.15px!important;
            text-transform:uppercase;font-weight:800!important;
        }
        .st-key-exp_integral [class*="st-key-exp_celda_"] .exp-value {
            color:#132E40!important;font-size:12px!important;
            line-height:1.45!important;font-weight:720!important;
            overflow-wrap:anywhere;
        }
        .st-key-exp_integral [class*="st-key-exp_editar_"] button {
            background:#F8F1E4!important;color:#8C6D37!important;
            border:1px solid #D5B578!important;border-radius:5px!important;
            width:28px!important;height:27px!important;min-height:27px!important;
            padding:0!important;font-size:13px!important;
        }
        .st-key-exp_integral [class*="st-key-exp_editar_"] button:hover {
            background:#EFDDBA!important;border-color:#B68948!important;
        }
        .st-key-exp_integral [class*="st-key-exp_celda_"] [data-testid="stExpander"] {
            border:1px solid #C7D7DF!important;border-radius:6px!important;
            background:#FFFFFF!important;
        }
        .exp-int-photo {
            min-height:240px;height:100%;border:1px solid #B7CBD3;
            border-radius:6px;display:flex;flex-direction:column;
            justify-content:flex-end;position:relative;overflow:hidden;
            background:linear-gradient(135deg,#274A5D,#112C3D);
            background-position:center;background-size:cover;
            box-shadow:inset 0 0 0 1px rgba(255,255,255,.16);
        }
        .exp-int-photo:before {
            content:"";position:absolute;inset:0;pointer-events:none;
            background:linear-gradient(180deg,transparent 40%,rgba(13,32,46,.72));
        }
        .exp-int-photo-label {position:relative;padding:15px;color:#F1F5F5}
        .exp-int-photo-label strong {
            display:block;font-size:12px;letter-spacing:1.25px;font-weight:800;
        }
        .exp-int-photo-label span {
            display:block;font-size:9px;letter-spacing:1.05px;margin-top:5px;color:#D6E4EB;
        }
        .exp-int-photo-line {position:relative;height:3px;background:#C79B53;width:32px;margin-top:7px}
        /* Documentos en tres módulos rectangulares, sin pestañas. */
        .st-key-exp_integral [class*="st-key-exp_int_doc_"] {
            padding:8px 12px 9px!important;
            border:1px solid #D2DDE1!important;
            border-left:3px solid #C49B55!important;
            border-radius:6px!important;
            background:rgba(255,255,255,.93)!important;
            box-shadow:0 4px 12px rgba(19,46,64,.055)!important;
        }
        .st-key-exp_integral [class*="st-key-exp_int_doc_"] [data-testid="stExpander"] {
            border:0!important;background:transparent!important;
        }
        .st-key-exp_integral [class*="st-key-exp_int_doc_"] [data-testid="stExpander"] summary {
            color:#142F43!important;font-weight:800!important;
        }
        .exp-int-timeline {padding:10px 12px 5px;border-top:0!important}
        .exp-int-timeline .exp-etapas:before {
            height:2px!important;background:#D1DFE1!important;top:14px!important;
        }
        .exp-int-timeline .exp-punto {
            width:30px!important;height:30px!important;
            border:2px solid #D4DFE2!important;
            background:#F3F6F6!important;
            color:#667F8C!important;
            border-radius:50%!important;
        }
        .exp-int-timeline .exp-etapa.completa .exp-punto,
        .exp-int-timeline .exp-etapa.actual .exp-punto {
            background:#417765!important;color:#FFFFFF!important;
            border-color:#417765!important;
        }
        .exp-int-timeline .exp-etapa.actual:not(.completa) .exp-punto {
            background:#B99557!important;border-color:#B99557!important;
        }
        .exp-int-timeline .exp-etapa-nombre {color:#1A364A!important;font-weight:800!important;font-size:12px!important}
        .exp-int-timeline .exp-etapa-nota {color:#738794!important;font-size:10px!important}
        .st-key-exp_int_admin {
            background:#163247!important;color:#fff!important;
            padding:12px 20px!important;border:0!important;
            border-radius:0!important;
        }
        .st-key-exp_int_admin [data-testid="stExpander"] {
            border:1px solid rgba(216,180,116,.38)!important;
            background:rgba(255,255,255,.055)!important;border-radius:6px!important;
        }
        .st-key-exp_int_admin [data-testid="stExpander"] summary,
        .st-key-exp_int_admin [data-testid="stExpander"] summary p {
            color:#FFF!important;font-weight:750!important;
        }
        /* Texto legible en el panel oscuro de administración del expediente.
           Mantener el texto introducido en campos claros en color oscuro. */
        .st-key-exp_int_admin [data-testid="stWidgetLabel"] p,
        .st-key-exp_int_admin [data-testid="stCheckbox"] label p,
        .st-key-exp_int_admin .stMarkdown p,
        .st-key-exp_int_admin .stMarkdown strong,
        .st-key-exp_int_admin label,
        .st-key-exp_int_admin [data-testid="stTextInput"] label p {
            color:#FFFFFF!important;
        }
        .st-key-exp_int_admin [data-testid="stTextInput"] input {
            color:#182C3B!important;
            background:#F5F8FA!important;
        }
        .st-key-exp_int_admin [data-testid="stAlert"] p,
        .st-key-exp_int_admin [data-testid="stAlert"] strong {
            color:#FFFFFF!important;
        }
        .st-key-exp_int_admin button p {
            color:#FFFFFF!important;
        }
        .st-key-exp_int_admin button[kind="secondary"] p {
            color:#182C3B!important;
        }
        .exp-int-admin-readonly {
            font-size:11px;color:#D3E0E6;
            letter-spacing:.7px;
        }
        @media(max-width:1150px) {
            .exp-int-tagline {display:none}
            .exp-int-hero {grid-template-columns:minmax(0,1fr) minmax(170px,.4fr)}
            .exp-int-meta {gap:14px}
        }
        @media(max-width:720px) {
            .exp-int-hero {grid-template-columns:1fr;padding:22px 18px 20px 30px}
            .exp-int-summary {align-items:flex-start;gap:12px}
            .exp-int-meta {text-align:left;gap:25px}
            .exp-int-section {padding:18px 13px 20px!important}
            .exp-int-heading {gap:10px}
            .exp-int-index {font-size:28px;padding-right:11px}
            .exp-int-section-title {font-size:13px}
            .exp-int-photo {min-height:180px}
            .exp-int-timeline .exp-etapa-nombre {font-size:10px!important}
        }
        

/* Expediente integral compacto / ajustes 2026-10-23 */
.st-key-exp_integral { margin-top: 3px!important; }
.st-key-exp_integral .exp-int-hero {
    min-height: 0!important; padding:16px 22px 16px 30px!important;
    gap:12px!important; align-items:center!important;
}
.st-key-exp_integral .exp-int-hero:before {
    top:16px!important; bottom:16px!important;left:16px!important;width:4px!important;
}
/* El trazo ocupa de verdad el espacio ENTRE el título y el estatus. */
.st-key-exp_integral .exp-int-hero:after {content:none!important;display:none!important;}
.st-key-exp_integral .exp-int-hero > .exp-int-artwork {
    position:absolute!important;
    top:0!important;
    bottom:0!important;
    left:37%!important;
    width:44%!important;
    z-index:0!important;
    overflow:hidden!important;
    pointer-events:none!important;
    background-size:contain!important;
    background-repeat:no-repeat!important;
    background-position:center center!important;
    opacity:.87!important;
    -webkit-mask-image:linear-gradient(90deg,transparent 0%,#000 20%,#000 83%,transparent 100%)!important;
    mask-image:linear-gradient(90deg,transparent 0%,#000 20%,#000 83%,transparent 100%)!important;
}
.st-key-exp_integral .exp-int-hero > div:not(.exp-int-artwork) {
    z-index:2!important;
    position:relative!important;
}
.st-key-exp_integral .exp-int-overline {
    font-size:9px!important; margin-bottom:5px!important; letter-spacing:1.4px!important;
}
.st-key-exp_integral .exp-int-number {
    font-size:clamp(19px,1.9vw,27px)!important; line-height:1.15!important;
}
.st-key-exp_integral .exp-int-project {
    font-size:11px!important;line-height:1.35!important;margin-top:5px!important;
}
.st-key-exp_integral .exp-int-summary {
    gap:8px!important;
    align-self:start!important;
}
.st-key-exp_integral .exp-int-status {
    padding:6px 11px!important;font-size:11px!important;
}
.st-key-exp_integral .exp-int-meta {gap:16px!important;}
.st-key-exp_integral .exp-int-meta span {
    font-size:8px!important;letter-spacing:1px!important;margin-bottom:2px!important;
}
.st-key-exp_integral .exp-int-meta strong {font-size:11px!important;}
.st-key-exp_integral [class*="st-key-exp_int_seccion_"] {
    padding:10px 18px 12px!important;
}
.st-key-exp_integral .exp-int-heading {
    gap:10px!important;margin-bottom:8px!important;min-height:29px!important;
}
.st-key-exp_integral .exp-int-index {
    font-size:25px!important;padding-right:10px!important;
}
.st-key-exp_integral .exp-int-section-title {
    font-size:12px!important;letter-spacing:1.1px!important;
}
.st-key-exp_integral .exp-int-section-desc {font-size:10px!important;margin-top:1px!important;}
.st-key-exp_integral .exp-int-tagline {font-size:8px!important;}
.st-key-exp_int_seccion_1 [data-testid="stHorizontalBlock"] {
    gap:10px!important;
}
/* Tarjetas más compactas y centradas: placa técnica ligera */
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] {
    position:relative!important;
    box-sizing:border-box!important;
    min-height:76px!important;
    padding:10px 13px 10px!important;
    background:linear-gradient(160deg,#FEFFFF 0%,#F5F8F9 100%)!important;
    border:1px solid #B6C8CF!important;
    border-top:2px solid #C39B5B!important;
    border-radius:8px 8px 4px 4px!important;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.95), 0 2px 8px rgba(26,56,70,.045)!important;
    overflow:hidden!important;
    transition:background .18s ease,border-color .18s ease,box-shadow .18s ease!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"]::after {
    content:"";
    position:absolute;bottom:0;left:14px;
    width:34px;height:2px;
    background:linear-gradient(90deg,#C8A164,rgba(200,161,100,0));
    pointer-events:none;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"]:hover {
    background:linear-gradient(150deg,#FFFFFF,#F8FBFB)!important;
    border-color:#B69255!important;
    box-shadow:0 4px 12px rgba(21,53,69,.08)!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] > [data-testid="stVerticalBlock"] {
    display:flex!important;
    flex-direction:column!important;
    justify-content:center!important;
    align-items:stretch!important;
    min-height:52px!important;
    gap:2px!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] [data-testid="stHorizontalBlock"] {
    margin:0!important;
    align-items:center!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] .exp-label {
    color:#5B7381!important;
    font-size:8px!important;
    letter-spacing:1.15px!important;
    line-height:1.2!important;
    font-weight:800!important;
    text-transform:uppercase!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] .exp-value {
    color:#173448!important;
    font-size:11px!important;
    font-weight:720!important;
    line-height:1.35!important;
    text-align:center!important;
    margin:1px 0 0!important;
    padding:1px 3px 3px!important;
    overflow-wrap:anywhere!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_editar_"] button {
    width:24px!important;height:24px!important;min-height:24px!important;
    background:#F9F1E4!important;
    border:1px solid #CAA76E!important;
    border-radius:7px!important;
    color:#7D5C32!important;
    box-shadow:0 1px 3px rgba(65,56,38,.045)!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [data-testid="stExpander"] {
    border:1px solid #B8CBD2!important;
    border-radius:8px!important;
    background:rgba(249,252,252,.94)!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [data-testid="stExpander"] summary {
    min-height:32px!important;
    padding:5px 9px!important;
    border-radius:8px!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 [data-testid="stExpander"] summary p {
    color:#294A5A!important;font-size:11px!important;font-weight:700!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 .exp-int-photo {
    height:100%!important;
    min-height:173px!important;
    border:1px solid #AFC2C9!important;
    border-radius:7px!important;
    background-size:cover!important;
    background-position:center!important;
    box-shadow:0 5px 14px rgba(18,45,61,.12)!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 .exp-int-photo-label {
    padding:12px!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 .exp-int-photo-label strong {
    font-size:10px!important;line-height:1.2!important;
}
.st-key-exp_integral .st-key-exp_int_seccion_1 .exp-int-photo-label span {
    font-size:8px!important;line-height:1.3!important;
}
.st-key-exp_integral [class*="st-key-exp_int_doc_"] {
    padding:3px 6px 4px!important;box-shadow:none!important;
}
.st-key-exp_integral .exp-int-timeline {padding:2px 6px 0!important;}
.st-key-exp_integral .exp-int-timeline .exp-punto {
    width:24px!important;height:24px!important;margin-bottom:6px!important;
}
.st-key-exp_integral .exp-int-timeline .exp-etapas:before {top:11px!important;}
.st-key-exp_integral .exp-int-timeline .exp-etapa-nombre {font-size:10px!important;}
.st-key-exp_integral .exp-int-timeline .exp-etapa-nota {font-size:9px!important;margin-top:2px!important;}
.st-key-exp_int_admin {padding:7px 14px!important;}
@media(max-width:960px) {
    .st-key-exp_integral .exp-int-hero > .exp-int-artwork {left:37%!important;width:43%!important;opacity:.72!important;}
    .st-key-exp_integral .st-key-exp_int_seccion_1 .exp-int-photo {min-height:165px!important;}
}
@media(max-width:850px) {
    .st-key-exp_integral .exp-int-hero {padding:17px 13px 17px 27px!important;}
    .st-key-exp_integral .exp-int-hero:before {left:11px!important;}
    .st-key-exp_integral [class*="st-key-exp_int_seccion_"] {padding:13px!important;}
}
@media(max-width:720px) {
    .st-key-exp_integral .st-key-exp_int_seccion_1 [class*="st-key-exp_celda_"] {
        min-height:72px!important;
        padding:9px 10px 10px!important;
    }
}
    </style>""", unsafe_allow_html=True)


    # Trazos del Centro: SVG codificado como imagen de fondo CSS. En Streamlit
    # el SVG como etiqueta dentro de st.markdown no siempre se presenta.
    dibujo_centro_svg = """<svg xmlns="http://www.w3.org/2000/svg" width="750" height="185" viewBox="0 0 750 185">
      <defs>
        <linearGradient id="dg" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#CDAE71" stop-opacity="0.05"/>
          <stop offset="0.38" stop-color="#D7B980" stop-opacity="0.58"/>
          <stop offset="1" stop-color="#E6C98F" stop-opacity="0.92"/>
        </linearGradient>
        <linearGradient id="ag" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#B8CFDA" stop-opacity="0.02"/>
          <stop offset="0.45" stop-color="#B8CFDA" stop-opacity="0.42"/>
          <stop offset="1" stop-color="#DFEDF3" stop-opacity="0.70"/>
        </linearGradient>
      </defs>
      <g fill="none" stroke="#8BA9B9" stroke-width="0.8" opacity="0.30">
        <path d="M0 25H750 M0 69H750 M0 113H750 M0 157H750"/>
        <path d="M68 0V185 M160 0V185 M252 0V185 M344 0V185 M436 0V185 M528 0V185 M620 0V185 M712 0V185"/>
      </g>
      <g fill="none" stroke-linecap="round">
        <path d="M-30 161 C105 144 150 88 280 108 S473 153 780 22" stroke="url(#ag)" stroke-width="1.5"/>
        <path d="M-30 146 C108 130 160 71 285 93 S480 132 780 8" stroke="url(#dg)" stroke-width="2.5"/>
        <path d="M-30 133 C104 117 162 55 283 79 S479 113 780 -6" stroke="url(#ag)" stroke-width="1.1"/>
        <path d="M66 155 C194 161 235 44 368 59 S547 116 710 43" stroke="#DAB675" opacity="0.65" stroke-width="1.1" stroke-dasharray="4 7"/>
        <path d="M282 35V142 M448 30V146 M617 8V108" stroke="#E0EAF0" opacity="0.43" stroke-width="1" stroke-dasharray="3 6"/>
      </g>
      <g fill="#E1C185" stroke="#1E4859" stroke-width="1.3">
        <circle cx="285" cy="93" r="4.2"/><circle cx="463" cy="130" r="4.2"/><circle cx="635" cy="68" r="4.2"/>
      </g>
    </svg>"""
    dibujo_centro_uri = "data:image/svg+xml;base64," + base64.b64encode(
        dibujo_centro_svg.encode("utf-8")
    ).decode("ascii")

    st.markdown(
        _rotulos_area(textwrap.dedent(f"""
        <div class="centro-exp-head" style="background-image:url('{dibujo_centro_uri}'),linear-gradient(105deg,#142B3F,#244B5A);background-size:53% 100%,100% 100%;background-position:right center,center center;background-repeat:no-repeat;">
            <div class="centro-exp-icon">⌕</div>
            <div>
                <div class="centro-exp-kicker">MESA DE DISEÑO GEOMÉTRICO · GESTIÓN DOCUMENTAL</div>
                <div class="centro-exp-title">Centro de expedientes</div>
                <div class="centro-exp-desc">Búsqueda, consulta y seguimiento de los oficios registrados</div>
            </div>
        </div>
        <div class="centro-exp-search-label">LOCALIZAR UN EXPEDIENTE</div>
        """)),
        unsafe_allow_html=True
    )


    st.markdown("""
    <style>
      .centro-exp-head {display:flex;align-items:center;gap:17px;padding:20px 25px;
        background:linear-gradient(105deg,#142B3F,#244B5A);border:1px solid #567083;
        border-left:5px solid #C9A35E;border-radius:14px 14px 10px 10px;
        box-shadow:0 9px 22px rgba(18,42,59,.14);margin:23px 0 0;color:white}

      /* El grabado técnico está integrado directamente como fondo SVG.
         No depende de pseudo-elementos ni etiquetas SVG procesadas por Markdown. */
      .centro-exp-head {
          position:relative!important;
          overflow:hidden!important;
      }
      .centro-exp-icon {display:flex;justify-content:center;align-items:center;
        width:55px;height:55px;flex-shrink:0;border:1px solid #C9A35E;border-radius:12px;
        background:rgba(255,255,255,.09);font-size:30px;color:#E4BD76}
      .centro-exp-kicker {font-size:10px;letter-spacing:2px;color:#D7B579;font-weight:750}
      .centro-exp-title {font-size:27px;line-height:1.25;font-weight:800;color:#FFF;margin:5px 0}
      .centro-exp-desc {font-size:12px;color:#D4E1E7}
      .centro-exp-search-label {background:#EDF3F5;border:1px solid #CBD8DE;border-bottom:0;
        border-radius:11px 11px 0 0;color:#254559;font-size:11px;font-weight:800;
        letter-spacing:1.4px;padding:12px 18px;margin-top:13px}
      .st-key-consulta_oficio input {background:#F8FBFC!important;border:1.5px solid #9CB6C5!important;
        border-radius:9px!important;min-height:43px!important}
      .st-key-btn_buscar_oficio button {background:#1F5269!important;border-color:#1F5269!important}
      .st-key-btn_nuevo_oficio_consulta button {border:1px solid #BA9656!important;
        color:#173A4B!important;background:#F9F5ED!important}
      div[data-testid="stTabs"] [data-baseweb="tab-list"] {gap:9px;background:#EAF1F4;
        border:1px solid #D0DBE0;border-radius:11px;padding:7px;margin:13px 0 16px}
      div[data-testid="stTabs"] button[role="tab"] {font-weight:700;border-radius:8px;padding:10px 14px}
      div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
        background:#1D455A!important;color:#FFFFFF!important}
      .exp-titulo {margin-top:14px!important}
      .st-key-expediente_campos {background:#EFF5F7!important;border:1px solid #B9CED9!important;
        border-radius:12px!important;padding:16px!important}
      .st-key-expediente_campos [class*="st-key-exp_celda_"] {border:1px solid #C4D6DF!important;
        background:#FFFFFF!important;border-radius:9px!important}
      @media(max-width:700px) {.centro-exp-head{padding:15px;gap:11px}
        .centro-exp-title{font-size:21px}.centro-exp-kicker{font-size:8px}}
    </style>
    """, unsafe_allow_html=True)

    # ---------------------------------------------------------
    # BARRA DE BÚSQUEDA
    # ---------------------------------------------------------

    col_busqueda, col_boton_buscar, col_nuevo = st.columns(
        [5.4, 1.45, 2.35],
        gap="small"
    )

    with col_busqueda:

        consulta = st.text_input(
            "Buscar oficio",
            placeholder="Folio, número, asunto o nombre del oficio firmado...",
            label_visibility="collapsed",
            key="consulta_oficio"
        )


    with col_boton_buscar:

        buscar_oficio = st.button(
            "⌕  Buscar",
            key="btn_buscar_oficio",
            type="primary",
            use_container_width=True
        )


    with col_nuevo:

        if st.button(
            "＋  Registrar nuevo oficio",
            key="btn_nuevo_oficio_consulta",
            use_container_width=True
        ):
            st.session_state.mostrar_registro = True
            st.rerun()

    # =========================================================
    # BUSCAR RESULTADO
    # =========================================================

    oficio_encontrado = None

    if consulta.strip() and not df_oficios.empty:
        consulta_normalizada = normalizar_texto(consulta)
        coincidencias = []

        for _, fila in df_oficios.iterrows():
            # El nombre se obtiene de la columna Firmado, que contiene
            # los vínculos registrados como {"nombre": ..., "url": ...}.
            # No se busca en el enlace: el usuario introduce el nombre visible.
            nombres_firmados = [
                normalizar_texto(documento.get("nombre", ""))
                for documento in leer_documentos(fila.get("Firmado", ""))
            ]
            campos = [
                normalizar_texto(fila.get("Folio", "")),
                normalizar_texto(fila.get("No. de oficio", "")),
                normalizar_texto(fila.get("Asunto", "")),
                *nombres_firmados,
            ]
            if any(consulta_normalizada in campo for campo in campos):
                # Priorizar coincidencias exactas para no abrir otro expediente
                # cuando un nombre o número es parte de otro.
                exacta = any(consulta_normalizada == campo for campo in campos)
                coincidencias.append((exacta, fila))

        if coincidencias:
            coincidencias.sort(key=lambda elemento: not elemento[0])
            oficio_encontrado = coincidencias[0][1]
            if len(coincidencias) > 1:
                st.caption(
                    f"Se encontraron {len(coincidencias)} expedientes coincidentes. "
                    "Se muestra primero la coincidencia más precisa; "
                    "agrega más palabras para afinar la búsqueda."
                )

    # =========================================================
    # MOSTRAR EXPEDIENTE ÚNICAMENTE SI EXISTE
    # =========================================================

    if oficio_encontrado is not None:

        # =====================================================
        # DATOS DEL EXPEDIENTE
        # =====================================================

        folio_resultado = str(
            oficio_encontrado.get("Folio", "")
        )

        numero_resultado = str(
            oficio_encontrado.get("No. de oficio", "")
        )

        proyecto_resultado = str(
            oficio_encontrado.get("Proyecto", "")
        )

        responsable_resultado = str(
            oficio_encontrado.get("Responsable", "")
        )

        estatus_resultado = str(
            oficio_encontrado.get("Estatus", "")
        )

        fecha_resultado = str(
            oficio_encontrado.get("Fecha de recepción", "")
        )

        asunto_resultado = str(
            oficio_encontrado.get("Asunto", "")
        )


        # Detalle institucional del expediente
        from html import escape

        def dato_visible(valor, defecto="—"):
            valor = str(valor or "").strip()
            return escape(valor if valor and valor.lower() != "nan" else defecto)

        estado = estatus_resultado.strip().lower()
        if estado in ("concluido", "firmado", "finalizado"):
            paso = 4
        elif estado in ("en firma", "en espera de firma", "en proceso de firma"):
            paso = 3
        elif estado in ("en revisión", "en revision", "en proceso"):
            paso = 2
        else:
            paso = 1

        # Estilos cargados al inicio de la consulta para evitar espacios en blanco.
        campos = [
            ("folio", "Folio", "Folio", folio_resultado),
            ("responsable", "Responsable", "Responsable", responsable_resultado),
            ("estatus", "Estatus", "Estatus", estatus_resultado),
            ("fecha", "Fecha de recepción", "Fecha de recepción", fecha_resultado),
            ("proyecto", "Proyecto", "Proyecto", proyecto_resultado),
            ("asunto", "Asunto", "Asunto", asunto_resultado),
        ]
        identidad = (folio_resultado.strip(), numero_resultado.strip())
        if st.session_state.get("exp_identidad") != identidad:
            st.session_state.exp_identidad = identidad
            st.session_state.exp_editando = None
        if st.session_state.pop("exp_guardado", False):
            st.success("Campo actualizado correctamente en Google Sheets.")

        def editar_campo(codigo, etiqueta, columna, valor):
            # Un responsable SETF puede trabajar en el Word asignado, pero la
            # edición de metadatos generales está reservada a los cinco administradores.
            autorizado = (puede_administrar_oficios_actual()
                          if _area_activa_validada() == "setf"
                          else puede_editar_responsable(responsable_resultado))
            with st.container(key=f"exp_celda_{codigo}"):
                titulo_col, boton_col = st.columns([8, 1], gap="small")
                with titulo_col:
                    st.markdown(f'<div class="exp-label">{escape(etiqueta)}</div>',
                                unsafe_allow_html=True)
                with boton_col:
                    if autorizado and st.button("✎", key=f"exp_editar_{codigo}",
                                 help=f"Editar {etiqueta}"):
                        st.session_state.exp_editando = (
                            None if st.session_state.get("exp_editando") == codigo else codigo
                        )
                        st.rerun()
                if autorizado and st.session_state.get("exp_editando") == codigo:
                    clave = f"exp_valor_{codigo}_{folio_resultado}_{numero_resultado}"
                    if codigo == "responsable":
                        opciones = (_responsables_setf() if _area_activa_validada() == "setf" else
                                    ["Alejandra", "Alex", "Jesús", "Cecilia", "Tte. Barajas",
                                     "Ricardo", "Magali", "John", "Karina"])
                        if valor and valor not in opciones:
                            opciones.insert(0, valor)
                        nuevo = st.selectbox(etiqueta, opciones,
                                             index=opciones.index(valor) if valor in opciones else 0,
                                             key=clave, label_visibility="collapsed")
                    elif codigo == "estatus":
                        opciones = ESTATUS_OFICIOS.copy()
                        if valor and valor not in opciones:
                            opciones.insert(0, valor)
                        nuevo = st.selectbox(etiqueta, opciones,
                                             index=opciones.index(valor) if valor in opciones else 0,
                                             key=clave, label_visibility="collapsed")
                        fecha_firma = None
                        if nuevo == "Firmado":
                            fecha_guardada = str(oficio_encontrado.get("Fecha de entrega", "")).strip()
                            fecha_inicial = None
                            for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                                try:
                                    fecha_inicial = datetime.strptime(fecha_guardada, formato).date()
                                    break
                                except ValueError:
                                    continue
                            fecha_firma = st.date_input(
                                "Fecha de entrega (obligatoria)",
                                value=fecha_inicial,
                                key=f"exp_fecha_firma_{folio_resultado}_{numero_resultado}",
                                format="DD/MM/YYYY",
                            )
                    elif codigo == "asunto":
                        nuevo = st.text_area(etiqueta, value=valor, key=clave,
                                             label_visibility="collapsed", height=75)
                    elif codigo == "fecha":
                        fecha_inicial = None
                        for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                            try:
                                fecha_inicial = datetime.strptime(valor.strip(), formato).date()
                                break
                            except ValueError:
                                pass
                        fecha_nueva = st.date_input(etiqueta, value=fecha_inicial,
                                                    key=clave, label_visibility="collapsed",
                                                    format="DD/MM/YYYY")
                        nuevo = fecha_nueva.strftime("%d/%m/%Y") if fecha_nueva else ""
                    else:
                        nuevo = st.text_input(etiqueta, value=valor, key=clave,
                                              label_visibility="collapsed")
                    guardar, cancelar = st.columns(2, gap="small")
                    with guardar:
                        if st.button("✓", key=f"exp_guardar_{codigo}", help="Guardar cambio",
                                     use_container_width=True):
                            if not str(nuevo).strip() and codigo in ("folio", "responsable"):
                                st.error("Este campo no puede quedar vacío.")
                            else:
                                try:
                                    if codigo == "estatus":
                                        actualizar_estatus_y_entrega(
                                            folio_resultado, numero_resultado, nuevo,
                                            fecha_firma.isoformat() if fecha_firma else ""
                                        )
                                    else:
                                        actualizar_campo_oficio(
                                            folio_resultado, numero_resultado, columna, str(nuevo).strip()
                                        )
                                    st.session_state.exp_editando = None
                                    st.session_state.exp_guardado = True
                                    st.rerun()
                                except Exception as error:
                                    st.error(f"No se guardó el cambio: {error}")
                    with cancelar:
                        if st.button("×", key=f"exp_cancelar_{codigo}", help="Cancelar edición",
                                     use_container_width=True):
                            st.session_state.exp_editando = None
                            st.rerun()
                else:
                    if codigo in ("proyecto", "asunto") and len(str(valor)) > 110:
                        with st.expander("Ver texto completo"):
                            st.markdown(
                                f'<div class="exp-value">{dato_visible(valor)}</div>',
                                unsafe_allow_html=True
                            )
                    else:
                        st.markdown(
                            f'<div class="exp-value">{dato_visible(valor)}</div>',
                            unsafe_allow_html=True
                        )

        etapas = [
            ("Recepción", fecha_resultado or "Registro"),
            ("Revisión", "Análisis documental"),
            ("Proceso de firma", "Formalización"),
            ("Firmado", "Documento formalizado"),
        ]
        etapas_html = ""
        for i, (titulo, nota) in enumerate(etapas, 1):
            clase = "completa" if i < paso else ("actual" if i == paso else "pendiente")
            indicador = "✓" if i < paso or (i == 4 and paso == 4) else str(i)
            etapas_html += (
                f'<div class="exp-etapa {clase}"><div class="exp-punto">{indicador}</div>'
                f'<div class="exp-etapa-nombre">{escape(titulo)}</div>'
                f'<div class="exp-etapa-nota">{dato_visible(nota)}</div></div>'
            )

        # ============================================================
        # EXPEDIENTE INTEGRAL: NUEVA PRESENTACIÓN, MISMA LÓGICA
        # Conserva editar_campo(), _panel_gestion_vinculos(), etapas_html,
        # permisos, validaciones y operaciones sobre Sheets y Drive.
        # ============================================================
        estado_etiqueta = str(estatus_resultado or "").strip().casefold()
        if estado_etiqueta == "firmado":
            clase_estado = "exp-int-firmado"
        elif "firma" in estado_etiqueta or "revisi" in estado_etiqueta:
            clase_estado = "exp-int-proceso"
        else:
            clase_estado = "exp-int-otro"

        # Recursos decorativos locales. No se necesitan servicios externos.
        # "trazo_expediente.png": PNG con transparencia, 1400 x 500.
        # "tarjeta_diseno.png": imagen fotográfica existente del selector.
        def _imagen_integral_data_uri(nombre):
            ruta = ASSETS_DIR / nombre
            if not ruta.is_file():
                return ""
            if ruta.stat().st_size > 7 * 1024 * 1024:
                return ""  # Evita incrustar imágenes excesivamente pesadas.
            tipo = "image/png" if ruta.suffix.lower() == ".png" else "image/jpeg"
            return "data:" + tipo + ";base64," + base64.b64encode(ruta.read_bytes()).decode("ascii")

        imagen_trazo = _imagen_integral_data_uri("trazo_expediente.png")
        imagen_tramo = _imagen_integral_data_uri("tarjeta_diseno.png")

        # La ilustración de trazo suele traer espacio vacío a la izquierda
        # (y, en algunas exportaciones, fondo negro). Aislamos sus líneas y
        # recortamos ese espacio para situarlas donde el usuario las quiere.
        # Esto no modifica el PNG original en assets.
        if imagen_trazo:
            try:
                from PIL import Image, ImageChops
                dibujo = Image.open(ASSETS_DIR / "trazo_expediente.png").convert("RGBA")
                r, g, b, a = dibujo.split()
                luminosidad = ImageChops.lighter(ImageChops.lighter(r, g), b)
                # Un fondo negro se vuelve transparente; en un PNG con alfa
                # genuino, se respeta su transparencia original.
                mascara_luz = luminosidad.point(lambda v: max(0, min(255, (v - 15) * 5)))
                nuevo_alfa = ImageChops.multiply(a, mascara_luz)
                dibujo.putalpha(nuevo_alfa)
                limites = nuevo_alfa.point(lambda v: 255 if v > 25 else 0).getbbox()
                if limites:
                    margen = max(8, int(dibujo.width * .012))
                    x0 = max(0, limites[0] - margen)
                    y0 = max(0, limites[1] - margen)
                    x1 = min(dibujo.width, limites[2] + margen)
                    y1 = min(dibujo.height, limites[3] + margen)
                    dibujo = dibujo.crop((x0, y0, x1, y1))
                    salida = BytesIO()
                    dibujo.save(salida, format="PNG", optimize=True)
                    imagen_trazo = "data:image/png;base64," + base64.b64encode(salida.getvalue()).decode("ascii")
            except Exception:
                # El PNG original sigue siendo válido si Pillow no está disponible.
                pass

        # Gráfico de respaldo por si la imagen no está en assets.
        trazo_svg_respaldo = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 580 180">
        <g fill="none" stroke="#D9BA80" stroke-width="1.5" opacity=".7">
        <path d="M-20 156C150 152 180 64 320 86S492 132 608 28"/>
        <path d="M-20 141C150 135 180 49 320 70S492 118 608 12"/>
        <path d="M-20 170C150 165 180 80 320 101S492 146 608 43" stroke="#C2D8E2"/>
        </g><g fill="none" stroke="#AEC8D6" opacity=".3" stroke-width=".7">
        <path d="M0 50H580 M0 100H580 M0 150H580 M100 0V180 M200 0V180 M300 0V180 M400 0V180 M500 0V180"/>
        </g></svg>"""
        if not imagen_trazo:
            imagen_trazo = "data:image/svg+xml;base64," + base64.b64encode(
                trazo_svg_respaldo.encode("utf-8")
            ).decode("ascii")
        fondo_fotografia = (
            f"background-image:linear-gradient(180deg,rgba(13,34,47,.06),rgba(13,34,47,.82)),url('{imagen_tramo}');"
            if imagen_tramo else ""
        )

        with st.container(key="exp_integral"):
            st.markdown(_rotulos_area(f"""
            <div class="exp-int-hero">
              <div class="exp-int-artwork" style="background-image:url('{imagen_trazo}');" aria-hidden="true"></div>
              <div>
                <div class="exp-int-overline">MESA DE DISEÑO GEOMÉTRICO / EXPEDIENTE TÉCNICO</div>
                <div class="exp-int-number">{dato_visible(numero_resultado or folio_resultado)}</div>
                <div class="exp-int-project">{dato_visible(proyecto_resultado, 'Proyecto no especificado')}</div>
              </div>
              <div class="exp-int-summary">
                <div class="exp-int-status {clase_estado}">{dato_visible(estatus_resultado, 'Sin estatus')}</div>
                <div class="exp-int-meta">
                  <div><span>Folio</span><strong>{dato_visible(folio_resultado)}</strong></div>
                  <div><span>Fecha de recepción</span><strong>{dato_visible(fecha_resultado)}</strong></div>
                </div>
              </div>
            </div>
            """), unsafe_allow_html=True)

            # Autorización ya existente. No se altera cómo se guarda información.
            if not puede_editar_responsable(responsable_resultado):
                if es_administrador():
                    st.error("No se pudo habilitar la edición. Vuelve a iniciar sesión y verifica el rol configurado.")
                else:
                    st.info(
                        "Consulta de solo lectura: este oficio pertenece a "
                        f"{responsable_resultado or 'un responsable sin identificar'}. "
                        "Para editarlo debes tenerlo asignado en tu cuenta o acceder con un rol administrador."
                    )

            with st.container(key="exp_int_seccion_1"):
                st.markdown(_rotulos_area(f"""
                <div class="exp-int-heading">
                  <div class="exp-int-index">01</div>
                  <div><div class="exp-int-section-title">INFORMACIÓN GENERAL</div>
                    <div class="exp-int-section-desc">Datos del oficio y su contexto técnico</div></div>
                  <div class="exp-int-tagline">TRAZO / ALINEAMIENTO / RASANTE</div>
                </div>
                """), unsafe_allow_html=True)

                # Estructura compacta en tres columnas equilibradas:
                # contexto del oficio, datos editables y referencia fotográfica.
                # No se crean nuevos campos ni se modifican los permisos.
                izquierda, control, foto = st.columns([1.65, 1.0, .90], gap="small")
                with izquierda:
                    editar_campo(*campos[4])  # Proyecto
                    editar_campo(*campos[5])  # Asunto: conserva "Ver texto completo"
                with control:
                    editar_campo(*campos[1])  # Responsable
                    # El folio, el estatus y la recepción se ven en la cabecera;
                    # desde aquí siguen siendo editables con sus widgets originales.
                    with st.expander("✎ Editar datos de control", expanded=False):
                        editar_campo(*campos[0])  # Folio
                        editar_campo(*campos[2])  # Estatus
                        editar_campo(*campos[3])  # Fecha de recepción
                with foto:
                    st.markdown(_rotulos_area(f"""
                    <div class="exp-int-photo" style="{fondo_fotografia}">
                      <div class="exp-int-photo-label">
                        <strong>DISEÑO GEOMÉTRICO</strong>
                        <span>REFERENCIA VISUAL · VÍA FÉRREA</span>
                        <div class="exp-int-photo-line"></div>
                      </div>
                    </div>
                    """), unsafe_allow_html=True)

            with st.container(key="exp_int_seccion_2"):
                st.markdown("""
                <div class="exp-int-heading">
                  <div class="exp-int-index">02</div>
                  <div><div class="exp-int-section-title">DOCUMENTACIÓN VINCULADA</div>
                    <div class="exp-int-section-desc">Archivos y documentos relacionados con este expediente</div></div>
                  <div class="exp-int-tagline">PLANOS / OFICIOS / ANEXOS</div>
                </div>
                """, unsafe_allow_html=True)

                # El documento principal de un trámite directo no es un antecedente.
                # Se consulta aparte, sin romper las tres categorías existentes.
                try:
                    tipo_actual = _modalidad_expediente(folio_resultado, numero_resultado)
                except Exception as error:
                    tipo_actual = None
                    st.caption("Modalidad documental no disponible: " + str(error))
                if tipo_actual and tipo_actual.get("Tipo de trámite") == "Directo":
                    enlace_principal = str(tipo_actual.get("URL principal", "")).strip()
                    st.caption("MODALIDAD · TRÁMITE DIRECTO · " +
                               ("CON ANTECEDENTES" if tipo_actual.get("Tiene antecedentes") == "Sí"
                                else "SIN ANTECEDENTES"))
                    if enlace_principal.startswith("https://"):
                        st.link_button(
                            "Ver oficio principal · " + str(tipo_actual.get("Nombre principal", "")),
                            enlace_principal, use_container_width=True)
                # Los paneles ordinarios siguen funcionando sin cambios.
                antecedentes_resultado = leer_documentos(oficio_encontrado.get("Antecedente", ""))
                anexos_resultado = leer_documentos(oficio_encontrado.get("Anexo", ""))
                firmado_resultado = leer_documentos(oficio_encontrado.get("Firmado", ""))
                doc_1, doc_2, doc_3 = st.columns(3, gap="small")
                clave_expediente = re.sub(r"[^a-zA-Z0-9_]", "_", f"{folio_resultado}_{numero_resultado}")
                with doc_1:
                    with st.container(key="exp_int_doc_antecedentes"):
                        _panel_gestion_vinculos(oficio_encontrado, "Antecedente", "Antecedentes",
                                               "▤", f"{clave_expediente}_antecedente")
                with doc_2:
                    with st.container(key="exp_int_doc_anexos"):
                        _panel_gestion_vinculos(oficio_encontrado, "Anexo", "Anexos",
                                               "◇", f"{clave_expediente}_anexo")
                with doc_3:
                    with st.container(key="exp_int_doc_firmado"):
                        _panel_gestion_vinculos(oficio_encontrado, "Firmado", "Oficio firmado",
                                               "▣", f"{clave_expediente}_firmado")

            with st.container(key="exp_int_seccion_3"):
                st.markdown("""
                <div class="exp-int-heading">
                  <div class="exp-int-index">03</div>
                  <div><div class="exp-int-section-title">SEGUIMIENTO DEL TRÁMITE</div>
                    <div class="exp-int-section-desc">Etapas del proceso documental</div></div>
                  <div class="exp-int-tagline">RECEPCIÓN / REVISIÓN / FIRMA / ARCHIVO</div>
                </div>
                """, unsafe_allow_html=True)
                # El paso procede del estatus real, y la fecha visible de
                # recepción procede de Sheets. No se inventan otras fechas.
                st.markdown(f"""
                <div class="exp-int-timeline"><div class="exp-etapas">{etapas_html}</div></div>
                """, unsafe_allow_html=True)

            # Operación destructiva exactamente con el mismo control y permisos.
            with st.container(key="exp_int_admin"):
                # En SETF las acciones destructivas se asignan por identificador
                # de cuenta; el rol de administrador DG NO concede estos permisos.
                if puede_administrar_oficios_actual():
                    with st.expander("⚙ Gestión administrativa del expediente · Eliminar oficio", expanded=False):
                        _panel_eliminar_oficio(folio_resultado, numero_resultado, origen="busqueda")
                else:
                    st.markdown(
                        '<div class="exp-int-admin-readonly">CONTROL DOCUMENTAL · EXPEDIENTE DE CONSULTA</div>',
                        unsafe_allow_html=True,
                    )

    elif consulta.strip():
        st.warning(
            "No se encontró ningún oficio que coincida con la búsqueda."
        )

    else:
        st.markdown(
            """
            <div style="
                margin-top:8px;
                padding:14px 16px;
                border-left:2px solid #B08A4A;
                color:#82919B;
                font-size:11px;
                background:rgba(255,255,255,0.38);
            ">
                Ingresa un folio, número de oficio o palabras del asunto
                para consultar el expediente.
            </div>
            """,
            unsafe_allow_html=True
        )

    # ==========================================
    # SEPARADOR Y REGISTRO FUERA DEL TABLERO PRINCIPAL
    # ==========================================
    st.write("")
    st.divider()
    st.markdown(
        "<p style='text-align:center;color:#667784;font-size:11px;font-weight:600;letter-spacing:2px;'>ADMINISTRACIÓN DOCUMENTAL</p>",
        unsafe_allow_html=True
    )
    st.write("")

  # =========================================================
    # REGISTRO DE NUEVO OFICIO
    # =========================================================

    if "mostrar_registro" not in st.session_state:
        st.session_state.mostrar_registro = False

    # Los estilos se aplican solo al contenedor del formulario.
    st.markdown("""
    <style>
    .st-key-tarjeta_registro_oficio {
        background: #29343E !important;
        border: 1px solid #607789 !important;
        border-radius: 14px !important;
        padding: 26px !important;
        box-shadow: 0 12px 30px rgba(20, 35, 48, .16);
    }
    .st-key-tarjeta_registro_oficio label,
    .st-key-tarjeta_registro_oficio [data-testid="stWidgetLabel"] p,
    .st-key-tarjeta_registro_oficio .stMarkdown p,
    .st-key-tarjeta_registro_oficio h1,
    .st-key-tarjeta_registro_oficio h2,
    .st-key-tarjeta_registro_oficio h3,
    .st-key-tarjeta_registro_oficio strong {
        color: #E8F0F5 !important;
    }
    .st-key-tarjeta_registro_oficio [data-testid="stTextInput"] input,
    .st-key-tarjeta_registro_oficio [data-testid="stTextArea"] textarea,
    .st-key-tarjeta_registro_oficio [data-testid="stDateInput"] input,
    .st-key-tarjeta_registro_oficio [data-baseweb="select"] > div {
        background: #394B59 !important;
        color: #F4F8FB !important;
        border: 1px solid #71899A !important;
        border-radius: 7px !important;
    }
    .st-key-tarjeta_registro_oficio [data-baseweb="select"] span,
    .st-key-tarjeta_registro_oficio [data-baseweb="select"] input,
    .st-key-tarjeta_registro_oficio [data-baseweb="select"] svg {
        color: #F4F8FB !important;
        fill: #D8E5EC !important;
    }
    .st-key-tarjeta_registro_oficio input::placeholder,
    .st-key-tarjeta_registro_oficio textarea::placeholder {
        color: #B9CAD6 !important;
        opacity: 1 !important;
    }
    .st-key-tarjeta_registro_oficio div[data-testid="stButton"] button {
        background: #47677D !important;
        border: 1px solid #7895A7 !important;
        color: #FFFFFF !important;
    }
    .st-key-tarjeta_registro_oficio div[data-testid="stButton"] button:hover {
        background: #587D96 !important;
    }
    .st-key-tarjeta_registro_oficio hr {
        border-color: #647D8D !important;
    }
    /* La alineación de cada sección se define en su propio encabezado. */
    </style>
    """, unsafe_allow_html=True)

    if st.session_state.mostrar_registro:

        col_titulo, col_volver = st.columns([4, 1])

        with col_titulo:
            st.markdown("<h3 style='text-align:left;'>Registrar nuevo oficio</h3>", unsafe_allow_html=True)

        with col_volver:
            if st.button(
                "← Volver a consulta",
                key="volver_consulta_oficios",
                use_container_width=True
            ):
                st.session_state.mostrar_registro = False
                st.rerun()

        with st.container(
            border=True,
            key="tarjeta_registro_oficio"
        ):
            formulario_nuevo_oficio()
    # =========================================================
    # TABLA GENERAL DE OFICIOS Y SELECCIÓN PARA PDF
    # =========================================================
    st.write("")
    st.markdown(
        """
        <div style="text-align:center; margin-bottom:20px;">
            <div style="
                color:#B08A4A;
                font-size:11px;
                font-weight:700;
                letter-spacing:2px;
                margin-bottom:6px;
            ">
                CONTROL DOCUMENTAL
            </div>
            <div style="
                color:#263B4A;
                font-size:22px;
                font-weight:700;
            ">
                Registro general de oficios
            </div>
            <div style="
                color:#8795A3;
                font-size:12px;
                margin-top:4px;
            ">
                Consulta general de los oficios registrados
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    tabla_oficios = obtener_oficios()
    if _area_activa_validada() == "setf" and es_administrador_documental_setf() and not tabla_oficios.empty:
        with st.expander("Reasignación de emergencia · SETF", expanded=False):
            st.caption("Solo para ausencias o contingencias. El cambio de responsable se registra y no elimina versiones previas.")
            candidatos = tabla_oficios.to_dict("records")
            seleccionados = {}
            for r in candidatos:
                fol = str(r.get("Folio", "")).strip()
                num = str(r.get("No. de oficio", "")).strip()
                if not num:
                    continue
                clave = f"{num} · Folio {fol} · {str(r.get('Responsable',''))}"
                seleccionados[clave] = (fol, num, str(r.get("Responsable", "")).strip())
            if seleccionados:
                with st.form("setf_reasignacion_emergencia"):
                    elegido = st.selectbox("Oficio a reasignar", list(seleccionados))
                    anterior = seleccionados[elegido][2]
                    destino = st.selectbox("Nuevo responsable", _responsables_setf(), index=None,
                                           placeholder="Selecciona un integrante de SETF")
                    motivo = st.text_area("Motivo de emergencia (obligatorio)", placeholder="Ej. Ausencia del responsable principal")
                    confirmar = st.checkbox("Confirmo la reasignación excepcional")
                    enviar = st.form_submit_button("Reasignar y guardar en bitácora", type="primary")
                if enviar:
                    if not destino or not motivo.strip() or not confirmar:
                        st.error("Selecciona al nuevo responsable, indica un motivo y confirma la operación.")
                    elif normalizar_texto(destino) == normalizar_texto(anterior):
                        st.warning("El nuevo responsable debe ser distinto del actual.")
                    else:
                        folio_r, numero_r, original = seleccionados[elegido]
                        try:
                            _reasignar_oficio_setf_emergencia(folio_r, numero_r, original, destino, motivo.strip())
                            st.success("Reasignación guardada. El oficio aparecerá en los pendientes del nuevo responsable.")
                            st.rerun()
                        except Exception as err:
                            st.error(f"No se pudo completar la reasignación: {err}")


    columnas_oficios = [
        "Folio",
        "No. de oficio",
        "Asunto",
        "Proyecto",
        "Responsable",
        "Estatus",
        "Fecha de recepción",
        "Fecha de entrega",
        "Antecedente",
        "Anexo",
        "Firmado",
        "Observaciones"
    ]

    if not tabla_oficios.empty:

        columnas_disponibles = [
            columna
            for columna in columnas_oficios
            if columna in tabla_oficios.columns
        ]

        tabla_oficios = tabla_oficios[columnas_disponibles].copy()

        # Los enlaces de antecedentes y anexos se consultan en el expediente.
        # No es necesario analizar JSON documental para pintar la tabla.

        # -------------------------------------------------
        # TABLA INSTITUCIONAL Y SELECCIÓN DE OFICIOS
        # -------------------------------------------------

        st.markdown("""
        <style>
        .st-key-tabla_institucional {
            background: rgba(255,255,255,0.96);
            border: 1px solid #D8E2E8;
            border-radius: 10px;
            padding: 18px;
            box-shadow: 0 6px 20px rgba(25,48,63,0.06);
        }

        .st-key-tabla_institucional
        [data-testid="stDataFrame"] {
            border-radius: 7px;
            overflow: hidden;
        }

        .st-key-tabla_institucional
        [data-testid="stButton"] button {
            border-radius: 5px;
        }
        </style>
        """, unsafe_allow_html=True)

        with st.container(key="tabla_institucional"):

            st.markdown(
                "**CONSULTA Y EMISIÓN DOCUMENTAL**"
            )

            st.caption(
                "Selecciona los registros que deseas incluir "
                "en el reporte institucional."
            )

            busqueda_tabla = st.text_input(
                "Buscar registros",
                placeholder="Folio, oficio, proyecto o responsable...",
                key="buscar_tabla_oficios",
            )

            col_proyecto, col_estado, col_responsable = st.columns(3)

            def valores_filtro(columna):
                if columna not in tabla_oficios.columns:
                    return []
                return sorted({
                    str(valor).strip()
                    for valor in tabla_oficios[columna].dropna()
                    if str(valor).strip()
                }, key=str.casefold)

            proyectos_registrados = valores_filtro("Proyecto")

            def grupo_de_proyecto(nombre):
                normalizado = normalizar_texto(nombre)
                if "tren maya" in normalizado:
                    return "Tren Maya"
                if "aifa" in normalizado and "pachuca" in normalizado:
                    return "AIFA-Pachuca"
                if "mexico-queretaro" in normalizado or "mexico–queretaro" in normalizado:
                    return "México-Querétaro"
                if "san luis potosi-saltillo" in normalizado:
                    return "San Luis Potosí-Saltillo"
                if "queretaro-san luis potosi" in normalizado:
                    return "Querétaro-San Luis Potosí"
                if "mazatlan-los mochis" in normalizado:
                    return "Mazatlán-Los Mochis"
                if "irapuato-guadalajara" in normalizado:
                    return "Irapuato-Guadalajara"
                return nombre

            grupos_proyecto = sorted(
                {grupo_de_proyecto(nombre) for nombre in proyectos_registrados},
                key=str.casefold,
            )

            with col_proyecto:
                filtro_proyecto = st.selectbox(
                    "Filtrar por proyecto",
                    ["Todos"] + grupos_proyecto,
                    key="filtro_tabla_proyecto",
                )

                subproyectos_disponibles = [
                    nombre for nombre in proyectos_registrados
                    if filtro_proyecto != "Todos"
                    and grupo_de_proyecto(nombre) == filtro_proyecto
                ]
                filtro_subproyecto = st.selectbox(
                    "Subproyecto / tramo",
                    ["Todos los subproyectos"] + subproyectos_disponibles,
                    key="filtro_tabla_subproyecto",
                    disabled=filtro_proyecto == "Todos",
                    help="Muestra los nombres completos registrados en Google Sheets.",
                )

            with col_estado:
                opciones_estatus = ["Todos"] + ESTATUS_OFICIOS

                filtro_estatus = st.selectbox(
                    "Filtrar por estatus",
                    opciones_estatus,
                    key="filtro_tabla_estatus",
                )

            with col_responsable:
                filtro_responsable = st.selectbox(
                    "Filtrar por responsable",
                    ["Todos"] + valores_filtro("Responsable"),
                    key="filtro_tabla_responsable",
                )

            tabla_filtrada = tabla_oficios.copy()

            if busqueda_tabla:
                coincidencias = (
                    tabla_filtrada
                    .fillna("")
                    .astype(str)
                    .apply(
                        lambda columna: columna.str.contains(
                            busqueda_tabla,
                            case=False,
                            regex=False,
                        )
                    )
                    .any(axis=1)
                )

                tabla_filtrada = tabla_filtrada[
                    coincidencias
                ]

            if filtro_proyecto != "Todos" and "Proyecto" in tabla_filtrada.columns:
                tabla_filtrada = tabla_filtrada[
                    tabla_filtrada["Proyecto"].fillna("").astype(str).map(
                        grupo_de_proyecto
                    ) == filtro_proyecto
                ]
                if filtro_subproyecto != "Todos los subproyectos":
                    tabla_filtrada = tabla_filtrada[
                        tabla_filtrada["Proyecto"].fillna("").astype(str).str.strip()
                        == filtro_subproyecto
                    ]

            for columna, valor_filtro in (
                ("Estatus", filtro_estatus),
                ("Responsable", filtro_responsable),
            ):
                if valor_filtro != "Todos" and columna in tabla_filtrada.columns:
                    tabla_filtrada = tabla_filtrada[
                        tabla_filtrada[columna].fillna("").astype(str).str.strip()
                        == valor_filtro
                    ]

            tabla_filtrada = tabla_filtrada.copy()

            # Selección estable por identidad del oficio, incluso al filtrar.
            if "oficios_marcados" not in st.session_state:
                st.session_state.oficios_marcados = set()
            if "pdf_reporte_generado" not in st.session_state:
                st.session_state.pdf_reporte_generado = None

            def identidad_oficio(fila):
                return (
                    str(fila.get("Folio", "")).strip(),
                    str(fila.get("No. de oficio", "")).strip(),
                )

            # Se usa el índice original, no la posición del filtro, para
            # que el PDF respete las filas seleccionadas.
            ids_visibles = [identidad_oficio(fila) for _, fila in tabla_filtrada.iterrows()]
            tabla_filtrada.insert(
                0, "Seleccionar",
                [identidad in st.session_state.oficios_marcados for identidad in ids_visibles],
            )

            # Cuando cambian los filtros, se recrea solo el editor.
            # Los oficios seleccionados permanecen en session_state.
            import hashlib
            firma_filtro = repr((
                busqueda_tabla, filtro_proyecto, filtro_subproyecto,
                filtro_estatus, filtro_responsable, tuple(ids_visibles),
            ))
            version_editor = hashlib.sha1(firma_filtro.encode("utf-8")).hexdigest()[:12]
            if st.session_state.get("firma_editor_actual") != version_editor:
                st.session_state.firma_editor_actual = version_editor
                st.session_state.epoch_editor = st.session_state.get("epoch_editor", 0) + 1
            clave_editor = f"editor_seleccion_oficios_{st.session_state.epoch_editor}"

            def sincronizar_seleccion():
                cambios = st.session_state.get(clave_editor, {}).get("edited_rows", {})
                marcados = st.session_state.oficios_marcados
                for posicion, cambios_fila in cambios.items():
                    posicion = int(posicion)
                    if posicion < len(ids_visibles) and "Seleccionar" in cambios_fila:
                        identidad = ids_visibles[posicion]
                        if cambios_fila["Seleccionar"]:
                            marcados.add(identidad)
                        else:
                            marcados.discard(identidad)
                st.session_state.pdf_reporte_generado = None

            columnas_visibles = [
                "Seleccionar",
                "Folio",
                "No. de oficio",
                "Fecha de recepción",
                "Asunto",
                "Proyecto",
                "Responsable",
                "Estatus",
            ]

            columnas_visibles = [
                c for c in columnas_visibles
                if c in tabla_filtrada.columns
            ]

            seleccion_editor = st.data_editor(
                tabla_filtrada[columnas_visibles],
                hide_index=True,
                use_container_width=True,
                height=410,
                key=clave_editor,
                on_change=sincronizar_seleccion,
                disabled=[
                    c for c in columnas_visibles
                    if c != "Seleccionar"
                ],
                column_config={
                    "Seleccionar": st.column_config.CheckboxColumn(
                        "✓",
                        width="small",
                    ),
                    "Folio": st.column_config.TextColumn(
                        "Folio",
                        width="small",
                    ),
                    "No. de oficio": st.column_config.TextColumn(
                        "Número de oficio",
                        width="medium",
                    ),
                    "Fecha de recepción": st.column_config.TextColumn(
                        "Fecha de recepción",
                        width="medium",
                    ),
                    "Asunto": st.column_config.TextColumn(
                        "Asunto",
                        width="large",
                    ),
                    "Proyecto": st.column_config.TextColumn(
                        "Proyecto",
                        width="medium",
                    ),
                    "Responsable": st.column_config.TextColumn(
                        "Responsable",
                        width="medium",
                    ),
                    "Estatus": st.column_config.TextColumn(
                        "Estatus",
                        width="medium",
                    ),
                },
            )

            # Seleccionados de toda la hoja, no solo del filtro actual.
            marcados = st.session_state.oficios_marcados
            oficios_seleccionados = tabla_oficios.loc[
                tabla_oficios.apply(identidad_oficio, axis=1).isin(marcados)
            ].copy()

            st.caption(f"Registros seleccionados: {len(oficios_seleccionados)}")
            # La acción aparece automáticamente al marcar exactamente una casilla.
            # Se renderiza fuera de expansores para que no pase inadvertida.
            if len(oficios_seleccionados) == 1:
                seleccionado = oficios_seleccionados.iloc[0]
                folio_borrar = str(seleccionado.get("Folio", "")).strip()
                numero_borrar = str(seleccionado.get("No. de oficio", "")).strip()
                if puede_administrar_oficios_actual():
                    st.markdown("**Administración del oficio seleccionado**")
                    clave_dialogo = f"abrir_borrado_{folio_borrar}_{numero_borrar}"
                    if st.button(
                        f"🗑 Eliminar oficio seleccionado · {numero_borrar}",
                        key=clave_dialogo, type="secondary", use_container_width=True,
                    ):
                        st.session_state["eliminacion_pendiente"] = (folio_borrar, numero_borrar)
                    if st.session_state.get("eliminacion_pendiente") == (folio_borrar, numero_borrar):
                        _panel_eliminar_oficio(folio_borrar, numero_borrar, origen="tabla")
                        if st.button("Cancelar eliminación", key=f"cancelar_{clave_dialogo}",
                                     use_container_width=True):
                            st.session_state.pop("eliminacion_pendiente", None)
                            st.rerun()
                else:
                    st.info(
                        "Seleccionaste un oficio. Para eliminarlo se requiere permiso "
                        "administrativo de esta área. En SETF solo pueden hacerlo "
                        "Arturo, Francely, Alondra, Adán o Daniel, mediante sus cuentas autorizadas."
                        if _area_activa_validada() == "setf" else
                        "Seleccionaste un oficio. La eliminación requiere iniciar sesión "
                        "con una cuenta de administrador o propietario."
                    )
            elif len(oficios_seleccionados) > 1:
                if puede_administrar_oficios_actual():
                    identidades_lote = sorted(set(
                        identidad_oficio(fila) for _, fila in oficios_seleccionados.iterrows()
                    ))
                    total_lote = len(identidades_lote)
                    sello_lote = hashlib.sha256(repr(identidades_lote).encode()).hexdigest()[:12]
                    st.markdown("**Eliminación administrativa de varios oficios**")
                    if st.button(f"🗑 Eliminar {total_lote} oficios seleccionados",
                                 key=f"abrir_lote_{sello_lote}", use_container_width=True):
                        st.session_state["eliminacion_lote_pendiente"] = sello_lote
                    if st.session_state.get("eliminacion_lote_pendiente") == sello_lote:
                        st.warning(f"Vas a eliminar {total_lote} expedientes de Google Sheets, incluyendo "
                                   "sus borradores, eventos y modalidades. Los archivos de Drive se conservarán. "
                                   "Esta acción no se puede deshacer desde la aplicación.")
                        st.write("**Oficios seleccionados:**")
                        for folio, numero in identidades_lote:
                            st.caption(f"• Folio {folio} — {numero}")
                        acepto = st.checkbox("Confirmo que revisé todos los oficios del lote.",
                                             key=f"acepto_lote_{sello_lote}")
                        frase = f"ELIMINAR {total_lote}"
                        escrito = st.text_input(f"Para confirmar, escribe exactamente: {frase}",
                                                key=f"texto_lote_{sello_lote}")
                        col_borrar, col_cancelar = st.columns([2, 1])
                        with col_borrar:
                            if st.button("Eliminar definitivamente el lote", type="secondary",
                                         disabled=not (acepto and escrito.strip() == frase),
                                         key=f"confirmacion_lote_{sello_lote}", use_container_width=True):
                                try:
                                    with st.spinner("Registrando auditoría y eliminando expedientes..."):
                                        _eliminar_oficios_lote_confirmado(identidades_lote)
                                    st.session_state.oficios_marcados = set()
                                    st.session_state.pdf_reporte_generado = None
                                    st.session_state.epoch_editor = st.session_state.get("epoch_editor", 0) + 1
                                    st.session_state.pop("eliminacion_lote_pendiente", None)
                                    st.success(f"Se eliminaron {total_lote} oficios.")
                                    st.rerun()
                                except Exception as error:
                                    st.error(f"Eliminación múltiple incompleta: {error}")
                        with col_cancelar:
                            if st.button("Cancelar", key=f"cancelar_lote_{sello_lote}",
                                         use_container_width=True):
                                st.session_state.pop("eliminacion_lote_pendiente", None)
                                st.rerun()
                else:
                    st.info("La eliminación múltiple está reservada a las cuentas con permisos "
                            "de administración de oficios de esta área.")
            else:
                st.session_state.pop("eliminacion_pendiente", None)

            col_generar, col_limpiar = st.columns([3, 1])
            with col_generar:
                if st.button(
                    "Preparar reporte institucional en PDF",
                    type="primary",
                    use_container_width=True,
                    disabled=oficios_seleccionados.empty,
                    key="preparar_reporte_oficios",
                ):
                    with st.spinner("Generando reporte PDF..."):
                        st.session_state.pdf_reporte_generado = generar_pdf_oficios(
                            oficios_seleccionados
                        )
            with col_limpiar:
                if st.button("Limpiar selección", use_container_width=True,
                             key="limpiar_seleccion_oficios"):
                    st.session_state.oficios_marcados = set()
                    st.session_state.pop("eliminacion_lote_pendiente", None)
                    st.session_state.pdf_reporte_generado = None
                    st.session_state.epoch_editor = st.session_state.get("epoch_editor", 0) + 1
                    # Nuevo editor para reflejar todas las casillas desmarcadas.
                    st.rerun()

            if st.session_state.pdf_reporte_generado is not None:
                st.download_button(
                    "⬇ Descargar reporte institucional en PDF",
                    data=st.session_state.pdf_reporte_generado,
                    file_name="relacion_oficios.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                    key="descargar_reporte_oficios",
                )
            elif oficios_seleccionados.empty:
                st.info("Selecciona al menos un oficio para generar el PDF.")
            else:
                st.caption("Cuando termines de seleccionar, presiona «Preparar reporte». ")

    else:
        st.info("No hay oficios registrados para mostrar.")


# =========================================================
# MÓDULO INGENIERÍA DE DETALLE FASE I
# =========================================================

elif st.session_state.menu_activo == "urgentes":
    _modulo_lateral_documentos("urgentes")

elif st.session_state.menu_activo == "firmados":
    _modulo_lateral_documentos("firmados")

elif st.session_state.menu_activo == "minutas":
    if _area_activa_validada() == "setf":
        _modulo_minutas_setf()
    else:
        _modulo_minutas_diseno_pendiente()

elif st.session_state.menu_activo == "reuniones":
    _modulo_reuniones_setf()

elif st.session_state.menu_activo == "diseno":
    if st.button("← Volver al Menú Principal", key="volver_diseno"):
        st.session_state.menu_activo = "home"
        st.rerun()

    st.markdown("---")
    st.markdown("## Sistemas Eléctricos · Módulo de planos" if _area_activa_validada() == "setf" else "## Ingeniería de Detalle Fase I")
    st.info("Módulo de planos en preparación. La consulta, carga, revisión y gestión de planos se implementarán en una próxima etapa." if _area_activa_validada() == "setf" else "Espacio preparado para la gestión de proyectos y control de esta mesa.")

# Estilo de la ficha de pendientes: solo los elementos propios de SETF.
if st.session_state.get("area_actual") == "setf":
    st.markdown("""<style>
    .setf-exp-heading {display:flex;flex-direction:column;gap:4px;margin:2px 0 12px}
    .setf-exp-heading span {font-size:10px;letter-spacing:1.7px;color:#9B7540;font-weight:800}
    .setf-exp-heading strong {font-size:19px;line-height:1.25;color:#173447;font-weight:750}
    .setf-exp-state-label {font-size:10px;font-weight:750;letter-spacing:1.3px;color:#9B7540;margin:18px 0 6px}
    .setf-exp-state {padding:11px 13px;border-radius:9px;border:1px solid #D6DAD6;background:#F7F6F1;color:#253F42;font-size:13px;font-weight:700}
    [class*="st-key-setf_documentos_laterales_"] {background:rgba(255,253,249,.96)!important;border:1px solid #DBCDB8!important;border-radius:15px!important;padding:18px!important}
    [class*="st-key-pend_word_acciones_"] {background:rgba(255,253,250,.95)!important;border:1px solid #D9CCB9!important;border-radius:14px!important;padding:12px!important}
    @media(max-width:850px) {.setf-exp-heading strong {font-size:17px}}
    </style>""", unsafe_allow_html=True)


# Presentación compacta del expediente SETF: dos tarjetas equilibradas como en el boceto.
# El CSS no cambia el comportamiento del componente ni el acceso a Google Drive.
if st.session_state.get("area_actual") == "setf":
    st.markdown("""<style>
    [class*="st-key-setf_word_panel_"],
    [class*="st-key-setf_documentos_laterales_"] {
        background:rgba(255,253,249,.98)!important;
        border:1px solid #D5CAB9!important;
        border-radius:14px!important;
        padding:18px 19px!important;
        box-sizing:border-box!important;
        box-shadow:0 4px 14px rgba(34,40,42,.045)!important;
    }
    @media (min-width:980px) {
        [class*="st-key-setf_word_panel_"],
        [class*="st-key-setf_documentos_laterales_"] {min-height:290px!important;}
    }
    .setf-exp-panel-title {display:flex;align-items:center;gap:9px;margin:0 0 16px;line-height:1.25;}
    .setf-exp-panel-title strong {font-size:17px;color:#18323D;font-weight:760;}
    .setf-exp-panel-icon {display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
        border-radius:8px;background:#E9EDEB;color:#385A5C;font-size:19px;flex:none;}
    .setf-word-version {padding:12px 14px;border:1px solid #E1DFD8;border-radius:10px;
        background:#F6F5F0;display:flex;flex-direction:column;gap:6px;margin-bottom:13px;}
    .setf-word-version span {font-weight:760;letter-spacing:1.15px;font-size:10px;color:#9B7848;}
    .setf-word-version strong {font-size:17px;color:#21383E;font-weight:760;line-height:1.35;}
    .setf-word-version small {font-size:12px;color:#607176;line-height:1.4;}
    [class*="st-key-setf_word_panel_"] [data-testid="stExpander"],
    [class*="st-key-setf_documentos_laterales_"] [data-testid="stExpander"] {
        background:#FDFCF9!important;border:1px solid #D7DCD9!important;border-radius:9px!important;
        margin:4px 0 9px!important;
    }
    [class*="st-key-setf_documentos_laterales_"] [data-testid="stExpander"] summary {min-height:46px!important;}
    [class*="st-key-setf_documentos_laterales_"] .setf-exp-state-label {
        margin:20px 0 6px!important;font-size:10px!important;letter-spacing:1.1px!important;color:#957244!important;
    }
    [class*="st-key-setf_documentos_laterales_"] .setf-exp-state {
        background:transparent!important;border:0!important;padding:2px 0 8px!important;
        font-size:14px!important;color:#21383E!important;
    }
    [class*="st-key-setf_word_panel_"] [data-testid="stAlert"] {margin:8px 0!important;}
    @media(max-width:980px) {
        [class*="st-key-setf_word_panel_"], [class*="st-key-setf_documentos_laterales_"] {
            padding:13px!important;min-height:0!important;
        }
        .setf-exp-panel-title strong {font-size:15px;}
    }
    </style>""", unsafe_allow_html=True)
